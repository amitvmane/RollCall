"""
Integrations (web admins): partner app keys and webhooks for one group.
Rules live in services/partner_api.py and services/webhooks.py.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

import db as _db
from api.identity import identity_from_header
from api.routes.local_groups import _admin
from api.auth import AuthedToken, require_scope
from services import api_usage, partner_api, webhooks

router = APIRouter()


class AppKey(BaseModel):
    key_id: str
    kind: str = Field("app", description="'app' (made here) or 'personal' (an admin's /gentoken)")
    label: str
    scopes: List[str]
    created_at: str = ""
    expires_at: str = ""
    last_used_at: Optional[str] = None
    last_used_ip: Optional[str] = None
    revoked: bool = False
    status: str = Field("active", description="'active', 'expired' or 'revoked'")
    partner: Optional[str] = None
    contact: Optional[str] = None
    agreement_ref: Optional[str] = None
    allowed_ips: List[str] = Field(default_factory=list)
    issued_by_user_id: Optional[int] = None
    rotated_to: Optional[str] = None


class Webhook(BaseModel):
    id: int
    url: str
    events: List[str]
    active: bool
    failures: int = 0
    created_at: int = 0
    last_status: Optional[str] = None
    last_delivery_at: Optional[int] = None


class IntegrationsResponse(BaseModel):
    keys: List[AppKey]
    webhooks: List[Webhook]
    events: List[str]
    scopes: List[str]
    you_are_owner: bool = False


class CreateKeyRequest(BaseModel):
    label: str = Field(..., max_length=60, description="The app this key is for")
    scopes: List[str]
    days: int = Field(365, ge=1, le=partner_api.MAX_DAYS)
    partner: Optional[str] = Field(None, max_length=100, description="The organisation the key is issued to")
    contact: Optional[str] = Field(None, max_length=254, description="Who to contact there (email or phone)")
    agreement_ref: Optional[str] = Field(None, max_length=100,
                                         description="Reference of the agreement made off the platform")
    allowed_ips: Optional[List[str]] = Field(None, description="Only these addresses / CIDR ranges may use the key")


class CreateKeyResponse(BaseModel):
    key_id: str
    token: str = Field(..., description="Shown ONCE — store it now")
    label: str
    scopes: List[str]
    expires_at: str
    partner: Optional[str] = None
    contact: Optional[str] = None
    agreement_ref: Optional[str] = None
    allowed_ips: List[str] = Field(default_factory=list)


class RotateKeyRequest(BaseModel):
    days: Optional[int] = Field(None, ge=1, le=partner_api.MAX_DAYS,
                                description="Lifetime of the new key (default: same as the old one)")


class RotateKeyResponse(CreateKeyResponse):
    previous_key_id: str
    previous_expires_at: str = Field(..., description="The old key keeps working until then")


class CreateWebhookRequest(BaseModel):
    url: str = Field(..., max_length=500)
    events: List[str] = Field(default_factory=lambda: list(webhooks.EVENTS))


class CreateWebhookResponse(Webhook):
    secret: str = Field(..., description="Shown ONCE — used to verify X-RollCall-Signature")


class WebhookActiveRequest(BaseModel):
    active: bool


async def _actor(group_token: str, id_token: Optional[str]):
    chat_id, uid = await _admin(group_token, id_token)
    name = (_db.get_member_display_info(chat_id, uid) or {}).get("first_name") or "admin"
    return chat_id, uid, name


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(e))


@router.get("/web/group/{group_token}/integrations", response_model=IntegrationsResponse,
            summary="App keys and webhooks for this group (web admins)")
async def list_integrations(group_token: str = Path(...),
                            id_token: Optional[str] = Depends(identity_from_header)) -> IntegrationsResponse:
    chat_id, uid, _ = await _actor(group_token, id_token)
    return IntegrationsResponse(
        keys=[AppKey(**k) for k in partner_api.list_keys(chat_id)],
        webhooks=[Webhook(**w) for w in webhooks.list_endpoints(chat_id)],
        events=list(webhooks.EVENTS), scopes=list(partner_api.SCOPES),
        you_are_owner=_db.get_web_admin_role(chat_id, uid) == "owner")


@router.post("/web/group/{group_token}/api-keys", response_model=CreateKeyResponse,
             summary="Make an app key (the token is shown once)")
async def create_key(body: CreateKeyRequest, group_token: str = Path(...),
                     id_token: Optional[str] = Depends(identity_from_header)) -> CreateKeyResponse:
    chat_id, uid, name = await _actor(group_token, id_token)
    return CreateKeyResponse(**partner_api.create_key(
        chat_id, uid, name, body.label, body.scopes, body.days, partner=body.partner,
        contact=body.contact, agreement_ref=body.agreement_ref, allowed_ips=body.allowed_ips))


@router.post("/web/group/{group_token}/api-keys/{key_id}/rotate", response_model=RotateKeyResponse,
             summary="Rotate an app key: a new key on the same terms; the old one works for 7 more days")
async def rotate_key(body: RotateKeyRequest, group_token: str = Path(...),
                     key_id: str = Path(..., max_length=32),
                     id_token: Optional[str] = Depends(identity_from_header)) -> RotateKeyResponse:
    chat_id, uid, name = await _actor(group_token, id_token)
    return RotateKeyResponse(**partner_api.rotate_key(chat_id, key_id, uid, name, body.days))


@router.post("/web/group/{group_token}/api-keys/{key_id}/revoke", status_code=204,
             summary="Revoke an app key")
async def revoke_key(group_token: str = Path(...), key_id: str = Path(..., max_length=32),
                     id_token: Optional[str] = Depends(identity_from_header)) -> None:
    chat_id, uid, name = await _actor(group_token, id_token)
    partner_api.revoke_key(chat_id, key_id, uid, name)


@router.post("/web/group/{group_token}/webhooks", response_model=CreateWebhookResponse,
             summary="Add a webhook (the signing secret is shown once)")
async def create_webhook(body: CreateWebhookRequest, group_token: str = Path(...),
                         id_token: Optional[str] = Depends(identity_from_header)) -> CreateWebhookResponse:
    chat_id, uid, name = await _actor(group_token, id_token)
    # Owners only: a webhook streams every vote and dues entry to an outside
    # address — and in an open Telegram group every member is a web admin.
    if _db.get_web_admin_role(chat_id, uid) != "owner":
        raise HTTPException(status_code=403, detail="Only a group owner can add a webhook.")
    try:
        ep = webhooks.create_endpoint(chat_id, body.url, body.events, uid)
    except webhooks.WebhookRejected as e:
        raise _bad(e)
    _db.log_admin_action(chat_id, uid, name, "create_webhook", target_name=ep["url"])
    return CreateWebhookResponse(**ep)


@router.post("/web/group/{group_token}/webhooks/{webhook_id}/delete", status_code=204,
             summary="Delete a webhook")
async def delete_webhook(group_token: str = Path(...), webhook_id: int = Path(...),
                         id_token: Optional[str] = Depends(identity_from_header)) -> None:
    chat_id, uid, name = await _actor(group_token, id_token)
    try:
        webhooks.delete_endpoint(chat_id, webhook_id)
    except webhooks.WebhookRejected as e:
        raise _bad(e)
    _db.log_admin_action(chat_id, uid, name, "delete_webhook", target_name=str(webhook_id))


@router.post("/web/group/{group_token}/webhooks/{webhook_id}/active", status_code=204,
             summary="Switch a webhook on or off")
async def webhook_active(body: WebhookActiveRequest, group_token: str = Path(...), webhook_id: int = Path(...),
                         id_token: Optional[str] = Depends(identity_from_header)) -> None:
    chat_id, uid, _ = await _actor(group_token, id_token)
    # Any admin may switch one OFF (less goes out); switching one back ON
    # resumes streaming to an outside address, which is an owner's call — the
    # same rule as adding it.
    if body.active and _db.get_web_admin_role(chat_id, uid) != "owner":
        raise HTTPException(status_code=403, detail="Only a group owner can switch a webhook back on.")
    try:
        webhooks.set_active(chat_id, webhook_id, body.active)
    except webhooks.WebhookRejected as e:
        raise _bad(e)


@router.post("/web/group/{group_token}/webhooks/{webhook_id}/test", status_code=202,
             summary="Send a 'ping' event to a webhook")
async def test_webhook(group_token: str = Path(...), webhook_id: int = Path(...),
                       id_token: Optional[str] = Depends(identity_from_header)) -> dict:
    chat_id, _, _ = await _actor(group_token, id_token)
    try:
        queued = webhooks.send_test(chat_id, webhook_id)
    except webhooks.WebhookRejected as e:
        raise _bad(e)
    return {"queued": queued}


# ── Usage dashboard ──────────────────────────────────────────────────────────

class UsageNumbers(BaseModel):
    requests: int = 0
    errors: int = Field(0, description="4xx (except 429) and 5xx answers")
    server_errors: int = 0
    rate_limited: int = Field(0, description="429 answers")
    error_rate: float = Field(0.0, description="errors as a % of requests")
    avg_ms: int = Field(0, description="average time to answer, milliseconds")


class UsageDay(UsageNumbers):
    date: str = Field(..., description="UTC day, YYYY-MM-DD")


class UsageRoute(UsageNumbers):
    route: str = Field(..., description="Method and endpoint template")


class UsageKey(UsageNumbers):
    key_id: str
    kind: Optional[str] = None
    label: Optional[str] = None
    partner: Optional[str] = None
    status: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None
    last_used_ip: Optional[str] = None


class UsageWebhook(BaseModel):
    id: int
    url: str
    active: bool
    delivered: int = 0
    failed: int = 0
    pending: int = 0


class GroupUsageResponse(BaseModel):
    days: int
    totals: UsageNumbers
    daily: List[UsageDay]
    top_routes: List[UsageRoute]
    keys: List[UsageKey]
    webhooks: List[UsageWebhook]


class KeyTerms(BaseModel):
    key_id: Optional[str] = None
    kind: Optional[str] = None
    label: Optional[str] = None
    partner: Optional[str] = None
    agreement_ref: Optional[str] = None
    scopes: Optional[List[str]] = None
    status: Optional[str] = None
    expires_at: Optional[str] = None
    allowed_ips: Optional[List[str]] = None
    last_used_at: Optional[str] = None


class RateLimitTerms(BaseModel):
    requests: int
    window_seconds: int


class KeyUsageResponse(BaseModel):
    days: int
    key: KeyTerms
    rate_limit: RateLimitTerms
    totals: UsageNumbers
    daily: List[UsageDay]
    top_routes: List[UsageRoute]


@router.get("/web/group/{group_token}/integrations/usage", response_model=GroupUsageResponse,
            summary="API usage dashboard: requests, errors, rate limits per key and endpoint, webhook deliveries (web admins)")
async def group_usage(group_token: str = Path(...), days: int = Query(7, ge=1, le=90),
                      id_token: Optional[str] = Depends(identity_from_header)) -> GroupUsageResponse:
    chat_id, _, _ = await _actor(group_token, id_token)
    return GroupUsageResponse(**api_usage.group_usage(chat_id, days))


@router.get("/chats/{chat_id}/usage", response_model=KeyUsageResponse,
            summary="Your key's own usage and terms (any scope)")
async def key_usage(chat_id: int = Path(...), days: int = Query(7, ge=1, le=90),
                    token: AuthedToken = Depends(require_scope("any"))) -> KeyUsageResponse:
    """What this key has done in the last 1, 7, 30 or 90 days (the nearest of
    those to `days`), by day and by endpoint, with its expiry, scopes, IP
    allowlist and rate limit — so a partner can watch its own integration.
    Only ever this key: another partner's traffic is never shown."""
    return KeyUsageResponse(**api_usage.usage_for_key(chat_id, token.token_hash, days))
