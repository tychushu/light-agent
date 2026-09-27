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
`/debug-context`, `/exit`. `/clear` starts a fresh session, preserving the previous conversation in local history.
Stats include each request’s usage and character counts. Token totals may be partial when `usage_missing_requests` is nonzero. Stats use server-provided token counts; no tokenizer or estimated tokens are used.
Debug context is the last request body (messages include the system prompt), with
the configured API key redacted. It can still contain private file/tool content.

## Tools and approval

- `shell(command, cwd?, timeout?)`: Termux bash, noninteractive stdin, bounded
  stdout/stderr, process-group timeout. Reuses existing git/grep/curl/patch/etc.
- `read_file(path, offset?, limit?)`: bounded UTF-8 read, byte offsets/limits.
- `write_file(path, content)`: atomic write in an existing parent directory.
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
can recover. API requests are not automatically retried.

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
including tool rounds. Requests are non-streaming; SSE parsing is not used.

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
