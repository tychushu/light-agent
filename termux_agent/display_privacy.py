"""Summaries for file-writing arguments; never mutate API messages."""
import json

WRITE_TOOLS = frozenset(("write_file", "edit_file"))


def _size(value):
    if not isinstance(value, str):
        return {"type": type(value).__name__}
    return {"chars": len(value), "bytes": len(value.encode("utf-8", "replace"))}


def safe_tool_arguments(name, arguments):
    if name not in WRITE_TOOLS:
        return arguments
    if not isinstance(arguments, dict):
        return {"body_hidden": True, "arguments": _size(arguments)}
    path = arguments.get("path")
    result = {"path": path if isinstance(path, str) else f"[{type(path).__name__}]",
              "body_hidden": True}
    if name == "write_file":
        result["content"] = _size(arguments.get("content"))
    else:
        result["old_text"] = _size(arguments.get("old_text"))
        result["new_text"] = _size(arguments.get("new_text"))
        count = arguments.get("expected_replacements", 1)
        result["expected_replacements"] = count if isinstance(count, int) else "[invalid]"
    return result


def hide_write_payloads(value):
    if isinstance(value, list):
        return [hide_write_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: hide_write_payloads(item) for key, item in value.items()}
    name = value.get("name")
    if isinstance(name, str) and name in WRITE_TOOLS and "arguments" in value:
        args = value["arguments"]
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                parsed = args
            result["arguments"] = json.dumps(safe_tool_arguments(name, parsed), ensure_ascii=False)
        else:
            result["arguments"] = safe_tool_arguments(name, args)
    return result
