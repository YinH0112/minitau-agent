# 应用内配置 API 与供应商

本次改动让 `uv run minitau` 可以从没有环境变量的状态开始。首次启动显示配置向导，进入会话后可以用 `/config` 修改 key、端点和模型，用 `/provider` 切换供应商。没有真实 API 时可以选择 `demo` 离线体验。

## 如何使用

```powershell
uv run minitau
```

选择供应商名称或编号。真实供应商依次输入 Base URL、模型 ID、API key；前两项回车使用默认值，已有 key 时回车保留。输入 `q` 或 Ctrl+C 取消。key 通过 `getpass.getpass()` 隐藏输入，不通过普通聊天输入读取。

```text
minitau> /config deepseek
minitau> /provider
minitau> /provider openai
minitau> /model 你的模型ID
```

支持 `deepseek`、`kimi`、`openai`、`custom`、`demo` 和 `fake`。前三者沿用已有项目预设；`custom` 用于其他 OpenAI Chat Completions 兼容服务，填写 API 根地址，例如 `https://example.com/v1`，不要填写完整的 `/chat/completions` 路径。模型 ID 需要是账号和服务端支持的实际 ID，向导允许直接输入。

配置完成只表示本地设置生效，不会自动发送测试请求；下一条聊天消息才会调用服务。401 错误可以用 `/config <供应商>` 替换 key，无需重新启动。

## 修改了哪些代码

| 文件 | 修改及原因 |
| --- | --- |
| `src/minitau_coding/provider_settings.py`（新增） | 保存非秘密设置和凭据，分别放入 `settings.json` / `credentials.json`；Pydantic 校验地址与模型 ID；通过临时文件加原子替换防止半截写入损坏旧文件 |
| `src/minitau_coding/provider_runtime.py`（新增） | 根据保存的设置或环境变量创建 Provider；保存所有自己创建的实例，统一关闭；提供配置状态列表，避免 UI 直接处理 HTTP 客户端 |
| `src/minitau_coding/configuration.py`（新增） | 可注入输入函数的配置向导，普通输入和隐藏输入分开；先完成全部输入与校验再保存；支持取消和回车保留 key |
| `src/minitau_coding/cli.py` | 首次启动、缺 key 时打开向导；print 模式只读取已有设置；退出时关闭运行期间创建的全部 Provider |
| `src/minitau_coding/commands.py` | 加入 `/config` 和 `/provider`；`/status` 展示供应商；`/model` 同步已保存供应商的默认模型 |
| `src/minitau_coding/repl.py` | 接收配置请求，在 idle 中执行向导，再应用到会话；配置输入不会进入 Agent 消息流 |
| `src/minitau_coding/session.py` | 加入可注入的 Provider 工厂与 `set_provider()`；同时切换实例、供应商名称和模型；跨供应商的恢复与分支选择也恢复正确实例 |
| `tests/test_interactive_configuration.py`（新增） | 验证从零配置、下一轮请求、重启复用、取消、旧环境变量覆盖、跨供应商恢复及资源释放 |
| `README.md` | 更新启动、命令、配置优先级和本地凭据说明 |

## 为什么按这几层设计

先对照了 tau 的 `tau_coding/provider_config.py`、`credentials.py`、`provider_runtime.py` 和 `session.py`：tau 把持久化设置、凭据读取、Provider 实例创建、会话状态切换分开。`set_provider()` 按供应商设置选择默认模型，`_set_provider_model()` 创建新实例并登记到 `owned_providers`，`aclose()` 统一释放。

Minitau 保留这个职责分离，但实现范围是现有的 OpenAI 兼容协议和 API key，不包含 tau 的 OAuth 和其他协议适配器。配置暂时按项目放在 `.minitau/`，便于教学与隔离不同项目。

Session 原本借用启动时传入的 Provider。因此新工厂仍由应用入口管理生命周期，Session 不关闭调用者提供的实例。应用入口在 `finally` 中关闭所有创建过的 Provider，包括切换中创建但没有成功应用的实例；一个实例只关闭一次。这样替换供应商后不会遗漏 HTTP 客户端清理，也不破坏现有嵌入式调用。

`CodingSessionConfig.provider_factory` 是一个可调用对象：输入供应商名字，返回 `(Provider 实例, 默认模型)`。`runtime.create` 是绑定方法，传入配置后，Session 只需要调用它；无需知道 key 保存在什么文件。没有这个工厂的嵌入式会话仍保留原有行为。

## 数据如何流动

```text
首次启动 / /config
  → 向导：供应商、URL、模型、隐藏输入 key
  → ProviderSettingsStore：分别保存设置与凭据
  → ProviderRuntime.create：读取设置与 key，创建 Provider
  → CodingSession：写 ModelChangeEntry，更新 Harness 配置
  → 下一条用户消息：Harness → agent loop → 新 Provider → HTTP 请求
```

API key 只经过隐藏输入、凭据文件和运行时 HTTP 配置。配置向导没有调用 `session.prompt()`，所以配置内容不会作为用户消息发送到模型。模型切换日志只包含供应商名称、模型和窗口。

切换时先创建新 Provider，再持久化模型切换记录，成功后才替换内存状态。新客户端创建或会话写入失败时，当前对话继续使用原来的 Provider。配置文件与会话日志是两套持久化数据，没有跨文件事务：向导保存成功但会话应用失败时，已保存配置可能成为下次启动的默认值，当前会话保持原状态并显示错误。

跨供应商分支选择也先准备对应 Provider，再追加分支选择记录，最后更新运行状态，避免已切换日志分支、实际客户端却没切换。切换供应商时清空原供应商的上下文窗口设置，采用现有默认预算；不同服务的真实窗口需另行明确。

## Ctrl+C 为什么要区别处理

等待模型响应时，Ctrl+C 请求 Agent 取消；等待配置输入时，Ctrl+C 应直接取消向导。这两种状态需要不同的 SIGINT 处理器。因此 `/config` 命令只返回“请求打开向导”的结果，REPL 在活动命令结束后用 `catch_idle_interrupts()` 读取配置，再通过活动操作监督器执行会话切换。取消向导后恢复原处理器，继续聊天。

## 保存位置与优先级

- `<cwd>/.minitau/settings.json`：默认供应商、各供应商的端点和默认模型。
- `<cwd>/.minitau/credentials.json`：各供应商的 API key，本地明文；Git 已忽略整个 `.minitau/`。
- `<cwd>/.minitau/sessions/*.jsonl`：聊天历史和供应商/模型选择，不由配置流程写入 key。

供应商和模型：显式启动参数优先，其次恢复会话的分支选择，然后项目默认设置。进入后通过 `/config` 或 `/provider` 作出的新选择会替代启动选择；后续 `/resume` 和 `/branch` 继续跟随目标分支。

key：本地保存值优先，缺少保存值时再读取环境变量。端点：保存的设置优先，否则沿用旧环境变量方式。这样应用中更新 key 后，旧终端环境变量不会再把它覆盖。`/model` 同时更新当前会话和已保存供应商的默认模型；未保存设置的环境变量模式仍保持原有会话级行为。

每个文件使用同目录临时文件写入、flush/fsync 后 replace，权限设置参考 tau 的凭据存储方式。在 POSIX 上临时文件设置为 `0600`；Windows 的 `chmod` 不是用户级 ACL，凭据访问权限取决于目录权限。它不是加密存储。现有工具没有文件系统沙箱，这次改动也没有增加凭据访问隔离。

## 验证

```powershell
uv run pytest -q tests/test_interactive_configuration.py
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

新增验收使用真正的 OpenAI 兼容适配器配合 `httpx.MockTransport`，检查实际请求地址、Authorization、模型 ID、历史消息和客户端关闭。不会调用真实 API，也不会消耗模型额度。实际 key 是否有效需要用户在终端发送消息验证。
