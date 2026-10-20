"""Width-aware two-column transcript and lazy slash-command completion."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import unicodedata


COMMANDS = (
    "/help", "/clear", "/stats", "/config", "/debug", "/debug-context",
    "/api", "/agent", "/skill", "/copy", "/paste", "/sessions", "/session", "/title",
    "/history", "/silent", "/auto", "/run", "/sub", "/exit",
)
SUBCOMMANDS = {
    "/api": ("list", "add", "switch", "show", "default", "remove", "next", "prev"),
    "/agent": ("directory", "reload", "off"),
    "/skill": ("list", "info", "load", "unload", "clear"),
    "/session": ("new", "fork", "load", "rename", "rename-id", "delete", "save"),
    "/sessions": ("find",),
    "/silent": ("on", "off"),
    "/auto": ("on", "off", "steps"),
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


def session_table(rows, width, current_id='', home='', redact=None):
    """One terminal row per session; keep IDs intact and fit the other cells."""
    def clean(value):
        text = str(value)
        if redact:
            text = redact(text)
        text = ' '.join(text.split())
        return ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' for c in text)

    rows = list(rows)
    api_values = []
    for row in rows:
        profile = str(row.get('api') or '-')
        model = row.get('model') or ''
        api_values.append(clean(profile + (' / ' + str(model) if model else '')))
    budget = max(28, int(width) - 1)  # Reserve the final column against auto-wrap.
    separator = '  ' if budget >= 79 else ' '
    time_width = 16 if budget >= 99 else (12 if budget >= 47 else 5)
    remaining = budget - time_width - 13 - 4 * len(separator)
    desired_api = max([10, *(_cell_width(value) for value in api_values)])
    api_width = min(desired_api, 48, remaining - 2, max(3, remaining * 3 // 5))
    title_width = min(32, max(1, (remaining - api_width) // 2))
    path_width = remaining - api_width - title_width
    widths = (time_width, 13, title_width, api_width, path_width)
    headers = ('最后会话时间' if time_width >= 12 else '时间', 'ID',
               'title' if title_width >= 5 else 'T',
               'API / 模型' if api_width >= 10 else ('API/模型' if api_width >= 8 else 'API'),
               '工作路径' if path_width >= 8 else ('路径' if path_width >= 4 else 'P'))

    def render(values):
        return separator.join(_table_cell(value, size, middle=index in (3, 4), pad=index < 4)
                              for index, (value, size) in enumerate(zip(values, widths))).rstrip()

    result = [render(headers)]
    home = str(home).rstrip('/')
    for row, api in zip(rows, api_values):
        updated = clean(row.get('updated') or '-')
        if len(updated) >= 16 and updated[4:5] == '-':
            updated = updated[:16] if time_width == 16 else (updated[5:16] if time_width == 12 else updated[11:16])
        directory = str(row.get('directory') or '-')
        if home and (directory == home or directory.startswith(home + '/')):
            directory = '~' + directory[len(home):]
        marker = '*' if row.get('id') == current_id else ' '
        result.append(render((updated, marker + clean(row.get('id') or '-'),
                              clean(row.get('title') or 'New session'),
                              api, clean(directory))))
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

    def sessions(self, rows, current_id='', home='', redact=None):
        self.finish()
        # Use full terminal width; repeated message timestamps/roles consume
        # space needed for session IDs, API names and working directories.
        lines = session_table(rows, self.width(), current_id, home, redact)
        self.out.write('\n'.join(lines) + '\n')
        self.out.flush()

    def continuation(self):
        if not self.enabled:
            return "... "
        return " " * 6 + ("...: " if self.width() < 52 else "...     │ ")


class Completer:
    def __init__(self, readline, profiles, skills, active_skills, session_ids,
                 directory):
        self.readline = readline
        self.profiles = profiles
        self.skills = skills
        self.active_skills = active_skills
        self.session_ids = session_ids
        self.directory = directory
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
            if len(words) == 1:
                options = SUBCOMMANDS.get(root, ())
                if root == "/api":
                    options = (*options, *self.profiles())
                elif root == "/agent":
                    options = (*options, *self._agent_files(prefix))
            elif root == "/api" and words[1] in ("show", "default", "remove", "switch"):
                options = self.profiles()
            elif root == "/session" and words[1] in ("load", "delete", "rename-id") and len(words) == 2:
                options = self.session_ids()
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
