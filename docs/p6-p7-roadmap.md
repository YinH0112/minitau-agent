# P6 / P7 后续代码路线（基于当前实现）

> 本文件是单 Agent 交互基础阶段的详细实施路线。完整目标以及 P8–P13（glob/grep、Skill、Hook、MCP、跨会话记忆、多 Agent）的依赖顺序见 [完整 Agent 路线](agent-roadmap.md)。

更新：2026-09-28。本文件是待实现方案；当前最近一次完整验收为 237 个测试通过、mypy 与 ruff 通过，来自上一轮可靠性修复记录，本次文档调整未重新运行测试。

## 1. 当前起点与顺序调整

已经具备：真实 OpenAI 兼容 Provider、四个编码工具、取消令牌、JSONL 重放、context_entry_ids、自动压缩、print 模式的错误和进度展示。

尚未具备：CodingSession、公开手动压缩接口、命令注册表、会话列表/切换编排、可运行的 REPL、当前分支位置持久化。

推荐顺序：

```text
P6.0 生命周期验收
  → P6.1 抽出 CodingSession，先让 print 模式复用
  → P6.2 会话列表/创建/恢复/改名
  → P6.3 手动压缩
  → P6.4 最小命令集
  → P7.1–P7.3 最小 REPL、流式展示、取消退出
  → P6.5 分支与 P6.6 模型切换
  → P7.4 完整场景验收
```

调整理由：先完成“单会话连续使用”的闭环，就能实际验证会话编排。分支和运行时换模型会同时改变历史选择、配置和资源归属，放到已有可运行 REPL 后逐项加入，问题更容易定位。

学习目标：P6 学资源归属、状态恢复和命令路由；P7 学事件消费、终端状态和异步取消。每一步都先写能区分正确/错误行为的测试，再实现。

## 2. 明确模块职责

| 模块 | 负责 | 约束 |
|---|---|---|
| `minitau_agent/harness.py` | 消息/IDs、记录链尾、运行状态、取消、自动与手动压缩 | 是活动历史的唯一写入入口 |
| `minitau_agent/session/*` | 条目模型、序列化、路径查询、重放 | 不读取 CLI 参数、不管理 Provider |
| 新 `minitau_coding/session.py` | 装配/替换 Harness，对外提供会话操作 | 不复制消息列表和压缩算法，不调用 Harness 私有方法 |
| 新 `minitau_coding/session_manager.py` | 定位文件、列表、校验并装载快照 | 先扫描 JSONL，不引入第二份索引数据库 |
| 新 `minitau_coding/commands.py` | 解析命令、校验参数、调用公开会话接口 | 返回结构化结果，不直接 print 或调用 Typer Exit |
| 新 `minitau_coding/rendering.py` | 消费事件并输出正文/状态 | 不改消息，不写 session 文件 |
| 新 `minitau_coding/repl.py` | 输入循环、运行/取消/退出状态 | 同时最多一个活动会话操作 |
| `minitau_coding/cli.py` | 参数、入口分流、应用生命周期 | print 与 REPL 复用相同装配逻辑 |

Provider 由应用入口的单个异步生命周期拥有，退出时统一 `aclose()`；同 Provider 下新建/恢复会话复用该实例。CodingSession 借用 Provider，不在每次换会话时关闭共享 client。P6.6 换 Provider 时再明确转移所有权。

SessionManager 装载的是只读 `SessionSnapshot`：messages、entry_ids、last_entry_id、session_info、path。CodingSession 只在装载/切换时使用快照，运行中直接以 Harness 为准。

## 3. P6.0：先验证长驻进程需要的生命周期

重点文件：`harness.py`、`loop.py`、`openai_compatible.py` 和现有取消测试。

现有测试是起点，不代表所有长驻场景都覆盖。新增以下门槛测试，失败时先做局部修复：

- 关闭事件迭代器、外层 task.cancel、存储 append 失败后，运行标志和当前 signal 均能复位；下一次 prompt 能正常运行。
- 已将运行锁移到事件流首次消费时；验证“创建后尚未迭代就放弃”不会占用会话或改写历史。已经开始消费而未完成的迭代器仍由调用方负责关闭。
- Harness 的 finally 中，补齐结果落盘失败也必须执行状态复位，可用嵌套 try/finally 保证。
- 外层取消时，loop 创建的工具任务、Provider 取下一项任务都被取消并等待回收。合作式 cancel 与外层 task.cancel 分别验证。
- 当前 AgentEnd 之后还可能产生收尾压缩事件。前端必须以整个异步迭代器结束作为操作完成标志，不能收到 AgentEnd 就提前退出消费。
- 同时发起第二个 prompt/compact/会话切换时拒绝操作，不能交叉修改历史。

验收产物：可重复开始、取消、失败、关闭的单个会话操作；不提前引入任务调度框架。

## 4. P6.1：从 print 模式抽出 CodingSession

新增 `session.py`，迁移 CLI 目前的上下文发现、工具与系统提示词装配、新建/恢复 Harness 逻辑。

建议公开接口（设计草图）：

```python
class CodingSession:
    def prompt(self, text: str) -> AsyncIterator[AgentEvent]: ...
    def cancel(self) -> None: ...
    @property
    def is_running(self) -> bool: ...
    @property
    def messages(self) -> tuple[AgentMessage, ...]: ...
    async def new(self) -> SessionSummary: ...
    async def resume(self, ref: str) -> SessionSummary: ...
```

- 使用小型配置 dataclass 传 cwd、Provider、model、窗口，减少 CLI 参数在多层重复传递。
- 运行中的会话操作只通过公开接口；需要链尾快照时为 Harness 增加只读属性，不能读写 `_last_entry_id`、`_entry_ids`。
- 新建空会话可立即写一条 SessionInfoEntry；第一条用户输入到来时填写默认标题。不会为了初始化会话调用模型。
- `cli.py` 的 print 路径先改为消费 `CodingSession.prompt()`，保持当前 stdout/stderr 与退出码契约。

验收：现有 print/CLI 会话测试通过；注入 Provider 可完整测试 session，测试不依赖 Typer；Provider 在应用退出时恰好关闭一次。

## 5. P6.2：会话管理先做完整的“装载后切换”

新增 `session_manager.py`，迁出 `_sessions_directory`、ID 生成、精确/前缀解析与最新会话选择。

建议接口：`list_sessions()`、`load(ref)`、`latest()`、`create()`。列表展示 ID、标题、更新时间、当前会话标记。

- 列表只扫当前 cwd 的 `.minitau/sessions`；ref 作为 ID/前缀匹配，不直接接受路径或 glob 表达式。
- 标题直接从 SessionInfoEntry 重放得到。暂不自动生成标题，不单独维护 index.json。
- `/resume` 先读取、解析、校验并构造候选 Harness；全部成功后才替换当前会话。文件损坏、歧义前缀、无匹配时原会话继续可用。
- 恢复始终同时传 messages、entry_ids、last_entry_id；单独 `replace_messages()` 不足以切换到另一条历史链。
- cwd 在本次应用运行中固定。读取到不同 cwd 的会话时明确拒绝或要求重新启动，避免历史里的项目与工具实际工作目录不一致。
- `/name` 追加 SessionInfoEntry，保留 cwd。新增 Harness 的窄接口 `append_session_info(...)` 统一维护 parent_id/链尾；SessionManager 不在活动 Harness 背后直接追加条目。
- 本阶段约定一个会话文件仅由一个活动写入者使用；跨进程并发写入不作为支持能力。

验收：新建、恢复、列表、改名和失败回滚；改名后下一条消息 parent_id 正确；坏文件不阻止列出其他好会话；相同更新时间有稳定排序。

## 6. P6.3：把手动压缩变成公开会话操作

先给 Harness 增加公开 `compact(keep_recent_tokens=0) -> AsyncIterator[AgentEvent]`，再由 CodingSession 转发。自动与手动压缩复用选范围、摘要、持久化和原位替换的内部实现。

- 手动操作拥有与 prompt 相同的互斥锁、signal、取消与 finally 收尾；发出 reason=manual 的 CompactionStart/End。
- `keep_recent_tokens=0` 保持旧计划的“全压”语义。没有可压历史时返回明确的无操作结果，不调用 Provider；单条非摘要消息是否可压缩单独定义，不受自动压缩的“少于两条”限制误伤。
- 成功后模型上下文为摘要，JSONL 原始历史仍在；不调用 `replace_messages([summary])` 绕过 CompactionEntry。
- 失败、取消、落盘错误时有效上下文保持原样。
- 摘要使用 Goal / Constraints / Completed / In progress / Next steps / Key files 六项结构，但不要求模型必须生成可解析 JSON。
- 提交摘要请求前估算摘要 prompt 大小。单次装不下时给出明确说明；分块总结留作后续独立增强。

验收：手动压缩→退出→恢复一致；取消摘要不写成功记录；同任务内自动与手动压缩共用实现但触发条件互不混淆。

## 7. P6.4：最小命令集与统一结果

新增 `commands.py`，用 dataclass 描述 name/help/handler。解析只分离第一个命令词，余下字符串按原样交给 handler，避免把中文标题和 Windows 路径交给 shell 解析器。

| 命令 | 初版行为 |
|---|---|
| `/help` | 从注册表生成帮助 |
| `/status` | 当前会话、cwd、model、估算 token/窗口、运行状态 |
| `/sessions` | 列出当前项目会话 |
| `/new` | 新建并切换 |
| `/resume <id或前缀>` | 校验成功后切换 |
| `/name <标题>` | 追加元信息并更新列表显示 |
| `/compact` | 执行手动压缩 |
| `/exit` | 返回退出意图，由 REPL 做清理 |

`CommandResult` 至少包含 handled、message、exit_requested、error。耗时命令通过传入的事件接收器消费 session 的事件流，复用 renderer；不把 async generator 混入普通字符串字段。

普通文本交给模型；未知 `/xxx` 命令返回帮助提示，不意外发送给模型。约定 `//text` 转义成发送给模型的 `/text`。命令调用不作为 UserMessage 写入历史，实际状态变更通过已有条目记录。

验收：解析、参数缺失、未知命令、转义、空白/中文标题；运行中不允许切换、压缩或改模型。命令 handler 可脱离终端独立测试。

## 8. P7.1：先做顺序运行的最小 REPL

新增 `repl.py`，CLI 不带 `-p` 时进入一个 `asyncio.run(run_repl(...))`。整个应用只创建一次事件循环和 Provider。

- 首版采用“读一行→完整消费当前操作→再读下一行”，运行时不接收普通排队输入，暂不暴露 steering/follow-up。
- 标准库 `input()` 只在空闲时调用，此时没有正在执行的模型/工具任务。输入通过可注入的小接口隔离，测试使用预设字符串序列。
- 不用 `asyncio.to_thread(input)` 反复创建阻塞输入任务，避免退出时后台线程仍卡在 stdin。以后需要边生成边打字时，再替换输入适配器。
- 空行忽略，EOF 正常退出；初始位置参数作为第一轮 prompt；`--resume`、`--continue`、`--context-window` 与 print 模式走同一装配路径。
- 入口先判断 TTY。管道输入只用于 `-p`；无 `-p` 且 stdin 非 TTY 时给出明确用法错误，不先把 stdin 全读完再进入交互。把当前无条件 `_merge_stdin_prompt` 改到 print 分支。

离线演示的补充：当前 factory 的 FakeProvider 只预置一次回复。保留用于确定性测试的脚本 FakeProvider 不变，新增小型 DemoProvider 或明确的工厂演示实现，可对每轮输入生成回应；不因 REPL 第二轮耗尽脚本而错误宣称交互不可用。复杂工具场景仍用注入的脚本 FakeProvider 测试。

验收：连续三轮、初始 prompt、新建/恢复后继续、EOF、无 TTY；关闭时 Provider 恰好释放一次，print 模式测试保持通过。

## 9. P7.2：把事件展示抽成共享 renderer

从 `_run_print_session` 的分支输出抽出 `rendering.py`，print 与 REPL 共用错误、工具和压缩展示。REPL 开启文本增量输出。

- 消费 `MessageUpdateEvent.assistant_message_event` 中的 TextDelta；仅输出新增部分，不重复打印 partial 快照。
- 按每条助手消息跟踪已输出正文。MessageEnd 若已有增量，只补尚未输出部分/换行；FakeProvider 只有 done 时，完整输出正文。
- 出错前的部分文本保留；工具调用本身不当正文打印，状态继续走 stderr。
- `/compact` 的状态事件也经过同一 renderer。不要依赖 AgentEnd 关闭渲染器；以迭代器结束作为整个操作边界。
- 首版无终端动画和光标重绘，先验证字符不重复、不丢失与 stdout/stderr 分离。

验收：多文本块、仅 done、delta 后 done、部分输出后 error、工具调用前后两条助手消息，捕获输出逐字比较。

## 10. P7.3：明确 Ctrl+C 和退出状态

状态为 `IDLE → RUNNING/COMPACTING → CANCELLING → IDLE`，任一状态可请求 `CLOSING`。

- IDLE 下 Ctrl+C 清空本次输入并继续；空闲状态连续两次 Ctrl+C（例如 2 秒窗口内）退出。`/exit` 和 EOF 直接进入关闭流程。
- RUNNING/COMPACTING 第一次 Ctrl+C 只调用 session.cancel()，继续消费结果和收尾事件。清理完成才重新出现输入提示符。
- CANCELLING 再按 Ctrl+C，记录“清理后退出”的意图，启动有限等待；不要直接 `sys.exit()` 越过 finally。
- 活动操作期间使用临时的主线程 SIGINT 处理器，只设置取消/退出标志；恢复默认处理器后再调用空闲 input。不依赖 Windows 不支持的事件循环信号接口；主循环定期处理标志。
- 超过关闭期限时取消活动任务并 await 回收；进程清理无法确认成功则报告失败，不能打印“全部已停止”。P6.0 的外层取消测试是这里的前提。
- 最外层 finally 关闭事件流、活动操作和 Provider；错误退出码与 print 约定一致。

验收：通过注入中断动作分别测试模型停滞流、bash、手动压缩、空闲输入；Windows 真终端手工验证一次/两次 Ctrl+C。模拟动作测试不能替代真实终端行为验收。

## 11. P6.5：基础 REPL 稳定后，再实现同文件分支

新增命令 `/tree`、`/branch <entry_id>`。保留 append-only JSONL，同一个会话文件保存多个分支。

现有 `path_to_entry()` 只是树查询基础。还需完成：

1. entries.py 增加选择当前分支位置的 LeafEntry，并加入判别联合和序列化测试；标记保存目标 entry_id，不作为模型消息。
2. SessionState 增加按指定 leaf 重放，以及解析默认活动 leaf 的规则。无标记的旧线性文件继续恢复最新有效链尾。
3. 显式选择分支后立即落盘标记，即使用户未发新消息就退出，下次也恢复到选中节点；后续消息以选中节点为 parent，而不是文件最后一行的 ID。
4. SessionInfo 的标题/cwd 保持会话全局元信息；分支消息按 root→leaf 路径取，不能把其他分支的消息混入。
5. 初版只允许安全节点：完整助手回合结束或压缩节点；拒绝尚未配齐工具结果的中间节点，给出可选择的最近安全位置。
6. 从完整历史路径应用 CompactionEntry；在压缩前节点分叉和压缩后节点分叉都要验证。选择失败时当前分支不变。

验收：A→B→C 后从 B 创建 D，恢复 D 时看不到 C；切回 C 后看不到 D；只切换不发消息也可跨进程恢复；旧 JSONL 兼容；缺失节点/循环引用明确报错。

## 12. P6.6：模型切换先限定范围，再扩 Provider

先做 `/model` 查询、`/model <name>` 在当前 Provider 内切换，空闲时生效；保留消息历史并重新计算窗口。

- 增加 ModelChangeEntry 记录 Provider 名、model、context_window，绝不记录 API key；旧文件无此条目时使用启动配置。
- 明确恢复优先级：显式启动参数 > 会话中最后生效的配置 > 工厂默认值。分支上模型配置按活动历史路径重放。
- 当前兼容模型的 message/tool 协议是可支持范围，不承诺任意厂商之间直接迁移历史。
- 新 model 名无法仅靠本地保证有效；设置成功与首个模型请求成功分开报告，不额外发探测请求消耗费用。
- 跨 Provider 切换留作下一步：先构造并校验新配置、落盘成功后替换引用，再关闭旧实例；失败时旧实例和会话仍可用。

验收：历史保留、窗口变化、启动覆盖、重启恢复、运行中拒绝、构造失败不关闭旧 Provider。

## 13. 最终验收与建议提交粒度

| 提交/阶段 | 可观察的完成信号 |
|---|---|
| P6.0 | 取消/异常/迭代器关闭后可再次运行，无遗留活动任务 |
| P6.1 | print 由 CodingSession 驱动，原行为测试通过 |
| P6.2 | 新建/列表/改名/恢复，失败时当前会话保留 |
| P6.3–P6.4 | 命令离线测试通过；手动压缩跨重启可恢复 |
| P7.1–P7.3 | 连续三轮交互、文本无重复、Ctrl+C 后可再提问、退出可清理 |
| P6.5 | 分支互不混入，只切换不提问也能恢复 |
| P6.6 | 同 Provider 换模型与配置重放正确 |
| P7.4 | 对临时项目完成“读文件→修改→取消长命令→继续→压缩→恢复→分支”流程 |

每步运行相关测试，跨包接口变化加 mypy；阶段完成运行完整 pytest 和 ruff。真实端点的最小冒烟放在离线测试通过之后，验证一轮文本和一次工具往返即可。不要把 TUI、插件、自动标题生成、并行排队输入或多 Provider 大目录加入本轮范围。
