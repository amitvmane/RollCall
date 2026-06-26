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
    fee: Optional[str] = None
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
    group_name: str = ""
    rollcalls: list[WebRollcallResponse]
    upcoming: list[UpcomingRollcall] = Field(default_factory=list)
    shh_mode: bool = False


class WebGroupSettingsRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin making the change")
    shh_mode: Optional[bool] = Field(None, description="Silent mode — suppresses per-vote bot notifications")


class ScheduledRollcallRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin")
    title: str = Field(..., min_length=1, max_length=200, description="Rollcall title")
    scheduled_at: str = Field(..., description="ISO 8601 UTC datetime when the rollcall should auto-start, e.g. 2026-07-01T09:00:00Z")


class ScheduledRollcallItem(BaseModel):
    id: int
    title: str
    scheduled_at: str
    created_by_name: str


class ScheduledRollcallsResponse(BaseModel):
    items: List[ScheduledRollcallItem] = Field(default_factory=list)


class WebVoteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Display name for the voter")
    vote: Literal["in", "out", "maybe"]
    # A raw tg_user_id is no longer trusted on its own — attributing a vote to a
    # real Telegram account requires a signed identity token proving that
    # account. Without one the vote is recorded as a name-only proxy entry.
    id_token: Optional[str] = Field(None, description="Signed identity token to attribute the vote to a real Telegram user")
    # Telegram @handle (without @) passed alongside the name so the model can
    # format "First (@handle)" when a proxy with the same first name exists.
    username: Optional[str] = Field(None, max_length=64, description="Telegram username (without @) for display-name disambiguation")
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
    recent_sessions: List[dict] = Field(default_factory=list)


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


# ── Web push schemas ──────────────────────────────────────────────────────────

class PushSubscribeKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=10)
    keys: PushSubscribeKeys
    tg_user_id: Optional[int] = Field(None, description="Verified Telegram user_id to link this subscription to an identity")


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=10)


class VapidPublicKeyResponse(BaseModel):
    public_key: str


class WebStartRollcallRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin starting the rollcall")
    title: str = Field(..., min_length=1, max_length=200, description="Rollcall title")


class WebEndRollcallRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin ending the rollcall")
    rollcall_num: int = Field(1, ge=1, description="1-based rollcall number to end (defaults to first)")


class WebAdminStatusResponse(BaseModel):
    is_admin: bool
