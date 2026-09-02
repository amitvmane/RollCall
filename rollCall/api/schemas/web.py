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
    # Set only for a one-time entry (Schedule -> Once) — a UTC ISO datetime,
    # the exact fire time, as opposed to schedule_day/schedule_time's
    # "next occurrence of this weekday" for a recurring template.
    scheduled_at: Optional[str] = None


class WebGroupResponse(BaseModel):
    group_token: str
    group_name: str = ""
    rollcalls: list[WebRollcallResponse]
    upcoming: list[UpcomingRollcall] = Field(default_factory=list)
    shh_mode: bool = False
    dues_enabled: bool = False
    bot_username: str = ""
    timezone: str = "Asia/Kolkata"
    admin_rights: bool = False
    ghost_tracking_enabled: bool = True
    absent_limit: int = 1


class WebGroupSettingsRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin making the change")
    shh_mode: Optional[bool] = Field(None, description="Silent mode — suppresses per-vote bot notifications")
    timezone: Optional[str] = Field(None, description="IANA timezone, e.g. Asia/Kolkata — usually the admin's browser-detected zone")
    admin_rights: Optional[bool] = Field(None, description="Admin-only mode — restricts bot commands to Telegram admins")
    ghost_tracking_enabled: Optional[bool] = Field(None, description="Track members who RSVP IN but don't show up")
    absent_limit: Optional[int] = Field(None, ge=1, le=99, description="Missed sessions before a ghost warning triggers")


class ScheduledRollcallRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin")
    title: str = Field(..., min_length=1, max_length=200, description="Rollcall title")
    scheduled_at: str = Field(..., description="ISO 8601 UTC datetime when the rollcall should auto-start, e.g. 2026-07-01T09:00:00Z")


class ScheduledRollcallItem(BaseModel):
    id: int
    title: str
    scheduled_at: str
    created_by_name: str
    # Populated when `title` references a saved template (the unified "New
    # Rollcall" flow's one-time path always does) — the template's real
    # display fields for a richer list item than the bare technical name.
    display_title: Optional[str] = None
    location: Optional[str] = None
    fee: Optional[str] = None
    limit: Optional[int] = None


class ScheduledRollcallsResponse(BaseModel):
    items: List[ScheduledRollcallItem] = Field(default_factory=list)


class ScheduledRollcallCreateResponse(BaseModel):
    id: int
    title: str
    scheduled_at: str


class WebEndRollcallResponse(BaseModel):
    ended: int = Field(..., description="1-based rollcall number that was ended")


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


class WebProxyVoteRequest(BaseModel):
    """Admin casts a vote on behalf of a non-Telegram member (web parity for
    /sif /sof /smf). The actor is resolved from the signed id_token, never
    from a client-supplied id."""
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    rollcall_num: int = Field(..., ge=1, description="1-based rollcall number")
    proxy_name: str = Field(..., min_length=1, max_length=64, description="Name of the member being voted for")
    vote: Literal["in", "out", "maybe"]
    comment: Optional[str] = Field(None, max_length=100, description="Optional note to attach to the vote")


class WebRemoveUserRequest(BaseModel):
    """Web parity for the admin console's per-voter remove action."""
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    rollcall_num: int = Field(..., ge=1, description="1-based rollcall number")
    name: str = Field(..., min_length=1, max_length=64, description="Display name or @username of the user to remove")


class WebMoveUserRequest(BaseModel):
    """Web parity for the admin console's per-voter move-to-list action."""
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    rollcall_num: int = Field(..., ge=1, description="1-based rollcall number")
    name: str = Field(..., min_length=1, max_length=64, description="Display name or @username to match")
    new_status: Literal["in", "out", "maybe"]


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
    badges: List[str] = Field(default_factory=list)


class WebStatsWeekday(BaseModel):
    weekday: str
    sessions: int = 0
    avg_in: float = 0.0


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


class WebStatsResponseTimeEntry(BaseModel):
    user_id: int
    display_name: Optional[str] = None
    username: Optional[str] = None
    avg_response_seconds: int = 0
    best_response_seconds: int = 0
    rollcall_count: int = 0


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
    response_time_leaderboard: List[WebStatsResponseTimeEntry] = Field(default_factory=list)
    personal: Optional[WebStatsPersonal] = None
    weekday_stats: List[WebStatsWeekday] = Field(default_factory=list)


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
    location: Optional[str] = Field(None, max_length=200)
    fee: Optional[str] = Field(None, max_length=50)
    limit: Optional[int] = Field(None, ge=1, le=1000)
    event_day: Optional[str] = Field(None, description="Weekday name the event happens on — used to auto-close (both-or-neither with event_time). Ignored if finalize_at is set.")
    event_time: Optional[str] = Field(None, description="HH:MM the event happens at — used to auto-close (both-or-neither with event_day). Ignored if finalize_at is set.")
    finalize_at: Optional[str] = Field(None, description="UTC ISO 8601 datetime — exact one-time close time, for a rollcall that doesn't recur. Takes precedence over event_day/event_time.")
    save_as_template: Optional[str] = Field(None, max_length=50, description="If set, also saves these fields as a reusable template under this name")


class WebEndRollcallRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin ending the rollcall")
    rollcall_num: int = Field(1, ge=1, description="1-based rollcall number to end (defaults to first)")


class WebAdminStatusResponse(BaseModel):
    is_admin: bool


class WebTemplateResponse(BaseModel):
    """Template status for the group web page's admin-only editor
    (self-serve, id_token-gated; the separate token-gated /admin/ console
    is a bottleneck for non-owner group admins, so this lives on the page
    every web admin already has self-serve access to). Covers both the
    template's content and its recurring schedule."""
    name: str
    title: Optional[str] = None
    location: Optional[str] = None
    fee: Optional[str] = None
    limit: Optional[int] = None
    schedule_enabled: bool = False
    schedule_day: Optional[str] = None
    schedule_time: Optional[str] = None
    recurrence_type: str = "weekly"
    event_day: Optional[str] = None
    event_time: Optional[str] = None
    last_scheduled_date: Optional[str] = None
    schedule_expires_at: Optional[str] = None


class WebCanonicalRef(BaseModel):
    """A resolved canonical identity — either a real Telegram user or a
    proxy name acting as the merge target for other aliases."""
    kind: Literal["user", "proxy"]
    user_id: Optional[int] = None
    proxy_name: Optional[str] = None


class WebIdentityItem(BaseModel):
    """One mergeable identity in the chat: an active real member, or a
    proxy name ever used. merged_into is None when this identity IS
    itself canonical (unmerged, or the target other aliases point at)."""
    kind: Literal["user", "proxy"]
    user_id: Optional[int] = None
    proxy_name: Optional[str] = None
    display_name: str
    merged_into: Optional[WebCanonicalRef] = None
    proxy_count: Optional[int] = None
    proxy_last_seen: Optional[str] = None
    standalone: bool = False  # confirmed real person, out of the review queue but still a merge target


class WebIdentityGroupResponse(BaseModel):
    """A canonical identity plus every alias currently merged into it."""
    kind: Literal["user", "proxy"]
    user_id: Optional[int] = None
    proxy_name: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    display_name: str = ""


class WebIdentityListResponse(BaseModel):
    identities: List[WebIdentityItem]
    groups: List[WebIdentityGroupResponse]
    discarded: List[str] = Field(default_factory=list)
    standalone: List[str] = Field(default_factory=list)


class WebIdentitySuggestion(BaseModel):
    alias_proxy_name: str
    candidate_kind: Literal["user", "proxy"]
    candidate_user_id: Optional[int] = None
    candidate_proxy_name: Optional[str] = None
    candidate_display_name: str
    score: int
    confidence: Literal["exact_username", "exact_first_name", "exact_proxy", "close"] = "close"


class WebIdentitySuggestionsResponse(BaseModel):
    suggestions: List[WebIdentitySuggestion]


class WebMergeIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40, description="Proxy name being folded in")
    canonical_user_id: Optional[int] = Field(None, description="Merge target: a real Telegram user")
    canonical_proxy_name: Optional[str] = Field(None, max_length=40, description="Merge target: another proxy name")


class WebUnmergeIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40)


class WebUnmergeIdentityResponse(BaseModel):
    unmerged: bool


class WebDismissSuggestionRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40)
    candidate_user_id: Optional[int] = None
    candidate_proxy_name: Optional[str] = Field(None, max_length=40)


class WebDismissSuggestionResponse(BaseModel):
    dismissed: bool


class WebDiscardIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40, description="Invalid/garbage proxy name to hide")


class WebDiscardIdentityResponse(BaseModel):
    discarded: bool


class WebUndiscardIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40)


class WebUndiscardIdentityResponse(BaseModel):
    restored: bool


class WebStandaloneIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(
        ..., max_length=40,
        description="Proxy name confirmed to be a real person with no Telegram account")


class WebStandaloneIdentityResponse(BaseModel):
    standalone: bool


class WebUnstandaloneIdentityRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    alias_proxy_name: str = Field(..., max_length=40)


class WebUnstandaloneIdentityResponse(BaseModel):
    restored: bool


class WebSetScheduleRequest(BaseModel):
    """When the template auto-opens a new rollcall — distinct from
    WebUpdateTemplateRequest.event_day/event_time, which is when the game
    itself happens and is used to auto-close it."""
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    recurrence_type: Literal["daily", "weekly", "biweekly", "monthly"] = "weekly"
    schedule_day: Optional[str] = Field(None, description="Weekday name (weekly/biweekly) — ignored for monthly/daily")
    schedule_time: str = Field(..., description="HH:MM local time")
    monthly_day: Optional[int] = Field(None, ge=1, le=31, description="Day of month (monthly only)")
    expires_at: Optional[str] = Field(None, description="\"YYYY-MM-DD\" — schedule auto-disables after this date (template stays). Defaults to 1 year out if omitted.")


class WebToggleScheduleRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")


class WebUpdateTemplateRequest(BaseModel):
    """Full-form update of a template's content — unlike the token-gated
    REST API's upsert_template contract, this route always receives every
    field from the web editor, so a blank/omitted field here means "clear
    it", not "leave unchanged" (see web_update_template's translation to
    services.templates.upsert_template's own None=preserve / ""=clear
    convention).

    event_day/event_time are the game's own day+time (used to auto-close a
    rollcall started from this template) — distinct from schedule_day/
    schedule_time on WebSetScheduleRequest, which control when the rollcall
    auto-opens."""
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    title: Optional[str] = Field(None, max_length=200)
    location: Optional[str] = Field(None, max_length=200)
    fee: Optional[str] = Field(None, max_length=50)
    limit: Optional[int] = Field(None, ge=0, le=1000, description="0 clears the cap (no valid real limit is 0)")
    event_day: Optional[str] = Field(None, description="Weekday name the event itself happens on (used for auto-close)")
    event_time: Optional[str] = Field(None, description="HH:MM the event itself happens at (used for auto-close)")
    # Alternative to event_day/event_time for a one-time close time (the New
    # Rollcall modal's Schedule -> Once path uses this instead of a weekday,
    # since "next Xday" doesn't make sense for a single specific occurrence
    # — see services.templates.start_template's offset fallback). Unlike the
    # fields above, these follow plain None=leave-unchanged semantics rather
    # than this route's blank-clears convention, since only one caller sets
    # them today and always sends the full trio together when relevant.
    offset_days: Optional[int] = Field(None, ge=0, description="Days after the rollcall opens that it should auto-close")
    offset_hours: Optional[int] = Field(None, ge=0, le=23, description="Hours (in addition to offset_days) after opening that it should auto-close")
    offset_minutes: Optional[int] = Field(None, ge=0, le=59, description="Minutes (in addition to offset_days/hours) after opening that it should auto-close")


class WebStartTemplateRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    extra_title: Optional[str] = Field(None, max_length=200, description="Optional suffix appended to the template's base title")


# ── Ghost review (post-session no-show marking) ──────────────────────────────

class WebGhostCandidate(BaseModel):
    """One person who can be marked a no-show for a session."""
    user_id: Optional[int] = Field(None, description="Telegram user id; null for a proxy/guest")
    proxy_name: Optional[str] = Field(None, description="Proxy name (added via /sif); null for a real user")
    name: str = Field(..., description="Display name")
    was_out: bool = Field(False, description="Ended the session in the OUT list — a late drop-out, shown separately")


class WebGhostSession(BaseModel):
    """An ended rollcall still waiting to be reviewed."""
    rollcall_id: int
    title: str
    ended_at: Optional[str] = None
    candidates: List[WebGhostCandidate] = Field(default_factory=list)


class WebGhostSessionsResponse(BaseModel):
    ghost_tracking_enabled: bool = Field(..., description="False → reviewing does nothing; the UI should say so")
    autoforgive_days: int = Field(..., description="Unreviewed sessions are treated as 'everyone attended' after this many days; 0 disables")
    sessions: List[WebGhostSession] = Field(default_factory=list)


class WebGhostReviewRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting web admin")
    rollcall_id: int = Field(..., description="Which ended rollcall is being reviewed")
    ghost_user_ids: List[int] = Field(default_factory=list, description="Real members who did not show")
    ghost_proxy_names: List[str] = Field(default_factory=list, description="Proxy/guest names who did not show")


class WebGhostReviewResponse(BaseModel):
    ghosts: int = Field(..., description="How many no-shows were recorded")
    forgiven: int = Field(..., description="How many attendees had one past absence cleared")
    lines: List[str] = Field(default_factory=list, description="Human-readable summary, one line per no-show")


# ── Web-admin roles ──────────────────────────────────────────────────────────

class WebAdminEntry(BaseModel):
    tg_user_id: int
    tg_name: Optional[str] = None
    role: str = Field("admin", description="'owner' or 'admin'")
    added_at: Optional[str] = None
    is_you: bool = Field(False, description="True for the caller's own row")


class WebAdminListResponse(BaseModel):
    admin_source: str = Field(..., description="'platform' = Telegram decides, 'local' = this list decides")
    you_are_owner: bool
    admins: List[WebAdminEntry] = Field(default_factory=list)


class WebSetAdminRoleRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the acting owner")
    tg_user_id: int = Field(..., description="Whose role to change")
    role: str = Field(..., description="'owner' or 'admin'")
