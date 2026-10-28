"""Notes helpers: appending text and reading the drop reason.

Notes are markdown a person or agent wrote. These functions use plain string
operations only. Their output is text, never evaluated or passed to a template.
"""

import re

_FENCE = re.compile(r"^(`{3,})(.*)$")
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


def dropped_reason(notes: str | None) -> str:
    """The text of the last dropped block in the notes, or "" when there is none.

    The notes are read line by line. A fence opens a block and the next line of at
    least as many backticks, and nothing else, closes it. Only a block whose fence
    is labeled dropped counts, and a fence inside another block is its text.
    """
    reason = ""
    fence: str | None = None
    label = ""
    body: list[str] = []
    for line in (notes or "").split("\n"):
        if fence is None:
            opening = _FENCE.match(line)
            if opening:
                fence, label, body = opening[1], opening[2].strip(), []
        elif line.startswith(fence) and line.strip("`") == "":
            if label == _LABEL:
                reason = "\n".join(body)
            fence = None
        else:
            body.append(line)
    return reason
