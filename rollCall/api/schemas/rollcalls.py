"""Pydantic models for rollcall lifecycle endpoints."""

from typing import List, Optional

from pydantic import BaseModel, Field

from .common import UserPayload


class StartRollcallRequest(BaseModel):
    title: Optional[str] = Field(
        None,
        description="Rollcall title. Empty/None → '<Empty>' placeholder.",
    )
    started_by_user_id: int = Field(..., description="Telegram user id of the caller")
    started_by_name: str
    started_by_username: Optional[str] = None


class EndRollcallRequest(BaseModel):
    ended_by_user_id: int
    ended_by_name: str
    ended_by_username: Optional[str] = None


class RollcallResponse(BaseModel):
    """Single rollcall snapshot — mirrors services.common.serialize_rollcall."""

    id: Optional[int] = Field(None, description="DB primary key, if persisted")
    number: int = Field(..., description="1-based display number")
    rc_index: int = Field(..., description="0-based index within the chat")
    title: str
    in_list: List[UserPayload]
    out_list: List[UserPayload]
    maybe_list: List[UserPayload]
    wait_list: List[UserPayload]
    in_count: int
    out_count: int
    maybe_count: int
    wait_count: int
    limit: Optional[int] = None
    location: Optional[str] = None
    event_fee: Optional[str] = None
    individual_fee: Optional[str] = None
    timezone: Optional[str] = None
    finalize_date: Optional[str] = None
    reminder_hours: Optional[int] = None


class EndedByPayload(BaseModel):
    id: int
    name: str
    username: Optional[str] = None


class RenumberEntry(BaseModel):
    old: int
    new: int
    title: str


class EndRollcallResponse(BaseModel):
    ended: RollcallResponse
    rc_number_ended_1based: int
    ghost_eligible: bool
    ghost_rc_db_id: Optional[int] = None
    ended_by: EndedByPayload
    remaining: List[RollcallResponse]
    renumbered: List[RenumberEntry]


class LineupPlayer(BaseModel):
    user_id: Optional[int] = Field(None, description="Member's id; null for a guest without an account")
    name: str
    guest: bool


class LineupPosition(BaseModel):
    position: str = Field(..., description='"<line>:<index>"; line 0 is the keeper, then back to front')
    role: str = Field(..., description="GK, DEF, MID or FWD")
    player: Optional[LineupPlayer] = Field(None, description="null = an empty spot")


class LineupTeam(BaseModel):
    label: str = Field(..., description="A or B")
    formation: str = Field(..., description="Outfield lines back to front, e.g. 2-3-1")
    positions: List[LineupPosition]
    subs: List[LineupPlayer]


class LineupResponse(BaseModel):
    rollcall_id: int
    available: bool = Field(..., description="False when the group has no sport set or the game doesn't have 2 teams")
    sport: Optional[str] = Field(None, description="e.g. football")
    players_per_side: Optional[int] = None
    locked: bool
    teams: List[LineupTeam] = Field(default_factory=list)
