"""
The signed-in person's own account (services/account.py) and the one-click
unsubscribe link in notification emails (services/email_notify.py).

Every account route takes a GLOBAL identity token only — a chat-bound
/weblogin token proves who someone is in one group, not who owns the account.
"""

import html
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from api.identity import identity_from_header, require_identity
from api.routes.email_auth import _ip, _require_enabled, _send
from services import account, email_auth, email_notify

router = APIRouter()

_SIGN_IN = "Sign in first."


class AccountResponse(BaseModel):
    user_id: int
    name: str = ""
    email: Optional[str] = None
    has_telegram: bool = False
    email_only: bool = False
    email_enabled: bool = False
    email_notify: Optional[str] = Field(None, description="'auto', 'always' or 'off' — email-only accounts")
    can_delete: bool = False


class EmailNotifyRequest(BaseModel):
    pref: str = Field(..., pattern="^(auto|always|off)$")


class ChangeEmailRequest(BaseModel):
    email: str = Field(..., max_length=254)
    group_token: Optional[str] = Field(None, max_length=64)


class SignOutEverywhereResponse(BaseModel):
    id_token: str = Field(..., description="A fresh sign-in for THIS device; every other one has ended")


class DeleteRequest(BaseModel):
    confirm: str = Field(..., description="Must be the word DELETE")


class DeleteResponse(BaseModel):
    groups: int


@router.get("/auth/account", response_model=AccountResponse, summary="Your account")
async def get_account(id_token: Optional[str] = Depends(identity_from_header)) -> AccountResponse:
    return AccountResponse(**account.summary(require_identity(id_token, detail=_SIGN_IN)))


@router.post("/auth/account/email-notify", response_model=AccountResponse,
             summary="When notification emails are sent (email-only accounts)")
async def set_email_notify(body: EmailNotifyRequest,
                           id_token: Optional[str] = Depends(identity_from_header)) -> AccountResponse:
    uid = require_identity(id_token, detail=_SIGN_IN)
    account.set_email_notify(uid, body.pref)
    return AccountResponse(**account.summary(uid))


@router.post("/auth/account/email/change", status_code=202,
             summary="Email a link to the NEW address; opening it on this device switches your sign-in email")
async def change_email(body: ChangeEmailRequest, request: Request,
                       id_token: Optional[str] = Depends(identity_from_header)) -> dict:
    _require_enabled()
    uid = require_identity(id_token, detail=_SIGN_IN)
    await _send(email_auth.start_change(uid, body.email, body.group_token, _ip(request)))
    return {"sent": True}


@router.post("/auth/account/sign-out-everywhere", response_model=SignOutEverywhereResponse,
             summary="End every other sign-in of this account")
async def sign_out_everywhere(id_token: Optional[str] = Depends(identity_from_header)) -> SignOutEverywhereResponse:
    uid = require_identity(id_token, detail=_SIGN_IN)
    return SignOutEverywhereResponse(id_token=account.sign_out_everywhere(uid))


@router.post("/auth/account/delete", response_model=DeleteResponse,
             summary="Delete an email-only account (past votes and money records stay in each group's history)")
async def delete_account(body: DeleteRequest,
                         id_token: Optional[str] = Depends(identity_from_header)) -> DeleteResponse:
    uid = require_identity(id_token, detail=_SIGN_IN)
    if body.confirm.strip().upper() != "DELETE":
        from exceptions import incorrectParameter
        raise incorrectParameter("Type DELETE to confirm.")
    return DeleteResponse(**account.delete(uid))


# ── Unsubscribe (link in every notification email) ───────────────────────────
# GET shows a button and changes nothing — mail scanners open links. POST
# turns emails off: the button, and mail apps' one-click unsubscribe (RFC 8058).

_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/><title>RollCall — emails</title>
<link rel="stylesheet" href="/shared/tokens.css"/><link rel="stylesheet" href="/shared/base.css"/>
<style>body{{background:var(--bg);color:var(--text);font-family:var(--font-body);display:flex;
justify-content:center;padding:48px 16px}}main{{max-width:420px;text-align:center}}
button{{padding:12px 20px;border-radius:10px;border:0;background:var(--accent);color:#fff;font-size:1rem}}</style>
</head><body><main>{body}</main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status)


@router.get("/auth/email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe_page(t: str = Query("", max_length=100)) -> HTMLResponse:
    if not email_notify.verify_unsubscribe(t):
        return _page("<h2>This link isn't valid</h2><p>Change email settings from your account on the RollCall page.</p>", 400)
    tok = html.escape(t, quote=True)
    return _page(f"<h2>Stop RollCall emails?</h2><p>You'll stop getting game and reminder emails. "
                 f"Sign-in links still arrive when you ask for one.</p>"
                 f'<form method="post" action="/api/v1/auth/email/unsubscribe?t={tok}">'
                 f'<button type="submit">Unsubscribe</button></form>')


@router.post("/auth/email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe(t: str = Query("", max_length=100)) -> HTMLResponse:
    pid = email_notify.verify_unsubscribe(t)
    if not pid:
        return _page("<h2>This link isn't valid</h2>", 400)
    email_notify.set_pref(pid, email_notify.OFF)
    return _page("<h2>Unsubscribed</h2><p>No more RollCall notification emails. You can turn them back on "
                 "from your account on the RollCall page.</p>")
