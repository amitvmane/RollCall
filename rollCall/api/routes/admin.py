"""
Admin user-management routes.

DELETE /chats/{chat_id}/rollcalls/{rc_number}/users/{name}
  Remove a user (real or proxy) from any list in a rollcall.

PATCH  /chats/{chat_id}/rollcalls/{rc_number}/users/{name}/status
  Move a user to a different status list (in/out/maybe).

Both routes require the `admin` scope. rc_number in the URL is 1-based
(matching the display number users see); the service layer expects 0-based.
"""

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


router = APIRouter()


@router.delete(
    "/chats/{chat_id}/rollcalls/{rc_number}/users/{name}",
    response_model=DeleteUserResponse,
    summary="Remove a user from a rollcall",
)
async def delete_user(
    body: DeleteUserRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
    name: str = Path(..., description="Display name or @username of the user to remove"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> DeleteUserResponse:
    async with manager.get_chat_write_lock(chat_id):
        result = admin_svc.delete_user_from_rollcall(
            chat_id=chat_id,
            rc_number=rc_number - 1,
            name=name,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    return DeleteUserResponse(**result)


@router.patch(
    "/chats/{chat_id}/rollcalls/{rc_number}/users/{name}/status",
    response_model=SetStatusResponse,
    summary="Move a user to a different status list",
)
async def set_user_status(
    body: SetStatusRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
    name: str = Path(..., description="Display name or @username to match"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> SetStatusResponse:
    async with manager.get_chat_write_lock(chat_id):
        result = admin_svc.set_user_status(
            chat_id=chat_id,
            rc_number=rc_number - 1,
            name=name,
            new_status=body.new_status,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    return SetStatusResponse(**result)
