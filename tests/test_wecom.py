"""Tests for WeCom Smart Robot channel.

Tests cover:
- WeComWebSocketClient initialization and signature generation
- WeComChannel initialization, start, stop
- Message sending (text and markdown)
- Inbound message handling
- Access control
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.wecom import WeComChannel, WeComWebSocketClient, WsCmd


@pytest.fixture
def wecom_config():
    """Create mock WeCom config."""
    config = MagicMock()
    config.bot_id = "test_bot_123456"
    config.secret = "test_secret_key_abc"
    config.ws_url = "wss://openws.work.weixin.qq.com"
    config.allow_from = ["user1", "user2"]
    return config


@pytest.fixture
def message_bus():
    """Create mock message bus."""
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    return bus


class TestWeComWebSocketClient:
    """Test WeComWebSocketClient class."""

    def test_init(self):
        """Test client initialization."""
        client = WeComWebSocketClient(
            bot_id="test_bot",
            secret="test_secret",
            ws_url="wss://test.com",
            heartbeat_interval=30,
        )

        assert client.bot_id == "test_bot"
        assert client.secret == "test_secret"
        assert client.ws_url == "wss://test.com"
        assert client.heartbeat_interval == 30
        assert client._connected is False
        assert client._authenticated is False

    def test_generate_signature(self):
        """Test signature generation."""
        client = WeComWebSocketClient(
            bot_id="test_bot",
            secret="test_secret",
        )

        timestamp = "1234567890"
        nonce = "abcdef123456"
        signature = client._generate_signature(timestamp, nonce)

        # Verify signature format (SHA256 hex)
        assert len(signature) == 64
        assert all(c in "0123456789abcdef" for c in signature)

        # Verify signature is deterministic
        signature2 = client._generate_signature(timestamp, nonce)
        assert signature == signature2

    def test_generate_signature_different_inputs(self):
        """Test signature changes with different inputs."""
        client = WeComWebSocketClient(
            bot_id="test_bot",
            secret="test_secret",
        )

        sig1 = client._generate_signature("ts1", "nonce1")
        sig2 = client._generate_signature("ts2", "nonce1")
        sig3 = client._generate_signature("ts1", "nonce2")

        assert sig1 != sig2
        assert sig1 != sig3
        assert sig2 != sig3


class TestWeComChannel:
    """Test WeComChannel class."""

    @pytest.mark.asyncio
    async def test_channel_init(self, wecom_config, message_bus):
        """Test channel initialization."""
        channel = WeComChannel(wecom_config, message_bus)

        assert channel.name == "wecom"
        assert channel.config == wecom_config
        assert channel._client is None
        assert channel._running is False

    @pytest.mark.asyncio
    async def test_channel_start_missing_config(self, message_bus):
        """Test channel start with missing config."""
        config = MagicMock()
        config.bot_id = ""
        config.secret = ""
        config.ws_url = ""
        config.allow_from = []

        channel = WeComChannel(config, message_bus)

        # Should log error and return early
        await channel.start()

        assert channel._client is None

    @pytest.mark.asyncio
    async def test_channel_start(self, wecom_config, message_bus):
        """Test channel start creates client."""
        channel = WeComChannel(wecom_config, message_bus)

        # Mock the client.connect method to avoid actual connection
        with patch.object(WeComWebSocketClient, "connect", new_callable=AsyncMock) as mock_connect:
            # Start channel (will hang, so we cancel after a short time)
            start_task = asyncio.create_task(channel.start())
            await asyncio.sleep(0.1)

            # Verify client was created
            assert channel._client is not None
            assert isinstance(channel._client, WeComWebSocketClient)
            assert channel._client.bot_id == wecom_config.bot_id
            assert channel._client.secret == wecom_config.secret

            # Cancel the start task
            start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_channel_stop(self, wecom_config, message_bus):
        """Test channel stop."""
        channel = WeComChannel(wecom_config, message_bus)

        # Mock client
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        channel._client = mock_client
        channel._running = True

        await channel.stop()

        assert channel._running is False
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_text_message(self, wecom_config, message_bus):
        """Test sending text message."""
        channel = WeComChannel(wecom_config, message_bus)

        # Mock client
        mock_client = AsyncMock()
        mock_client._connected = True
        mock_client._authenticated = True
        mock_client.send_message = AsyncMock()
        channel._client = mock_client

        msg = OutboundMessage(
            channel="wecom",
            chat_id="user123",
            content="Hello, World!",
            metadata={},
        )

        await channel.send(msg)

        mock_client.send_message.assert_called_once_with("user123", "Hello, World!", "text")

    @pytest.mark.asyncio
    async def test_send_markdown_message(self, wecom_config, message_bus):
        """Test sending markdown message."""
        channel = WeComChannel(wecom_config, message_bus)

        # Mock client
        mock_client = AsyncMock()
        mock_client._connected = True
        mock_client._authenticated = True
        mock_client.send_message = AsyncMock()
        channel._client = mock_client

        markdown_content = "**Bold** and `code`"
        msg = OutboundMessage(
            channel="wecom",
            chat_id="user123",
            content=markdown_content,
            metadata={},
        )

        await channel.send(msg)

        mock_client.send_message.assert_called_once_with("user123", markdown_content, "markdown")

    @pytest.mark.asyncio
    async def test_send_not_ready(self, wecom_config, message_bus):
        """Test sending when not ready."""
        channel = WeComChannel(wecom_config, message_bus)

        # Mock client not connected
        mock_client = AsyncMock()
        mock_client._connected = False
        mock_client._authenticated = False
        channel._client = mock_client

        msg = OutboundMessage(
            channel="wecom",
            chat_id="user123",
            content="Hello",
            metadata={},
        )

        await channel.send(msg)

        mock_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_inbound_message(self, wecom_config, message_bus):
        """Test handling inbound message."""
        channel = WeComChannel(wecom_config, message_bus)
        channel._handle_message = AsyncMock()

        message_data = {
            "msgtype": "text",
            "text": {"content": "Hello bot!"},
            "from": {"userid": "user1"},
            "chatid": "chat123",
            "chattype": "direct",
            "msgid": "msg_456",
        }

        await channel._on_message(message_data)

        channel._handle_message.assert_called_once()
        call_args = channel._handle_message.call_args
        assert call_args.kwargs["sender_id"] == "user1"
        assert call_args.kwargs["chat_id"] == "chat123"
        assert call_args.kwargs["content"] == "Hello bot!"

    @pytest.mark.asyncio
    async def test_handle_inbound_message_not_allowed(self, wecom_config, message_bus):
        """Test handling inbound message from unauthorized user."""
        channel = WeComChannel(wecom_config, message_bus)
        channel._handle_message = AsyncMock()

        message_data = {
            "msgtype": "text",
            "text": {"content": "Hello bot!"},
            "from": {"userid": "unauthorized_user"},
            "chatid": "chat123",
            "chattype": "direct",
            "msgid": "msg_456",
        }

        await channel._on_message(message_data)

        channel._handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_non_text_message(self, wecom_config, message_bus):
        """Test handling non-text message."""
        channel = WeComChannel(wecom_config, message_bus)
        channel._handle_message = AsyncMock()

        message_data = {
            "msgtype": "image",
            "from": {"userid": "user1"},
            "chatid": "chat123",
        }

        await channel._on_message(message_data)

        channel._handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_empty_message(self, wecom_config, message_bus):
        """Test handling empty message."""
        channel = WeComChannel(wecom_config, message_bus)
        channel._handle_message = AsyncMock()

        message_data = {
            "msgtype": "text",
            "text": {"content": ""},
            "from": {"userid": "user1"},
            "chatid": "chat123",
        }

        await channel._on_message(message_data)

        channel._handle_message.assert_not_called()


class TestWsCmd:
    """Test WebSocket command enums."""

    def test_cmd_values(self):
        """Test command enum values."""
        assert WsCmd.AUTH == "auth"
        assert WsCmd.AUTH_RESP == "auth_resp"
        assert WsCmd.CALLBACK == "callback"
        assert WsCmd.SEND_MSG == "send_msg"
        assert WsCmd.SEND_MSG_RESP == "send_msg_resp"
        assert WsCmd.HEARTBEAT == "heartbeat"
        assert WsCmd.HEARTBEAT_ACK == "heartbeat_ack"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
