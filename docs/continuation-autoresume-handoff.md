# 长任务续跑修复：代码更新交接

日期：2026-11-04。基线：`6fcf895a7842817d34679cdfbed128aec9e049e4`；分支：`codex/optimization-review`。

> 交接：同一冻结候选在本地和 S24U 各通过 511 项测试，手机实际 CLI 控制验收通过，测试后回取的 82 个源文件全部匹配清单。正式代码/配置前后指纹审计仍受权限限制；未部署正式环境，未运行真实提供商长任务。

## 1. 范围与诊断依据

本轮在第三轮优化的既有未提交改动上继续修正长任务续跑；不能把工作区全部差异归因于本轮。
用户授权的是修复、上传 S24U 隔离候选自测和交接文档；未执行 Git 提交/远程推送，也未替换手机正式工程。

生产只读检查确认两个独立问题：

- 普通“继续执行”曾走新任务入口，覆盖 `task_state.goal`，并把刚完成的活动工具链当成旧任务历史整理；
  连续续跑因此形成多条 `· compact task` 分支，真实目标被续跑填充词替代。
- TOML 中部分预期用于 cloud 的调用、时间、续段和恢复选项位于 `[apis.local]` 内。
  注释和空行不结束 TOML 表；实际 cloud 仍使用 32 次调用、600 秒等回退值。

一次约 100K 请求字符后的泛化 Responses 失败不能证明由压缩导致；字符数和估算 token 也不是
提供商实测窗口。归档快照的 `running` 状态、被诊断子进程的 `-9` 退出不能单独证明 Agent 被 Android 杀死。

## 2. 行为变更

### 同一任务续跑

- 对未完成且可续跑的任务，短且未加引号的“继续执行”“接着做”、`continue`、`resume`、`keep going`
  等走 `/continue` 的同任务入口；大小写、首尾空白和通常标点不影响识别。
- 保留任务 ID、真实目标、当前工具链和检查点，不把续跑词当作新的压缩边界。
- 旧历史中的续跑填充消息也不再作为任务锚点；原始用户消息仍原样保留。
- “继续执行，但先备份”等带新约束的输入仍是新任务；`"continue"` 等引号内文字是字面输入。
- 审批拒绝或未完成输出等明确禁止续跑的结果，不会被裸续跑输入重新启动。
- 委派计数和恢复时的拒绝扫描使用相同的真实任务边界，避免旧填充消息隐藏保护状态。

### 有界自动恢复

仅在 `/run` 或 `auto_continue=true` 的自动任务中生效；普通聊天和串行子任务不启用外层自动续跑。

- 有新的成功工具证据时，可在执行段/调用/时间限制后续跑；接受响应前的暂时性故障可退避后恢复。
- 接受正文后的错误、断流及流内限流不自动重放；可用的部分正文和工具参数保留为惰性草稿。
- 每次运行或显式续跑最多自动恢复 8 次，单次等待默认不超过 300 秒；服务端冷却不得截短。
  耗尽内部重试或最后一次 5xx 的 `Retry-After` 也保留，重开预算不清除共享冷却。
- 自动等待计入时间预算并写入检查点，等待无法完整容纳时停止；耗尽 token 后不先等再停。
  故障恰好发生在最后一次逻辑调用时，在清空进展标记前判断是否可以重开调用/时间窗口。
- 连续恢复失败且无新进展时停止；重复旧结果不重置失败计数和递增退避，不无限延长 Python 栈。
- 非主线程或已有 alarm 导致的永久 deadline 设置拒绝，不作为网络故障反复等待重试。
- 完成、用户/提供商阻塞、审批拒绝、无新进展、token 上限、不可恢复上下文超限、未完成输出及永久错误停止。
- 最终自动恢复次数和停止说明写入检查点；恢复最近保存的进展指纹，避免把同样的结果当成新进展。
- 未知/失败写入保护不因预算续开而清除，不自动重放旧工具调用。

### 上下文与诊断

- 两个工具组保留默认值由 2 提升至 6；预算压缩优先尝试配置深度、一半及 1 组。
- 六组是偏好，不是硬下限或按窗口百分比分配；旧配置显式写 2 时仍覆盖默认值。
- 续跑控制提示再次提供真实目标的前 600 字符；完整用户目标仍保留在历史中。
- Responses 空白失败事件显示事件类型；结构化错误诊断记录异常类型、可用 HTTP 状态与距最近压缩的调用数。
- 启动和 `/auto` 显示当前 API 实际生效的调用、时间及自动恢复限制，便于识别配置作用域错误。

## 3. 配置与预算契约

| 配置 | 默认 | 含义 |
|---|---:|---|
| `auto_continue` | false | 持久化自动任务模式；`/run` 可单次开启 |
| `auto_resume` | true | 自动任务中的有界外层恢复 |
| `auto_resume_max` | 8 | 每次运行/显式续跑的自动恢复次数，0–50；0 关闭 |
| `auto_resume_max_wait` | 300.0 | 单次恢复等待秒数，0–3600 |
| `auto_compact_keep_groups` | 6 | 预算压缩的首选保留深度 |
| `tool_history_keep_groups` | 6 | 真实新任务开始时保留的旧完整工具组；0 关闭 |

**启用自动恢复后，调用/时间限制是预算窗口，不再是整个任务固定总上限。**
新的有效进展可支持重开耗尽的调用/时间窗口，最多 `auto_resume_max` 次。默认最多九个窗口，
即最多 9×32 个父模型逻辑调用窗口额度、9×600 个活动秒；执行和自动等待都计入时间窗口，
用户手动暂停不计，HTTP 重试和子任务另有各自限制。
`max_task_tokens` 在自动重开窗口时不重置。用户在预算耗尽后显式续跑可按当前配置重开调用、时间和 token 窗口。
需要固定调用/时间总上限时设置 `auto_resume=false`。

共享选项放在第一个 `[apis.*]` 表头前；cloud 专属选项明确放在 `[apis.cloud]` 下。例如：

```toml
auto_continue = true
auto_resume = true
auto_resume_max = 8
auto_resume_max_wait = 300.0
tool_history_keep_groups = 6

[apis.cloud]
# 保留现有 base_url、model、api_key_env 与 wire_api；不要把密钥写入 TOML。
max_task_calls = 32
max_task_seconds = 600.0
auto_extend_segments = true
auto_compact_keep_groups = 6
context_recovery = true
no_progress_detection = true
```

不要把 local 的 `chat_max_tokens_field` 等兼容性选项盲目搬到 cloud；真实 `context_window_tokens`
应依据该接口支持的模型窗口填写。启动时用 `/config` 核实覆盖值，并显式选择正确 API，
不要仅根据 TOML 的 `default_api` 推断已保存的选中配置。

## 4. 验证记录

| 验证 | 当前结果 |
|---|---|
| 初轮本地集成完整套件（重试加固前） | 496 项通过，33.729 秒；Python 3.11.15，httpx 0.28.1 |
| 续跑与实际 Client 集成回归 | 61 项通过，含 47 项续跑/REPL 回归和 14 项离线真实 Client 集成测试 |
| 英文界面回归 | 修复帮助标签后 19 项通过；原断言未放宽 |
| 首次冻结完整运行 | 510 项中 1 项失败：旧断言认为显式续跑不能重开已用尽的调用预算；该候选不作为最终验收版本 |
| 预算契约回归 | 更新显式续跑契约，并另测 auto_resume=false 不自行越过硬上限；相关 65 项通过 |
| 关闭续跑识别的反向对照 | 6/6 用例按预期失败；包含重复归档会话数量断言，不是正式基线二进制运行 |
| 实际 CLI 入口 | `/config`、`/status`、`/help`、`/exit` 已运行，无提供商请求 |
| 最终冻结候选本地验收 | 511 项通过，31.233 秒；82 个源文件测试前后 SHA-256 一致；实际 CLI 控制入口和 6/6 反向对照通过 |
| S24U 原生完整验收 | 511 项通过，58.916 秒；Python 3.13.13，httpx 0.28.1，退出码 0，无跳过 |
| S24U 实际 CLI | `/config`、`/status`、`/help`、`/exit` 通过；显式 cloud、内存会话、隔离 sandbox，无提供商请求 |
| S24U 候选完整性 | 解包前源包/清单哈希匹配；原生测试与 CLI 后回取源文件，82/82 逐项匹配冻结清单 |
| 真实提供商长任务 | 未运行；自动化提供商请求均模拟 |

手机运行时本轮确认：Python 3.13.13、httpx 0.28.1。前次成功预检的正式工程 HEAD 为
`6fcf895`，并已有 `termux_agent/config.py` 本地改动；本轮不覆盖该文件。
正式代码/配置读取曾被自动权限模式以 `Production Reads` 拒绝；本次源码指纹审计也被
权限分类错误阻断，未绕过限制，因此没有测试前后正式工程状态/文件指纹的一致性证明。
保留 Termux 的 `LD_PRELOAD=/data/data/com.termux/files/usr/lib/libtermux-exec.so`，
避免上一轮漏带 loader 的执行错误。

本次验收使用独立候选、独立 HOME/TMPDIR、现有 Python/httpx 和逐文件 SHA-256 清单；
不使用生产会话数据库，不替换生产配置，不复制或打印真实 API 密钥。
权限服务对部分组合命令超时，因而采用标准解包、标准 unittest/CLI 入口及回取源文件后本地哈希对照。
手机执行前直接逐文件校验未完成；执行前已确认源包和清单哈希，执行后的 82 项逐文件对照全部通过。

## 5. 产物与交接状态

### 最终本地验收候选

持久化私密证据目录：`.local-history/continuation-20261104-mUHXC1/final-candidate/`（Git 忽略）。
其中保留源包、清单、构建元数据、完整测试日志、实际 CLI 日志、反向对照结果和可复现辅助脚本。
上一级目录的旧源包对应首次失败运行，**不能与本节最终候选混用**。

| 项目 | 值 |
|---|---|
| 冻结时间 | 2026-11-04 09:52:17 UTC |
| 文件数 | 82：运行时/资源 31、测试文件 46、根目录文件 5 |
| 源包 | `final-candidate/candidate-source.tar.gz` |
| 清单 | `final-candidate/source-manifest.json` |
| 源包 SHA-256 | `45e169c4fe28550a0e7e57449d8897aeca90e3deed98c4f622e9d24cb713b4ed` |
| 清单 SHA-256 | `8cde5c4aa0fac1dcbfeaf887dc79d5420331026863ff40647e1ccdfb10a5e6ad` |
| 本地完整日志 | `final-candidate/local-final-tests.txt` |
| CLI 日志 | `final-candidate/local-final-cli.txt` |
| 反向对照 | `final-candidate/negative-control.json`、`negative-control.txt` |
| 手机隔离目录 | `/data/data/com.termux/files/home/ta-resume-pJZ6Au` |
| 手机当前状态 | 已上传、解包并通过原生回归与 CLI 控制验收；见 `final-candidate/native-status.json` |
| 原生完整日志 | `final-candidate/native-r4trx0/native-final-tests.txt` |
| 原生 CLI 日志 | `final-candidate/native-r4trx0/native-final-cli.txt` |
| 原生结果/后验哈希 | `final-candidate/native-r4trx0/native-result.json`、`source-after-sha256.json` |

源包只包含代码、测试和五个根目录文件，不含生产配置、会话数据库、真实密钥或私密证据。
本文位于源包之外，更新交接记录不会改变已验收的运行时/测试哈希。
临时构建位置是 `/Users/tycushu/ramdisk/ta-resume-3yDEJK/final-hj7vrR/`；
RAM 磁盘重启后清空，但上述最终源包、日志和辅助脚本已另存到持久目录。

### 原生验收复测操作

本轮已完成上传、空目录解包、标准 unittest 入口和实际 CLI 验收；以下用于复测，不是未完成任务清单。
现有手机 `candidate` 已非空，不要重新解包覆盖；需要全新验收时另建空目录并同步调整路径。
直接在手机执行逐文件校验的命令受权限服务限制，未运行；本轮实际使用解包前源包/清单校验，
再回取原生测试和 CLI 之后的候选，用本地隔离 Python 逐项验证全部 82 个源文件。
正式工程前后指纹审计仍未完成，不通过其他会话、测试钩子或权限设置修改来绕过拒绝。

在本地工程目录上传最终源包和清单：

```bash
scp -P 8022 .local-history/continuation-20261104-mUHXC1/final-candidate/candidate-source.tar.gz .local-history/continuation-20261104-mUHXC1/final-candidate/source-manifest.json u0_a414@192.168.86.192:/data/data/com.termux/files/home/ta-resume-pJZ6Au/
```

在手机 Termux 上先校验两个文件，再向仍为空的 `candidate` 目录解包；目录已有内容时另建隔离位置，不覆盖：

```bash
printf '45e169c4fe28550a0e7e57449d8897aeca90e3deed98c4f622e9d24cb713b4ed  %s\n8cde5c4aa0fac1dcbfeaf887dc79d5420331026863ff40647e1ccdfb10a5e6ad  %s\n' "$HOME/ta-resume-pJZ6Au/candidate-source.tar.gz" "$HOME/ta-resume-pJZ6Au/source-manifest.json" | sha256sum -c -
```

```bash
test -z "$(ls -A "$HOME/ta-resume-pJZ6Au/candidate")" && tar -xzf "$HOME/ta-resume-pJZ6Au/candidate-source.tar.gz" -C "$HOME/ta-resume-pJZ6Au/candidate"
```

使用清单逐文件验证 82 项。下面的只读验证命令应在测试前和测试后各运行一次：

```bash
"$HOME/opt/termux-agent/.venv/bin/python" -I -c 'import hashlib,json,sys; from pathlib import Path; stage=Path(sys.argv[1]); manifest=json.loads((stage/"source-manifest.json").read_text()); bad=[name for name,digest in manifest.items() if hashlib.sha256((stage/"candidate"/name).read_bytes()).hexdigest()!=digest]; assert not bad,bad; print("verified source files:",len(manifest))' "$HOME/ta-resume-pJZ6Au"
```

进入隔离候选目录：

```bash
cd "$HOME/ta-resume-pJZ6Au/candidate"
```

完整套件使用独立 HOME/TMPDIR，去除继承的密钥环境，保留原生 Termux loader。
显式 `PYTHONPATH` 配合 `-P` 避免从当前目录误加载旧包，`-B` 禁止写字节码；
从候选目录运行标准入口。退出码必须为 0，日志末尾必须为 `OK`：

```bash
env -i HOME="$HOME/ta-resume-pJZ6Au/home" TMPDIR="$HOME/ta-resume-pJZ6Au/tmp" PATH="$PATH" PREFIX="$PREFIX" LD_PRELOAD=/data/data/com.termux/files/usr/lib/libtermux-exec.so LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}" PYTHONPATH="$HOME/ta-resume-pJZ6Au/candidate" PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 LANG=C.UTF-8 TERM=xterm-256color "$HOME/opt/termux-agent/.venv/bin/python" -B -P -m unittest discover -s tests -v > "$HOME/ta-resume-pJZ6Au/artifacts/native-final-tests.txt" 2>&1
```

本轮还实际驱动了 `python -m termux_agent.cli` 入口，发送 `/config`、`/status`、`/help`、`/exit`：
同样使用隔离环境、示例配置、显式 `--api cloud --no-session --no-agent`，工具目录为隔离 sandbox，
没有发送模型任务。日志确认内存会话、自动恢复设置和新的裸续跑帮助文字。
普通聊天仍显示自动恢复计数 0；`/config` 中 `auto_resume=true`、上限 8，只在 `/run` 或自动模式中启用。
不要使用 `python -m termux_agent`（不存在 `__main__.py`），也不要调用正式 `ta` wrapper 并假定其加载候选。
原生日志、版本、测试数/耗时及测试后源文件哈希已持久化；正式工程前后指纹审计仍受权限限制。
独立 HOME 不是文件系统沙箱；之后的人工工具任务仍需选择可丢弃目录，并遵守原有审批策略。

### 部署与回滚边界

本轮不会自动部署或回滚正式工程，也没有 Git 提交或远程推送。
正式部署必须另获授权，并使用本地及原生验收均通过的同一完整候选，包含必要未跟踪模块，不能只复制 `agent.py`。
部署前先保存代码和配置，并使用 SQLite 一致性备份保留会话；
不要在数据库运行时只复制主 SQLite 文件而忽略 WAL。部署后核实启动入口加载候选而非旧 editable 安装。
回滚代码与配置应独立于会话恢复，不自动重放已完成或未知工具。

## 6. 明确限制

- 不提供 Android 进程保活；进程被终止后仍需重新启动、恢复并续跑。
- 压缩仍是确定性提取和有界引用，不是完整滚动语义摘要；缺失细节需要显式检索归档。
- 未新增模型可调用的归档检索工具；引用仍有数量/大小上限。
- 未验证真实网关兼容性、服务端计费或模型精确窗口；不把模拟测试等同于线上长任务可靠性证明。
- 自动恢复尽力减少人为续跑，不承诺所有故障都不中断；安全停止优先于无限消耗或重复副作用。

## 7. P0 / P1 / P2 代码审查优化与交接（2026-11-17）

本轮基于 `ba613f1` 实施 P0（配置校验与安全审批加固）、P1（每步性能快赢）与 P2（工具细节增强），并已根据独立交叉审查意见完成全部 4 项 P1 回归修正，保持 SQLite v4 表结构与 4 核心工具契约不变。

### 变更清单

- **P0.1 配置白名单与云端明文 HTTP 密钥校验**（[config.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/config.py)、[api_profiles.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/api_profiles.py)）：
  - 统一 `PROFILE_OVERRIDABLE_KEYS`、`TOP_LEVEL_VALID_KEYS` 与 `PROFILE_VALID_KEYS`；顶层及 `[apis.*]` / `profiles/*.toml` 出现未知键或误写入 `api_key` 时快速失败（`ValueError`），同时保留 `[apis.*]` 内忽略 `ui_language` 的兼容规则；
  - 补齐 `max_output_chars`、`max_input_file_chars`、`approval_policy`、`max_subagents`、`subagent_max_steps`、`auto_continue`、`max_continuations`、`silent_writes`、`web_fetch_enabled`、`lightpanda_binary` 的按 Profile 覆盖能力（`retry_thinking_400` 与 `api_key_env` 仍不从顶层继承）；
  - 当 `api_kind == "cloud"` 且携带非空 `api_key`（或 `/api add ... cloud` 携带 `api_key_env`）时拒绝公网明文 `http://` 地址；通过 `is_lan_or_local_url()` 优先以 `ipaddress.ip_address` 及无 DNS 查询的 `socket.getaddrinfo(..., AI_NUMERICHOST)` 解析 IP 字面量（涵盖 IPv6、十进制与十六进制 IPv4 形式），仅对非全局路由的回环/私有/链路本地/CGNAT（`100.64.0.0/10`）地址以及 `localhost`/显式局域网后缀（`.local`/`.lan`/`.home`/`.home.arpa`/`.internal`/`.localdomain`）放行 `http://`，严格拦截公网 IPv4/IPv6 与公网域名。
- **P0.2 全链路密钥脱敏规则统一**（[security.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/security.py)、[sessions.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/sessions.py)、[cli.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/cli.py)、[agent.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/agent.py)）：
  - 新增 `active_secret_values()` 与 `redact_text()`，统一覆盖已注册 `_KEY_NAMES`、`API_KEY` / `API_TOKEN` / `SECRET`、`*_API_KEY` / `*_API_TOKEN` / `*_SECRET`（环境变量值保持长度 `>= 4` 防短占位符误伤）以及显式传入密钥（`extra` 不设长度下限，确保短显式密钥同样从会话快照、调试输出与终端回显中脱敏），按长度降序替换；
  - `scrub_keys()`、`save_readline_history()`、`cli.redact()` 与 `agent.debug_context()` 统一复用上述脱敏逻辑，无活跃密钥时 `scrub_keys()` 直接返回 `copy.deepcopy(value)`。
- **P0.3 Shell 风险审批加固**（[approval.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/approval.py)）：
  - 支持识别短选项连写受保护路径（如 `-o~/.bashrc`）；将 `unlink` 纳入破坏性文件操作；
  - 对 `git` 命令在完整参数 `args` 中直接匹配破坏性操作（`clean -f/-d`、`reset --hard`、`checkout` 后接 `--` 或含 `-f`/`--force`、`restore` 非 `--staged`/`-S`、`stash drop/clear`、`branch -D`/`--delete -f`、`push --force/+`），避免 `--attr-source status` 或 `git -C -- reset --hard HEAD` 等把参数值伪装成只读子命令或 `--` 分隔符而绕过审批。
- **P1 每步性能快赢**（[token_budget.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/token_budget.py)、[agent.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/agent.py)、[cli.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/cli.py)）：
  - `estimate_text_tokens()` 增加有界缓存（`<= 4096` 字符直接缓存，超长文本以长度+SHA-256 为键）；
  - `TokenCounter._local_count()` 缓存不变工具 schema 的 `tool_tokens`（在 `invalidate()` 时清理），`_compact_for_context()` 对完全相同的压缩候选去重，避免重复调用本地 `/tokenize`；
  - 依据独立复核结论移除 `Client` 侧基于浅层对象标识的请求缓存，`Client.chat()` 始终对当前 `messages`（含嵌套 `content` 片段、工具参数及 `_responses_output` 加密推理项）和 `tools` 重新序列化，杜绝嵌套就地修改导致的陈旧请求体问题；
  - `cli.load_session()` 合并为单次 `snapshot()` 构建并派生双指纹。
- **P2 `read_file` 导航增强**（[tools.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/tools.py)）：
  - `_read_file()`（字节窗口）与 `_read_lines()`（行窗口）返回值新增 `"eof": bool`。
- **回归测试**（[tests/test_review_optimizations.py](file:///Volumes/MX5002T/data/light-agent/tests/test_review_optimizations.py)）：新增 16 项单元测试覆盖上述 P0/P1/P2 行为与 4 类复核回归边界。

### 验证记录与环境边界

- 在隔离虚拟环境（含真实 `httpx 0.28.1`，隔离 `HOME`、`TMPDIR`，纯离线 `env -i` 环境）下验证：
  - 分隔符绕过探测 `separator_probe.py`：**通过**（`git -C -- reset --hard HEAD` 实际执行成功且触发 `on-risk` 审批）；
  - 扩展边界探测 `extended_probes.py`：**9 / 9 全部通过**（涵盖私有 IPv4/IPv6 放行、公网 IPv4 / 公网 IPv6 / 十进制与十六进制 IPv4 字面量拦截、`git --attr-source status reset --hard HEAD` 与 `git --attr-source status push --force origin HEAD` 审批拦截）；
  - 独立复核边界探测 `review_probes.py`：**12 / 12 全部通过**（涵盖 `chat_completions` / `responses` 嵌套修改新鲜序列化、短显式密钥会话与调试脱敏、LAN/Magpie 网关放行、`git --config-env ... reset --hard` / `push --force` 审批）；
  - 完整单元测试套件 `python -m unittest discover -s tests`：**597 项全部通过（`OK`，耗时 38.825 秒，0 失败、0 跳过）**。
- 所有网络请求均为本地 `httpx.MockTransport` 或模拟客户端，未运行 `live_smoke.py` 或连接真实 Termux 设备；正式部署前请在目标设备环境同步核验。

## 8. P3 架构演进与交接（2026-11-17）

在完成 P0–P2 及全部复核修复的基础上，本阶段完成 P3 架构演进的两项结构重构，进一步消除长会话存储写放大并提升 CLI/Agent 可维护性。

### 变更清单

- **P3.1 `SessionStore` 追加式消息表（v5 Schema，自动兼容 v1–v4）**（[sessions.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/sessions.py)、[cli.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/cli.py)）：
  - `snapshot()` 输出 `version: 5`，`restore()` 兼容读取 `version in (1, 2, 3, 4, 5)`；
  - 移除一切 SQL 字符串正则改写、`sessions_v5_view` 与 `_SessionConnection`，`self.db` 恢复为原生 `sqlite3.connect(path, timeout=5)`，100% 原生兼容 `SELECT 'FROM sessions'`、`sessions.payload`、`main.sessions`、`"sessions"` 与 CTE `WITH q AS (...)` 等所有合法 SQL 查询；
  - 保留 `sessions(id, title, updated, revision, payload)` 为真实表并写入完整序列化 JSON，同时维护 `session_messages(session_id, seq, message)` 追加式消息表：
    - 写入侧通过表级 `AFTER INSERT`、`单一 AFTER UPDATE OF id, payload`（`sessions_sync_update_v2`，避免两个 `AFTER UPDATE` 触发器在同时改写 `id` 与缩短 `payload` 时因 LIFO 执行顺序残留旧尾部消息）及 `AFTER DELETE` 触发器（`_SESSIONS_SYNC_TRIGGERS_SQL`）增量比对同步 `session_messages`，仅删除缩容尾部并仅对新增或变更的尾部消息执行 `INSERT OR REPLACE`，未变更前缀行及其 `rowid` 保持不变；
    - 为确保已打开的旧版 v1–v4 `SessionStore` 与直接执行 `SELECT payload FROM sessions` 的原生 SQLite 读取器同样读到完整历史，兼容契约决定在 `sessions.payload` 列继续保留完整 `messages` 数组（不置空为 `[]`）；
  - 打开遗留 v1–v4 数据库时，`_init_schema()` / `_backfill_legacy_messages()` 在不修改或不删除 `sessions` 表的前提下将尚未拆分的历史消息回填到 `session_messages`；
  - 新增 `_read_snapshot()` WAL 事务上下文管理器包裹 `load()`、`listing()`、`recall()`、`export()` 及 `cli.py:ReplController.load_session()` 的跨表读取，确保元数据与消息来自同一已提交 WAL 读快照，不被并发写入切裂。
- **P3.2 `Agent` 计数器 Dataclass 与 `cli.py:main()` 控制器重构**（[agent.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/agent.py)、[cli.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/cli.py)）：
  - 在 [agent.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/agent.py) 中新增 `@dataclass class AgentCounters`（统一管理全部 30 项运行及 RTK 统计计数器，提供 `to_dict()` / `load_dict()`），并通过 `_CounterProperty` 数据描述符保持 `agent._total_calls` 等属性与 `agent.counters` 单一数据源双向同步；
  - 在 [cli.py](file:///Volumes/MX5002T/data/light-agent/termux_agent/cli.py) 中将原 `main()` 内嵌闭包状态与命令分发重构为 `ReplController` 类（拆分 `_cmd_compact`、`_cmd_api`、`_cmd_agent`、`_cmd_skill`、`_cmd_session`、`_cmd_copy`、`_cmd_paste`、`_cmd_shell` 与 `_dispatch_prompt` / `run` / `close`），保持对模块级全局符号（`Agent`、`Client`、`SessionStore`、`snapshot`、`restore`、`_readline`、`sys`）的动态引用，兼容全部测试 Monkeypatch。

### 验证记录

- 新增 6 项 P3 专项测试（[tests/test_review_optimizations.py](file:///Volumes/MX5002T/data/light-agent/tests/test_review_optimizations.py)）：验证 `session_messages` 前缀 `rowid` 保留、同时修改 `id` 与缩短 `payload` 无旧尾部残留、WAL 并发写交错下 `load()` 单快照一致性、打开 v4 数据库回填且已打开的旧版 v4 连接可继续检查点与读取完整历史、原生 `sqlite3.connect` 与 6 类 SQL 语法/字面量兼容、`AgentCounters` 双向同步及 `ReplController` 命令分发；
- P3 触发器更新探测 `trigger_update_probe.py`（针对当前工作区）：`payload_only: true`、`id_only: true`、`id_and_payload: true`，**3 / 3 全部通过**；
- P3 SQL 兼容性复核探测 `sql_compatibility_probes.py`（针对当前工作区）：**10 / 10 全部通过**；
- P3 持久化一致性探测 `persistence_probes.py`（针对当前工作区）：`load_returns_one_committed_snapshot: true`、`already_open_v4_writer_can_keep_checkpointing: true`，**2 / 2 全部通过**；
- 在非 TTY 与独立真实路径 `TMPDIR` + 真实 `httpx 0.28.1` 环境下运行完整测试套件：`python -m unittest discover -s tests` **603 项全部通过（`OK`，耗时 28.745 秒，0 失败、0 跳过）**；
- `tests.test_review_optimizations` **22 项全部通过**，`separator_probe.py` **通过**。
- 注：macOS 默认系统 `TMPDIR=/var/folders/...` 含 `/var -> /private/var` 符号链接，运行涉及工作目录严格比对与 RTK 归档防符号链接校验的测试时需在无符号链接的真实临时目录（如 `/private/tmp` 或项目下独立临时目录）执行；网络请求均基于本地模拟传输层，未在 Termux 实机或真实外部提供商上运行。
