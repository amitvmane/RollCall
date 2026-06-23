"""Schemas for magic-link web voting endpoints."""
from typing import List, Literal, Optional
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


class WebHeartbeatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64, description="Client-generated session UUID (per browser tab)")


class WebPresenceResponse(BaseModel):
    active_now: int = 0
    total_views: int = 0


# ── Public stats schemas (no auth required, served via group token) ───────────

class WebStatsPersonal(BaseModel):
    """Personal stats for the currently identified user."""
    rank: Optional[int] = None
    total_participants: int = 0
    sessions_attended: int = 0
    total_rollcalls_in_chat: int = 0
    attendance_rate: Optional[float] = None
    voting_rate: Optional[float] = None
    best_streak: int = 0
    current_streak: int = 0
    ghost_count: int = 0
    total_in_votes: int = 0
    total_out_votes: int = 0
    total_maybe_votes: int = 0
    total_waiting_to_in: int = 0


class WebStatsLeaderEntry(BaseModel):
    rank: int
    display_name: Optional[str] = None
    user_id: Optional[int] = None
    kind: str = "real"
    sessions_attended: int = 0
    total_sessions_voted: int = 0
    attendance_rate: Optional[float] = None
    voting_rate: Optional[float] = None


class WebStatsHistoryEntry(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None
    ended_at: Optional[str] = None
    in_count: int = 0
    out_count: int = 0
    maybe_count: int = 0


class WebStatsGhostEntry(BaseModel):
    name: Optional[str] = None
    ghost_count: int = 0


class WebGroupStatsResponse(BaseModel):
    total_rollcalls: int = 0
    avg_attendance: float = 0.0
    total_participants: int = 0
    real_participants: int = 0
    proxy_participants: int = 0
    real_attendance_slots: int = 0
    proxy_attendance_slots: int = 0
    waitlist_promotions: int = 0
    leaderboard: List[WebStatsLeaderEntry] = Field(default_factory=list)
    ghost_leaderboard: List[WebStatsGhostEntry] = Field(default_factory=list)
    recent_history: List[WebStatsHistoryEntry] = Field(default_factory=list)
    personal: Optional[WebStatsPersonal] = None
