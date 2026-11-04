"""Process-local registries of credential names and agent-owned paths.

These narrow defaults supplement approval heuristics. They do not isolate the
filesystem or prevent a user-authorized program from loading its own credentials.
Registries store names/paths only; filtering uses current values transiently to
remove environment aliases of the registered keys as well.
"""
import os
from pathlib import Path

_KEY_NAMES = {'TERMUX_AGENT_API_KEY'}
_FILES = set()
_DIRECTORIES = set()
MIN_ALIAS_SECRET_LENGTH = 16


def register_key_names(names):
    # Even a non-Shell identifier can be an OS environment key. If configured,
    # filter it rather than silently ignoring it for failing an identifier regex.
    _KEY_NAMES.update(name for name in names if isinstance(name, str) and name)


def _path_forms(value):
    path = Path(value).expanduser().absolute()
    result = {path}
    try:
        result.add(path.resolve())
    except (OSError, RuntimeError):
        pass
    return result


def protect_directory(path):
    _DIRECTORIES.update(_path_forms(path))


def register_config(path, names):
    register_key_names(names)
    path = Path(path).expanduser().absolute()
    _FILES.update(_path_forms(path))
    # Custom --config files need protection too, without treating an entire
    # project directory containing config.toml as protected application data.
    protect_directory(path.parent / 'profiles')
    for name in ('credentials.env', 'selected-api', 'system_prompt.txt', 'task_prompt.txt',
                 'task_continue.txt', 'task_resume.txt', 'subagent_prompt.txt'):
        _FILES.update(_path_forms(path.parent / name))


def protected_agent_path(path):
    home = Path.home()
    directories = set(_DIRECTORIES)
    for root in (home / '.config/termux-agent', home / '.local/share/termux-agent', home / '.ssh'):
        directories.update(_path_forms(root))
    forms = _path_forms(path)
    return any(candidate in _FILES or any(candidate.is_relative_to(root) for root in directories)
               for candidate in forms)


def child_environment():
    """Keep ordinary Termux variables, strip all registered API keys and aliases."""
    current = os.environ.copy()
    names = _KEY_NAMES | {name for name in current if name in ('API_KEY', 'API_TOKEN') or
                         name.endswith(('_API_KEY', '_API_TOKEN'))}
    # Named credential variables are always removed. A short local placeholder
    # such as "1" or "none" must not also erase unrelated ordinary variables.
    values = {current[name] for name in names if len(current.get(name, '')) >= MIN_ALIAS_SECRET_LENGTH}
    return {name: value for name, value in current.items()
            if name not in names and name not in ('BASH_ENV', 'ENV') and value not in values}
