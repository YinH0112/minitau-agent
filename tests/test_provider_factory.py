"""Provider 工厂的测试：接线层的第一块砖。"""

import pytest

from minitau_ai.factory import DEFAULT_FAKE_MODEL, create_provider
from minitau_ai.fake import FakeProvider


def test_none_name_selects_fake_provider():
    """不指定 provider 时默认返回 FakeProvider 和它的默认模型。"""
    provider, model = create_provider(None)

    assert isinstance(provider, FakeProvider)
    assert model == DEFAULT_FAKE_MODEL


async def test_fake_name_streams_a_greeting():
    """fake 的默认剧本能吐出一句问候语——print 模式的可断言输出。"""
    provider, _ = create_provider("fake")

    events = [
        event
        async for event in provider.stream_response(
            model="fake", system="s", messages=[], tools=[]
        )
    ]

    assert events[-1].message.text == "Hello from the minitau fake provider!"


def test_unknown_provider_raises_value_error():
    """未知名字：ValueError，且消息里带上可用选项。"""
    with pytest.raises(ValueError, match="anthropic"):
        create_provider("anthropic")

    with pytest.raises(ValueError, match="fake"):
        create_provider("nope")
