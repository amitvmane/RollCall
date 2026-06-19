"""Pydantic models for template + schedule routes."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .rollcalls import RollcallResponse


class TemplateResponse(BaseModel):
    name: str
    title: Optional[str] = None
    limit: Optional[int] = None
    location: Optional[str] = None
    fee: Optional[str] = None
    offset_days: Optional[int] = None
    offset_hours: Optional[int] = None
    offset_minutes: Optional[int] = None
    event_day: Optional[str] = None
    event_time: Optional[str] = None
    schedule_day: Optional[str] = None
    schedule_time: Optional[str] = None
    schedule_enabled: bool = False
    recurrence_type: str = "weekly"
    last_scheduled_date: Optional[str] = None


class UpsertTemplateRequest(BaseModel):
    admin_user_id: int
    admin_name: str
    title: Optional[str] = None
    limit: Optional[int] = None
    location: Optional[str] = None
    fee: Optional[str] = None
    offset_days: Optional[int] = None
    offset_hours: Optional[int] = None
    offset_minutes: Optional[int] = None
    event_day: Optional[str] = Field(
        None,
        description="Full weekday name (e.g. 'friday') for auto-close"
    )
    event_time: Optional[str] = Field(
        None,
        description="HH:MM for auto-close time"
    )


class StartTemplateRequest(BaseModel):
    admin_user_id: int
    admin_name: str
    extra_title: Optional[str] = Field(
        None,
        description="Optional suffix appended to the template's base title"
    )


class DeleteTemplateResponse(BaseModel):
    name: str
    deleted: bool


class ScheduleResponse(BaseModel):
    name: str
    schedule_day: Optional[str] = None
    schedule_time: Optional[str] = None
    schedule_enabled: bool = False
    recurrence_type: str = "weekly"
    last_scheduled_date: Optional[str] = None


class SetScheduleRequest(BaseModel):
    admin_user_id: int
    admin_name: str
    recurrence_type: Literal["weekly", "biweekly", "monthly"] = "weekly"
    schedule_day: Optional[str] = Field(
        None,
        description="Full weekday name for weekly/biweekly. Required unless recurrence_type=monthly."
    )
    schedule_time: Optional[str] = Field(
        None,
        description="HH:MM. Required."
    )
    monthly_day: Optional[int] = Field(
        None,
        ge=1, le=31,
        description="Day of month (1-31). Required when recurrence_type=monthly."
    )


class ToggleScheduleRequest(BaseModel):
    admin_user_id: int
    admin_name: str
