"""Login, logout, the key exchange, and the account."""

from fastapi import APIRouter, Request, Response, status

from barrelvault.account import account_view, set_timezone
from barrelvault.api.deps import COOKIE, Actor, AppSettings, DbSession, presented_secret
from barrelvault.api.schemas import AccountOut, AccountPatch, ExchangeIn, LoginIn, TokenOut
from barrelvault.auth import exchange_key, login, logout, session_expires_at

router = APIRouter()


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def login_route(
    body: LoginIn, request: Request, response: Response, session: DbSession, settings: AppSettings
) -> None:
    secret = login(session, body.email, body.password, session_length=settings.session_length)
    response.set_cookie(
        COOKIE,
        secret,
        max_age=settings.session_length,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_route(request: Request, response: Response, session: DbSession, _actor: Actor) -> None:
    logout(session, presented_secret(request))
    response.delete_cookie(COOKIE, httponly=True, samesite="lax")


@router.post("/exchange")
def exchange_route(body: ExchangeIn, session: DbSession, settings: AppSettings) -> TokenOut:
    token = exchange_key(session, body.key, session_length=settings.session_length)
    return TokenOut(token=token, expires_at=session_expires_at(session, token))


@router.get("/account")
def account_route(session: DbSession, actor: Actor) -> AccountOut:
    view = account_view(session, actor)
    return AccountOut(name=view.name, kind=view.kind, email=view.email, timezone=view.timezone)


@router.patch("/account")
def account_patch_route(body: AccountPatch, session: DbSession, actor: Actor) -> AccountOut:
    view = set_timezone(session, actor, body.timezone)
    return AccountOut(name=view.name, kind=view.kind, email=view.email, timezone=view.timezone)
