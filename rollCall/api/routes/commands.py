"""
Command reference endpoint — backs the /help/ web page.

Returns the exact same data /help renders in Telegram, straight from
commands_registry.py (the repo's single source of truth for every
command — see that file's own docstring). No separate copy of command
docs to keep in sync.

Unauthenticated: command *documentation* isn't sensitive, only the
actions the commands trigger are — this matches existing behavior,
since /help admin in Telegram already shows admin/super_admin command
docs to anyone who types it, no admin check.
"""
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from commands_registry import (
    COMMANDS, USER_CATEGORY_ORDER, ADMIN_CATEGORY_ORDER, CATEGORY_EMOJI,
)


router = APIRouter()


class CommandEntry(BaseModel):
    name: str
    aliases: List[str]
    scope: str
    category: str
    args: Optional[str] = None
    sample: Optional[str] = None
    summary: str
    details: Optional[str] = None


class CommandsResponse(BaseModel):
    commands: List[CommandEntry]
    user_category_order: List[str]
    admin_category_order: List[str]
    category_emoji: dict


@router.get("/commands", response_model=CommandsResponse)
async def list_commands() -> CommandsResponse:
    return CommandsResponse(
        commands=[
            CommandEntry(
                name=c["name"],
                aliases=c.get("aliases") or [],
                scope=c["scope"],
                category=c["category"],
                args=c.get("args"),
                sample=c.get("sample"),
                summary=c["summary"],
                details=c.get("details"),
            )
            for c in COMMANDS
        ],
        user_category_order=USER_CATEGORY_ORDER,
        admin_category_order=ADMIN_CATEGORY_ORDER,
        category_emoji=CATEGORY_EMOJI,
    )
