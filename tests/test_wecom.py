"""Tests for WeCom channel."""

import pytest
from types import SimpleNamespace
import httpx

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.wecom import WeComChannel
from nanobot.config.schema import WeComConfig


class _FakeHTTPXResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.content = content
        self.headers = {}

    def json(self):
        return self._json_data


class _FakeHTTPXClient:
    _instances = []

    def __init__(self):
        self._instances.append(self)
        self._responses = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, timeout=None):
        return _FakeHTTPXResponse(json_data={"errcode": 0, "access_token": "fake_token", "expires_in": 7200})

    async def get(self, url, timeout=None):
        if "gettoken" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0, "access_token": "fake_token", "expires_in": 7200})
        elif "message/get" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0, "message_list": []})
        elif "qyapi.weixin.qq.com" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0, "file_url": "http://fake.url/file"})
        return _FakeHTTPXResponse()

    def post(self, url, json=None, files=None, timeout=None):
        if "media/upload" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0, "media_id": "fake_media_id"})
        elif "message/send" in url or "appchat/send" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0})
        return _FakeHTTPXResponse()

    async def post(self, url, json=None, files=None, timeout=None):
        if "media/upload" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0, "media_id": "fake_media_id"})
        elif "message/send" in url or "appchat/send" in url:
            return _FakeHTTPXResponse(json_data={"errcode": 0})
        return _FakeHTTPXResponse()


@pytest.mark.asyncio
async def test_wecom_channel_init():
    """Test WeCom channel initialization."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = WeComChannel(config, bus)

    assert channel.config.corp_id == "test_corp"
    assert channel.config.agent_id == "1000001"
    assert channel.name == "wecom"


@pytest.mark.asyncio
async def test_wecom_is_allowed(monkeypatch):
    """Test WeCom channel permission checking."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["user1", "user2"],
    )
    channel = WeComChannel(config, MessageBus())

    assert channel.is_allowed("user1") is True
    assert channel.is_allowed("user2") is True
    assert channel.is_allowed("user3") is False

    # Test wildcard
    config_wildcard = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    channel_wildcard = WeComChannel(config_wildcard, MessageBus())
    assert channel_wildcard.is_allowed("any_user") is True


@pytest.mark.asyncio
async def test_wecom_get_access_token(monkeypatch):
    """Test WeCom access token retrieval."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
    )
    channel = WeComChannel(config, MessageBus())

    monkeypatch.setattr(httpx, "AsyncClient", _FakeHTTPXClient)

    token = await channel._get_access_token()
    assert token == "fake_token"


@pytest.mark.asyncio
async def test_wecom_on_message_text(monkeypatch):
    """Test WeCom text message handling."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = WeComChannel(config, bus)

    message_data = {
        "msgid": "msg_001",
        "msgtype": "text",
        "userid": "user1",
        "conversation_id": "conv1",
        "text": {"content": "Hello nanobot!"}
    }

    await channel._on_message(message_data)

    # Check message was published to bus
    # Note: In real test, we would consume from bus, but for simplicity we check no exception


@pytest.mark.asyncio
async def test_wecom_on_message_dedup(monkeypatch):
    """Test WeCom message deduplication."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = WeComChannel(config, bus)

    message_data = {
        "msgid": "msg_duplicate",
        "msgtype": "text",
        "userid": "user1",
        "text": {"content": "Duplicate test"}
    }

    # Process same message twice
    await channel._on_message(message_data)
    initial_count = len(channel._processed_message_ids)
    await channel._on_message(message_data)
    final_count = len(channel._processed_message_ids)

    # Should not add duplicate
    assert initial_count == final_count


@pytest.mark.asyncio
async def test_wecom_send_text(monkeypatch):
    """Test WeCom text message sending."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = WeComChannel(config, bus)
    channel._access_token = "fake_token"
    channel._token_expiry = 9999999999

    monkeypatch.setattr(httpx, "Client", _FakeHTTPXClient)

    msg = OutboundMessage(
        channel="wecom",
        chat_id="user1",
        content="Test message",
    )

    await channel.send(msg)
    # Should not raise exception


@pytest.mark.asyncio
async def test_wecom_send_to_group(monkeypatch):
    """Test WeCom message sending to group chat."""
    config = WeComConfig(
        enabled=True,
        corp_id="test_corp",
        agent_id="1000001",
        agent_secret="test_secret",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = WeComChannel(config, bus)
    channel._access_token = "fake_token"
    channel._token_expiry = 9999999999

    monkeypatch.setattr(httpx, "Client", _FakeHTTPXClient)

    msg = OutboundMessage(
        channel="wecom",
        chat_id="@group123",
        content="Group message",
    )

    await channel.send(msg)
    # Should not raise exception


def test_wecom_get_extension_from_mime():
    """Test MIME type to extension mapping."""
    assert WeComChannel._get_extension_from_mime("image/jpeg", "image") == ".jpg"
    assert WeComChannel._get_extension_from_mime("image/png", "image") == ".png"
    assert WeComChannel._get_extension_from_mime("audio/amr", "voice") == ".amr"
    assert WeComChannel._get_extension_from_mime("application/pdf", "file") == ".pdf"
    assert WeComChannel._get_extension_from_mime("unknown/type", "file") == ".file"


@pytest.mark.asyncio
async def test_wecom_empty_config():
    """Test WeCom channel with empty config."""
    config = WeComConfig()
    channel = WeComChannel(config, MessageBus())

    assert config.enabled is False
    assert config.corp_id == ""
    assert config.agent_secret == ""
