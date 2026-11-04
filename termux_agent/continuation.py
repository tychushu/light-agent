"""Small completion markers and a stream filter for explicit task runs."""
import re
import unicodedata

TASK_MARKERS = ("[TASK_DONE]", "[TASK_BLOCKED]", "[TASK_CONTINUE]")
CONTROL_USER_PREFIXES = ("[Context archive:", "[Task memo:", "[TASK_RESUME]", "[TASK_CONTINUE]")
_STATUS = re.compile(r"\[TASK_(DONE|BLOCKED|CONTINUE)\]\s*$")
# A message that only says "keep going".  It carries no new instruction, so it must
# never be treated as the start of a task or as the goal of one.  The list is exact
# on purpose: "继续执行，但先备份" or "continue with file two" are real instructions.
_RESUME_PHRASES = frozenset({
    "继续", "继续执行", "继续任务", "继续吧", "继续做", "继续干", "继续跑", "继续进行",
    "请继续", "请继续执行", "请继续任务", "接着", "接着做", "接着来", "接着执行", "接着干",
    "continue", "continue please", "please continue", "continue the task", "go on",
    "keep going", "carry on", "resume", "proceed",
})
_NON_WORD = re.compile(r"[\W_]+")


def is_resume_text(text):
    """True only for a bare, short "keep going" message (punctuation/width/case ignored)."""
    if not isinstance(text, str) or not text.strip() or len(text) > 40:
        return False
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    # The REPL promises that quoted keywords are literal model input, not controls.
    if normalized.startswith(('"', "'", '`', '“', '‘', '「', '『')):
        return False
    folded = _NON_WORD.sub(" ", normalized).strip()
    return folded in _RESUME_PHRASES


def is_task_start(text):
    """A human instruction that begins a task: not a generated control note or a bare resume."""
    return (isinstance(text, str) and not text.startswith(CONTROL_USER_PREFIXES)
            and not is_resume_text(text))
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


def control_prompt(marker, text):
    """Mark generated messages even when an existing local template is unmarked."""
    body = text.lstrip()
    while body.startswith(marker):
        body = body[len(marker):].lstrip()
    return marker + '\n' + body


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
