"""P2 Step 4b: CLI 会话持久化与恢复（--resume / --continue）。"""

from __future__ import annotations

from typer.testing import CliRunner

from minitau_agent.session.jsonl import entries_from_json_lines
from minitau_coding.cli import app

runner = CliRunner()


def sessions_dir(cwd) -> object:
    return cwd / ".minitau" / "sessions"


def read_entries(path) -> list:
    return entries_from_json_lines(path.read_text(encoding="utf-8").split("\n"))


def run_print(cwd, prompt: str, *extra: str):
    return runner.invoke(app, ["--print", prompt, "--cwd", str(cwd), *extra])


class TestSessionPersistence:
    def test_print_mode_creates_session_file(self, tmp_path):
        result = run_print(tmp_path, "hi")

        assert result.exit_code == 0
        files = list(sessions_dir(tmp_path).glob("*.jsonl"))
        assert len(files) == 1
        entries = read_entries(files[0])
        # 元数据 → 初始模型配置 → user → assistant
        assert [e.type for e in entries] == [
            "session_info", "model_change", "message", "message"
        ]
        assert entries[0].title == "hi"
        assert entries[0].cwd == str(tmp_path)
        # parent_id 链：None → id0 → id1 → id2
        assert entries[1].parent_id == entries[0].id
        assert entries[2].parent_id == entries[1].id
        assert entries[3].parent_id == entries[2].id

    def test_long_prompt_title_is_truncated_to_40_chars(self, tmp_path):
        run_print(tmp_path, "x" * 50)

        entries = read_entries(next(iter(sessions_dir(tmp_path).glob("*.jsonl"))))
        assert entries[0].title == "x" * 40

    def test_session_id_announced_on_stderr_not_stdout(self, tmp_path):
        result = run_print(tmp_path, "hi")

        assert result.exit_code == 0
        assert result.stdout.strip() == "Hello from the minitau fake provider!"
        assert "Session" in result.stderr


class TestResume:
    def test_resume_appends_to_the_same_file_and_chains(self, tmp_path):
        run_print(tmp_path, "第一问")
        session_file = next(iter(sessions_dir(tmp_path).glob("*.jsonl")))
        session_id = session_file.stem

        result = run_print(tmp_path, "第二问", "--resume", session_id)

        assert result.exit_code == 0
        # 还是同一个文件，没有新建
        assert len(list(sessions_dir(tmp_path).glob("*.jsonl"))) == 1
        entries = read_entries(session_file)
        # run1: info + model + u + a = 4；resume 追加 u + a = 6
        assert len(entries) == 6
        # resume 的 user 接在旧 assistant 链尾上
        assert entries[4].parent_id == entries[3].id
        assert entries[5].parent_id == entries[4].id
        # 不重复写 session_info
        assert [e.type for e in entries].count("session_info") == 1

    def test_resume_accepts_unique_prefix(self, tmp_path):
        run_print(tmp_path, "hi")
        session_id = next(iter(sessions_dir(tmp_path).glob("*.jsonl"))).stem

        result = run_print(tmp_path, "again", "--resume", session_id[:8])

        assert result.exit_code == 0
        assert len(list(sessions_dir(tmp_path).glob("*.jsonl"))) == 1

    def test_resume_unknown_prefix_is_a_usage_error(self, tmp_path):
        result = run_print(tmp_path, "hi", "--resume", "no-such-session")

        assert result.exit_code == 2
        assert "no-such-session" in result.output

    def test_ambiguous_prefix_lists_candidates(self, tmp_path):
        run_print(tmp_path, "hi")
        run_print(tmp_path, "hi")
        # 手动改出共同前缀，制造歧义
        files = sorted(sessions_dir(tmp_path).glob("*.jsonl"))
        for i, path in enumerate(files):
            path.rename(path.parent / f"shared-prefix-{i}.jsonl")

        result = run_print(tmp_path, "hi", "--resume", "shared-prefix")

        assert result.exit_code == 2
        assert "shared-prefix-0" in result.output
        assert "shared-prefix-1" in result.output


class TestContinue:
    def test_continue_picks_the_most_recently_touched_session(self, tmp_path):
        run_print(tmp_path, "old question")
        run_print(tmp_path, "new question")

        result = run_print(tmp_path, "follow up", "--continue")

        assert result.exit_code == 0
        files = sorted(sessions_dir(tmp_path).glob("*.jsonl"))
        assert len(files) == 2
        # 被 continue 的文件有 6 条 entry（4 + 2），另一个保持 4 条
        counts = sorted(len(read_entries(f)) for f in files)
        assert counts == [4, 6]

    def test_continue_without_any_session_is_a_usage_error(self, tmp_path):
        result = run_print(tmp_path, "hi", "--continue")

        assert result.exit_code == 2


class TestMutualExclusion:
    def test_resume_and_continue_cannot_combine(self, tmp_path):
        run_print(tmp_path, "hi")
        session_id = next(iter(sessions_dir(tmp_path).glob("*.jsonl"))).stem

        result = run_print(tmp_path, "hi", "--resume", session_id, "--continue")

        assert result.exit_code == 2
