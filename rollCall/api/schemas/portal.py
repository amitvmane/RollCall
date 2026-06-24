from typing import List, Optional
from pydantic import BaseModel


class PortalGroupSummary(BaseModel):
    chat_id: int
    group_name: Optional[str] = None
    timezone: str = "Asia/Kolkata"
    group_web_token: Optional[str] = None
    sessions_attended: int = 0
    total_sessions: int = 0
    attendance_rate: Optional[float] = None
    current_streak: int = 0
    best_streak: int = 0
    rank: Optional[int] = None
    has_active_rollcall: bool = False


class PortalGroupsResponse(BaseModel):
    tg_user_id: int
    groups: List[PortalGroupSummary]


class PortalSessionEntry(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None
    ended_at: Optional[str] = None
    status: str  # 'in', 'out', 'maybe', 'miss'


class PortalGroupHistoryResponse(BaseModel):
    chat_id: int
    tg_user_id: int
    sessions: List[PortalSessionEntry]
