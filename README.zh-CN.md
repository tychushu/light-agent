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
approval_policy = "on-risk"
max_steps = 16
max_output_chars = 20000
max_context_chars = 500000
timeout = 120.0
stream = true
silent_writes = true

auto_compact = true
auto_compact_threshold = 0.75
auto_compact_keep_turns = 2
auto_compact_keep_groups = 2

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

`base_url` 填 API 根地址，例如以 `/v1` 结尾；客户端自动添加 `/responses` 或
`/chat/completions`。Responses 支持 JSON、SSE、工具调用、思考内容保留及无状态多轮历史回传。
可自由添加多个本地、云端 API 与不同模型；名称没有固定限制。

- `/api`：列出配置；`/api NAME`、`/api N`、`/api next`：切换并新建会话。
- `/api default NAME`：设置以后启动时默认使用的 API。
- `/api add NAME URL MODEL [KEY_ENV] [local|cloud]`：创建独立配置文件。
- `/api show NAME`、`/config`：查看设置，不显示密钥值。
- `/api remove NAME`：仅删除通过命令添加的非当前配置，并保留恢复副本。

独立配置存放在 `~/.config/termux-agent/profiles/`。命名配置未指定 `api_key_env`
时不发送密钥，不借用其他配置的凭据。`ta --config PATH` 可指定其他配置文件。

## 上下文自动压缩与状态栏

每次调用模型前检查上下文，默认达到预算 **75%** 时提前压缩，尽量降至触发阈值的四分之三，
为后续执行预留空间。字符预算和已知 token 窗口都会参与检查；token 预算先扣除输出预留。
跨轮次历史也能压缩，最后一条用户消息没有工具调用时仍然有效。手工配置的阈值优先。

压缩会创建独立的活动会话，完整原始历史保留在旧会话中。保留系统提示、全部用户指令、
近期回答、最近完整的助手/工具调用组以及未知副作用。较早工具输出变为有界的产物引用；
必要时将较早回答提取为明确标注的片段。保留组中的思考与协议数据不改写、不拆散。
这是本地确定性精简，不额外调用模型总结；片段不等于完整语义摘要，缺失事实需显式找回。

| 命令 | 用途 |
|---|---|
| `/compact` | 手动执行跨轮次精简，保留用户指令 |
| `/compact N` | 明确只保留最近 N 个用户轮次，范围 1–100 |
| `/compact task [N]` | 精简当前任务的早期工具链，保留最近 N 个完整工具组 |
| `/recall TEXT`、`/history`、`/session load ID`、`/export` | 检索、查看或导出完整历史 |

交互终端 Status 栏显示会话 ID、模型、工作路径、标题和当前上下文，例如：

```text
Context=正常 | Chars=120,000/500,000 (24%) | Tokens≈40,000/未知窗口 | Auto=75%
```

- `Chars`：序列化消息、工具定义与自动任务提示的字符总量及本地上限，**不是模型 token 窗口**。
- `Tokens≈`：估算输入 token；未配置窗口时明确显示“未知窗口”，不虚构 token 百分比。
- 已知真实窗口时，在对应 API 设置 `context_window_tokens`，状态栏同时显示 token 百分比与输出预留。
- Status 刷新不访问网络。`token_count_mode = "local"` 可在请求前按需读取 llama.cpp 元数据、
  调用其 tokenizer；云端使用估算。`/stats` 区分服务端用量和请求前估算。
- `token_count_mode = "off"`：关闭 token 计数；字符保护仍生效。

将 `auto_compact = false` 和 `auto_compact_task = false` 可关闭自动压缩。后者为单任务压缩
偏好，默认关闭。保留的指令或近期结果仍无法容纳时，在请求前停止；可显式使用 `/compact 1`
或新建会话。`max_output_chars` 限制单次工具输出，范围 128–1,000,000 字符，不代表上下文长度。
设置 `max_output_tokens` 时，`output_reserve_tokens` 必须至少等于它。

## 会话与历史管理

会话保存在 `~/.local/share/termux-agent/sessions.sqlite3`，采用 SQLite、修订号校验及持久化
工具检查点。中断工具的结果标记为未知，不自动重放。原 API 失效或配置变更时仍可进入只读
历史模式，无需网络客户端。`--no-session` 仅使用内存，退出后压缩归档丢失；手动持久化压缩需启用会话存储。

| 命令 | 说明 |
|---|---|
| `/sessions [N]`、`/sessions find TEXT` | 带序号的一行列表：时间、ID、标题、API/模型、消息数、路径 |
| `/resume` | 列出近期会话，输入序号或 ID；回车、Ctrl-C、Ctrl-D 取消选择 |
| `/resume N`、`/resume ID` | 按上次列表序号或完整 ID 恢复 |
| `/session load ID`、`/session load N` | 直接恢复会话 |
| `/title NAME`、`/session rename-id ID NAME` | 修改当前或指定会话标题 |
| `/session new`、`/clear`、`/session fork [NAME]` | 新建空白会话或独立副本 |
| `/session save`、`/session delete ID` | 保存或明确删除会话 |
| `/history [N] [skip]`、`/recall TEXT` | 格式化查看历史、翻页或检索 |
| `/export [ID] [markdown/json] [PATH]` | 导出会话，不覆盖已有文件；格式参数二选一 |
| `/stats`、`/status` | 查看用量、预算、停止原因和继续方式 |
| `/help`、`/config`、`/exit` | 简体中文帮助、有效配置、退出 |

序号始终对应最后一次显示的列表，包括筛选结果；自动保存或排序变化不会改变映射。
Tab 可补全命令、API、列表序号/会话 ID、局部指令文件及技能名称。

## 工具、连续任务与终端输入

核心工具为 `shell`、`read_file`、`write_file`、`edit_file`。Shell 限制输出并在超时等情况下
清理子进程组。读取支持从 1 开始的闭区间行号或字节窗口；写入自动创建父目录、原子替换，
并校验磁盘文件哈希。`silent_writes = true` 默认隐藏工具显示中的正文，实际协议内容和审批不变。

审批策略：`always` 每次询问，`on-risk` 对识别出的风险操作询问，`never` 不询问。
风险识别是启发式检查，不是文件系统隔离；非交互模式下需要审批的调用会被拒绝。

`/run TASK` 或 `/auto on` 开启有界连续执行；持久化配置为 `auto_continue = true`、
`max_steps` 和 `max_continuations`（默认 3）。`/continue [N]` 继续同一任务的新执行段。
`max_task_calls`（默认 32）、`max_task_seconds`（默认 600）和可选 `max_task_tokens`
跨执行段限制任务；时间预算在请求/工具边界检查。`auto_extend_segments = true` 允许在取得
进展且未耗尽预算时自动延长。`context_recovery = true` 允许请求接受前的上下文错误在成功
精简后重试一次。未完成输出保留为草稿，不执行其中未完成的工具调用。

HTTP 500/502/503/504、识别出的结构化 429 错误及适用的连接故障使用有界自动重试：
`retry_max_attempts = 3`、`retry_wait_budget = 20.0`，总过程受请求超时约束。
已接受响应和部分流不会重放；错误透传会脱敏凭据。
`retry_thinking_400` 是针对特定 thinking 格式错误的独立 API 选项，默认关闭。

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
  子任务上下文独立，文件系统共享。`max_subagents = 3`（0 关闭）、`subagent_max_steps = 6`；不嵌套委派。
- **网页：** 可选 Lightpanda `web_fetch`，默认关闭。安装接线、工具契约和验收见
  [web_fetch 说明](docs/web-fetch.md)。
- **RTK：** 可选 Shell 输出压缩，默认关闭。单独安装原生
  [RTK](https://github.com/rtk-ai/rtk)，设置 `rtk_enabled = true`，可指定 `rtk_binary`。
  已验证 RTK 1.1.0 的 `--no-default-features` 构建。仅过滤受支持的简单 Git、搜索、pytest
  文本输出；原命令只执行一次，退出码和 stderr 保留，失败回退原输出。
  有界完整输出私密保存在 `output-artifacts/`；`/stats` 显示实际应用后的字节/字符节省，不冒充实测 token。

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
