"""Pydantic response models for stats, ghost, and settings routes."""

from typing import List, Optional, Union

from pydantic import BaseModel, Field


# ─── Stats ────────────────────────────────────────────────────────────────────

class PersonalStatsResponse(BaseModel):
    user_id: int
    total_rollcalls_in_chat: int
    sessions_attended: int
    attendance_rate: Optional[float] = None
    total_in_votes: int = 0
    total_out_votes: int = 0
    total_maybe_votes: int = 0
    total_sessions_voted: int = 0
    voting_rate: Optional[float] = None
    total_waiting_to_in: int = 0
    best_streak: int = 0
    current_streak: int = 0
    ghost_count: int = 0


class TopAttendee(BaseModel):
    name: Optional[str]
    sessions: int
    attendance_rate: Optional[float]


class GhostEntry(BaseModel):
    name: Optional[str]
    ghost_count: int


class GroupStatsResponse(BaseModel):
    total_rollcalls: int
    total_attendances: int
    unique_participants: int
    top_attendees: List[TopAttendee]
    ghost_leaderboard: List[GhostEntry]


class LeaderboardEntry(BaseModel):
    rank: int
    name: Optional[str]
    user_id: Optional[int]
    is_proxy: bool
    sessions: int
    attendance_rate: Optional[float]


class HistoryEntry(BaseModel):
    id: Optional[int]
    title: Optional[str]
    ended_at: Optional[str]
    in_count: int
    out_count: int
    maybe_count: int


# ─── Ghost ────────────────────────────────────────────────────────────────────

class GhostSettingsResponse(BaseModel):
    ghost_tracking_enabled: bool
    absent_limit: int


class ToggleGhostRequest(BaseModel):
    enabled: bool
    admin_user_id: int
    admin_name: str


class SetAbsentLimitRequest(BaseModel):
    limit: int = Field(..., ge=1)
    admin_user_id: int
    admin_name: str


class ClearAbsentRequest(BaseModel):
    admin_user_id: int
    admin_name: str
    target_user_id: Optional[int] = None
    proxy_name: Optional[str] = None


class ClearAbsentResponse(BaseModel):
    cleared: bool


class GhostLeaderboardEntry(BaseModel):
    name: Optional[str]
    user_id: Optional[int]
    is_proxy: bool
    ghost_count: int


# ─── Settings ─────────────────────────────────────────────────────────────────

class ChatSettingsResponse(BaseModel):
    timezone: str
    shh_mode: bool
    admin_rights: bool
    ghost_tracking_enabled: bool
    absent_limit: int


class SetTimezoneRequest(BaseModel):
    timezone: str
    admin_user_id: int
    admin_name: str


class SetShhModeRequest(BaseModel):
    enabled: bool
    admin_user_id: int
    admin_name: str


class SetLimitRequest(BaseModel):
    limit: int = Field(..., ge=0)
    admin_user_id: int
    admin_name: str


class SetLocationRequest(BaseModel):
    location: str
    admin_user_id: int
    admin_name: str


class SetFeeRequest(BaseModel):
    fee: str
    admin_user_id: int
    admin_name: str
