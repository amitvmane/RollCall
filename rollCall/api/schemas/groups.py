"""
Pydantic schemas for admin group-management endpoints.
"""

from typing import Optional

from pydantic import BaseModel


class GroupSummary(BaseModel):
    chat_id: int
    group_name: Optional[str] = None
    timezone: str
    active_rollcalls: int
    absent_limit: int
    ghost_tracking_enabled: bool
    admin_rights: bool
    group_web_token: Optional[str] = None


class GroupSettings(BaseModel):
    chat_id: int
    group_name: Optional[str] = None
    timezone: str
    shh_mode: bool
    admin_rights: bool
    ghost_tracking_enabled: bool
    absent_limit: int


class UpdateGroupSettingsRequest(BaseModel):
    admin_user_id: int
    admin_name: str
    timezone: Optional[str] = None
    shh_mode: Optional[bool] = None
    admin_rights: Optional[bool] = None
    ghost_tracking_enabled: Optional[bool] = None
    absent_limit: Optional[int] = None
