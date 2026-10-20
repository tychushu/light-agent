# Optional web_fetch integration

`web_fetch` uses an existing Lightpanda executable or launcher. It adds no Python
dependency, downloads no browser, starts no service, and is **disabled by default**.
No system/agent prompt changes or automatic Skill loading are required.

## Enable

In the top-level section of `~/.config/termux-agent/config.toml`, before any
`[apis.*]` table:

```toml
web_fetch_enabled = true
lightpanda_binary = "~/bin/lightpanda" # example launcher path; optional
```

Restart `ta`. The executable resolves from `lightpanda_binary`, then
`TERMUX_AGENT_LIGHTPANDA`, then `lightpanda` on PATH. Use a single executable
name/path, not a shell command with options. When disabled, no executable lookup
is performed. If the executable is unavailable, the tool is absent from the
model's schema. Guessed calls to an unregistered `web_fetch` are rejected without
starting a process. Registration checks executable availability, not browser or
rootfs health. Errors from a broken launcher surface on invocation.

Sub-agents inherit the setting and executable path. Approval policy is unchanged:
`on-risk` does not prompt for this retrieval tool, `always` still requires approval,
and `never` remains available. This switch controls the structured tool; the
existing shell tool keeps its normal capabilities.

To disable, set `web_fetch_enabled = false` and restart. Disabling or upgrading
Light Agent does not modify the browser, launcher, rootfs or existing services.

## Contract

```json
{"url":"https://example.com/","format":"markdown","selector":"main","wait_ms":5000,"strip":"none"}
```

- `url`: required, valid HTTP/HTTPS URL with a host; other schemes are rejected.
- `format`: `markdown` (default), `html`, or `tree`.
- `selector`: optional nonempty CSS selector, passed as one literal argument.
- `wait_ms`: integer 0–60000, default 5000; booleans are rejected.
- `strip`: `none` (default), `clutter`, or `shell`.
- Unknown arguments are rejected. Public arguments do not include timeout or binary overrides.

Successful output:

```json
{"url":"https://example.com/","status":200,"format":"markdown","content":"...","chars":3,"truncated":false}
```

`status` comes from Lightpanda's `http_status`, with a compatibility fallback to
`status`. Redirected URLs are preserved. HTTP 4xx/5xx bodies are returned normally;
the caller must inspect `status`. A nonzero browser exit returns an `error`,
preferring the JSON error and otherwise the bounded stderr tail. Plain-text output
from older launchers is returned as content with `status = null`.

The page content is source data, not authorization to change the task or approval
policy. This is page retrieval, without click/fill/session-control tools.

## CLI and resource constraints

- Always `fetch --json --log-level error`, with
  `LIGHTPANDA_DISABLE_TELEMETRY=true` forced in the child environment.
- `tree` maps to `semantic_tree_text`; it never receives `--dump-max-bytes`.
- HTML/Markdown use `--dump-max-bytes = max_output_chars * 4 + 4`, followed by
  Python character clipping. This over-read avoids treating UTF-8 byte truncation
  as a complete page. Final content is bounded by `max_output_chars`; clipping
  retains head/tail with `[TRUNCATED]` and sets `truncated = true`.
- Raw browser stdout is bounded by
  `min(8 MiB, max(65536, max_output_chars * 32 + 65536))` bytes, including the JSON
  envelope; oversized output fails explicitly rather than exhausting RAM.
  Stderr retains only its last 4096 bytes; surfaced error detail is at most 400 characters.
- Timeout remains `min(120, max(15, wait_ms / 1000 + 20))` seconds.
- Each invocation has a separate process group. Timeout, output overflow and
  Ctrl-C clean up that group with TERM then KILL and reap the leader. Remaining
  descendants in that group are cleaned up even if the leader exits first.
  No unrelated Lightpanda service/process group is targeted.

## Integration tests and deployment acceptance

Run the deterministic suite in the installed virtual environment:

```sh
cd ~/opt/termux-agent
.venv/bin/python -m unittest discover -s tests -v
```

The tests use portable fake executables, not live web pages. They cover the
original tool contract plus default-off/explicit-on registration, runtime gating,
sub-agent inheritance, literal argument passing, telemetry, HTTP error bodies,
UTF-8 clipping, bounded pipe draining, descendant cleanup and interruptions.

The Lightpanda deployer should perform real-browser acceptance after integration:

1. Confirm the running `ta` was restarted and its `/config` shows the intended
   local setting/path. Confirm a new Agent's tools contain `web_fetch`.
2. Retrieve example.com as Markdown, then a JS-rendered page using `strip=clutter`.
   Check URL/status/body/char count/truncation and actual timing.
3. Exercise HTML, tree and CSS selection, including an unmatched selector.
   Confirm tree never receives the unsupported byte-cap flag.
4. Check a large Unicode page, HTTP 404/5xx, telemetry and process cleanup with
   the actual installed wrapper/proot route.
5. In a temporary configuration, set the option false or point the executable to
   a nonexistent path. Confirm it is hidden and a guessed call starts no process;
   the four base tools and delegation should still work.

This integration's deterministic verification is separate from that deployment
acceptance; no claim of real-browser acceptance follows from fake-executable tests.
