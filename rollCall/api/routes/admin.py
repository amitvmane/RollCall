"""
Admin user-management routes.

DELETE /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/users/{name}
  Remove a user (real or proxy) from any list in a rollcall.

PATCH  /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/users/{name}/status
  Move a user to a different status list (in/out/maybe).

Both require the `admin` scope. The older /rollcalls/{rc_number}/... paths
(1-based display number) still work but are deprecated — api/rollcall_ref.py.
"""

import logging

from fastapi import APIRouter, Depends, Path

from rollcall_manager import manager
from services import admin as admin_svc

from api.auth import AuthedToken, require_scope
from api.schemas.admin import (
    DeleteUserRequest,
    DeleteUserResponse,
    SetStatusRequest,
    SetStatusResponse,
)
from api.rollcall_ref import RcRef, dual, rollcall_ref
from api.telegram_mirror import mirror_panel_to_telegram, send_promotion_notification


router = APIRouter()


@dual(router.delete, "/chats/{chat_id}/rollcalls/{rc_number}/users/{name}",
      response_model=DeleteUserResponse, summary="Remove a user from a rollcall")
async def delete_user(
    body: DeleteUserRequest,
    _token: AuthedToken = Depends(require_scope("admin")),
    ref: RcRef = Depends(rollcall_ref),
    name: str = Path(..., description="Display name or @username of the user to remove"),
) -> DeleteUserResponse:
    chat_id = ref.chat_id
    logging.info("[api] delete_user chat=%d rc_id=%d name=%r by %s", chat_id, ref.rollcall_id, name, body.admin_name)
    async with manager.get_chat_write_lock(chat_id):
        result = admin_svc.delete_user_from_rollcall(
            chat_id=chat_id,
            rc_number=ref.index_now(),   # by id, inside the lock
            name=name,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    number = ref.number_now()
    await send_promotion_notification(chat_id, result.get("promoted") or [], number)
    await mirror_panel_to_telegram(chat_id, number)
    return DeleteUserResponse(**result)


@dual(router.patch, "/chats/{chat_id}/rollcalls/{rc_number}/users/{name}/status",
      response_model=SetStatusResponse, summary="Move a user to a different status list")
async def set_user_status(
    body: SetStatusRequest,
    _token: AuthedToken = Depends(require_scope("admin")),
    ref: RcRef = Depends(rollcall_ref),
    name: str = Path(..., description="Display name or @username to match"),
) -> SetStatusResponse:
    chat_id = ref.chat_id
    logging.info("[api] set_user_status chat=%d rc_id=%d name=%r → %s by %s", chat_id, ref.rollcall_id, name, body.new_status, body.admin_name)
    async with manager.get_chat_write_lock(chat_id):
        result = admin_svc.set_user_status(
            chat_id=chat_id,
            rc_number=ref.index_now(),   # by id, inside the lock
            name=name,
            new_status=body.new_status,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    number = ref.number_now()
    await send_promotion_notification(chat_id, result.get("promoted") or [], number)
    await mirror_panel_to_telegram(chat_id, number)
    return SetStatusResponse(**result)
