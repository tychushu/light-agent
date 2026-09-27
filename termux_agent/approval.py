"""A deliberately small human approval heuristic, not a sandbox."""
from __future__ import annotations

import re


_RISK_RULES = [
    (re.compile(r"(?:^|[;&|\s])(?:[\w./-]+/)?rm\b(?=[^\n]*(?:\s-r(?:f|\s)|\s-fr|--recursive|\s/\s*(?:$|[;|&])|\s\*\s*(?:$|[;|&])))", re.I), "recursive or broad deletion"),
    (re.compile(r"(?:^|[;&|\s])(?:pkg|apt|apt-get|pip)\s+(?:install|uninstall|remove|upgrade|full-upgrade)\b|(?:^|[;&|\s])python\d*(?:\.\d+)?\s+-m\s+pip\s+(?:install|uninstall)\b", re.I), "package installation or removal"),
    (re.compile(r"(?:^|[;&|\s])(?:[\w./-]+/)?(?:chmod|chown|kill|pkill|killall|reboot|shutdown|dd|mkfs(?:\.\w+)?|mount|umount|adb)\b", re.I), "system, process, device, or filesystem operation"),
    (re.compile(r"(?:^|\s)(?:sudo|su)\b", re.I), "privilege escalation attempt"),
    (re.compile(r"\b(?:rm|truncate)\b[^\n]*(?:\*|\{[^}]{20,}\})", re.I), "potentially large deletion"),
]


def reason(name: str, args: dict) -> str:
    """Return why a call is risky, or an empty string when no rule matches."""
    if name in ("write_file", "edit_file"):
        import os
        original_path = str(args.get("path", "")).replace("\\", "/").lower()
        path = os.path.expandvars(os.path.expanduser(original_path)).replace("\\", "/").lower()
        content = str(args.get("content", "")) if name == "write_file" else str(args.get("new_text", ""))
        from pathlib import PurePosixPath
        parts = PurePosixPath(path).parts
        config_files = {".bashrc", ".zshrc", ".profile", ".bash_profile", ".bash_login", ".zprofile", ".termuxrc"}
        if (re.search(r"\$\{?prefix\}?/etc/(?:profile|bash\.bashrc|zshrc)\b", original_path) or
                PurePosixPath(path).name in config_files or ".termux" in parts or
                any(parts[i:i + 2] == (".config", "termux") for i in range(max(0, len(parts) - 1))) or
                ("etc" in parts and PurePosixPath(path).name in {"profile", "bash.bashrc", "zshrc"} and
                 any("termux" in p for p in parts[:parts.index("etc")]))):
            return "write to shell or Termux configuration"
        try:
            existing_size = os.path.getsize(os.path.expanduser(os.path.expandvars(str(args.get("path", "")))))
        except OSError:
            existing_size = 0
        if existing_size >= 1024 * 1024:
            return "overwrite or edit of an existing large file"
        if name == "write_file" and len(content.encode("utf-8", "replace")) >= 1024 * 1024:
            return "large file overwrite"
        if name == "edit_file" and len(content.encode("utf-8", "replace")) >= 512 * 1024:
            return "large replacement"
    if name == "shell":
        command = str(args.get("command", ""))
        for pattern, why in _RISK_RULES:
            if pattern.search(command):
                return why
        writes = re.search(r"(?:>|\btee\b)", command, re.I)
        lower = command.lower().replace("\\", "/")
        target = re.search(r"(?:\$prefix|\$\{prefix\})/etc/(?:profile|bash\.bashrc|zshrc)|(?:^|[\s>])[^\s;]*(?:/\.termux(?:/|\b)|/\.config/termux/|/\.(?:bashrc|zshrc|profile|bash_profile|bash_login|zprofile|termuxrc)(?:\b|$))[^\s;]*", lower)
        if writes and target:
            return "write to shell or Termux configuration"
        if re.search(r"(?:^|[;&|]\s*)(?:[\w./-]+/)?(?:cat|printf|echo)\b[^\n]*>\s*/(?:etc|system|data|sdcard)(?:/|\b)", command, re.I):
            return "overwrite of a broad system or user data path"
    return ""


def needs_approval(name: str, args: dict, policy: str = "on-risk") -> bool:
    """Apply always/on-risk/never policy. Unknown policy errs toward approval."""
    if policy == "never":
        return False
    if policy == "always":
        return True
    if policy != "on-risk":
        return True
    return bool(reason(name, args))
