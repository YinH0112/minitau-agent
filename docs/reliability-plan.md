# Minitau 已实现功能的修复与完善方案

日期：2026-09-28。范围：修复已有 P1–P5 功能，为 P6/P7 提供稳定基础。
本文是实施方案，所有任务均待执行；本次只新增文档，不代表修复已完成。

## 1. 审查基线与目标

本次审查运行结果：pytest 208 passed、12 failed；mypy 检查 33 个源文件通过；ruff 7 条问题。
12 个失败均与 Provider 工厂的 fake 分支只剩 `...` 有关。

已复现的其他行为：

- 恢复历史后压缩，内存只剩摘要，磁盘重放仍出现原历史和摘要。
- Windows 命令休眠 2 秒，timeout=0.1，约 2.08 秒才返回，虽然 timed_out=True。
- SSE 没有完成信号直接 EOF、以及顶层 error 载荷，都被当作成功响应。
- 注入模型错误后 CLI 退出码为 0，且不展示错误。
- Windows 上 edit 修改 LF 文件时，将整份文件转换为 CRLF。

还需修复的源码路径：工具取消令牌未透传；自动压缩只在整个 Agent 循环结束后检查。
Provider 网络等待期间的取消、压缩期间的取消，是同一生命周期方案需要覆盖的边界。

完成目标：

1. 默认离线 CLI 和现有测试恢复正常。
2. 历史经“恢复—压缩—再恢复”后语义和顺序一致。
3. Provider 失败可见、取消有效、进程能收尾。
4. 文件编辑保留原始编码标记与换行风格。
5. 每次模型请求前检查上下文预算。

保留现有三包分层、JSONL 格式、独立 compaction 模块。暂不提前实现完整 CodingSession、REPL、其他 Provider 或 TUI。

## 2. 实施顺序

| 阶段 | 对应审查问题 | 主要目标 | 依赖 |
|---|---|---|---|
| R1 | 1 | 恢复 Fake 分支与测试基线 | 无 |
| R2 | 2 | 恢复与压缩的一致性 | R1 |
| R3 | 6 | edit 保留换行和 BOM | R1 |
| R4 | 4 | SSE 终态与错误处理 | R1 |
| R5 | 5 | CLI 错误、进度、资源关闭 | R4 |
| R6 | 3 | 工具、网络、压缩取消及进程收尾 | R4、R5 |
| R7 | 7 | 请求前预算检查与压缩 | R2、R6 |
| R8 | 文档与优化 | 同步路线图、清理注释与 lint | R1–R7 |

每阶段作为独立、可审查的改动。按表顺序实现即可，不需要一次大重构。

## 3. R1：恢复 Fake Provider

文件：`src/minitau_ai/factory.py`。

- 在 name 为 None 或 fake 时，实际构造 FakeProvider，预置 start/done 问候事件，并返回默认模型名。
- 真实 Provider 的配置和分支保持现有语义。
- 默认离线模式不应读取真实密钥或发起网络请求。
- 暂不直接删除 fake 相关“未使用导入”：恢复实现后重新判断它们是否需要。

验收：现有 provider_factory、print_mode、cli_sessions 测试通过，再运行完整现有测试。目标是已有 220 个测试全部通过；若出现其他失败，独立定位，不能为过关降低断言。

## 4. R2：保留历史消息与存储 ID 的对应关系

文件：`session/memory.py`、`harness.py`、`minitau_coding/cli.py`。

最小接口变更：

```python
# SessionState 增加字段，与 messages 一一对应
context_entry_ids: tuple[str, ...]

# AgentHarness.__init__ 新增可选关键字参数
entry_ids: Sequence[str | None] | None = None
```

- SessionState.from_entries 已有 message_rows，直接从压缩后的 rows 导出消息与 ID。摘要行使用 CompactionEntry.id。
- CLI 恢复时同时传入 state.messages 和 state.context_entry_ids。
- Harness 收到显式 IDs 时校验数量匹配；未提供 IDs 的纯内存场景继续允许 None。
- replace_messages 同样允许传 IDs，并拒绝运行中无协调的历史替换；统一消息与 ID 更新的入口。
- 有持久存储的历史必须保留真实 IDs。明确区分“磁盘恢复的历史”和“调用者临时注入的内存上下文”，不能用 None 掩盖已存储消息的身份。
- 压缩遵守先存储成功、再修改内存；存储失败时不改变内存消息和当前链尾。
- 把 _compaction_plan 放入压缩异常处理范围，保证选范围失败也不会让已完成的主任务无提示崩溃。

本阶段保留平行数组，避免同时重写 loop。ContextRow 可以留待以后统一状态容器时引入；单独换数据结构不能替代恢复 IDs 的修复。

验收测试：

- 新建会话 → 重新构造 Harness 恢复 → 触发自动压缩 → 再次重放，比较消息 role/content 与顺序，不要求合成摘要的 timestamp 完全相等。
- 连续两次“恢复—压缩”，确保旧摘要也被正确替换。
- 带工具调用及结果的历史，恢复后关联不丢失。
- 显式消息/ID 长度不匹配时立即报错。
- 存储 append 失败时，不出现只修改一半状态的问题。

兼容说明：无需修改 JSONL schema。已有错误压缩记录若缺少 replaces_entry_ids，不能可靠推断原来压缩了哪些消息；不自动删历史，测试与后续验收优先使用新会话。

## 5. R3：edit 精确保留文件格式

文件：`minitau_coding/tools.py` 的 create_edit_tool。

- 用 read_bytes().decode('utf-8') 读取真实换行。
- 保持现有顺序：剥离 BOM → 检测换行 → 归一为 LF → 校验并应用所有 edits。
- 用 write_bytes(final_content.encode('utf-8')) 写回，避免 Windows 文本模式二次转换。
- 沿用 write 工具已经验证过的字节读写策略。
- 混合换行继续采用现有“主导风格”策略，文档说明不承诺逐行保留混合格式。

验收：LF、CRLF、带 BOM、无末尾换行四类文件按 read_bytes 断言；匹配失败和编辑区域重叠时原始字节不变。Windows 和 POSIX 使用相同预期。

## 6. R4：建立明确的 Provider 终态规则

文件：`minitau_ai/_sse.py`、`stream.py`、`openai_compatible.py`、必要时 `_provider_events.py`。

解析状态至少记录：是否出现有效 choice、是否出现 finish_reason、是否收到 DONE、是否失败、是否输出过内容。

采用以下明确规则：

| 输入情况 | 输出行为 |
|---|---|
| 有 finish_reason，随后 EOF，没有 DONE | 允许正常结束，兼容省略 DONE 的端点 |
| 有有效 choice 和 DONE，没有 finish_reason | 允许协议终止；工具参数仍必须完整合法 |
| 无 finish_reason、无 DONE 就 EOF | 返回错误，保留已收到正文 |
| 顶层 error 或非法 JSON chunk | 返回错误，禁止再发成功终态 |
| 完全空流、仅 DONE、没有有效 choice | 返回错误，不视为空回答成功 |
| 工具参数 JSON 不完整或不是对象 | 不执行工具，返回可诊断错误 |

- 不再把非法工具参数伪装成 {'_raw_arguments': ...} 后继续执行。
- 保留合法空正文回复：有效 choice 的终止与“完全空流”不同。
- stream 翻译层只生成一个终态；遇到错误后不得再发 Done。
- 不把未知的非空 finish_reason 一律映射为成功；为已支持的值明确映射，对拒绝/过滤/不支持的值返回明确状态或诊断。
- 保留“已经吐出内容就不自动重放请求”的重试约束，避免文本与工具调用重复。
- 此阶段不扩展多模态或 Responses API。

验收：新增解析器单元测试与 httpx.MockTransport 集成测试，覆盖上表、分片工具参数、usage-only 尾块、429 后成功、401 不重试、输出后断流不重试。全部离线运行，使用假密钥。

## 7. R5：CLI 正确报告结果并关闭 Provider

文件：`minitau_coding/cli.py`，以及 Provider 生命周期协议/FakeProvider。

- _run_print_session 返回明确状态，由同步入口转换为退出码。
- 成功为 0；模型运行失败为 1；CLI 参数/配置错误继续使用 2；用户取消为 130。
- error/aborted 消息即使正文为空，也必须输出 error_message 或明确的兜底说明到 stderr。
- 部分正文可以保留在 stdout，但不掩盖错误退出码。
- 工具开始、结束和压缩状态写 stderr；不默认打印完整工具参数、请求头或密钥。
- 单个工具失败后模型可能自行修复，因此不要把任何 ToolExecutionEnd.is_error 都直接当成整次任务失败。
- 最小方式保留 MessageEnd 正文输出；真正逐 token 打印可以后续独立加入，避免本阶段引入重复输出。
- 在创建 Provider 后、可能开始使用它的异步生命周期最外层，用 try/finally 调用 aclose。建议为 ModelProvider 增加 aclose 协议、FakeProvider 实现空关闭，并同步测试替身；不在各处散落动态 getattr。
- 注入给 OpenAICompatibleProvider 的 HTTP client 仍由注入方管理，保持现有 owns_client 约定。

验收：模拟 401、断流、空错误正文时非零退出；部分正文仍可见；stdout 无工具进度；成功、异常、取消路径均调用 Provider 关闭；注入 client 不被误关。

## 8. R6：取消传播、超时和进程收尾

文件：`minitau_agent/tools.py`、`provider.py`、`loop.py`、`harness.py`、`compaction.py`、`minitau_coding/tools.py`、Provider 流实现。

### 8.1 统一取消接口

新增轻量 `minitau_agent/cancellation.py` 放 CancellationToken 协议，provider.py 可重新导出以保留导入兼容。这样 tools.py 引入令牌时不会与 provider.py 循环导入。

```python
class ToolExecutor(Protocol):
    def __call__(
        self,
        arguments: Mapping[str, JSONValue],
        *,
        signal: CancellationToken | None = None,
    ) -> Awaitable[AgentToolResult]: ...
```

- AgentTool.execute、loop 的执行入口和四个工具统一 signal 参数。
- 同步更新项目中的假工具。默认 None 只兼容省略参数的调用方，不能让旧的单参数 execute_fn 自动接受 signal，不能遗漏这一步。
- 工具结束后观察到取消就停止新的模型请求；为尚未执行的工具调用补取消结果，维持完整协议历史并持久化。
- 不混淆任务取消与网络错误：Provider 和 Agent 终态应标记 aborted。

### 8.2 bash 收尾

- 默认 timeout=120 秒，显式参数必须是有限正数，拒绝 bool、NaN、infinity。
- 子进程 stdin=DEVNULL，让交互读取看到 EOF；它不能替代默认超时。
- Windows 使用参数数组调用 taskkill /PID <pid> /T /F，POSIX 保留进程组清理；检查进程已退出与清理失败的情况。
- 清理后等待进程回收和输出读取完成，对收尾阶段也设置有限等待，不允许再次无限卡在 communicate。
- 清理/取消所有 watcher task 后 await 回收；外层 asyncio task 取消也走同一收尾路径。
- 保留超时前的输出。必要时把读取从 communicate 改为显式分块收集，以便收尾异常时也能返回已有内容；本阶段不额外追求完整日志流式 UI。
- 超时或取消后仍无法确认清理成功时，返回明确诊断，不能报告“已全部终止”。

### 8.3 网络与压缩取消

- 不能只在收到下一行 SSE 后检查 signal；网络连接建立、等待下一块和重试等待都需要可取消。
- 使用请求/读流任务与取消 watcher 竞速；取消时关闭流并回收任务。HTTP transport 错误保持 error，用户取消保持 aborted。
- run_compaction 接收当前 signal；取消时不得采用已收到的半截摘要，也不得写入 CompactionEntry。

验收：

- 长时本地子进程 + 短超时，在合理容差内返回，不等到子进程自然结束。
- 子进程结束前写入标记文件：取消后确认标记未产生，并确认相关进程已回收；失败测试自身也要 finally 清理。
- 命令等待 input 时收到 EOF，正常结束或明确报错，不依赖用户键盘输入。
- harness.cancel 能中断正在执行的假工具/本地 bash，且后续模型调用次数不再增加。
- 用可阻塞的假 HTTP stream 验证首块前、流中、重试时取消，无真实网络依赖。
- 压缩中取消后，内存与 JSONL 保持原历史。

路线图中 idle timeout 暂缓：持续无输出的计算也可能正常，不能用 idle timeout 替代总时长限制。

## 9. R7：每次模型请求前检查上下文

文件：`loop.py`、`harness.py`、`compaction.py`、`context_window.py`、CLI 配置入口。

### 9.1 调整触发时机

- 给 run_agent_loop 增加可选 before_model_request 异步事件回调；默认 None，直接调用 loop 的已有代码不受影响。
- 回调只定义通用请求前边界，loop 不导入存储、摘要算法或 CLI。
- 在 user/steering/follow-up 消息已追加并完成持久化、上一轮全部工具结果已落定后调用回调，再构造 provider context。
- Harness 实现回调：检查预算 → 可选压缩 → 输出压缩事件 → 允许或拒绝本次请求。
- 将主压缩检查从 AgentEnd 之后移到这个边界，避免已宣布结束却仍有长时间模型请求。

关键约束：loop 正在持有 self._messages 的列表引用。压缩必须使用切片赋值原位更新：

```python
self._messages[:] = new_messages
self._entry_ids[:] = new_entry_ids
```

不能沿用 self._messages = new_messages，否则 loop 会继续拿旧列表请求模型。
本次运行的事件历史仍表示实际发生过的消息；当前有效上下文由 harness.messages 提供。

### 9.2 预算与失败行为

- 暴露 context_window_tokens 配置，CLI 能显式设置，未知模型继续用有说明的默认值。
- 输出预留区和尾部保留预算随窗口缩放；例如作为初始策略，reserve=min(16384, window//4)，keep_recent=min(20000, threshold//2)，并校验预算为正、预留区小于窗口。
- 原有 128k 默认窗口的数值保持稳定；不要把 20k 固定保留预算用于所有小窗口。
- 尾部优先保留完整的最近 user 回合，不能从孤立 ToolResult 开始。若单个回合太大，明确返回无安全压缩范围，不能为满足数字任意切断协议。
- 压缩失败时保留历史；仅超过软阈值且仍能容纳输入/预留输出时可继续，已超过硬预算时输出明确错误，避免循环发送必然超限的请求。
- 每次请求边界最多进行有限次数压缩，检查压缩后是否真的降低占用。
- 摘要请求也有输入预算。先做预算检查和明确失败提示；分块摘要作为后续增强，不能假设换成摘要 prompt 就一定装得下。
- 字符数/4 仍只是估算。后续把已解析 usage 带入 AssistantMessage 的可选字段，用于诊断和校准；不要直接把上一轮 prompt_tokens 当作修改后上下文的精确值。

验收：FakeProvider 连续产出多轮工具调用，检查某次模型请求前已出现压缩；provider 实际收到的是摘要后的消息；恢复后调用同样生效；小窗口配置不会因保留 20k 而完全不压；不可压、压缩失败、用户取消均可正常终止，不丢历史、不无限重试。

## 10. R8：文档、维护与最后验收

- README 与 roadmap 更新为实际 P1–P5 进度，区分“实现存在”和“验收通过”。
- 把本方案中的已完成阶段及验证结果写回路线图，避免保留过时的“195 测试全绿”。
- 校正 list 遍历、异步生成器启动时机、错误兜底范围等注释；解释设计动机的注释保留，重复语法讲解可移到 execution-flow。
- 清理剩余 lint，不做无关的全仓格式重写。
- 每阶段运行对应行为测试；接口跨包变更运行 mypy；阶段完成运行完整 pytest。最终运行 pytest、mypy、ruff，记录最终测试数量。
- 如环境支持，在 Windows 与 POSIX 各验证换行和进程行为；只有 Windows 验证时明确标注 POSIX 未实测。
- 文档修改本身不需要新增代码测试。

后续可选优化：bash 输出边读边写日志并保留有界尾部缓存；统一 ContextRow 状态容器；结构化摘要 prompt；中文 token 估算校准。先完成正确性与生命周期修复，再按实测需要推进。
