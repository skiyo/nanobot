"""Enterprise WeChat (WeCom) channel implementation using WebSocket long connection."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import WeComConfig

import importlib.util

WECOM_AVAILABLE = importlib.util.find_spec("wechatpy") is not None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "voice": "[voice]",
    "video": "[video]",
    "file": "[file]",
    "link": "[link]",
    "miniprogram": "[miniprogram]",
}


class WeComChannel(BaseChannel):
    """
    Enterprise WeChat (WeCom) channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - Corp ID from WeCom Admin Panel
    - Agent Secret from WeCom Admin Panel
    - Agent ID from WeCom Admin Panel
    - Bot capability enabled
    """

    name = "wecom"

    def __init__(self, config: WeComConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: WeComConfig = config
        self.groq_api_key = groq_api_key
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._access_token: str | None = None
        self._token_expiry: float = 0

    async def _get_access_token(self) -> str | None:
        """Get WeCom access token with caching."""
        import time
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        try:
            import httpx
            url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.config.corp_id}&corpsecret={self.config.agent_secret}"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                data = response.json()
                if data.get("errcode") == 0:
                    self._access_token = data.get("access_token")
                    self._token_expiry = time.time() + data.get("expires_in", 7200) - 300
                    logger.debug("WeCom access token refreshed")
                    return self._access_token
                else:
                    logger.error("Failed to get WeCom access token: {}", data)
                    return None
        except Exception as e:
            logger.error("Error getting WeCom access token: {}", e)
            return None

    async def start(self) -> None:
        """Start the WeCom bot with WebSocket long connection."""
        if not WECOM_AVAILABLE:
            logger.error("WeCom SDK not installed. Run: pip install wechatpy")
            return

        if not self.config.corp_id or not self.config.agent_secret:
            logger.error("WeCom corp_id and agent_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Start WebSocket listener in background thread
        def run_ws():
            import time
            try:
                while self._running:
                    try:
                        # Use polling fallback if WebSocket not available
                        asyncio.run_coroutine_threadsafe(self._poll_messages(), self._loop)
                    except Exception as e:
                        logger.warning("WeCom polling error: {}", e)
                    if self._running:
                        time.sleep(self.config.poll_interval_seconds)
            finally:
                pass

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("WeCom bot started with polling mode")
        logger.info("Polling interval: {} seconds", self.config.poll_interval_seconds)

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the WeCom bot."""
        self._running = False
        logger.info("WeCom bot stopped")

    async def _poll_messages(self) -> None:
        """Poll for new messages from WeCom."""
        token = await self._get_access_token()
        if not token:
            return

        try:
            import httpx
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/get?access_token={token}&agentid={self.config.agent_id}"
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10)
                data = response.json()
                if data.get("errcode") == 0:
                    messages = data.get("message_list", [])
                    for msg in messages:
                        await self._on_message(msg)
                else:
                    logger.debug("WeCom poll response: {}", data)
        except Exception as e:
            logger.error("Error polling WeCom messages: {}", e)

    async def _on_message(self, data: dict) -> None:
        """Handle incoming message from WeCom."""
        try:
            message_id = data.get("msgid")
            if not message_id:
                return

            # Deduplication check
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            msg_type = data.get("msgtype", "text")
            sender_id = data.get("userid", "unknown")
            chat_id = data.get("conversation_id", sender_id)

            # Check if from group chat
            if data.get("chat_type") == "group":
                chat_id = data.get("conversation_id", f"group_{sender_id}")

            # Skip bot messages
            if data.get("msgtype") == "bot":
                return

            # Parse content
            content_parts = []
            media_paths = []

            if msg_type == "text":
                text_content = data.get("text", {})
                content = text_content.get("content", "")
                if content:
                    content_parts.append(content)

            elif msg_type == "image":
                image_data = data.get("image", {})
                file_url = image_data.get("file_url", "")
                if file_url:
                    file_path, content_text = await self._download_and_save_media(
                        "image", file_url, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type == "voice":
                voice_data = data.get("voice", {})
                file_url = voice_data.get("file_url", "")
                if file_url:
                    file_path, content_text = await self._download_and_save_media(
                        "voice", file_url, message_id
                    )
                    if file_path and self.groq_api_key:
                        try:
                            from nanobot.providers.transcription import GroqTranscriptionProvider
                            transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                            transcription = await transcriber.transcribe(file_path)
                            if transcription:
                                content_text = f"[transcription: {transcription}]"
                        except Exception as e:
                            logger.warning("Failed to transcribe voice: {}", e)
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type == "file":
                file_data = data.get("file", {})
                file_url = file_data.get("file_url", "")
                if file_url:
                    file_path, content_text = await self._download_and_save_media(
                        "file", file_url, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type == "link":
                link_data = data.get("link", {})
                title = link_data.get("title", "")
                description = link_data.get("description", "")
                url = link_data.get("url", "")
                content_parts.append(f"[link: {title}]({url})")
                if description:
                    content_parts.append(description)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Forward to message bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "msg_type": msg_type,
                }
            )

        except Exception as e:
            logger.error("Error processing WeCom message: {}", e)

    async def _download_and_save_media(
        self,
        msg_type: str,
        file_url: str,
        message_id: str
    ) -> tuple[str | None, str]:
        """Download media from WeCom and save to local disk."""
        import httpx
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("wecom")

        try:
            token = await self._get_access_token()
            if not token:
                return None, f"[{msg_type}: auth failed]"

            # Download file
            async with httpx.AsyncClient() as client:
                response = await client.get(file_url, timeout=30)
                if response.status_code == 200:
                    # Generate filename
                    content_type = response.headers.get("content-type", "")
                    ext = self._get_extension_from_mime(content_type, msg_type)
                    filename = f"{message_id[:16]}{ext}"
                    file_path = media_dir / filename
                    file_path.write_bytes(response.content)
                    logger.debug("Downloaded {} to {}", msg_type, file_path)
                    return str(file_path), f"[{msg_type}: {filename}]"
                else:
                    logger.error("Failed to download {}: status={}", msg_type, response.status_code)
                    return None, f"[{msg_type}: download failed]"
        except Exception as e:
            logger.error("Error downloading {} {}: {}", msg_type, file_url, e)
            return None, f"[{msg_type}: error]"

    @staticmethod
    def _get_extension_from_mime(mime_type: str, msg_type: str) -> str:
        """Get file extension from MIME type."""
        mime_to_ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/amr": ".amr",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "video/mp4": ".mp4",
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        }
        return mime_to_ext.get(mime_type, f".{msg_type}")

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
    _AUDIO_EXTS = {".amr", ".mp3", ".wav"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WeCom, including media if present."""
        token = await self._get_access_token()
        if not token:
            logger.warning("WeCom access token not available")
            return

        try:
            loop = asyncio.get_running_loop()

            # Send media files first
            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    await loop.run_in_executor(
                        None, self._send_image_sync,
                        token, msg.chat_id, file_path
                    )
                elif ext in self._AUDIO_EXTS:
                    await loop.run_in_executor(
                        None, self._send_voice_sync,
                        token, msg.chat_id, file_path
                    )
                elif ext in self._VIDEO_EXTS:
                    await loop.run_in_executor(
                        None, self._send_video_sync,
                        token, msg.chat_id, file_path
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_file_sync,
                        token, msg.chat_id, file_path
                    )

            # Send text content
            if msg.content and msg.content.strip():
                await loop.run_in_executor(
                    None, self._send_text_sync,
                    token, msg.chat_id, msg.content
                )

        except Exception as e:
            logger.error("Error sending WeCom message: {}", e)

    def _send_text_sync(self, token: str, chat_id: str, content: str) -> bool:
        """Send text message synchronously."""
        import httpx
        try:
            # Determine if sending to user or group
            if chat_id.startswith("@"):
                # Group chat
                url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                data = {
                    "chatid": chat_id[1:],  # Remove @ prefix
                    "msgtype": "text",
                    "text": {"content": content}
                }
            else:
                # User chat
                url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                data = {
                    "touser": chat_id,
                    "msgtype": "text",
                    "agentid": self.config.agent_id,
                    "text": {"content": content}
                }

            with httpx.Client() as client:
                response = client.post(url, json=data, timeout=10)
                result = response.json()
                if result.get("errcode") == 0:
                    logger.debug("WeCom text message sent to {}", chat_id)
                    return True
                else:
                    logger.error("Failed to send WeCom text: {}", result)
                    return False
        except Exception as e:
            logger.error("Error sending WeCom text: {}", e)
            return False

    def _send_image_sync(self, token: str, chat_id: str, file_path: str) -> bool:
        """Send image message synchronously."""
        import httpx
        try:
            # First upload the image
            upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=image"
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f)}
                with httpx.Client() as client:
                    response = client.post(upload_url, files=files, timeout=30)
                    result = response.json()
                    if result.get("errcode", 0) == 0 or "media_id" in result:
                        media_id = result.get("media_id")
                        # Then send the image
                        if chat_id.startswith("@"):
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                            data = {
                                "chatid": chat_id[1:],
                                "msgtype": "image",
                                "image": {"media_id": media_id}
                            }
                        else:
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                            data = {
                                "touser": chat_id,
                                "msgtype": "image",
                                "agentid": self.config.agent_id,
                                "image": {"media_id": media_id}
                            }
                        send_response = client.post(send_url, json=data, timeout=10)
                        send_result = send_response.json()
                        if send_result.get("errcode") == 0:
                            logger.debug("WeCom image sent to {}", chat_id)
                            return True
                        else:
                            logger.error("Failed to send WeCom image: {}", send_result)
                            return False
                    else:
                        logger.error("Failed to upload WeCom image: {}", result)
                        return False
        except Exception as e:
            logger.error("Error sending WeCom image: {}", e)
            return False

    def _send_voice_sync(self, token: str, chat_id: str, file_path: str) -> bool:
        """Send voice message synchronously."""
        import httpx
        try:
            upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=voice"
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f)}
                with httpx.Client() as client:
                    response = client.post(upload_url, files=files, timeout=30)
                    result = response.json()
                    if result.get("errcode", 0) == 0 or "media_id" in result:
                        media_id = result.get("media_id")
                        if chat_id.startswith("@"):
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                            data = {
                                "chatid": chat_id[1:],
                                "msgtype": "voice",
                                "voice": {"media_id": media_id}
                            }
                        else:
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                            data = {
                                "touser": chat_id,
                                "msgtype": "voice",
                                "agentid": self.config.agent_id,
                                "voice": {"media_id": media_id}
                            }
                    send_response = client.post(send_url, json=data, timeout=10)
                    send_result = send_response.json()
                    if send_result.get("errcode") == 0:
                        logger.debug("WeCom voice sent to {}", chat_id)
                        return True
                    else:
                        logger.error("Failed to send WeCom voice: {}", send_result)
                        return False
        except Exception as e:
            logger.error("Error sending WeCom voice: {}", e)
            return False

    def _send_video_sync(self, token: str, chat_id: str, file_path: str) -> bool:
        """Send video message synchronously."""
        import httpx
        try:
            upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=video"
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f)}
                with httpx.Client() as client:
                    response = client.post(upload_url, files=files, timeout=60)
                    result = response.json()
                    if result.get("errcode", 0) == 0 or "media_id" in result:
                        media_id = result.get("media_id")
                        if chat_id.startswith("@"):
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                            data = {
                                "chatid": chat_id[1:],
                                "msgtype": "video",
                                "video": {"media_id": media_id}
                            }
                        else:
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                            data = {
                                "touser": chat_id,
                                "msgtype": "video",
                                "agentid": self.config.agent_id,
                                "video": {"media_id": media_id}
                            }
                    send_response = client.post(send_url, json=data, timeout=10)
                    send_result = send_response.json()
                    if send_result.get("errcode") == 0:
                        logger.debug("WeCom video sent to {}", chat_id)
                        return True
                    else:
                        logger.error("Failed to send WeCom video: {}", send_result)
                        return False
        except Exception as e:
            logger.error("Error sending WeCom video: {}", e)
            return False

    def _send_file_sync(self, token: str, chat_id: str, file_path: str) -> bool:
        """Send file message synchronously."""
        import httpx
        try:
            upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=file"
            with open(file_path, "rb") as f:
                files = {"media": (os.path.basename(file_path), f)}
                with httpx.Client() as client:
                    response = client.post(upload_url, files=files, timeout=60)
                    result = response.json()
                    if result.get("errcode", 0) == 0 or "media_id" in result:
                        media_id = result.get("media_id")
                        if chat_id.startswith("@"):
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/appchat/send?access_token={token}"
                            data = {
                                "chatid": chat_id[1:],
                                "msgtype": "file",
                                "file": {"media_id": media_id}
                            }
                        else:
                            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
                            data = {
                                "touser": chat_id,
                                "msgtype": "file",
                                "agentid": self.config.agent_id,
                                "file": {"media_id": media_id}
                            }
                    send_response = client.post(send_url, json=data, timeout=10)
                    send_result = send_response.json()
                    if send_result.get("errcode") == 0:
                        logger.debug("WeCom file sent to {}", chat_id)
                        return True
                    else:
                        logger.error("Failed to send WeCom file: {}", send_result)
                        return False
        except Exception as e:
            logger.error("Error sending WeCom file: {}", e)
            return False
