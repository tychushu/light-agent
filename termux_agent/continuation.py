"""Small completion markers and a stream filter for explicit task runs."""
import re

TASK_MARKERS = ("[TASK_DONE]", "[TASK_BLOCKED]", "[TASK_CONTINUE]")
_STATUS = re.compile(r"\[TASK_(DONE|BLOCKED|CONTINUE)\]\s*$")


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
