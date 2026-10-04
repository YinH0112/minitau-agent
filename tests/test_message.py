"""AssistantMessage 构造便利性校验器的测试。"""

from minitau_agent.message import AssistantMessage, TextContent


def test_plain_string_content_becomes_single_text_block():
    message = AssistantMessage(content="你好")
    assert message.content == [TextContent(text="你好")]


def test_empty_string_content_becomes_empty_list():
    message = AssistantMessage(content="")
    assert message.content == []


def test_block_list_content_passes_through_unchanged():
    blocks = [TextContent(text="hi"), TextContent(text="there")]
    message = AssistantMessage(content=blocks)
    assert message.content == blocks


def test_construction_without_content_defaults_to_empty_list():
    message = AssistantMessage()
    assert message.content == []


def test_string_content_via_model_validate():
    message = AssistantMessage.model_validate({"content": "你好"})
    assert message.content == [TextContent(text="你好")]


def test_string_construction_serializes_as_blocks():
    """字符串进、块列表出——锁死"线格式永远块列表"。"""
    payload = AssistantMessage(content="你好").model_dump_json()
    assert '"content":[{"type":"text","text":"你好"}]' in payload
