"""Small completion markers and a stream filter for explicit task runs."""
import re

TASK_MARKERS = ("[TASK_DONE]", "[TASK_BLOCKED]", "[TASK_CONTINUE]")
_STATUS = re.compile(r"\[TASK_(DONE|BLOCKED|CONTINUE)\]\s*$")
_PROVIDER_REJECTION = re.compile(
    r"^\s*(?:the request was rejected because|request (?:blocked|rejected).*(?:safety|high risk|content filter)"
    r"|i (?:cannot|can't) (?:help|assist) with|(?:抱歉|对不起)[，, ]*我(?:无法|不能)(?:帮助|协助|提供))",
    re.I,
)


def provider_blocked(answer, finish_reason=None):
    content = answer.get("content")
    return bool(answer.get("refusal") or finish_reason == "content_filter" or
                (isinstance(content, str) and _PROVIDER_REJECTION.search(content)))


def task_status(text):
    match = _STATUS.search(text)
    if not match:
        return text, None
    return text[:match.start()].rstrip(), match.group(1).lower()


class MarkerFilter:
    def __init__(self, markers):
        self.markers = tuple(markers)
        self.pending = ""

    def feed(self, text, final=False):
        self.pending += text
        for marker in self.markers:
            self.pending = self.pending.replace(marker, "")
        keep = 0
        if not final:
            for marker in self.markers:
                for size in range(1, len(marker)):
                    if self.pending.endswith(marker[:size]):
                        keep = max(keep, size)
        shown = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return shown
