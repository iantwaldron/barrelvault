"""The database session, settings, and acting user each route depends on."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session

from barrelvault.auth import authenticate
from barrelvault.db import open_session
from barrelvault.errors import Invalid, Refused
from barrelvault.models import User
from barrelvault.settings import Settings
from barrelvault.surface import DuplicateWarnings

COOKIE = "barrelvault_session"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _session(request: Request) -> Iterator[Session]:
    # A read never writes, so it does not wait behind a writer for the lock.
    with open_session(immediate=request.method not in _SAFE_METHODS) as session:
        yield session


# scope="function" ends the session, and so commits, before the response is sent, so a
# failed commit is never reported to the client as a success.
DbSession = Annotated[Session, Depends(_session, scope="function")]


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _warnings(request: Request) -> DuplicateWarnings:
    warnings: DuplicateWarnings = request.app.state.duplicate_warnings
    return warnings


AppSettings = Annotated[Settings, Depends(_settings)]
Warnings = Annotated[DuplicateWarnings, Depends(_warnings)]


def presented_secret(request: Request) -> str:
    """The session secret a call presents: an agent's bearer token, or the person's cookie.

    A write that the cookie authenticates must send JSON, which a cross-site form cannot,
    so a page on another site cannot make the person's browser write here.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise Refused("refused")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.method not in _SAFE_METHODS and content_type != "application/json":
        raise Invalid("a write with the session cookie must send application/json")
    return cookie


def _actor(request: Request, session: DbSession) -> User:
    return authenticate(session, presented_secret(request))


Actor = Annotated[User, Depends(_actor)]
