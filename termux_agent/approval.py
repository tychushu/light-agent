"""Conservative human approval heuristics. This is not a shell sandbox."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex

from .paths import normalize_tool_args, tool_path
from .security import protected_agent_path

_CONFIG_FILES = {'.bashrc', '.zshrc', '.profile', '.bash_profile', '.bash_login', '.zprofile', '.termuxrc'}
_SYSTEM_COMMANDS = {'chmod', 'chown', 'kill', 'pkill', 'killall', 'reboot', 'shutdown',
                    'dd', 'mount', 'umount', 'adb'}
_SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
_PACKAGE_ACTIONS = {'install', 'uninstall', 'remove', 'upgrade', 'full-upgrade', 'add', 'update'}
_READERS = {'cat', 'less', 'head', 'tail', 'ls', 'grep', 'rg', 'wc', 'stat', 'file', 'readlink', 'realpath'}


def _config_path(value, cwd=None):
    try:
        path = tool_path(value, cwd)
    except (ValueError, OSError, RuntimeError):
        return False
    candidates = [path]
    try:
        candidates.append(path.resolve())
    except (OSError, RuntimeError):
        # A broken or cyclic symlink must not discard lexical .ssh/config rules.
        pass
    for candidate in candidates:
        parts = tuple(part.lower() for part in candidate.parts)
        if (protected_agent_path(candidate) or candidate.name.lower() in _CONFIG_FILES or
                '.termux' in parts or '.ssh' in parts or
                any(parts[i:i + 3] == ('.local', 'share', 'termux-agent') for i in range(len(parts) - 2)) or
                any(parts[i:i + 2] in (('.config', 'termux'), ('.config', 'termux-agent'), ('.git', 'hooks'))
                    for i in range(len(parts) - 1))):
            return True
        prefix = os.environ.get('PREFIX')
        if prefix and candidate.parent == Path(prefix) / 'etc' and candidate.name in ('profile', 'bash.bashrc', 'zshrc'):
            return True
        if ('etc' in parts and candidate.name in ('profile', 'bash.bashrc', 'zshrc')
                and any('termux' in part for part in parts[:parts.index('etc')])):
            return True
    return bool(re.search(r'\$\{?prefix\}?/etc/(?:profile|bash\.bashrc|zshrc)\b', value, re.I))


def _segment_reason(words, depth, cwd=None):
    words = list(words)
    # Inspect wrappers without evaluating environment values or Shell code.
    while words and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]) or
                     Path(words[0]).name in ('command', 'builtin', 'exec', 'env', 'nohup', 'time')):
        wrapper = words.pop(0)
        if Path(wrapper).name == 'env' and words and words[0].startswith('-'):
            return 'environment wrapper with options'
        while words and words[0] == '--':
            words.pop(0)
        if words and words[0].startswith('-'):
            return 'command wrapper with options'
    if not words:
        return ''
    if '$' in words[0]:
        return 'unresolved shell command name'
    name = Path(words[0]).name.lower()
    args = words[1:]
    reader = name in _READERS and not (name == 'rg' and any(
        arg == '--pre' or arg.startswith('--pre=') for arg in args))
    if not reader:
        for arg in args:
            paths = [arg]
            if arg.startswith('-') and '=' in arg:
                paths.append(arg.split('=', 1)[1])
            if any(_config_path(path, cwd) for path in paths):
                return 'command accesses protected configuration or data'
    if name in ('sudo', 'su'):
        return 'privilege escalation attempt'
    if name in ('eval', 'source', '.', 'if', 'for', 'while', 'until', 'case', 'function', '{', 'nice', 'timeout', 'xargs'):
        return 'shell evaluation or compound command'
    if name in ('busybox', 'toybox'):
        return _segment_reason(args, depth + 1, cwd) if depth < 3 else 'nested command wrapper'
    if name in _SHELLS:
        if depth < 3 and '-c' in args and args.index('-c') + 1 < len(args):
            return _shell_reason(args[args.index('-c') + 1], depth + 1, cwd)
        return 'shell script execution'
    if name in _SYSTEM_COMMANDS or name.startswith('mkfs'):
        return 'system, process, device, or filesystem operation'
    if name == 'shred' or name == 'crontab' and any(arg.startswith('-') and 'r' in arg[1:] for arg in args):
        return 'destructive file or scheduled-task operation'
    if name == 'pm' and any(arg in ('uninstall', 'install', 'clear', 'disable', 'disable-user') for arg in args):
        return 'Android package operation'
    if name == 'rm' and any(
            arg in ('--recursive', '--force', '/') or
            (arg.startswith('-') and not arg.startswith('--') and any(c in arg[1:].lower() for c in 'rf')) or
            any(c in arg for c in '*?{') for arg in args):
        return 'recursive or broad deletion'
    if name == 'find' and any(arg in ('-delete', '-exec', '-execdir', '-ok', '-okdir') for arg in args):
        return 'find deletion or command execution'
    if name == 'git' and (
            'clean' in args and any(arg.startswith('-') and ('f' in arg or 'd' in arg) for arg in args) or
            'reset' in args and '--hard' in args):
        return 'destructive git operation'
    if name == 'git' and 'push' in args and any(
            arg.startswith(('--force', '+')) or arg.startswith('-') and not arg.startswith('--') and 'f' in arg[1:]
            for arg in args):
        return 'force push to a remote repository'
    if name == 'rsync' and any(arg.startswith('--delete') for arg in args):
        return 'synchronization with destination deletion'
    if (name in ('pkg', 'apt', 'apt-get', 'npm', 'pnpm', 'yarn', 'cargo', 'gem') or
            re.fullmatch(r'pip\d*(?:\.\d+)?', name)) and any(arg in _PACKAGE_ACTIONS for arg in args):
        return 'package installation or removal'
    if re.fullmatch(r'python\d*(?:\.\d+)?', name):
        if '-c' in args:
            return 'inline program execution'
        if '-m' in args and 'pip' in args and any(arg in _PACKAGE_ACTIONS for arg in args):
            return 'package installation or removal'
    if re.fullmatch(r'(?:python|perl|ruby|node|nodejs|php|lua)\d*(?:\.\d+)*', name):
        if any(arg.startswith('-') and not arg.startswith('--') and any(c in arg[1:] for c in 'ceE')
               for arg in args):
            return 'inline program execution'
        readonly_options = {'-V', '-v', '--version', '-h', '--help'}
        if not args or '-' in args or all(arg.startswith('-') for arg in args) and not set(args) <= readonly_options:
            return 'program execution from standard input'
    if name in ('awk', 'gawk', 'mawk', 'nawk') and args and not set(args) <= {'--version', '--help', '-Wversion'}:
        return 'inline program execution'
    in_place = name == 'sed' and any(arg.startswith('-i') or arg.startswith('--in-place') for arg in args)
    writes = in_place or name in ('cp', 'mv', 'ln', 'tee', 'truncate', 'touch', 'install', 'rsync') or any('>' in word for word in words)
    if writes and any('$' in word for word in words):
        # Never expand arbitrary environment variables just to classify a path.
        return 'write with unresolved shell variables'
    if writes and any(_config_path(arg, cwd) for arg in words if arg and not arg.startswith('-')):
        return 'write to shell, credentials, hooks, or agent configuration/data'
    if name == 'truncate' and any('*' in arg or '{' in arg for arg in args):
        return 'potentially large deletion'
    for index, arg in enumerate(words[:-1]):
        if '>' in arg and re.match(r'^/(?:etc|system|data|sdcard)(?:/|$)', words[index + 1]):
            return 'overwrite of a broad system or user data path'
    return ''


def _shell_reason(command, depth=0, cwd=None):
    # shlex is not a Shell parser. Ask about evaluation we cannot inspect safely.
    if '$(' in command or '`' in command:
        return 'shell command substitution'
    try:
        lexer = shlex.shlex(command.replace('\n', ' ; '), posix=True, punctuation_chars=';&|()<>')
        lexer.whitespace_split = True
        # Do not let a comment consume the synthetic newline delimiter and hide
        # a command on the following physical line. Comment text may over-prompt.
        lexer.commenters = ''
        segments, current = [], []
        for word in lexer:
            if word and all(c in ';&|()' for c in word):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(word)
        if current:
            segments.append(current)
    except ValueError:
        return 'unparsed shell syntax'
    for words in segments:
        why = _segment_reason(words, depth, cwd)
        if why:
            return why
        if words[0] == 'cd':
            if len(words) > 2 or len(words) == 2 and words[1].startswith('-'):
                return 'directory change with unparsed options'
            try:
                cwd = tool_path(words[1] if len(words) == 2 else '~', cwd)
            except ValueError:
                return 'invalid directory change'
    return ''


def reason(name: str, args: dict) -> str:
    try:
        args = normalize_tool_args(name, args)
    except (ValueError, TypeError):
        return 'invalid tool path'
    if name in ('write_file', 'edit_file'):
        path = args.get('path', '')
        if _config_path(path):
            return 'write to shell, credentials, hooks, or agent configuration/data'
        try:
            existing_size = os.path.getsize(path)
        except OSError:
            existing_size = 0
        if existing_size >= 1024 * 1024:
            return 'overwrite or edit of an existing large file'
        content = str(args.get('content', '')) if name == 'write_file' else str(args.get('new_text', ''))
        if len(content.encode('utf-8', 'replace')) >= (1024 * 1024 if name == 'write_file' else 512 * 1024):
            return 'large file overwrite' if name == 'write_file' else 'large replacement'
    if name == 'shell':
        return _shell_reason(str(args.get('command', '')), cwd=args.get('cwd'))
    return ''


def needs_approval(name: str, args: dict, policy: str = 'on-risk') -> bool:
    if policy == 'never':
        return False
    if policy == 'always':
        return True
    if policy != 'on-risk':
        return True
    return bool(reason(name, args))
