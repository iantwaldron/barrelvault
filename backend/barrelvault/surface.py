"""State the API and MCP share, kept out of the service: the duplicate warnings.

A create that matches a task already in the account is refused with Duplicate. To
create it anyway, the caller sends the create again with allow_duplicate, and that is
honored only after a warning to the same acting user for the same title and due_at.
Each warning confirms one duplicate. The warnings live in this process's memory, so
a restart forgets them and the caller is warned again.
"""

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlmodel import Session

from barrelvault.account import account_of, actor_id
from barrelvault.errors import Conflict, Duplicate
from barrelvault.models import User
from barrelvault.tasks import TaskDetail, create_task, fold_case, normalize_title
from barrelvault.times import to_utc

WINDOW_SECONDS = 120
MAX_WARNINGS = 1000

Fingerprint = tuple[int, str, datetime | None]


class DuplicateWarnings:
    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_SECONDS,
        max_warnings: int = MAX_WARNINGS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_warnings
        self._clock = clock
        # Fingerprint to the time its warning went out. A dict keeps insertion order,
        # so the oldest warning is always first.
        self._issued: dict[Fingerprint, float] = {}
        self._lock = threading.Lock()

    def create(
        self,
        session: Session,
        actor: User,
        title: str,
        *,
        allow_duplicate: bool = False,
        **fields: Any,
    ) -> TaskDetail:
        """Create a task, warning before a duplicate and honoring a confirmation once."""
        key = self._fingerprint(session, actor, title, fields.get("due_at"))
        # The lock guards only the record. The create runs outside it, so a session
        # waiting on the database never holds up another caller's check.
        if allow_duplicate:
            with self._lock:
                self._prune()
                warned = self._issued.pop(key, None) is not None
            if not warned:
                raise Conflict("confirm a duplicate only after its warning")
            try:
                return create_task(session, actor, title, allow_duplicate=True, **fields)
            except Exception:
                # The confirmation did not create the task, so the warning stands.
                self._record(key)
                raise
        try:
            return create_task(session, actor, title, **fields)
        except Duplicate:
            self._record(key)
            raise

    def _record(self, key: Fingerprint) -> None:
        """Record a warning as just issued. A repeat moves to the back with a fresh time."""
        with self._lock:
            self._issued.pop(key, None)
            self._issued[key] = self._clock()
            self._prune()

    def _fingerprint(
        self, session: Session, actor: User, title: str, due_at: datetime | None
    ) -> Fingerprint:
        """The acting user, the title as the duplicate check compares it, and the stored due_at."""
        owner = account_of(session, actor)
        stored_due = to_utc(due_at, owner.timezone) if due_at is not None else None
        return (actor_id(actor), fold_case(normalize_title(title)), stored_due)

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._issued and next(iter(self._issued.values())) < cutoff:
            del self._issued[next(iter(self._issued))]
        while len(self._issued) > self._max:
            del self._issued[next(iter(self._issued))]
