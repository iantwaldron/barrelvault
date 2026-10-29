"""Notes helpers: appending text and reading the drop reason.

Notes are markdown a person or agent wrote. These functions use plain string
operations only. Their output is text, never evaluated or passed to a template.
"""

import re

# A code fence opens with three or more backticks or tildes, indented by up to three
# spaces, and closes with at least as many of the same character and nothing after them
# but spaces. A backtick fence's info string has no backtick.
_OPEN = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})(.*)$")
_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_LABEL = "dropped"


def append_note(notes: str | None, text: str) -> str:
    """Notes with the text added. Existing notes get a blank line before it."""
    if not notes:
        return text
    return f"{notes}\n\n{text}"


def dropped_block(reason: str) -> str:
    """The fenced block a drop appends to notes.

    The fence is longer than any run of backticks in the reason, the way markdown
    nests a fence, so a fence inside the reason stays part of its text.
    """
    text = reason.strip()
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{_LABEL}\n{text}\n{fence}"


def close_open_fence(notes: str | None) -> str:
    """Notes with a closing fence added when they end inside an open one.

    A drop calls this before it appends its block, so the block is not read as part
    of an earlier block that was never closed.
    """
    text = notes or ""
    _reason, fence = _scan(text)
    if fence is None:
        return text
    return f"{text}\n{fence}"


def dropped_reason(notes: str | None) -> str:
    """The text of the last dropped block in the notes, or "" when there is none."""
    reason, _fence = _scan(notes or "")
    return reason


def _scan(notes: str) -> tuple[str, str | None]:
    """Read the notes line by line. Return the last dropped reason and any open fence.

    Only a block whose fence is labeled dropped counts, and a fence inside another
    block is that block's text.
    """
    reason = ""
    fence: str | None = None
    label = ""
    body: list[str] = []
    for line in notes.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence is None:
            opening = _OPEN.match(line)
            if opening:
                fence, label, body = opening[1], opening[2].strip(), []
            continue
        closing = _CLOSE.match(line)
        if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence):
            if label == _LABEL:
                reason = "\n".join(body)
            fence = None
        else:
            body.append(line)
    return reason, fence
