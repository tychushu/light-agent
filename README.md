# Light Agent

A thin native Termux ↔ OpenAI-compatible API adapter. Python >=3.11, one
runtime dependency (`httpx`), four tools, local sessions, no framework or daemon.

## Install

From a clone or a copy of this directory, in **native Termux**:

```sh
git clone https://github.com/tychushu/light-agent.git
cd light-agent
python -m venv .venv
. .venv/bin/activate
pip install -e .
export TERMUX_AGENT_API_KEY='your API key'
ta
```

`pip install -e .` also works in an existing Python environment.
The command remains `ta` and the Python package is `termux-agent`.
A key is optional for endpoints that do not require authentication. The key is read
only from the environment, never from TOML. No Node, Rust, Java, Go or proot needed.

The installed phone copy is `~/opt/termux-agent`; its `ta` launcher uses an isolated
venv. On this phone, the launcher loads private environment credentials from
`~/.config/termux-agent/credentials.env` (mode 0600). Existing exported values take
precedence. Fresh source installs still require setting environment variables.

## Configuration

Optional `~/.config/termux-agent/config.toml` (flat keys):

```toml
base_url = "http://192.168.86.244:8085/v1"
model = "Qwen3.8-27B-MTPLX-Speed"
max_steps = 16
max_output_chars = 20000
approval_policy = "on-risk"
timeout = 120.0 # HTTP request timeout; shell defaults to 30 seconds
```

`ta --config path.toml` selects another config. `ta --prompt '执行 uname -a'` runs
one turn. REPL commands: `/help`, `/clear`, `/stats`, `/config`, `/debug`,
`/debug-context`, `/skill list|info|load|unload|clear`, `/copy`, `/paste`, `/exit`. `/clear` starts a fresh session, preserving the previous conversation in local history.
Stats include each request’s usage and character counts. Token totals may be partial when `usage_missing_requests` is nonzero. Stats use server-provided token counts; no tokenizer or estimated tokens are used.
Debug context is the last request body (messages include the system prompt), with
the configured API key redacted. It can still contain private file/tool content.

## Tools and approval

- `shell(command, cwd?, timeout?)`: Termux bash, noninteractive stdin, bounded
  stdout/stderr, process-group timeout. Reuses existing git/grep/curl/patch/etc.
- `read_file(path, start_line?, end_line?)`: first 50 UTF-8 text lines by default; explicit line ranges are one-based/inclusive, and `offset?`/`limit?` remain available for byte windows.
- `write_file(path, content)`: creates parent directories and writes atomically.
- `edit_file(path, old_text, new_text, expected_replacements=1)`: exact match count
  required; no diff engine. Use `patch` or `git apply` through shell when needed.

`always` asks for every tool, `on-risk` asks for obvious dangerous operations,
`never` disables approvals. Approval is a heuristic, **not a sandbox**. Shell is
arbitrary code and obfuscation/interpreters can bypass risk matching. Noninteractive
runs deny calls that need approval. Use `always` if every action needs review.
The default short system prompt asks the model to inspect and verify changes;
verification is model behavior, not a mandatory filesystem policy.

No services, Android settings, root configuration or startup scripts are changed.
Completed turns are saved locally unless `--no-session` is used. Failed calls become tool results so the model
can recover. SSE token/reasoning/tool-call deltas stream interactively. HTTP 502/503/504 and connect failures retry at most twice; partial streams are never replayed.

## Test / uninstall

```sh
python -m unittest discover -s tests -v
python tests/live_smoke.py # requires configured API and environment key
pip uninstall termux-agent
```

For the isolated installation on this phone:

```sh
~/opt/termux-agent/.venv/bin/pip uninstall -y termux-agent
rm "$PREFIX/bin/ta"
# Optional: remove the source and its isolated venv after checking the path:
rm -rf "$HOME/opt/termux-agent"
```

Configuration, if you created it, is at `~/.config/termux-agent/config.toml`.

## API profiles and scoped agent.md

Cloud and local endpoints use the same OpenAI-compatible chat-completions + tools
protocol. TLS verification remains enabled for HTTPS. Other proprietary protocols
are not adapted. Add named profiles to `~/.config/termux-agent/config.toml`:

```toml
# Optional top-level default_api = "local" must appear before [apis.*] tables.
[apis.local]
base_url = "http://192.168.86.244:8085/v1"
model = "Qwen3.8-27B-MTPLX-Speed"
api_key_env = "TERMUX_AGENT_API_KEY"

[apis.cloud]
base_url = "https://YOUR-PROVIDER/v1"
model = "YOUR-MODEL-ID"
api_key_env = "CLOUD_API_KEY"
```

Set the corresponding environment variable before starting `ta`. `/api` lists
profiles; `/api cloud` switches; `ta --api local` selects at startup. `default`
uses the original top-level configuration. Each named profile has its own key
variable; omission means no Authorization header, never reuse another profile's
key. Switching saves the previous session, reloads TOML, starts fresh conversation/statistics, and keeps the
currently selected instruction file. Unknown/invalid profiles leave the current
session intact. Nothing is sent until your next prompt.

`ta --directory /path/to/project` sets the working directory and reads only
`/path/to/project/agent.md`. No parent traversal, global config instruction file,
skill index, or automatic `~/agent.md` loading. The filename is lowercase
`agent.md`. Tool shell `cd` does not change the parent REPL's scope.

`ta --agent session.md` explicitly selects a session file (relative to the chosen
working directory), replacing directory instructions. `--no-agent` disables both.
Inside the REPL:

- `/agent`: display loaded file and character count.
- `/agent path/to/session.md`: load a session file.
- `/agent directory`: return to the launch directory's `agent.md`.
- `/agent reload`: explicitly reread the selected file.
- `/agent off`: disable instruction files.

Instruction changes start a fresh conversation and reset usage statistics;
`/clear` saves the current session and starts a fresh one with the selected instructions.
Files are not silently reread each turn. The 16000-character limit is enforced
before sending (oversize files produce an error, never silently truncate).
`/stats` counts the actual combined system prompt; `/debug-context` shows the
injected text. No file means the original 163-character system prompt is unchanged.

## Preconfigured phone endpoints

The phone defaults to `local` (Qwen3.8-27B-MTPLX-Speed at the existing LAN API).
`/api cloud` selects AgentRouter `deepseek-v4-flash` at
`https://agentrouter.org/v1`. Its profile sets
`user_agent = "claude-cli/2.1.119 (external, cli)"` and a 300-second HTTP timeout.
Assistant `reasoning_content` is preserved verbatim for subsequent requests,
including tool rounds. Streaming is enabled by default for OpenAI-compatible profiles; set `stream = false` for non-stream endpoints.

`ta --api local` and `ta --api cloud` also work directly. Private credentials
are intentionally absent from this source package. The phone launcher injects
them via environment variables; the application still never reads keys from TOML.
To fully remove the phone credential copy, additionally remove
`~/.config/termux-agent/credentials.env` after uninstalling the launcher.

## Sessions and conversation history

`ta` starts a new session. Completed turns and normal exits are saved to
`~/.local/share/termux-agent/sessions.sqlite3` (directory 0700, database 0600).
SQLite is part of the Python standard library; no new dependency or service.
`ta --no-session` keeps the run entirely in memory.

| Command | Behavior |
|---|---|
| `/sessions [N]` | List up to N recent sessions (default 50, max 500) |
| `/session` | Show current session ID and saved status |
| `/session new [title]` | Save current session, create a blank one |
| `/session load ID` | Save current and restore the specified session |
| `ta --session ID` | Resume a session after restarting ta |
| `/session rename title` | Rename current session |
| `/session fork [title]` | Copy current conversation/statistics into a separate session |
| `/session save` | Explicitly save current state |
| `/session delete ID` | Confirm and delete an inactive session |
| `/history [N] [skip]` | View last N messages, skipping recent messages to page backward |

Use exact 12-character IDs from `/sessions`. For noninteractive deletion, append
`--yes`. Active-session deletion is refused; first switch away. History defaults
to 20 messages, allows 1–100 at once and bounds each displayed message to 4000
characters. `/history 20 20` shows the preceding page. Stored messages are complete;
this display truncation never changes the saved conversation. Reasoning text is
retained for the API protocol but hidden from the normal history display.

API/model identity, working directory, loaded agent.md text snapshot and source,
messages (including tool IDs/results and reasoning_content), and usage counters
are restored. Current credentials come from environment, never the database.
A changed API profile endpoint/model prevents resuming; restore its old profile
or start fresh. Restoring does not reread agent.md; `/agent reload` deliberately
starts a new session with its current contents. Scope override flags cannot be
combined with `--session`.

`/api`, `/agent` changes and `/clear` preserve old sessions before starting fresh.
`/clear` now resets current-session stats too; old stats remain in saved history.
Two processes cannot silently overwrite the same changed session: a revision
conflict leaves the in-memory conversation intact; `/session fork` saves it under
a new ID. Failed/incomplete tool turns are not committed. Abrupt process termination
can lose the active unfinished turn; only the last completed snapshot is restored,
and old tool calls are never replayed automatically.

The database is local plaintext, containing conversation/file/tool content, with
known API key values redacted. This is history storage, not an automatic memory
system: unrelated sessions are never loaded into prompts. Snapshots are limited to
20 MiB per session. Deleting a session removes its database record, not a guaranteed
secure erase. Uninstall leaves history intact; remove the database separately if
no longer needed.

## SSH terminal editing

Interactive `ta` enables Python's built-in readline support. GNU readline uses
Emacs-style editing with explicit bindings for Backspace (`Ctrl-H` and `DEL`) and
forward Delete (`ESC [ 3 ~`), including SecureCRT sessions. Arrow navigation and
UTF-8 character deletion are handled by readline. No global `stty` settings are
changed and no separate input-history file is written. Restart an already-running
`ta` after updating to load this fix.


## Additional mobile and skill workflows

OpenAI-compatible API profiles stream Server-Sent Events by default. Content and
reasoning deltas display as they arrive; split tool-call names and JSON arguments
are reassembled before entering the agent loop. `data: null`, empty choices and
`[DONE]` frames are ignored. Transient HTTP 502/503/504 or connect failures retry
at 250 ms then 500 ms; no retry replays a partially received response.
Set `stream = false` in TOML for providers without SSE support. Usage is reported
when the stream ends with a usage frame. Configure a larger profile timeout for
long reasoning.

`read_file(path, start_line=1, end_line=50)` reads inclusive, one-based line ranges
within the normal output cap. Long individual lines are clipped safely. Byte
`offset`/`limit` reads remain available and cannot be mixed with line ranges.
`write_file` creates parent directories before its atomic replace.

Hermes skills load only after `/skill list`, `/skill info NAME`, or `/skill load NAME`;
startup does not scan the library. Sources are `~/.hermes/skills/`, `./skills/`,
and `~/.config/termux-agent/skills/`, in that order. Duplicate names use the first
source. Loaded instruction bodies have explicit start/end markers and bounded size.
`/skill unload NAME` and `/skill clear` remove injected text; session snapshots retain
currently active skills. One skill is capped at 48,000 characters and all active
skills together at 128,000. Skill files are user-selected instructions, so inspect
trusted skills before loading them.

`"""` on a line begins a multi-line input terminated by a matching `"""` line.
A final backslash continues onto the next input line. GNU readline history is saved
at `~/.local/share/termux-agent/repl_history`, mode 0600, limited to 2000 entries;
known API keys are redacted. Use `!command` to run shell directly without LLM tokens;
the same approval rules apply. A shell tool taking at least ten seconds triggers a
short `termux-vibrate` notification when Termux:API is installed. `/copy [text]`
copies explicit text or the last assistant answer; `/paste` displays the Android
clipboard contents, bounded by the configured output limit. These clipboard actions
use the installed `termux-clipboard-set/get` commands.

Run the full tests on native Termux with:

```sh
python -m unittest discover -s tests -v
```

## Multiple local and cloud APIs

Keep the existing `[apis.local]` and `[apis.cloud]` entries in `config.toml`.
Additional profiles live in separate files under
`~/.config/termux-agent/profiles/NAME.toml`. The directory is private (0700);
files are 0600 and contain endpoint/model settings plus an **environment variable
name**, never an API Key value. There is no limit of one local or cloud profile.

Inside `ta`, `/api` or `/api list` shows numbered local and cloud profiles,
the active profile, Key availability, and the startup default. Switch with
`/api NAME`, `/api 2`, `/api next`, or `/api prev`; `ta --api NAME` selects at
startup. A switch saves the prior session and starts a clean conversation for
the selected API.

```text
/api add local-fast http://192.168.86.244:8086/v1 MODEL_ID LOCAL_FAST_API_KEY local
/api add cloud-fast https://provider.example/v1 MODEL_ID CLOUD_FAST_API_KEY cloud
/api cloud-fast
/api default cloud-fast
/api show cloud-fast
/api remove cloud-fast
```

The add command accepts `--no-stream`, `--timeout=300`, and a quoted
`--user-agent="client name"` when a provider needs those settings. Local or cloud
can be omitted; private LAN addresses are classified as local. The optional
Key name must be an environment variable such as `CLOUD_FAST_API_KEY`; export
its value before starting `ta` or add it to the phone's private
`~/.config/termux-agent/credentials.env`. Never paste a Key value into `/api add`.
`/api default NAME` changes the startup choice and switches immediately.

The two original inline profiles remain available. `/api remove` applies only
to profiles added under `profiles/`, refuses the active profile, and keeps a
hidden recovery copy. It does not remove credentials or old sessions.
`/api show NAME` displays settings without credential values. You can also edit
an added profile's TOML file to change its model, timeout, or stream setting;
`/api NAME` reloads the file on selection. Existing `config.toml` syntax and
saved sessions remain compatible.

## Terminal completion and conversation columns

In an interactive SSH/Termux terminal, the prompt is the left conversation
column (`You │`). Assistant text, reasoning, tool activity, and informational
messages use their own labels beside a wrapping content column. Chinese text
uses terminal cell widths for wrapping. When the terminal is narrower than
52 columns, messages use a compact stacked label such as `Agent:`. Streaming
continues to print as chunks arrive, and `ta --prompt` without a TTY keeps
plain output for scripts. This remains a line-oriented REPL, not a full-screen UI.

Press Tab to cycle slash commands. Completion also offers `/api` profile names,
`/session load` and `delete` IDs, and local Markdown files for `/agent`.
`/skill load` and `info` offer Hermes Skill names only when that particular
argument is being completed. Startup and ordinary chat input do not scan Skills.
Use `/help` to see all commands. Readline history and Backspace/Delete bindings
continue to work in SecureCRT.

## Tab completion and conversation columns

In an interactive terminal, each turn uses a role column and a wrapping content
column. For example, `You │`, `Agent │`, `Think │`, and `Tool │` identify the
source of each line. The layout follows the terminal width and switches to a
compact `Agent:` style below 52 columns. Streaming text remains live; there is
no full-screen interface or redraw loop. Piped `ta --prompt` output stays plain
text, with reasoning and answer on separate lines.

Press Tab to cycle command matches. After `/api`, it also completes profile
names; `/session load` and `/session delete` complete saved IDs; `/agent`
completes Markdown files in the current directory; and `/skill load` or
`/skill info` completes installed Skill names. Completion only reads the Skill
library when that Skill argument is being completed. Ordinary text and `!shell`
input do not trigger command completion. The startup hint and `/help` list the
available commands.

## Submit local text by path

Enter a path starting with `~`, `～`, or `/`, press Tab to complete directories
and filenames, and press Enter to send that file's text as the current user
message. For example:

```text
~/notes/prompt.md
～/notes/prompt.md
/storage/emulated/0/Documents/task.txt
```

Completion lists names only; files are read after submission. UTF-8 text (including
a UTF-8 BOM) is supported. File contents are sent exactly, without a generated
prompt or silent truncation; the terminal displays the resolved source path,
character count, and active API. The normal session history stores the submitted
text. This also works with `ta --prompt '~/notes/prompt.md'`.

Known slash commands such as `/help` and `/api local` take precedence. Quotes can
force a filename that conflicts with a command: `"/help"`. Filenames with spaces
work directly or inside quotes. Enter one path at a time; ask a follow-up question
in the next message if needed. Directories, devices, missing/non-readable files,
binary data, and non-UTF-8 files produce a readable error before any API request.

The default limit is 64,000 characters. Set the top-level
`max_input_file_chars = 64000` in config.toml to adjust it (1–1,000,000). Oversized
files are refused so partial contents are never mistaken for the complete text.
