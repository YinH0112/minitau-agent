"""CLI --print 模式接线的端到端测试。"""

from typer.testing import CliRunner

from minitau_coding.cli import app

runner = CliRunner()


def test_print_mode_prints_fake_provider_greeting(tmp_path):
    """--print 走完全链路：system 组装 → harness → loop → provider → 输出。

    --cwd 指到 tmp_path：print mode 会往 <cwd>/.minitau/sessions/ 落盘，
    测试不得污染真实项目目录。
    """
    result = runner.invoke(app, ["--print", "hello", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "Hello from the minitau fake provider!" in result.output


def test_print_mode_unknown_provider_exits_with_usage_error():
    """未知 provider：ValueError 转成 typer.BadParameter，退出码 2。"""
    result = runner.invoke(app, ["--print", "hi", "--provider", "anthropic"])

    assert result.exit_code == 2


def test_print_mode_requires_a_prompt():
    """无 prompt 的 --print：usage error。（既有行为，特征测试）"""
    result = runner.invoke(app, ["--print"])

    assert result.exit_code == 2
