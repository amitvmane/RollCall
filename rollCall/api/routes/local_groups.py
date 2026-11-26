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
    pending: bool = False     # the group approves its members; an admin will decide
    denied: bool = False      # an admin already said no; asking again changes nothing
    name: Optional[str] = None


class MembershipResponse(BaseModel):
    member: bool
    pending: bool = False
    denied: bool = False
    needs_approval: bool = False
    can_join: bool = False
    can_leave: bool = False
    name: Optional[str] = None


class RemovableMember(BaseModel):
    user_id: int
    name: str
    role: Optional[str] = None


class RemovableMembersResponse(BaseModel):
    members: list[RemovableMember]


class RemoveMemberResponse(BaseModel):
    user_id: int
    removed: bool
    name: Optional[str] = None


class JoinRequestEntry(BaseModel):
    user_id: int
    name: Optional[str] = None
    requested_at: int


class JoinRequestsResponse(BaseModel):
    requests: list[JoinRequestEntry]


class JoinDecisionRequest(BaseModel):
    user_id: int
    approve: bool


class JoinDecisionResponse(BaseModel):
    user_id: int
    approved: bool
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


@router.get("/web/group/{group_token}/membership", response_model=MembershipResponse,
            summary="Where you stand in this group: member, waiting for approval, or able to join")
async def get_membership(group_token: str = Path(...),
                         id_token: Optional[str] = Depends(identity_from_header)) -> MembershipResponse:
    chat_id = _chat(group_token)
    uid = require_identity(id_token, detail="Sign in first.", chat_id=chat_id)
    return MembershipResponse(**local_groups.membership(chat_id, uid))


@router.post("/web/group/{group_token}/leave", status_code=204,
             summary="Leave this group (people without Telegram; Telegram members leave the Telegram chat)")
async def leave_group(group_token: str = Path(...),
                      id_token: Optional[str] = Depends(identity_from_header)) -> None:
    chat_id = _chat(group_token)
    uid = require_identity(id_token, detail="Sign in first.", chat_id=chat_id)
    local_groups.leave(chat_id, uid)


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


async def _admin(group_token: str, id_token: Optional[str]) -> tuple[int, int]:
    from api.web_admin import check_web_admin_live
    chat_id = _chat(group_token)
    uid = require_identity(id_token, detail="Sign in first.", chat_id=chat_id)
    if not await check_web_admin_live(chat_id, uid):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    return chat_id, uid


@router.get("/web/group/{group_token}/join-requests", response_model=JoinRequestsResponse,
            summary="People waiting to join (admins; groups that approve their members)")
async def list_join_requests(group_token: str = Path(...),
                             id_token: Optional[str] = Depends(identity_from_header)) -> JoinRequestsResponse:
    chat_id, _ = await _admin(group_token, id_token)
    return JoinRequestsResponse(requests=[JoinRequestEntry(**r) for r in local_groups.list_requests(chat_id)])


@router.post("/web/group/{group_token}/join-requests/decide", response_model=JoinDecisionResponse,
             summary="Approve or deny a join request (admins)")
async def decide_join_request(body: JoinDecisionRequest, group_token: str = Path(...),
                              id_token: Optional[str] = Depends(identity_from_header)) -> JoinDecisionResponse:
    chat_id, uid = await _admin(group_token, id_token)
    info = _db.get_member_display_info(chat_id, uid) or {}
    return JoinDecisionResponse(**local_groups.decide_request(
        chat_id, body.user_id, body.approve, actor_user_id=uid, actor_name=info.get("first_name") or "admin"))



@router.get("/web/group/{group_token}/members/removable", response_model=RemovableMembersResponse,
            summary="Members an admin can remove from the page (people without Telegram; everyone in a web-only group)")
async def removable_members(group_token: str = Path(...),
                            id_token: Optional[str] = Depends(identity_from_header)) -> RemovableMembersResponse:
    chat_id, _ = await _admin(group_token, id_token)
    return RemovableMembersResponse(members=[RemovableMember(**m) for m in local_groups.removable_members(chat_id)])


@router.post("/web/group/{group_token}/members/remove", response_model=RemoveMemberResponse,
             summary="Remove a member (admins) — past votes and money records stay in the group's history")
async def remove_member(body: AdminChangeRequest, group_token: str = Path(...),
                        id_token: Optional[str] = Depends(identity_from_header)) -> RemoveMemberResponse:
    chat_id, uid = await _admin(group_token, id_token)
    info = _db.get_member_display_info(chat_id, uid) or {}
    return RemoveMemberResponse(**local_groups.remove_member(
        chat_id, body.user_id, actor_user_id=uid, actor_name=info.get("first_name") or "admin"))


class AccountMergeRequest(BaseModel):
    from_user_id: int
    into_user_id: int


class AccountMergeResponse(BaseModel):
    groups: int
    dues_groups: int


@router.post("/web/group/{group_token}/accounts/merge", response_model=AccountMergeResponse,
             summary="Owners: merge an email-only account (only ever in this group) into another member")
async def merge_accounts_in_group(body: AccountMergeRequest, group_token: str = Path(...),
                                  id_token: Optional[str] = Depends(identity_from_header)) -> AccountMergeResponse:
    from services.account_merge import merge_in_group
    chat_id, uid = await _admin(group_token, id_token)
    info = _db.get_member_display_info(chat_id, uid) or {}
    r = await merge_in_group(chat_id, body.from_user_id, body.into_user_id,
                             actor_user_id=uid, actor_name=info.get("first_name") or "owner")
    return AccountMergeResponse(groups=r["groups"], dues_groups=r["dues_groups"])



class BuzzMutedEntry(BaseModel):
    user_id: int
    name: str
    unanswered: int


class BuzzMutedResponse(BaseModel):
    after: int
    muted: list[BuzzMutedEntry]


@router.get("/web/group/{group_token}/buzz-muted", response_model=BuzzMutedResponse,
            summary="Members skipped by buzzes for not answering them (admins) — same as /buzz_mute")
async def buzz_muted(group_token: str = Path(...),
                     id_token: Optional[str] = Depends(identity_from_header)) -> BuzzMutedResponse:
    from services import buzz_mute
    chat_id, _ = await _admin(group_token, id_token)
    return BuzzMutedResponse(after=buzz_mute.threshold(chat_id),
                             muted=[BuzzMutedEntry(**m) for m in buzz_mute.muted_members(chat_id)])


@router.post("/web/group/{group_token}/buzz-muted/unmute", status_code=204,
             summary="Ping a skipped member again (admins) — same as /buzz_unmute")
async def buzz_unmute(body: AdminChangeRequest, group_token: str = Path(...),
                      id_token: Optional[str] = Depends(identity_from_header)) -> None:
    from services import buzz_mute
    chat_id, uid = await _admin(group_token, id_token)
    info = _db.get_member_display_info(chat_id, uid) or {}
    buzz_mute.unmute(chat_id, body.user_id, uid, info.get("first_name") or "admin")
