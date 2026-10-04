# 已实现功能的可靠性修复记录

日期：2026-09-28。本文记录实际代码变更。原始审查发现、实施顺序与更详细的设计取舍见 [reliability-plan.md](reliability-plan.md)。

## 1. Provider 工厂与默认 CLI

改动：`src/minitau_ai/factory.py`、`src/minitau_ai/fake.py`、`src/minitau_coding/cli.py`。

- 恢复 `create_provider(None)` 和 `create_provider("fake")` 的真实分支。之前分支只有 `...`，会落入 Unknown provider；默认 CLI 因而无法运行。
- FakeProvider 增加无资源的 `aclose()`，统一 Provider 生命周期接口。
- CLI 把会话路径解析移到 Provider 构造之前，减少参数错误时的无用资源创建；Provider 使用结束后从 `finally` 关闭。
- CLI 在模型消息为 error/aborted 时把原因写到 stderr，并分别返回 1/130；工具开始/结束、压缩状态也输出到 stderr。stdout 仍只放助手正文，方便管道使用。工具调用失败不立即判定整次任务失败，模型仍有机会自行修复。
- 增加 `--context-window` 参数，以便为不同模型显式配置窗口；非正数在 CLI 拒绝。

为什么：恢复离线测试基线，让真实模型错误不再表现为“空输出且退出码 0”，并确保 HTTP client 有明确的关闭时机。

## 2. 会话恢复、压缩与中断结果落盘

改动：`src/minitau_agent/session/memory.py`、`src/minitau_agent/harness.py`、`src/minitau_coding/cli.py`。

- SessionState 从现有 message_rows 导出 `context_entry_ids`。CLI 恢复 Harness 时同时传入消息和对应条目 ID；Harness 验证长度相同，并拒绝带已有链尾却缺少条目 ID 的恢复请求。
- 压缩前要求持久化历史都有条目 ID。先写 CompactionEntry，成功后再原位更新内存消息和 IDs。原位更新也让运行中的 loop 继续引用正确的列表。
- 中断补出的 ToolResult 以及手工插入的未记录消息，在下一轮开始或当前轮结束时写入 JSONL；不再只存在于内存。
- 压缩计划或摘要失败时保留历史并广播失败状态；同一任务内不对同一失败反复发起摘要请求。模型任务已经失败时不再启动收尾压缩。

为什么：旧实现恢复消息时把所有 `_entry_ids` 设成 None，导致压缩只在内存生效，重放时原历史又出现。中断后的补齐结果也需要进入持久历史，才能保持下一次模型请求的工具协议完整。

兼容边界：JSONL 格式未变。旧版已经写出的、缺少 `replaces_entry_ids` 的错误压缩记录没有足够信息自动恢复其原意，程序不会猜测并删除历史。

## 3. edit 与 bash 工具

改动：`src/minitau_coding/tools.py`、`src/minitau_agent/tools.py`、新增 `src/minitau_agent/cancellation.py`。

- edit 用 `read_bytes().decode("utf-8")` 检测原始换行和 BOM，用 `write_bytes()` 写回。之前 Windows 文本模式会把未修改的 LF 行尾也变为 CRLF。
- bash 默认超时设为 120 秒，关闭 stdin；显式 timeout 拒绝布尔值、非有限值和非正数。
- Windows 超时/取消时使用 `taskkill /T /F` 清理 shell 及子进程；POSIX 仍清理进程组。收尾读取设有限等待，无法确认进程已停止时返回诊断。
- 取消令牌抽成独立协议，避免 Provider 与工具之间循环导入。AgentTool 新增可选的 `execute_with_signal`，现有只接受参数的假工具继续能用；bash 接收令牌。
- loop 执行工具时同时观察取消令牌，取消后等待工具有限时间完成清理，并阻止后续模型请求。

为什么：原先 bash 的 `token=None` 使取消穿透无效；Windows 仅杀直接 shell 后，孙进程仍可持有输出管道，让 0.1 秒的超时实际等到命令自然结束。现在测试验证实际耗时，而非只看 `timed_out` 标志。

## 4. 模型流的成功、失败与取消

改动：`src/minitau_ai/_sse.py`、`_provider_events.py`、`stream.py`、`openai_compatible.py`、`src/minitau_agent/compaction.py`。

- SSE 解析器记录是否收到有效 choice、DONE 和 finish_reason。无完成证据的 EOF、只有 DONE 的空流、顶层 error、非法工具调用索引或 JSON 参数，都返回错误；不能执行残缺工具调用。
- 已有 finish_reason 的端点允许省略 DONE；有效 choice 加 DONE 允许没有 finish_reason。未知结束原因显式返回错误。
- RawStreamError 区分普通错误和用户取消，翻译层只发一个终态事件，并保留部分正文用于诊断。
- Provider 流外层为“等待下一项”和取消令牌做竞速：即使 HTTP 连接正等待下一字节，也能关闭流并返回 aborted。
- 摘要请求也接收当前取消令牌；取消后的半截摘要不会写入 CompactionEntry。

为什么：此前截断的正文和顶层错误载荷都可能被包装成成功回答；等待网络下一块时仅检查循环内令牌无法及时响应取消。

## 5. 模型请求前的上下文预算

改动：`src/minitau_agent/loop.py`、`harness.py`、`compaction.py`、`context_window.py`。

- loop 增加通用的 `before_model_request` 事件回调，Harness 在每次模型请求前检查预算并尝试压缩；完整任务结束后的检查仍保留。
- 压缩边界优先对齐下一条 user；若最新回合还没有下一条 user，则回退到该回合的 user 起点，以保留整段“用户请求—工具调用—工具结果”。
- 尾部保留预算随配置窗口缩小；预留区也随小窗口缩小。128k 默认窗口的旧阈值保持不变。
- 压缩后估算仍超过硬窗口时生成可见错误，停止本次模型请求；不会反复发送明显过大的上下文。

为什么：只在整个 Agent 循环结束后压缩，长工具链的中间模型请求仍可能超窗。固定保留 20k token 也会让小窗口永远找不到可压前缀。

局限：当前 token 估算仍是字符数经验公式，不等同于模型实际 tokenizer；摘要请求本身尚未实现分块总结。单个最新回合大到无法容纳时会明确报错，避免拆断工具协议。

## 6. 测试与文档

改动：新增 `tests/test_reliability_regressions.py`，更新 compaction、context_window、取消与恢复测试；更新 `README.md` 和路线图顶部进度提示。

新增回归场景覆盖：模拟 HTTP 断流/错误/无效工具参数/停滞流取消；恢复后压缩再重放；中断工具结果持久化；LF、CRLF、BOM 原始字节；Windows bash 真实耗时和取消；CLI 非零退出；长工具循环中间压缩。

最终验收命令：

```powershell
uv run pytest -q
uv run mypy
uv run ruff check src tests
```

最终验收结果：`237 passed in 11.80s`；mypy 检查 34 个源文件，0 错误；ruff 全部通过。真实第三方 Provider 未做付费联调；网络行为以 `httpx.MockTransport` 离线验证。当前环境是 Windows，POSIX 进程组分支未在本机实测。
