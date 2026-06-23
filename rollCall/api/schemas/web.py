"""Schemas for magic-link web voting endpoints."""
from typing import Literal, Optional
from pydantic import BaseModel, Field


class WebUser(BaseModel):
    name: str
    comment: str = ""
    is_proxy: bool = True


class WebRollcallResponse(BaseModel):
    rollcall_id: int
    web_token: str = ""
    title: str
    finalize_date: Optional[str] = None
    finalize_epoch: Optional[float] = None
    limit: Optional[int] = None
    location: Optional[str] = None
    in_list: list[WebUser] = Field(default_factory=list, alias="in")
    out_list: list[WebUser] = Field(default_factory=list, alias="out")
    maybe_list: list[WebUser] = Field(default_factory=list, alias="maybe")
    waiting_list: list[WebUser] = Field(default_factory=list, alias="waiting")

    model_config = {"populate_by_name": True}


class UpcomingRollcall(BaseModel):
    name: str
    title: Optional[str] = None
    schedule_day: Optional[str] = None
    schedule_time: Optional[str] = None
    recurrence_type: str = "weekly"
    event_day: Optional[str] = None
    event_time: Optional[str] = None
    location: Optional[str] = None
    fee: Optional[str] = None
    limit: Optional[int] = None


class WebGroupResponse(BaseModel):
    group_token: str
    rollcalls: list[WebRollcallResponse]
    upcoming: list[UpcomingRollcall] = Field(default_factory=list)


class WebVoteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Display name for the voter")
    vote: Literal["in", "out", "maybe"]
    tg_user_id: Optional[int] = Field(None, description="Telegram user_id when voting from inside Telegram WebApp")
    comment: Optional[str] = Field(None, max_length=100, description="Optional note to attach to the vote")
