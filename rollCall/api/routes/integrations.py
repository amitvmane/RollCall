"""
Integrations (web admins): partner app keys and webhooks for one group.
Rules live in services/partner_api.py and services/webhooks.py.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

import db as _db
from api.identity import identity_from_header
from api.routes.local_groups import _admin
from services import partner_api, webhooks

router = APIRouter()


class AppKey(BaseModel):
    key_id: str
    label: str
    scopes: List[str]
    created_at: str = ""
    expires_at: str = ""
    last_used_at: Optional[str] = None
    revoked: bool = False


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
    label: str = Field(..., max_length=60)
    scopes: List[str]
    days: int = Field(365, ge=1, le=partner_api.MAX_DAYS)


class CreateKeyResponse(BaseModel):
    key_id: str
    token: str = Field(..., description="Shown ONCE — store it now")
    label: str
    scopes: List[str]
    expires_at: str


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
    return CreateKeyResponse(**partner_api.create_key(chat_id, uid, name, body.label, body.scopes, body.days))


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
    chat_id, _, _ = await _actor(group_token, id_token)
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
