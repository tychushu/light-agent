"""Width-aware two-column transcript and lazy slash-command completion."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import unicodedata
from .i18n import text as ui_text


COMMANDS = (
    "/help", "/clear", "/compact", "/continue", "/status", "/recall", "/export", "/stats", "/config", "/debug", "/debug-context",
    "/api", "/agent", "/skill", "/copy", "/paste", "/sessions", "/session", "/resume", "/title",
    "/history", "/silent", "/auto", "/run", "/sub", "/exit",
)
SUBCOMMANDS = {
    "/api": ("list", "add", "switch", "show", "default", "remove", "next", "prev"),
    "/agent": ("directory", "reload", "off"),
    "/skill": ("list", "info", "load", "unload", "clear"),
    "/session": ("new", "fork", "load", "rename", "rename-id", "delete", "save"),
    "/sessions": ("find",),
    "/export": ("markdown", "json"),
    "/silent": ("on", "off"),
    "/auto": ("on", "off", "steps"),
    "/compact": ("task",),
}


def _cells(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1


def _cell_width(text):
    return sum(_cells(char) for char in text)


def _take_cells(text, budget, tail=False):
    groups = []
    for char in text:
        if unicodedata.combining(char):
            if groups:
                groups[-1] += char
        else:
            groups.append(char)
    result, used = [], 0
    for group in reversed(groups) if tail else groups:
        cells = _cell_width(group)
        if used + cells > budget:
            break
        result.append(group)
        used += cells
    return ''.join(reversed(result) if tail else result)


def _table_cell(text, width, middle=False, pad=True):
    if _cell_width(text) > width:
        if middle:
            left = (width - 1) // 2
            tail = _take_cells(text, width - 1 - left, tail=True)
            text = _take_cells(text, left) + '…' + tail
        else:
            text = _take_cells(text, width - 1) + '…'
    return text + ' ' * max(0, width - _cell_width(text)) if pad else text


def session_table(rows, width, current_id='', home='', redact=None, language='zh'):
    """One numbered terminal row per session; keep IDs intact and fit other cells."""
    def clean(value):
        text = str(value)
        if redact:
            text = redact(text)
        text = ' '.join(text.split())
        return ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' for c in text)

    rows = list(rows)
    home = str(home).rstrip('/')
    api_values = []
    count_values = []
    directory_values = []
    for row in rows:
        profile = str(row.get('api') or '-')
        model = row.get('model') or ''
        api_values.append(clean(profile + (' / ' + str(model) if model else '')))
        count = row.get('messages')
        count_values.append(clean('-' if count is None else count))
        directory = str(row.get('directory') or '-')
        if home and (directory == home or directory.startswith(home + '/')):
            directory = '~' + directory[len(home):]
        directory_values.append(clean(directory))
    budget = max(28, int(width) - 1)  # Reserve the final column against auto-wrap.
    separator = '  ' if budget >= 99 else ' '
    ordinal_width = max(1, len(str(max(1, len(rows)))))
    numbered_header = '序号' if budget >= 79 and language == 'zh' else '#'
    if numbered_header == '序号':
        ordinal_width = max(ordinal_width, _cell_width(numbered_header))
    count_width = max([6 if budget >= 63 else 2, *(_cell_width(value) for value in count_values)])
    budget = max(budget, ordinal_width + 13 + count_width + 6 * len(separator) + 4)
    time_width = 16 if budget >= 99 else (12 if budget >= 47 else 5)
    time_width = min(time_width, max(1, budget - ordinal_width - 13 - count_width - 6 * len(separator) - 3))
    remaining = budget - ordinal_width - time_width - 13 - count_width - 6 * len(separator)
    desired_api = max([10, *(_cell_width(value) for value in api_values)])
    api_width = min(desired_api, 48, max(1, min(remaining - 2, remaining * 2 // 3)))
    available = remaining - api_width
    desired_title = max([1, *(_cell_width(clean(row.get('title') or 'New session')) for row in rows)])
    desired_path = max([1, *(_cell_width(value) for value in directory_values)])
    if desired_title + desired_path <= available:
        extra = available - desired_title - desired_path
        title_width = min(32, desired_title + extra // 2)
        path_width = available - title_width
    else:
        balanced_path = max(1, (available + 1) // 2)
        if desired_path <= balanced_path:
            path_width = desired_path
        elif desired_title <= available - balanced_path:
            path_width = available - desired_title
        else:
            path_width = balanced_path
        title_width = available - path_width
    widths = (ordinal_width, time_width, 13, title_width, api_width, count_width, path_width)
    headers = (numbered_header, '最后会话时间' if time_width >= 12 else '时间', 'ID',
               'title' if title_width >= 5 else 'T',
               'API / 模型' if api_width >= 10 else ('API/模型' if api_width >= 8 else 'API'),
               '消息数' if count_width >= 6 else '数',
               '工作路径' if path_width >= 8 else ('路径' if path_width >= 4 else 'P'))
    if language == 'en':
        headers = (numbered_header, 'Updated' if time_width >= 12 else 'Time', 'ID',
                   'Title' if title_width >= 5 else 'T', 'API/model' if api_width >= 9 else 'API',
                   'Msgs' if count_width >= 4 else 'N', 'Cwd' if path_width >= 3 else 'P')

    def render(values):
        cells = []
        for index, (value, size) in enumerate(zip(values, widths)):
            cell = _table_cell(value, size, middle=index in (4, 6), pad=index < 5)
            if index == 5:
                cell = ' ' * max(0, size - _cell_width(cell)) + cell
            cells.append(cell)
        return separator.join(cells).rstrip()

    result = [render(headers)]
    for ordinal, (row, api, count, directory) in enumerate(
            zip(rows, api_values, count_values, directory_values), 1):
        updated = clean(row.get('updated') or '-')
        if len(updated) >= 16 and updated[4:5] == '-':
            updated = updated[:16] if time_width == 16 else (updated[5:16] if time_width == 12 else updated[11:16])
        marker = '*' if row.get('id') == current_id else ' '
        result.append(render((str(ordinal), updated, marker + clean(row.get('id') or '-'),
                              clean(row.get('title') or 'New session'),
                              api, count, directory)))
    return result


class Columns:
    def __init__(self, out, enabled=True, width=None, clock=None):
        self.out = out
        self.enabled = enabled
        self.width = width or (lambda: shutil.get_terminal_size((80, 24)).columns)
        self.clock = clock or datetime.now
        self.stamp = ""
        self.role = None
        self.used = 0
        self.line_started = False

    def _layout(self, role):
        width = max(20, self.width())
        if width < 52:
            prefix = f"{self.stamp} {role}: "
            continuation = " " * len(prefix)
        else:
            prefix = f"{self.stamp} {role:<8}│ "
            continuation = " " * 14 + "│ "
        return prefix, continuation, max(2, width - len(prefix))

    def _prefix(self):
        prefix, continuation, _ = self._layout(self.role)
        self.out.write(prefix if not self.line_started else continuation)
        self.line_started = True
        self.used = 0

    def feed(self, role, value, stamp=None):
        if not self.enabled:
            if self.role is not None and role != self.role:
                self.out.write("\n")
            if role != self.role and role == "Think":
                self.out.write("[reasoning] ")
            self.role = role
            self.out.write(value)
            self.out.flush()
            return
        if role != self.role:
            self.finish()
            self.role = role
            self.stamp = stamp if stamp is not None else self.clock().strftime("%H:%M")
        _, _, available = self._layout(role)
        for char in value:
            if char == "\n":
                self.out.write("\n")
                self.line_started = True
                self.used = 0
                continue
            text = "    " if char == "\t" else (
                char if char.isprintable() else f"\\x{ord(char):02x}")
            for item in text:
                cells = _cells(item)
                if self.used + cells > available:
                    self.out.write("\n")
                    self.line_started = True
                    self.used = 0
                if self.used == 0:
                    self._prefix()
                self.out.write(item)
                self.used += cells
        self.out.flush()

    def finish(self):
        if self.role is not None:
            self.out.write("\n")
            self.out.flush()
        self.role = None
        self.stamp = ""
        self.used = 0
        self.line_started = False

    def message(self, role, value, stamp=None):
        self.finish()
        if not self.enabled:
            self.out.write(value + "\n")
            self.out.flush()
            return
        self.feed(role, value, stamp=stamp)
        self.finish()

    def prompt(self):
        self.finish()
        if not self.enabled:
            return "ta> "
        label = "You: " if self.width() < 52 else "You     │ "
        return self.clock().strftime("%H:%M") + " " + label

    def sessions(self, rows, current_id='', home='', redact=None, language='zh'):
        self.finish()
        # Use full terminal width; repeated message timestamps/roles consume
        # space needed for session IDs, API names and working directories.
        lines = session_table(rows, self.width(), current_id, home, redact, language=language)
        self.out.write('\n'.join(lines) + '\n')
        self.out.flush()

    def continuation(self):
        if not self.enabled:
            return "... "
        return " " * 6 + ("...: " if self.width() < 52 else "...     │ ")


class Completer:
    def __init__(self, readline, profiles, skills, active_skills, session_ids,
                 directory, session_choices=None):
        self.readline = readline
        self.profiles = profiles
        self.skills = skills
        self.active_skills = active_skills
        self.session_ids = session_ids
        self.directory = directory
        self.session_choices = session_choices
        self._cache_key = None
        self._matches = []

    def _agent_files(self, prefix):
        # Complete files inside the current workspace only.
        path = Path(prefix)
        if path.is_absolute() or ".." in path.parts:
            return []
        base = (self.directory() / path.parent).resolve()
        if not base.is_relative_to(self.directory().resolve()) or not base.is_dir():
            return []
        return [str(path.parent / item.name) + ("/" if item.is_dir() else "")
                for item in base.iterdir()
                if item.name.startswith(path.name) and
                (item.is_dir() or item.suffix.lower() == ".md")]

    def _paths(self, prefix):
        quote = prefix[0] if prefix[:1] in ('"', "'") else ""
        body = prefix[1:] if quote else prefix
        if quote and body.endswith(quote):
            body = body[:-1]
        normalized = "~" + body[1:] if body.startswith("～") else body
        try:
            if normalized.startswith("~") and "/" not in normalized:
                base, partial, display = Path(normalized).expanduser(), "", body + "/"
            else:
                expanded = Path(normalized).expanduser()
                if body.endswith("/"):
                    base, partial, display = expanded, "", body
                else:
                    base, partial = expanded.parent, expanded.name
                    display = body.rpartition("/")[0] + "/"
            options = []
            for item in base.iterdir():
                if not item.name.startswith(partial) or (item.name.startswith(".") and not partial.startswith(".")):
                    continue
                is_dir = item.is_dir()
                options.append(quote + display + item.name + ("/" if is_dir else quote))
            return options
        except (OSError, RuntimeError, ValueError):
            return []

    def matches(self, line, begin, prefix):
        full_prefix = line[:begin] + prefix
        quoted_path = (len(full_prefix) >= 2 and full_prefix[0] in (chr(34), chr(39)) and full_prefix[1] in ("/", "~", "～"))
        root = line.split(maxsplit=1)[0] if line.split() else ""
        path_mode = quoted_path or full_prefix.startswith(("~", "～")) or (
            full_prefix.startswith("/") and root not in COMMANDS)
        if path_mode:
            options = self._paths(full_prefix)
            if begin == 0 and not quoted_path and full_prefix.startswith("/"):
                options += [name for name in COMMANDS if name.startswith(full_prefix)]
            return sorted({name[begin:] for name in options if name.startswith(full_prefix)})
        if not line.startswith("/"):
            return []
        if begin == 0:
            options = COMMANDS
        else:
            words = line[:begin].split()
            root = words[0] if words else ""
            selecting_session = (
                (root == "/resume" and len(words) == 1) or
                (root == "/session" and len(words) == 2 and words[1] in ("load", "delete"))
            )
            if selecting_session:
                choices = self.session_choices() if self.session_choices else ()
                options = (*choices, *self.session_ids())
            elif len(words) == 1:
                options = SUBCOMMANDS.get(root, ())
                if root == "/api":
                    options = (*options, *self.profiles())
                elif root == "/agent":
                    options = (*options, *self._agent_files(prefix))
                elif root == '/export':
                    options = (*options, *self.session_ids())
            elif root == "/api" and words[1] in ("show", "default", "remove", "switch"):
                options = self.profiles()
            elif root == "/session" and words[1] == "rename-id" and len(words) == 2:
                options = self.session_ids()
            elif root == '/export' and len(words) == 2:
                options = ('markdown', 'json')
            elif root == "/skill" and words[1] in ("load", "info"):
                options = self.skills()
            elif root == "/skill" and words[1] == "unload":
                options = self.active_skills()
            else:
                options = ()
        return sorted({name for name in options if name.startswith(prefix)})

    def __call__(self, prefix, state):
        line = self.readline.get_line_buffer()
        begin = self.readline.get_begidx()
        key = (line, begin, prefix)
        if state == 0 or key != self._cache_key:
            self._cache_key = key
            self._matches = self.matches(line, begin, prefix)
        return self._matches[state] if state < len(self._matches) else None


def format_context_status(status, language='zh'):
    """Explicit units: character guard is not the provider's token window."""
    chars, limit = status['chars'], status['limit']
    parts = [f"Context={ui_text(status['state'], language)}", f"Chars={chars:,}/{limit:,} ({chars / limit:.0%})"]
    token = status['tokens']
    count, window = token.get('prompt_tokens'), token.get('context_window_tokens')
    if count is None:
        parts.append('Tokens=' + ui_text('未计数', language))
    else:
        mark = '=' if token.get('exact') else '≈'
        parts.append(f"Tokens{mark}{count:,}/" + (f"{window:,} ({count / window:.0%}; "
                     f"{ui_text('预留', language)} {token['output_reserve_tokens']:,})" if window else ui_text('未知窗口', language)))
    parts.append(f"Auto={status['threshold']:.0%}" if status['auto_compact'] else 'Auto=' + ui_text('关闭', language))
    return ' | '.join(parts)
