# Light Agent

[简体中文](README.zh-CN.md) · English

A lightweight terminal agent for **native Termux**. Python 3.11+, standard library
and `httpx`; no agent framework, daemon, root, or proot required by the agent.
Local and cloud OpenAI-compatible APIs share the same terminal workflow.

## Install and run

```sh
git clone https://github.com/tychushu/light-agent.git
cd light-agent
python -m venv .venv
. .venv/bin/activate
pip install -e .
export CLOUD_API_KEY='your-key'
ta
```

The command is `ta`; the Python distribution is `termux-agent`. Keys come only
from environment variables, never TOML. An unauthenticated endpoint needs no key.
Existing deployments can keep their source location and launcher.

```sh
ta --api local
ta --directory ~/project
ta --session abcdef123456
ta --prompt 'Inspect the current directory'
```

## Configure APIs

Create `~/.config/termux-agent/config.toml`. Global settings go **before** any
`[apis.*]` table; profile settings override supported global options.

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
model = "your-local-model"
token_count_mode = "local"
output_reserve_tokens = 4096
max_output_tokens = 4096
chat_max_tokens_field = "max_tokens"

[apis.cloud]
kind = "cloud"
base_url = "https://your-provider.example/v1"
model = "your-cloud-model"
api_key_env = "CLOUD_API_KEY"
wire_api = "responses" # or "chat_completions" (default)
token_count_mode = "estimate"
# context_window_tokens = 32768 # set the actual window supported by your endpoint
```

`base_url` is the API root, such as `/v1`; the client appends `/responses` or
`/chat/completions`. Responses supports JSON/SSE, tool calls, reasoning history,
and stateless multi-turn replay. Profile names and models are user-defined.
`/api add` also supports profiles stored separately under
`~/.config/termux-agent/profiles/`. Missing `api_key_env` on a named profile sends
no key; credentials are never borrowed from another profile.

Switch with `/api NAME`, `/api N`, or `/api next`; switching saves the conversation
and starts a fresh session. `/api default NAME` sets the startup choice.
`/api show NAME` and `/config` omit credential values. `ta --config PATH` selects
another configuration file. `/api remove NAME` removes only added profiles,
refuses the active profile, and retains a recovery copy.

## Context management and status

Before every model request, automatic compaction checks both the character guard
and, when known, the token window minus reserved output. It starts at **75%** by
default and tries to reduce usage to three quarters of that trigger, leaving
headroom for subsequent steps. It works across conversation turns, even when the
latest user message has no tools. Explicitly configured thresholds are respected.

Compaction creates an independent active session and retains the full original
history. It keeps the system prompt, every human instruction, recent answers,
recent complete assistant/tool groups, and unknown side effects. Older tool
results become bounded artifact references; older answers may become labeled
excerpts if needed. Retained reasoning/protocol items are unchanged. This is a
local deterministic reduction, without an extra summarization API call; excerpts
are not a complete semantic summary. Missing facts must be recovered explicitly.

- `/compact`: run the same cross-turn reduction manually.
- `/compact N`: explicitly retain only the latest N human turns (1–100).
- `/compact task [N]`: reduce the current task's older complete tool groups.
- `/recall TEXT`, `/history`, `/session load ID`, and `/export` access saved history.

In interactive terminals the Status bar shows session ID, model, working path,
title, and current context, for example:

```text
Context=正常 | Chars=120,000/500,000 (24%) | Tokens≈40,000/未知窗口 | Auto=75%
```

`Chars` counts serialized messages plus tool schemas and automatic-task rules.
It is a local budget, **not** a model token window. `Tokens≈` is an estimate;
unknown windows are shown explicitly. Set `context_window_tokens` per profile
when the actual endpoint limit is known. With a known window, status includes its
token percentage and output reserve. Status redraws never make network requests.
`token_count_mode = "local"` can lazily use llama.cpp metadata/tokenization before
requests; cloud profiles use estimates. `/stats` separates provider usage from
preflight estimates. `token_count_mode = "off"` disables token counting.

Set `auto_compact = false` and `auto_compact_task = false` to disable automatic
reduction. The latter is an optional single-task reduction preference, off by
default. If preserved instructions or recent results still cannot fit, the agent
stops before sending; `/compact 1` or a new session is the explicit fallback.
`max_output_chars` limits one tool result (128–1,000,000 characters), not context
length. `output_reserve_tokens` must cover `max_output_tokens` when set.

## Sessions and commands

Sessions are stored privately in SQLite under
`~/.local/share/termux-agent/sessions.sqlite3`, with revision checks and durable
tool checkpoints. Interrupted tools have unknown outcomes; they are not replayed
automatically. Changed or missing API profiles open saved sessions in history-only
mode, without a network client. `--no-session` uses memory only; its compaction
archives disappear when the process exits. Persistent manual compaction requires
session storage.

| Command | Purpose |
|---|---|
| `/sessions [N]`, `/sessions find TEXT` | Numbered list: time, ID, title, API/model, message count, path |
| `/resume`, `/resume N`, `/resume ID` | Interactive picker or direct restore; N refers to the last displayed list |
| `/session load ID` or `/session load N` | Restore using the same displayed numbers or a full ID |
| `/title NAME`, `/session rename-id ID NAME` | Rename current or saved session |
| `/session new`, `/clear`, `/session fork [NAME]` | New conversation or independent copy |
| `/session save`, `/session delete ID` | Save or explicitly delete a session |
| `/history [N] [skip]`, `/recall TEXT` | Formatted history or saved-content search |
| `/export [ID] [FORMAT] [PATH]` (markdown or json) | Export without overwriting an existing file |
| `/stats`, `/status` | Usage, budgets, stop reason and continuation hint |
| `/help`, `/config`, `/exit` | Simplified Chinese help, effective settings, exit |

Numbers are tied to the last displayed list, including filtered lists; autosave
or a reordered database does not change their meaning. Tab completes commands,
profile names, displayed session numbers/IDs, scoped instruction files and Skills.

## Tools, task execution and terminal input

The four core tools are `shell`, `read_file`, `write_file`, and `edit_file`.
Shell captures bounded output with process-group cleanup. File reads support
one-based inclusive line ranges or byte windows. Writes create parent directories,
replace atomically, and verify on-disk hashes. `silent_writes = true` hides write
bodies in tool displays while retaining the real protocol data and approvals.

Approval policies: `always` asks for each tool, `on-risk` asks for recognized
risky operations, and `never` disables prompts. Approval matching is a heuristic,
not filesystem isolation. Noninteractive calls requiring approval are denied.

`/run TASK` or `/auto on` enables bounded continuous execution; persistent settings
are `auto_continue = true`, `max_steps`, and `max_continuations` (default 3).
`/continue [N]` resumes a task segment. `max_task_calls` (32), `max_task_seconds`
(600), and optional `max_task_tokens` bound the task across segments; time limits
are checked at request/tool boundaries. `auto_extend_segments = true` permits
progress-based segment extension within those limits. `context_recovery = true`
allows one retry after a pre-accept context error and successful reduction.
Incomplete output remains a draft and never executes incomplete tools.

Transient HTTP 500/502/503/504, classified structured 429 responses and eligible
connection failures share bounded retries: `retry_max_attempts = 3`,
`retry_wait_budget = 20.0`, within the request timeout. Accepted responses and
partial streams are not replayed. Errors are surfaced with credential redaction.
`retry_thinking_400` is a separate per-profile opt-in for a known thinking-format
error, disabled by default.

Interactive output uses timestamps, role columns and terminal-width wrapping,
including CJK text. SecureCRT Backspace/Delete and persistent readline history
are supported. Enter a `~/`, `～/`, or absolute text-file path to submit its UTF-8
contents; Tab completes paths. The default file-input cap is 64,000 characters
(`max_input_file_chars`); oversized or binary files are refused without truncation.
Triple-quote lines or a trailing backslash provide multiline input. `!command`
runs the shell directly with the same approval policy. `/copy [text]`, `/paste`
and long-task vibration use Termux:API when installed. Piped output stays plain.

## Prompts, Skills and optional integrations

Edit `~/.config/termux-agent/system_prompt.txt` directly; new sessions reload it.
Existing sessions retain their prompt snapshot. Automatic-task and worker prompts
are sibling text files; package templates are copied only when absent.
Directory `agent.md` or `--agent PATH` provides scoped instructions. Starting in
HOME does not implicitly load `~/agent.md`. `/agent directory`, `/agent reload`,
`/agent PATH`, and `/agent off` select instructions for a fresh session.

`/skill list|info|load|unload|clear` controls Hermes-format Skills manually.
Startup loads no Skills. Search paths are `~/.hermes/skills/`, `./skills/`, and
`~/.config/termux-agent/skills/`; active Skills persist with the session.

- **Workers:** `/sub TASK` or the `delegate` tool runs bounded synchronous workers
  using the current API/model and approval policy. Workers have separate context
  but share the filesystem. `max_subagents = 3` (0 disables) and
  `subagent_max_steps = 6`; no nested delegation.
- **Web:** optional Lightpanda `web_fetch`, disabled by default. See
  [setup, contract and acceptance](docs/web-fetch.md).
- **RTK:** optional shell-output reduction, disabled by default. Install native
  [RTK](https://github.com/rtk-ai/rtk) separately, then set `rtk_enabled = true` and
  optionally `rtk_binary`. RTK 1.1.0 with `--no-default-features` is verified.
  Only supported simple Git/search/pytest text output is filtered after the original
  command runs once. Exit status/stderr remain intact; failures use raw output.
  Full bounded output is kept privately under `output-artifacts/` for recovery.
  `/stats` reports applied UTF-8 byte/character savings, not measured token savings.

RTK raw artifacts expose UTF-8 `stdout_text` / `stderr_text`, bounded to 1 MiB per
capture. The private archive has an 8 MiB / 32-file best-effort retention limit,
enforced at capture time. Manual `!command` output is not filtered. Applied savings
exclude ordinary output clipping and are not actual tokenizer or provider counts.

## Development

```sh
python -m unittest discover -s tests -v
python tests/live_smoke.py # explicitly calls the configured API
pip uninstall termux-agent
```

Updates do not overwrite user prompt files or credentials. Restart a running
`ta` process to load updated Python modules. Configuration and session data remain
under `~/.config/termux-agent/` and `~/.local/share/termux-agent/`.
