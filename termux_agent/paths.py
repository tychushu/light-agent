"""One lexical path interpretation for file tools and their approval checks.

Do not resolve symlinks here: following them changes atomic replacement semantics.
Approval additionally inspects the resolved target, without promising a sandbox.
"""
import os
from pathlib import Path


def tool_path(value, base=None):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('path must be a non-empty string without NUL')
    if value.startswith('～'):
        value = '~' + value[1:]
    # Model-supplied paths are not Shell expressions. Expanding $VAR can leak
    # credentials into normalized arguments, file errors and saved tool results.
    expanded = Path(os.path.expanduser(value))
    if not expanded.is_absolute() and base is not None:
        expanded = Path(base) / expanded
    return Path(os.path.abspath(expanded))


def normalize_tool_args(name, args):
    result = dict(args)
    if name in ('read_file', 'write_file', 'edit_file') and 'path' in result:
        result['path'] = str(tool_path(result['path']))
    if name == 'shell' and result.get('cwd') is not None:
        result['cwd'] = str(tool_path(result['cwd']))
    return result
