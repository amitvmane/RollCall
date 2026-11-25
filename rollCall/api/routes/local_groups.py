"""
Web-only groups: create one, join any group as a person without Telegram,
and (owners) choose who administers a group that keeps its own admin list.
Rules live in services/local_groups.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

import db as _db
from api.identity import identity_from_header, require_identity
from services import local_groups

router = APIRouter()


class CreateGroupRequest(BaseModel):
    name: str = Field(..., max_length=64)
    timezone: Optional[str] = Field(None, max_length=64)
    your_name: Optional[str] = Field(None, max_length=64, description="How the group sees you")


class CreateGroupResponse(BaseModel):
    chat_id: int
    group_token: str
    group_name: str


class JoinRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=64)


class JoinResponse(BaseModel):
    joined: bool
    name: Optional[str] = None


class AdminChangeRequest(BaseModel):
    user_id: int


class AdminChangeResponse(BaseModel):
    user_id: int
    role: Optional[str] = None


def _chat(group_token: str) -> int:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    return int(chat["chat_id"])


def _name_of(user_id: int, fallback: Optional[str]) -> str:
    from services import principals
    pid = principals.principal_for_user(user_id)
    return (fallback or "").strip() or (principals.display_name(pid) if pid else None) or "Organiser"


@router.post("/web/groups", response_model=CreateGroupResponse, status_code=status.HTTP_201_CREATED,
             summary="Create a web-only group (no Telegram chat) — you become its owner")
async def create_web_group(body: CreateGroupRequest,
                           id_token: Optional[str] = Depends(identity_from_header)) -> CreateGroupResponse:
    # A global token only: creating a group isn't an act within any one chat.
    uid = require_identity(id_token, detail="Sign in first to create a group.")
    return CreateGroupResponse(**local_groups.create_group(
        uid, _name_of(uid, body.your_name), body.name, body.timezone))


@router.post("/web/group/{group_token}/join", response_model=JoinResponse,
             summary="Join this group as yourself (for people without Telegram)")
async def join_group(body: JoinRequest, group_token: str = Path(...),
                     id_token: Optional[str] = Depends(identity_from_header)) -> JoinResponse:
    chat_id = _chat(group_token)
    uid = require_identity(id_token, detail="Sign in first to join.", chat_id=chat_id)
    return JoinResponse(**local_groups.join(chat_id, uid, body.name or _name_of(uid, None)))


async def _owner_call(fn, group_token: str, body: AdminChangeRequest, id_token: Optional[str]):
    chat_id = _chat(group_token)
    uid = require_identity(id_token, detail="Sign in first.", chat_id=chat_id)
    info = _db.get_member_display_info(chat_id, uid) or {}
    return AdminChangeResponse(**fn(chat_id, body.user_id, actor_user_id=uid,
                                    actor_name=info.get("first_name") or "owner"))


@router.post("/web/group/{group_token}/admins/grant", response_model=AdminChangeResponse,
             summary="Owners of a group with its own admin list: make a member an admin")
async def grant_admin(body: AdminChangeRequest, group_token: str = Path(...),
                      id_token: Optional[str] = Depends(identity_from_header)) -> AdminChangeResponse:
    return await _owner_call(local_groups.grant_admin, group_token, body, id_token)


@router.post("/web/group/{group_token}/admins/remove", response_model=AdminChangeResponse,
             summary="Owners of a group with its own admin list: take admin away")
async def remove_admin(body: AdminChangeRequest, group_token: str = Path(...),
                       id_token: Optional[str] = Depends(identity_from_header)) -> AdminChangeResponse:
    return await _owner_call(local_groups.remove_admin, group_token, body, id_token)
