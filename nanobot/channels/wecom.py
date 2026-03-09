"""Enterprise WeChat (WeCom) Smart Robot channel using WebSocket.

This module implements the WeCom Smart Robot (AI Bot) channel using WebSocket
long connection, following the official @wecom/aibot-node-sdk pattern.

Official SDK: https://www.npmjs.com/package/@wecom/aibot-node-sdk
WebSocket URL: wss://openws.work.weixin.qq.com

Configuration requires only 2 items:
- bot_id: Your WeCom bot ID
- secret: Your WeCom bot secret for HMAC-SHA256 authentication

Example config:
    channels:
      wecom:
        enabled: true
        bot_id: "wb1234567890abcdef"
        secret: "your_secret_key_here"
        allow_from: []  # Empty = allow all users
"""

import asyncio
import hashlib
import hmac
import json
import time
from enum import Enum
from typing import Any, Callable

import websockets
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WeComConfig


class WsCmd(str, Enum):
    """WebSocket command types from official SDK."""

    AUTH = "auth"
    AUTH_RESP = "auth_resp"
    CALLBACK = "callback"
    SEND_MSG = "send_msg"
    SEND_MSG_RESP = "send_msg_resp"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"


class WeComWebSocketClient:
    """
    WeCom Smart Robot WebSocket client.

    Implements the official @wecom/aibot-node-sdk connection pattern:
    1. Connect to wss://openws.work.weixin.qq.com
    2. Authenticate with bot_id, timestamp, nonce, and HMAC-SHA256 signature
    3. Send periodic heartbeats to keep connection alive
    4. Receive callbacks for incoming messages
    5. Send messages via send_msg command

    Attributes:
        bot_id: WeCom bot ID from management console
        secret: WeCom bot secret for authentication
        ws_url: WebSocket server URL (default: wss://openws.work.weixin.qq.com)
        heartbeat_interval: Heartbeat interval in seconds (default: 30)

    Callbacks:
        on_authenticated: Called when authentication succeeds
        on_message: Called when receiving incoming message
        on_error: Called when error occurs
        on_disconnected: Called when connection is lost
    """

    def __init__(
        self,
        bot_id: str,
        secret: str,
        ws_url: str = "wss://openws.work.weixin.qq.com",
        heartbeat_interval: int = 30,
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.ws_url = ws_url
        self.heartbeat_interval = heartbeat_interval

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self._authenticated = False
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._reconnect_delay = 5
        self._max_reconnect_delay = 300

        # Callbacks
        self.on_authenticated: Callable | None = None
        self.on_message: Callable | None = None
        self.on_error: Callable | None = None
        self.on_disconnected: Callable | None = None

    async def connect(self) -> None:
        """Establish WebSocket connection and authenticate.

        Implements auto-reconnect with exponential backoff.
        """
        self._running = True

        while self._running and not self._connected:
            try:
                logger.info("Connecting to WeCom WebSocket: {}", self.ws_url)
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._connected = True

                    # Authenticate
                    await self._authenticate()

                    if self._authenticated:
                        logger.info("WeCom WebSocket connected and authenticated")
                        self._reconnect_delay = 5

                        # Start heartbeat and receive tasks
                        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                        self._receive_task = asyncio.create_task(self._receive_loop())

                        # Wait until disconnected
                        while self._running and self._connected:
                            await asyncio.sleep(0.1)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("WeCom WebSocket connection closed: {}", e)
                self._connected = False
                self._authenticated = False
                if self._running:
                    await self._handle_disconnect(f"Connection closed: {e}")
            except Exception as e:
                logger.error("WeCom WebSocket error: {}", e)
                self._connected = False
                self._authenticated = False
                if self._running:
                    await self._handle_disconnect(f"Error: {e}")

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        self._running = False
        self._connected = False
        self._authenticated = False

        # Cancel background tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # Close WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("WeCom WebSocket disconnected")

    async def _authenticate(self) -> None:
        """Authenticate with WeCom server using HMAC-SHA256 signature.

        Authentication frame format:
        {
            "cmd": "auth",
            "headers": {
                "bot_id": "xxx",
                "timestamp": "xxx",
                "nonce": "xxx",
                "signature": "xxx"
            },
            "body": {}
        }
        """
        try:
            timestamp = str(int(time.time()))
            nonce = hashlib.sha256(f"{timestamp}{self.bot_id}".encode()).hexdigest()[:16]
            signature = self._generate_signature(timestamp, nonce)

            auth_frame = {
                "cmd": WsCmd.AUTH,
                "headers": {
                    "bot_id": self.bot_id,
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "signature": signature,
                },
                "body": {},
            }

            await self._send_frame(auth_frame)
            logger.debug("Authentication request sent")

            # Wait for auth response (with timeout)
            try:
                response = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
                resp_data = json.loads(response)

                if resp_data.get("cmd") == WsCmd.AUTH_RESP:
                    if resp_data.get("headers", {}).get("code") == 0:
                        self._authenticated = True
                        logger.info("WeCom authentication successful")
                        if self.on_authenticated:
                            await self.on_authenticated()
                    else:
                        logger.error("Authentication failed: {}", resp_data)
                        if self.on_error:
                            await self.on_error(f"Auth failed: {resp_data}")
                else:
                    logger.error("Unexpected auth response: {}", resp_data)
            except asyncio.TimeoutError:
                logger.error("Authentication timeout")
                if self.on_error:
                    await self.on_error("Auth timeout")

        except Exception as e:
            logger.error("Authentication error: {}", e)
            if self.on_error:
                await self.on_error(f"Auth error: {e}")

    def _generate_signature(self, timestamp: str, nonce: str) -> str:
        """Generate HMAC-SHA256 signature.

        Signature = HMAC-SHA256(secret, bot_id + timestamp + nonce)

        Args:
            timestamp: Unix timestamp string
            nonce: Random nonce string

        Returns:
            Hex-encoded signature string
        """
        data = f"{self.bot_id}{timestamp}{nonce}"
        signature = hmac.new(
            self.secret.encode(), data.encode(), hashlib.sha256
        ).hexdigest()
        return signature

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat to keep connection alive."""
        while self._running and self._connected:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                if self._connected and self._ws:
                    heartbeat_frame = {"cmd": WsCmd.HEARTBEAT, "headers": {}, "body": {}}
                    await self._send_frame(heartbeat_frame)
                    logger.debug("Heartbeat sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _receive_loop(self) -> None:
        """Receive and process incoming messages."""
        while self._running and self._connected:
            try:
                message = await self._ws.recv()
                frame = json.loads(message)
                await self._process_frame(frame)
            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                logger.error("Receive error: {}", e)

    async def _process_frame(self, frame: dict[str, Any]) -> None:
        """Process incoming WebSocket frame."""
        cmd = frame.get("cmd", "")

        if cmd == WsCmd.HEARTBEAT_ACK:
            logger.debug("Heartbeat acknowledged")
        elif cmd == WsCmd.CALLBACK:
            await self._handle_callback(frame)
        elif cmd == WsCmd.SEND_MSG_RESP:
            await self._handle_send_response(frame)
        elif cmd == WsCmd.AUTH_RESP:
            # Already handled in authenticate
            pass
        else:
            logger.debug("Unknown command: {}", cmd)

    async def _handle_callback(self, frame: dict[str, Any]) -> None:
        """Handle incoming message callback."""
        try:
            body = frame.get("body", {})
            if self.on_message:
                await self.on_message(body)
        except Exception as e:
            logger.error("Callback handling error: {}", e)

    async def _handle_send_response(self, frame: dict[str, Any]) -> None:
        """Handle send message response."""
        headers = frame.get("headers", {})
        code = headers.get("code", -1)
        req_id = headers.get("req_id", "")

        if code == 0:
            logger.debug("Message sent successfully: {}", req_id)
        else:
            logger.error("Message send failed ({}): {}", code, frame)

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        """Send WebSocket frame."""
        if self._ws and self._connected:
            await self._ws.send(json.dumps(frame))

    async def send_message(self, chat_id: str, content: str, msg_type: str = "text") -> None:
        """Send message through WeCom.

        Send message frame format:
        {
            "cmd": "send_msg",
            "headers": {},
            "body": {
                "chatid": "xxx",
                "msgtype": "text|markdown",
                "text": {"content": "xxx"}
            }
        }

        Args:
            chat_id: Target chat ID (user ID for direct message, chat ID for group)
            content: Message content
            msg_type: Message type, either "text" or "markdown"
        """
        if not self._connected or not self._authenticated:
            logger.warning("Cannot send message: not connected or authenticated")
            return

        send_frame = {
            "cmd": WsCmd.SEND_MSG,
            "headers": {},
            "body": {
                "chatid": chat_id,
                "msgtype": msg_type,
                "text": {"content": content},
            },
        }

        await self._send_frame(send_frame)
        logger.debug("Message sent to chat: {}", chat_id)

    async def _handle_disconnect(self, reason: str) -> None:
        """Handle disconnection with reconnection logic."""
        if self.on_disconnected:
            await self.on_disconnected(reason)

        if self._running:
            logger.info("Reconnecting in {}s...", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            # Exponential backoff
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)


class WeComChannel(BaseChannel):
    """
    Enterprise WeChat Smart Robot channel using WebSocket.

    This channel uses the WeCom Smart Robot (AI Bot) feature with WebSocket
    long connection for bidirectional communication.

    Configuration:
        - bot_id: WeCom bot ID (required)
        - secret: WeCom bot secret (required)
        - ws_url: WebSocket server URL (optional, default: wss://openws.work.weixin.qq.com)
        - allow_from: List of allowed user IDs (optional, empty = all users)

    Features:
        - Bidirectional communication via WebSocket
        - Auto heartbeat and reconnection with exponential backoff
        - Text and Markdown message support
        - Group and private chat support
        - User access control
        - HMAC-SHA256 authentication

    Example usage:
        config = load_config()
        bus = MessageBus()
        channel = WeComChannel(config.channels.wecom, bus)
        await channel.start()
    """

    name = "wecom"

    def __init__(self, config: WeComConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WeComConfig = config
        self._client: WeComWebSocketClient | None = None
        self._running = False

    async def start(self) -> None:
        """Start the WeCom channel."""
        if not self.config.bot_id or not self.config.secret:
            logger.error("WeCom bot_id and secret not configured")
            return

        self._running = True
        self._client = WeComWebSocketClient(
            bot_id=self.config.bot_id,
            secret=self.config.secret,
            ws_url=self.config.ws_url or "wss://openws.work.weixin.qq.com",
            heartbeat_interval=30,
        )

        # Set up callbacks
        self._client.on_authenticated = self._on_authenticated
        self._client.on_message = self._on_message
        self._client.on_error = self._on_error
        self._client.on_disconnected = self._on_disconnected

        logger.info("WeCom Smart Robot channel starting...")
        bot_id_display = (
            self.config.bot_id[:8] + "..." if len(self.config.bot_id) > 8 else self.config.bot_id
        )
        logger.info("Bot ID: {}", bot_id_display)

        # Start WebSocket connection
        await self._client.connect()

    async def stop(self) -> None:
        """Stop the WeCom channel."""
        self._running = False
        if self._client:
            await self._client.disconnect()
        logger.info("WeCom channel stopped")

    async def _on_authenticated(self) -> None:
        """Called when authentication succeeds."""
        logger.info("WeCom channel authenticated and ready")

    async def _on_message(self, message: dict[str, Any]) -> None:
        """Handle incoming message from WeCom.

        Message format:
        {
            "msgid": "xxx",
            "chatid": "xxx",
            "chattype": "direct|group",
            "from": {"userid": "xxx"},
            "msgtype": "text",
            "text": {"content": "xxx"}
        }
        """
        try:
            msg_type = message.get("msgtype", "")
            if msg_type != "text":
                logger.debug("Skipping non-text message: {}", msg_type)
                return

            content = message.get("text", {}).get("content", "")
            if not content or not content.strip():
                return

            from_user = message.get("from", {}).get("userid", "")
            chat_id = message.get("chatid", "")
            chat_type = message.get("chattype", "direct")
            msg_id = message.get("msgid", "")

            if not self.is_allowed(from_user):
                logger.warning("Access denied for user: {}", from_user)
                return

            await self._handle_message(
                sender_id=from_user,
                chat_id=chat_id,
                content=content,
                metadata={
                    "msg_id": msg_id,
                    "msg_type": msg_type,
                    "chat_type": chat_type,
                },
            )
        except Exception as e:
            logger.error("Error handling inbound message: {}", e)

    async def _on_error(self, error: str) -> None:
        """Handle error from WebSocket client."""
        logger.error("WeCom error: {}", error)

    async def _on_disconnected(self, reason: str) -> None:
        """Handle disconnection."""
        logger.warning("WeCom disconnected: {}", reason)

    async def send(self, msg: OutboundMessage) -> None:
        """Send message through WeCom.

        Args:
            msg: OutboundMessage with channel, chat_id, content, and metadata
        """
        if not self._client or not self._client._connected or not self._client._authenticated:
            logger.warning("WeCom not ready for sending messages")
            return

        try:
            chat_id = msg.chat_id
            content = msg.content

            if msg.media:
                logger.warning("WeCom media sending not yet implemented")

            if content and content.strip():
                is_markdown = self._is_markdown(content)
                msg_type = "markdown" if is_markdown else "text"
                await self._client.send_message(chat_id, content, msg_type)
        except Exception as e:
            logger.error("Error sending WeCom message: {}", e)

    def _is_markdown(self, text: str) -> bool:
        """Detect if text contains Markdown formatting.

        Checks for common Markdown indicators like **, ```, #, -, etc.
        """
        markdown_indicators = ["**", "```", "# ", "- ", "* ", "1. ", "[", "](", "![", "|"]
        return any(indicator in text for indicator in markdown_indicators)


__all__ = ["WeComChannel", "WeComWebSocketClient", "WsCmd"]
