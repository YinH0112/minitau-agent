"""P6.5.4: REPL commands expose safe branch points and switch branches."""

from __future__ import annotations

from minitau_agent.message import AssistantMessage, ToolCall, UserMessage
from minitau_agent.session.entries import LeafEntry, MessageEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.commands import CommandContext, dispatch_input
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def make_context(tmp_path, *streams):
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path, provider=FakeProvider(streams), model="fake"
        ),
        session_id="branch-commands",
        title="demo",
    )

    async def on_event(_event):
        return None

    context = CommandContext(
        session=session,
        manager=SessionManager(tmp_path),
        on_event=on_event,
    )
    path = tmp_path / ".minitau" / "sessions" / "branch-commands.jsonl"
    return context, path


async def test_tree_lists_only_safe_nodes_and_marks_current_one(tmp_path):
    context, path = await make_context(tmp_path, reply("answer one"), reply("answer two"))
    _ = [event async for event in context.session.prompt("question one")]
    _ = [event async for event in context.session.prompt("question two")]
    entries = await JsonlSessionStorage(path).read_all()

    result = await dispatch_input("/tree", context)

    assert result.error is None
    assert result.handled is True
    assert result.prompt is None
    assert entries[3].id in (result.message or "")
    assert entries[5].id in (result.message or "")
    assert entries[2].id not in (result.message or "")
    assert entries[4].id not in (result.message or "")
    assert f"* {entries[5].id}" in (result.message or "")


async def test_branch_command_selects_entry_and_updates_tree(tmp_path):
    context, path = await make_context(tmp_path, reply("answer one"), reply("answer two"))
    _ = [event async for event in context.session.prompt("question one")]
    _ = [event async for event in context.session.prompt("question two")]
    selected_id = (await JsonlSessionStorage(path).read_all())[3].id

    selected = await dispatch_input(f"/branch {selected_id}", context)
    tree = await dispatch_input("/tree", context)

    assert selected.error is None
    assert [message.text for message in context.session.messages] == [
        "question one", "answer one"
    ]
    assert f"* {selected_id}" in (tree.message or "")
    assert isinstance((await JsonlSessionStorage(path).read_all())[-1], LeafEntry)
    assert (await context.manager.load(context.session.session_id)).last_entry_id == selected_id


async def test_bad_branch_command_never_changes_session_or_log(tmp_path):
    context, path = await make_context(tmp_path, reply("answer"))
    _ = [event async for event in context.session.prompt("question")]
    before = await JsonlSessionStorage(path).read_all()

    missing = await dispatch_input("/branch", context)
    unsafe = await dispatch_input(f"/branch {before[2].id}", context)
    unknown = await dispatch_input("/branch missing-id", context)
    tree_extra = await dispatch_input("/tree extra", context)

    assert all(result.error is not None for result in (missing, unsafe, unknown, tree_extra))
    assert await JsonlSessionStorage(path).read_all() == before
    assert [message.text for message in context.session.messages] == ["question", "answer"]


async def test_tree_reports_no_choices_for_new_session(tmp_path):
    context, _path = await make_context(tmp_path)

    result = await dispatch_input("/tree", context)

    assert result.error is None
    assert "no" in (result.message or "").lower()


async def test_tree_hides_unanswered_tool_call(tmp_path):
    context, path = await make_context(tmp_path)
    storage = JsonlSessionStorage(path)
    root = (await storage.read_all())[0]
    user = MessageEntry(
        id="user",
        parent_id=root.id,
        message=UserMessage(content="read a file"),
    )
    tool_use = MessageEntry(
        id="tool-use",
        parent_id=user.id,
        message=AssistantMessage(
            content=[ToolCall(id="call-1", name="read", arguments={})],
            stop_reason="toolUse",
        ),
    )
    await storage.append(user)
    await storage.append(tool_use)

    result = await dispatch_input("/tree", context)

    assert result.error is None
    assert "tool-use" not in (result.message or "")
