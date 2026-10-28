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


def add_tags(stored: str | None, submitted: str) -> str:
    """The stored form with the submitted tags after the existing ones."""
    return stored_tags(f"{stored or ''} {submitted}")


def remove_tags(stored: str | None, submitted: str) -> str:
    """The stored form without the submitted tags, compared in stored form."""
    removed = set(read_tags(stored_tags(submitted)))
    return " ".join(token for token in read_tags(stored) if token not in removed)


def has_tags(stored: str | None, wanted: str) -> bool:
    """True when every wanted tag is a whole token of the stored tags."""
    tokens = set(read_tags(stored))
    return all(token in tokens for token in read_tags(stored_tags(wanted)))
