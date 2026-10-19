"""Readable transcript views without modifying stored/API messages."""
import json

from .display_privacy import hide_write_payloads


def _value(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def history_entry(message, silent_writes=True, limit=4000):
    visible = hide_write_payloads(message) if silent_writes else message
    role = {"user": "You", "assistant": "Agent", "tool": "Tool"}.get(
        visible.get("role"), "Info")
    parts = []
    content = visible.get("content")
    if isinstance(content, list):
        content = "\n".join(item.get("text", "[" + str(item.get("type", "content")) + "]")
                            if isinstance(item, dict) else str(item) for item in content)
    if role == "Tool":
        identifier = visible.get("tool_call_id")
        if identifier:
            parts.append("Result · " + str(identifier))
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                pass
        if isinstance(content, dict):
            parts.extend(str(key) + ":\n" + _value(value) for key, value in content.items())
        elif content is not None:
            parts.append(_value(content))
    elif content is not None:
        parts.append(_value(content))
    for call in visible.get("tool_calls") or []:
        function = call.get("function") or {}
        name = str(function.get("name", "unknown"))
        identifier = call.get("id")
        label = "Call · " + name + (" · " + str(identifier) if identifier else "")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        parts.append(label + "\n" + _value(arguments))
    text = "\n\n".join(part for part in parts if part) or "[empty message]"
    if len(text) > limit:
        text = text[:limit] + "\n[TRUNCATED]"
    return role, text
