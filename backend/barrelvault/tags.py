"""Tag helpers. Every tag read and write goes through these.

The stored form is lowercase tokens separated by single spaces, in order of first
appearance, with no duplicates. No tokens is "".
"""


def stored_tags(submitted: str | None) -> str:
    """The stored form of a submitted tag string."""
    tokens: list[str] = []
    for token in (submitted or "").lower().split():
        if token not in tokens:
            tokens.append(token)
    return " ".join(tokens)


def read_tags(stored: str | None) -> list[str]:
    """The tokens of a stored tag string. A null reads as no tokens."""
    return (stored or "").split()
