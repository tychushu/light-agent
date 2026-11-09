# Light Agent

简体中文 · [English](README.md)

为 **原生 Termux** 设计的轻量终端 Agent。使用 Python 3.11+、标准库与唯一运行时依赖
`httpx`，无需 Agent 框架、后台服务、root 或 proot。支持本地、云端 OpenAI 兼容 API。

## 安装与启动

```sh
git clone https://github.com/tychushu/light-agent.git
cd light-agent
python -m venv .venv
. .venv/bin/activate
pip install -e .
export CLOUD_API_KEY='你的密钥'
ta
```

启动命令是 `ta`，Python 包名是 `termux-agent`。密钥仅从环境变量读取，不写入 TOML；
无需认证的本地接口可不设置密钥。已有部署可继续使用自己的工程位置和启动脚本。

```sh
ta --api local
ta --directory ~/project
ta --session abcdef123456
ta --prompt '检查当前目录'
```

## API 配置与切换

配置文件为 `~/.config/termux-agent/config.toml`。全局配置必须放在所有 `[apis.*]`
表之前；API 配置可覆盖支持的全局选项。

```toml
default_api = "local"
ui_language = "zh" # 简体中文 zh；英文 en
approval_policy = "on-risk"
max_steps = 16
max_output_chars = 20000
max_context_chars = 500000
timeout = 120.0
retry_max_attempts = 3
retry_wait_budget = 20.0
retry_total_timeout = 0.0 # 0 沿用 timeout；可单独设置接受响应前的总重试时限
max_response_bytes = 16777216 # 接收正文上限 16 MiB，可按 API 覆盖
stream = true
silent_writes = true

auto_compact = true
auto_compact_threshold = 0.75
auto_compact_keep_turns = 2
auto_compact_keep_groups = 6
tool_history_keep_groups = 6 # 更早已完成任务的工具历史；0 关闭主动整理

[apis.local]
kind = "local"
base_url = "http://127.0.0.1:8080/v1"
model = "你的本地模型ID"
token_count_mode = "local"
output_reserve_tokens = 4096
max_output_tokens = 4096
chat_max_tokens_field = "max_tokens"

[apis.cloud]
kind = "cloud"
base_url = "https://your-provider.example/v1"
model = "你的云端模型ID"
api_key_env = "CLOUD_API_KEY"
wire_api = "responses" # 或 chat_completions（默认）
token_count_mode = "estimate"
# context_window_tokens = 32768 # 填写接口实际支持的模型窗口
```

`# Cloud API` 等注释**不会结束 TOML 表**。`[apis.local]` 后的键在下一个表头前都属于
local，不因注释或空行变成全局。共享限制放在第一个表头前，云端专属限制明确放在
`[apis.cloud]` 内；用启动提示和 `/config` 核实当前 API 实际生效的调用、时间与自动恢复限制。

`base_url` 填 API 根地址，例如以 `/v1` 结尾；客户端自动添加 `/responses` 或
`/chat/completions`。Responses 支持 JSON、SSE、工具调用、思考内容保留及无状态多轮历史回传。
可自由添加多个本地、云端 API 与不同模型；名称没有固定限制。

无状态 Responses 请求会保留 reasoning 项及其加密内容，与函数调用、结果一起回传。
不能为了省 token 而一律删除 reasoning：[OpenAI 官方说明](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context)
建议保留当前工具链所需的推理项。加密字段的序列化字符数不等于实测输入 token 消耗；
用于终端显示的思考文字不会作为第二份 saved Responses 历史重复添加。

- `/api`：列出配置；`/api NAME`、`/api N`、`/api next`：切换并新建会话。
- `/api default NAME`：设置以后启动时默认使用的 API。
- `/api add NAME URL MODEL [KEY_ENV] [local|cloud]`：创建独立配置文件。
- `/api show NAME`、`/config`：查看设置，不显示密钥值。
- `/api remove NAME`：仅删除通过命令添加的非当前配置，并保留恢复副本。

独立配置存放在 `~/.config/termux-agent/profiles/`。命名配置未指定 `api_key_env`
时不发送密钥，不借用其他配置的凭据。`ta --config PATH` 可指定其他配置文件。

## 上下文自动压缩与状态栏

新的人类任务开始时，`tool_history_keep_groups` 会主动精简更早完成轮次中的完整工具组，
无需等到预算阈值。全局或各 API 可设置 0–100，默认保留最近 6 组，0 关闭此项整理。
全部用户指令、最终回答、当前任务的完整工具链、近期工具组及未知副作用均保留。
整理在当前会话内归档前一版上下文，不重放工具。`/continue`、简短的“继续执行”“接着做”或
`keep going` 不是新任务边界，旧历史中的续跑填充消息也不再作为边界，因此不触发新任务整理。

每次调用模型前检查上下文，默认达到预算 **75%** 时提前压缩，尽量降至触发阈值的四分之三，
为后续执行预留空间。字符预算和已知 token 窗口都会参与检查；token 预算先扣除输出预留。
跨轮次历史也能压缩，最后一条用户消息没有工具调用时仍然有效。手工配置的阈值优先。

自动压缩保留会话 ID 和标题，在同一个 SQLite 事务中保存完整的前一版并更新活跃上下文。保留系统提示、全部用户指令、
近期回答、最近完整的助手/工具调用组以及未知副作用。较早工具输出变为有界的产物引用；
必要时将较早回答提取为明确标注的片段。保留组中的思考与协议数据不改写、不拆散。
这是本地确定性精简，不额外调用模型总结；片段不等于完整语义摘要，缺失事实需显式找回。
预算触发压缩时，优先尝试 `auto_compact_keep_groups`（默认 6），再尝试一半及 1 组，
选择满足目标预算的最深保留候选。6 组不是硬下限，也不是按窗口百分比分配；现有配置
显式写了 2 时仍保留该覆盖值，需要自行改为 6 才能应用新偏好。
达到软阈值但尚未达到硬上限时，候选需至少释放五个百分点的预算并回到触发阈值以下。
手动压缩和硬上限恢复可采用更小的精简幅度。

| 命令 | 用途 |
|---|---|
| `/compact` | 手动执行跨轮次精简，保留用户指令 |
| `/compact N` | 明确只保留最近 N 个用户轮次，范围 1–100 |
| `/compact task [N]` | 精简当前任务的早期工具链，保留最近 N 个完整工具组 |
| `/session archives [ID\|N]`、`/session archive ARCHIVE_ID` | 列出压缩归档，查看指定快照最近消息。完整快照可用 `/export ARCHIVE_ID` 导出 |
| `/sessions all [N]` | 逐条显示旧版分支。默认列表把明确的线性压缩链折叠成一行，序号指向最新分支 |
| `/recall TEXT`、`/history`、`/session load ID`、`/export` | 检索、查看或导出完整历史 |

交互终端 Status 栏显示会话 ID、模型、工作路径、标题和当前上下文，例如：

```text
Context=正常 | Chars=120,000/500,000 (24%) | Tokens≈40,000/未知窗口 | Auto=75%
```

- `Chars`：实际序列化请求的字符总量及本地上限，包括工具定义与自动任务提示。
- `Tokens≈`：估算输入 token；未配置窗口时明确显示“未知窗口”，不虚构 token 百分比。
- 已知真实窗口时，在对应 API 设置 `context_window_tokens`，状态栏同时显示 token 百分比与输出预留。
- Status 刷新不访问网络。`token_count_mode = "local"` 可在请求前按需读取 llama.cpp 元数据、
  调用其 tokenizer；云端使用估算。`/stats` 区分服务端用量和请求前估算。
- `token_count_mode = "off"`：关闭 token 计数；字符保护仍生效。

对已确认可用窗口的长上下文 cloud 路由，可在该 API 配置下设置 `max_context_chars = 1000000`。
默认阈值对应 75 万字符。请求体和 API 配置相同时可复用服务端报告的输入用量。

将 `auto_compact = false` 和 `auto_compact_task = false` 可关闭自动压缩。后者为单任务压缩
偏好，默认关闭。保留的指令或近期结果仍无法容纳时，在请求前停止；可显式使用 `/compact 1`
或新建会话。`max_output_chars` 限制单次工具输出，范围 128–1,000,000 字符，不代表上下文长度。
设置 `max_output_tokens` 时，`output_reserve_tokens` 必须至少等于它。

## 会话与历史管理

在 config.toml 顶层设置 `ui_language = "zh"` 或 `ui_language = "en"`，重启 `ta` 后切换
帮助、操作提示、确认框、会话表头、上下文状态和程序控制消息。命令、简短角色标签、JSON
字段名、模型回复、上游错误详情和会话正文保持原文。这是全局界面设置，不随 API 或旧会话切换。

会话保存在 `~/.local/share/termux-agent/sessions.sqlite3`，采用 SQLite、修订号校验及持久化
工具检查点。中断工具的结果标记为未知，不自动重放。原 API 失效或配置变更时仍可进入只读
历史模式，无需网络客户端。`--no-session` 仅使用内存，退出后压缩归档丢失；手动持久化压缩需启用会话存储。

执行段意外退出时会标记为中断并尝试写入检查点，不覆盖原始异常；检查点写入错误仍会停止
执行。加载遗留 `running` 快照时仅在内存自愈，不改写原数据库行。硬终止不能运行清理，
因此由加载路径兜底。继续任务保留真实目标、尚未用尽的预算与未知工具结果，不重新授予
执行权限。预算耗尽后的显式续跑可以按当前配置重新开启预算窗口，但保持任务 ID 不变。
这不是进程保活服务：Android 杀死进程后仍需启动 `ta`、恢复会话并续跑。

重复 `/continue` 不堆叠相同续跑标记。尚未回答的真实用户消息原样保留，不与控制文字合并；
空的 user 尾消息可以替换为续跑控制消息。

| 命令 | 说明 |
|---|---|
| `/sessions [N]`、`/sessions find TEXT` | 带序号的一行列表：时间、ID、标题、API/模型、消息数、路径 |
| `/resume` | 列出近期会话，输入序号或 ID；回车、Ctrl-C、Ctrl-D 取消选择 |
| `/resume N`、`/resume ID` | 按上次列表序号或完整 ID 恢复 |
| `/session load ID`、`/session load N` | 直接恢复会话 |
| `/title NAME`、`/session rename-id ID NAME` | 修改当前或指定会话标题 |
| `/session new`、`/clear`、`/session fork [NAME]` | 新建空白会话或独立副本 |
| `/session save`、`/session delete [ID/N]` | 保存，或按完整 ID / 上次列表序号删除 |
| `/history [N] [skip]`、`/recall TEXT` | 格式化查看历史、翻页或检索 |
| `/export [ID] [markdown/json] [PATH]` | 导出会话，不覆盖已有文件；格式参数二选一 |
| `/stats`、`/status` | 查看用量、预算、停止原因和继续方式 |
| `/help`、`/config`、`/exit` | 简体中文帮助、有效配置、退出 |

序号始终对应最后一次显示的列表，包括筛选结果；自动保存或排序变化不会改变映射。
Tab 可补全命令、API、列表序号/会话 ID、局部指令文件及技能名称。

`/session delete` 在交互终端列出会话并进入选择；`/session delete 3` 直接使用上次列表的
序号。确认前显示目标 ID 和标题，输入 `y` / `yes` 才删除；空输入、Ctrl-C、Ctrl-D 取消
选择或确认。当前会话不能删除，目标若被其他进程修改需重新确认。删除后沿用原列表序号，
直到再次列出会话。脚本需显式使用 `/session delete ID --yes`，也可使用已显示的序号；
不会把后续管道输入当作确认。删除范围仅为该会话及草稿，不涉及工作目录文件。

## 工具、连续任务与终端输入

核心工具为 `shell`、`read_file`、`write_file`、`edit_file`。Shell 限制输出并在超时等情况下
清理子进程组。读取支持从 1 开始的闭区间行号或字节窗口；写入自动创建父目录、原子替换，
并校验磁盘文件哈希。`silent_writes = true` 默认隐藏工具显示中的正文，实际协议内容和审批不变。

审批策略：`always` 每次询问，`on-risk` 对识别出的风险操作询问，`never` 不询问。
风险识别是启发式检查，不是文件系统隔离；非交互模式下需要审批的调用会被拒绝。
识别命令包装、删除选项排列、包安装及 Shell 配置写入；无法解析或解释执行的语法采取保守审批，
引号或注释中的无害文本可能多提示。文件工具与审批统一处理 `~`、全角 `～` 和相对路径；
文件工具路径中的 `$VAR` 按字面保留，不展开 Agent 环境中的变量值。

`on-risk` 保护 Agent 配置/数据、自定义已加载配置文件、SSH 文件和 Git hooks：写入及以这些路径为参数的
命令需要审批，一小组明确的读取命令例外。
Shell、Lightpanda、RTK 及 Termux API 子进程保留普通运行变量，但不继承已配置的 API 密钥变量、
常见 API key/token 变量、长度至少 16 字符的相同密钥值别名及 `BASH_ENV`/`ENV`。具名密钥变量即使值很短也会移除。
父进程环境和 LLM 客户端凭据不变；
这些措施不限制用户已授权程序读取本地文件。

`/run TASK` 或 `/auto on` 开启连续执行；`auto_continue = true` 持久化该模式。
`max_steps` 限制单段调用，`max_continuations`（默认 3）限制无工具的催促轮数。
`/continue [N]` 从检查点继续同一任务。任务尚未完成且允许续跑时，未加引号的简短“继续执行”、
“接着做”、`continue`、`resume`、`keep going` 等自动走同一路径，保留任务 ID、真实目标和
当前历史。带额外指令的“继续执行，但先备份”仍是新任务；明确禁止续跑的结果不会被裸续跑
输入重新启动。

自动任务默认启用 `auto_resume = true` 的外层恢复循环。`auto_resume_max = 8` 限制每次
运行或显式续跑的自动恢复次数（0–50，0 关闭）；`auto_resume_max_wait = 300.0` 限制
单次恢复等待（0–3600 秒）。识别出的接受响应前的暂时性网络/上游故障与限流可退避后续跑，
遵守已保存的服务端冷却时间，包括最后一次或耗尽重试的 5xx 以及共享子任务冷却。
连续四次恢复仍失败且无新的工具进展时停止；重复成功读取旧结果不重置失败计数。界面显示次数、原因、等待及
当前 API 的有效限制，最终停止说明与次数也写入检查点。普通聊天与子任务不启用此循环。

`max_task_calls`（默认 32）和 `max_task_seconds`（默认 600 活动秒）跨执行段限制一个
预算窗口，在请求/工具边界检查。取得新的成功工具证据后，自动续跑可以重开耗尽的调用/时间
窗口，最多 `auto_resume_max` 次。因此启用 auto-resume 时，二者**不再是整个任务的固定
总上限**：默认最多九个窗口。执行时间与自动退避等待都计入时间窗口，用户手动暂停不计。
剩余时间无法容纳服务端冷却时停止，不截短冷却。需要固定调用/时间总上限时关闭
`auto_resume`。可选的 `max_task_tokens` 在自动重开窗口时持续累计；预算耗尽后用户显式
续跑可按当前配置重开调用、时间及 token 预算。未知写入保护和审批限制不会因重开预算而清除。

`auto_extend_segments = true` 允许取得工具进展后在当前预算窗口内延长执行段。
`context_recovery = true` 允许请求接受前的上下文错误在成功精简后重试一次。
完成、用户/上游阻塞、审批拒绝、到达上限却无新进展、token 上限、无法安全恢复的上下文超限、
未完成输出及永久/格式错误都会停止无人值守恢复。未完成输出保留为草稿，不执行其中的工具。
恢复属于尽力而为，不保证进程或任务永不中断。

HTTP 500/502/503/504、识别出的结构化 429 错误及适用的连接故障使用有界自动重试：
`retry_max_attempts` 包含首次 HTTP 尝试，范围 1–6，默认 3；`retry_wait_budget` 限制每个
逻辑请求的累计等待，范围 0–3600 秒，默认 20。`retry_total_timeout` 限制接受响应前包含等待
的总重试时限，范围 0–3600，0 沿用 `timeout`；单次尝试同时受 `timeout` 限制。
父任务剩余时间进一步约束重试，委派的子任务也遵守此边界。

接受响应后的读取空闲超时仍使用 `timeout`，不被更短的重试总时限缩小。
`max_response_bytes` 限制接收的 JSON/SSE 正文字节数，默认 16 MiB，范围 1 KiB–64 MiB，
包含 SSE 包装及终止事件。超限以 `response_limit` 停止；流式片段保留为草稿，其中工具不执行，
请求也不自动重放。

临时限流使用较慢的指数退避和随机抖动，优先遵守 `Retry-After` / `retry-after-ms`，
缺失时参考有效的请求/token 重置提示。所需等待超出预算时停止并延后，不截短后提前重试。429 中的余额、
配额、账单、HTML 或无法识别的错误不重试；暂时性 5xx 仍按状态码处理。依据见
[OpenAI 重试说明](https://developers.openai.com/api/docs/guides/rate-limits#retrying-with-exponential-backoff)。
三个重试选项均支持 API 单独覆盖；分钟级云端限流可配置为 6 次尝试、累计等待 180 秒、
总重试时限 480 秒，原任务硬预算仍然生效。

等待状态、HTTP 尝试计数和已观察到的活动时间会写入检查点。尝试耗尽后 `/status` 显示
`rate_limit`、冷却信息及续跑提示；`/continue` 保持原目标及未耗尽的预算，重开预算窗口也
继续遵守已保存的未来冷却时间。自动续段采用循环，不增长递归栈。只有接受响应前的故障
可触发无人值守重试。接受正文后的错误或断流即使是限流/暂时性失败事件，也停止自动恢复；
可用的部分文字及工具参数保留为惰性草稿，不进入可执行历史。非主线程或已有进程 alarm 等
永久性 deadline 设置拒绝也立即停止，不等待重试。错误详情继续脱敏；结构化诊断记录是否
接受响应、异常类型、可用 HTTP 状态码及距本进程最近压缩的调用数，不把相邻误判成压缩导致故障。
`retry_thinking_400` 是针对特定 thinking 格式错误的独立 API 选项，默认关闭。

在 REPL 中，裸输入 `continue`、`resume` 在存在未完成且可续跑的任务时继续该任务；
否则提示对应斜杠命令。`/resume` 本身仍用于恢复已保存会话。
裸输入 `clear`、`compact`、`status`、`help`、`exit` 始终只给命令提示，不直接执行；
匹配忽略大小写和首尾空白。确需将续跑词作为文字发送时可加引号，如 `"continue"`，
或写成完整指令。显式 `/run`、`--prompt` 及文件内容提交使用通常的 Agent 输入路径，
有未完成任务时同样会检测简短续跑输入。

终端支持时间戳、角色分栏、中文宽度换行、SecureCRT 退格/删除及持久化 readline 历史。
输入 `~/`、`～/` 或绝对文件路径，回车即可提交 UTF-8 文本；Tab 补全路径。
`max_input_file_chars` 默认 64,000 字符，超限或二进制文件会拒绝，不静默截断。
独立三引号行或末尾反斜杠支持多行输入。`!command` 直接执行 Shell，仍遵循审批。
安装 Termux:API 后可使用 `/copy [text]`、`/paste` 与长任务震动。管道输出保持纯文本。

## 提示词、技能与可选功能

内置提示词可直接编辑 `~/.config/termux-agent/system_prompt.txt`，新会话读取最新内容；
已有会话保留提示词快照。自动任务和子任务提示词位于同一目录，包内模板仅在文件缺失时复制。
目录 `agent.md` 或 `--agent PATH` 提供局部指令；从 HOME 启动不会自动加载 `~/agent.md`。
`/agent directory`、`/agent reload`、`/agent PATH`、`/agent off` 选择指令并新建会话。

`/skill list|info|load|unload|clear` 手动管理 Hermes 格式技能，启动时零加载。
搜索路径依次为 `~/.hermes/skills/`、`./skills/`、`~/.config/termux-agent/skills/`；
激活技能随会话保存、恢复。

- **子任务：** `/sub TASK` 或模型的 `delegate` 工具，在当前 API/模型和审批策略下同步执行。
  子任务上下文独立，文件系统共享。`max_subagents = 3`（0 关闭）、`subagent_max_steps = 3`。
  同一响应中的多个委派也逐个串行执行；步数是包含恢复请求在内的总逻辑调用硬上限，子任务不
  自动续段或嵌套委派。工具 schema 显示当前实际配置的上限。优先一次只委派一个小任务，查看
  结果后再规划下一个。
  同一 API 的子任务、父任务和后续子任务共享已知限流冷却，不因切换到另一个委派而提前请求。
  子任务额度在启动前按父任务写入检查点，`/continue` 和恢复会话不重置；新的用户任务或显式
  `/sub` 使用新的任务计数。
- **网页：** 可选 Lightpanda `web_fetch`，默认关闭。安装接线、工具契约和验收见
  [web_fetch 说明](docs/web-fetch.md)。
- **RTK：** 可选 Shell 输出压缩，默认关闭。单独安装原生
  [RTK](https://github.com/rtk-ai/rtk)，设置 `rtk_enabled = true`，可指定 `rtk_binary`。
  已验证 RTK 1.1.0 的 `--no-default-features` 构建。仅过滤受支持的简单 Git、搜索、pytest
  文本输出；原命令只执行一次，退出码和 stderr 保留，失败回退原输出。
  有界完整输出私密保存在 `output-artifacts/`；`/stats` 显示实际应用后的字节/字符节省，不冒充实测 token。
  发送给模型的压缩回执更精简，保存的历史仍保留完整诊断与恢复引用。

RTK 原始归档使用 UTF-8 的 `stdout_text` / `stderr_text`，每次捕获上限 1 MiB；私密归档在捕获时
尽力执行 8 MiB / 32 文件保留限制。手动 `!command` 不过滤，节省统计不包含普通输出截断。

## 开发与更新

```sh
python -m unittest discover -s tests -v
python tests/live_smoke.py # 此项会实际调用已配置 API
pip uninstall termux-agent
```

更新不会覆盖用户提示词文件或凭据。已运行的 `ta` 需退出重启，才能加载更新后的 Python 模块。
配置及会话数据分别位于 `~/.config/termux-agent/` 和 `~/.local/share/termux-agent/`。
