"""
Email sign-in and email linking — see services/email_auth.py for the rules.

Every route that changes an account takes a GLOBAL identity token only
(verify_identity_token with no chat_id): a chat-bound /weblogin token proves
who someone is in one group, and an email login works everywhere.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from api.identity import identity_from_header, issue_identity_token, require_identity
from exceptions import incorrectParameter, insufficientPermissions, rateLimited
from services import email_auth

router = APIRouter()


class EmailConfigResponse(BaseModel):
    enabled: bool


class EmailStartRequest(BaseModel):
    email: str = Field(..., max_length=254)
    name: Optional[str] = Field(None, max_length=64, description="Shown to the group; used on first sign-in")
    group_token: Optional[str] = Field(None, max_length=64, description="Group page to come back to")


class EmailLinkRequest(BaseModel):
    email: str = Field(..., max_length=254)
    group_token: Optional[str] = Field(None, max_length=64)


class EmailVerifyRequest(BaseModel):
    code: str = Field(..., max_length=200)


class EmailVerifyResponse(BaseModel):
    id_token: str
    user_id: int
    name: str
    email: str
    purpose: str = Field(..., description="'login', 'link', or 'merge_offer' (the email has its own account)")
    group_token: Optional[str] = None
    # merge_offer only: the email-only account found behind this address.
    merge_from_user_id: Optional[int] = None
    merge_from_name: Optional[str] = None
    merge_token: Optional[str] = None
    merge_votes: Optional[int] = None
    merge_groups: Optional[int] = None


class EmailMergeRequest(BaseModel):
    from_user_id: int
    merge_token: str = Field(..., max_length=300)


class EmailMergeResponse(BaseModel):
    groups: int
    dues_groups: int
    email: Optional[str] = None


class EmailStatusResponse(BaseModel):
    enabled: bool
    email: Optional[str] = None
    can_unlink: bool = False
    has_telegram: bool = False


def _require_enabled() -> None:
    if not email_auth.enabled():
        raise HTTPException(status_code=503, detail="Email sign-in isn't set up on this server.")


async def _send(coro) -> None:
    try:
        await coro
    except rateLimited as e:
        raise HTTPException(status_code=429, detail=str(e))
    except email_auth.EmailNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (incorrectParameter, insufficientPermissions):
        raise   # curated (bad address, not signed in): the app's handler words it
    except Exception:
        logging.exception("[email_auth] sending failed")
        raise HTTPException(status_code=502, detail="Couldn't send the email — try again in a minute.")


@router.get("/auth/email/config", response_model=EmailConfigResponse,
            summary="Whether this server offers email sign-in")
async def email_config() -> EmailConfigResponse:
    return EmailConfigResponse(enabled=email_auth.enabled())


@router.post("/auth/email/start", status_code=status.HTTP_202_ACCEPTED,
             summary="Email a one-time sign-in link (same answer whether or not the address has an account)")
async def email_start(body: EmailStartRequest) -> dict:
    _require_enabled()
    await _send(email_auth.start_login(body.email, body.name, body.group_token))
    return {"sent": True}


@router.post("/auth/email/link", status_code=status.HTTP_202_ACCEPTED,
             summary="Telegram members: email a link that adds this address as a sign-in")
async def email_link(body: EmailLinkRequest,
                     id_token: Optional[str] = Depends(identity_from_header)) -> dict:
    _require_enabled()
    uid = require_identity(id_token, detail="Sign in with Telegram on this device first.")
    await _send(email_auth.start_link(uid, body.email, body.group_token))
    return {"sent": True}


@router.post("/auth/email/verify", response_model=EmailVerifyResponse,
             summary="Redeem a mailed code for an identity token")
async def email_verify(body: EmailVerifyRequest,
                       id_token: Optional[str] = Depends(identity_from_header)) -> EmailVerifyResponse:
    # Who is clicking matters only for a link code: it must be the Telegram
    # account that asked for it (services.email_auth.redeem). Global only.
    from api.identity import verify_identity_token
    caller = verify_identity_token(id_token) if id_token else None
    result = email_auth.redeem(body.code, caller)
    return EmailVerifyResponse(id_token=issue_identity_token(result["user_id"]), **result)


@router.post("/auth/email/merge", response_model=EmailMergeResponse,
             summary="Merge the email-only account found by a link into your Telegram account")
async def email_merge(body: EmailMergeRequest,
                      id_token: Optional[str] = Depends(identity_from_header)) -> EmailMergeResponse:
    uid = require_identity(id_token, detail="Sign in with Telegram on this device first.")
    name = email_auth.telegram_name(uid) or "member"
    return EmailMergeResponse(**await email_auth.merge_accounts(uid, body.from_user_id, body.merge_token, name))


@router.get("/auth/email/me", response_model=EmailStatusResponse,
            summary="The email linked to the signed-in account")
async def email_me(id_token: Optional[str] = Depends(identity_from_header)) -> EmailStatusResponse:
    uid = require_identity(id_token, detail="Sign in first.")
    return EmailStatusResponse(**email_auth.status(uid))


@router.post("/auth/email/unlink", status_code=status.HTTP_204_NO_CONTENT,
             summary="Remove the email sign-in from a Telegram member's account")
async def email_unlink(id_token: Optional[str] = Depends(identity_from_header)) -> None:
    uid = require_identity(id_token, detail="Sign in first.")
    email_auth.unlink(uid)
