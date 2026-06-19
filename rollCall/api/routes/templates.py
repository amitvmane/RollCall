"""
Template + schedule routes.

All mutations require the 'admin' scope (templates drive rollcall creation
and scheduling, which is admin-only in the bot). Read access requires 'read'.
"""

from typing import List

from fastapi import APIRouter, Depends, Path, status

from services import templates as tmpl_svc

from api.auth import AuthedToken, require_scope
from api.schemas.rollcalls import RollcallResponse
from api.schemas.templates import (
    DeleteTemplateResponse,
    ScheduleResponse,
    SetScheduleRequest,
    StartTemplateRequest,
    TemplateResponse,
    ToggleScheduleRequest,
    UpsertTemplateRequest,
)


router = APIRouter()


@router.get(
    "/chats/{chat_id}/templates",
    response_model=List[TemplateResponse],
    summary="List all templates for a chat",
)
async def list_templates(
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[TemplateResponse]:
    return [TemplateResponse(**t) for t in tmpl_svc.list_templates(chat_id)]


@router.get(
    "/chats/{chat_id}/templates/{name}",
    response_model=TemplateResponse,
    summary="Get a single template by name",
)
async def get_template(
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> TemplateResponse:
    return TemplateResponse(**tmpl_svc.get_one_template(chat_id, name))


@router.put(
    "/chats/{chat_id}/templates/{name}",
    response_model=TemplateResponse,
    summary="Create or update a template (partial update supported)",
)
async def upsert_template(
    body: UpsertTemplateRequest,
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> TemplateResponse:
    result = tmpl_svc.upsert_template(
        chat_id=chat_id,
        name=name,
        admin_user_id=body.admin_user_id,
        admin_name=body.admin_name,
        title=body.title,
        limit=body.limit,
        location=body.location,
        fee=body.fee,
        offset_days=body.offset_days,
        offset_hours=body.offset_hours,
        offset_minutes=body.offset_minutes,
        event_day=body.event_day,
        event_time=body.event_time,
    )
    return TemplateResponse(**result)


@router.post(
    "/chats/{chat_id}/templates/{name}/start",
    response_model=RollcallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a rollcall from a template",
)
async def start_template(
    body: StartTemplateRequest,
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    result = await tmpl_svc.start_template(
        chat_id=chat_id,
        name=name,
        admin_user_id=body.admin_user_id,
        admin_name=body.admin_name,
        extra_title=body.extra_title,
    )
    return RollcallResponse(**result)


@router.delete(
    "/chats/{chat_id}/templates/{name}",
    response_model=DeleteTemplateResponse,
    summary="Delete a template",
)
async def delete_template(
    body: ToggleScheduleRequest,
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> DeleteTemplateResponse:
    return DeleteTemplateResponse(
        **tmpl_svc.delete_one_template(
            chat_id=chat_id,
            name=name,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    )


@router.get(
    "/chats/{chat_id}/templates/{name}/schedule",
    response_model=ScheduleResponse,
    summary="Get schedule info for a template",
)
async def get_schedule(
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> ScheduleResponse:
    return ScheduleResponse(**tmpl_svc.get_schedule(chat_id, name))


@router.put(
    "/chats/{chat_id}/templates/{name}/schedule",
    response_model=ScheduleResponse,
    summary="Set or update the auto-start schedule for a template",
)
async def set_schedule(
    body: SetScheduleRequest,
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> ScheduleResponse:
    return ScheduleResponse(
        **tmpl_svc.set_schedule(
            chat_id=chat_id,
            name=name,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
            recurrence_type=body.recurrence_type,
            schedule_day=body.schedule_day,
            schedule_time=body.schedule_time,
            monthly_day=body.monthly_day,
        )
    )


@router.delete(
    "/chats/{chat_id}/templates/{name}/schedule",
    response_model=ScheduleResponse,
    summary="Disable the auto-start schedule for a template",
)
async def disable_schedule(
    body: ToggleScheduleRequest,
    chat_id: int = Path(...),
    name: str = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> ScheduleResponse:
    return ScheduleResponse(
        **tmpl_svc.disable_schedule(
            chat_id=chat_id,
            name=name,
            admin_user_id=body.admin_user_id,
            admin_name=body.admin_name,
        )
    )
