"""Login, logout, the key exchange, and the account."""

from fastapi import APIRouter, Request, Response, status

from barrelvault.account import account_view, actor_id, set_timezone
from barrelvault.api.deps import COOKIE, Actor, AppSettings, DbSession
from barrelvault.api.schemas import (
    AccountOut,
    AccountPatch,
    ExchangeIn,
    LoginIn,
    TokenOut,
    errors,
)
from barrelvault.auth import (
    check_password,
    delete_session,
    exchange_key,
    session_expires_at,
    start_session,
)
from barrelvault.db import open_session

# Each route checks its input. Login and the exchange are refused without a session.
router = APIRouter(responses=errors(422))
_SESSION_ERRORS = errors(401)


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT, responses=_SESSION_ERRORS)
def login_route(body: LoginIn, request: Request, response: Response, settings: AppSettings) -> None:
    # The password check runs in a read session and only the mint takes the write lock,
    # so a burst of bad logins never holds up another call.
    with open_session(immediate=False) as session:
        user_id = check_password(session, body.email, body.password)
    with open_session() as session:
        secret = start_session(session, user_id, session_length=settings.session_length)
    response.set_cookie(
        COOKIE,
        secret,
        max_age=settings.session_length,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses=_SESSION_ERRORS)
def logout_route(response: Response, session: DbSession, actor: Actor) -> None:
    delete_session(session, actor_id(actor))
    response.delete_cookie(COOKIE, httponly=True, samesite="lax")


@router.post("/exchange", responses=_SESSION_ERRORS)
def exchange_route(body: ExchangeIn, session: DbSession, settings: AppSettings) -> TokenOut:
    token = exchange_key(session, body.key, session_length=settings.session_length)
    return TokenOut(token=token, expires_at=session_expires_at(session, token))


@router.get("/account", responses=_SESSION_ERRORS)
def account_route(session: DbSession, actor: Actor) -> AccountOut:
    view = account_view(session, actor)
    return AccountOut(name=view.name, kind=view.kind, email=view.email, timezone=view.timezone)


@router.patch("/account", responses=errors(401, 403))
def account_patch_route(body: AccountPatch, session: DbSession, actor: Actor) -> AccountOut:
    view = set_timezone(session, actor, body.timezone)
    return AccountOut(name=view.name, kind=view.kind, email=view.email, timezone=view.timezone)
