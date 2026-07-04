"""
Dues & Treasury REST routes.

All endpoints live under /web/group/{group_token}/dues/... so they share the
same group-token auth pattern as the rest of the web API.

Auth model:
  GET  endpoints — id_token as query param (no request body needed for reads)
  POST / PUT / PATCH / DELETE — id_token in the request body (or query param
      for DELETE, where a body is non-standard)

Access levels:
  • Any verified member: GET /dues/my, GET /dues/fund
  • Admin only (is_web_admin): all other reads + all writes
  • mark-paid: admin OR the game's designated collector (service enforces this)

Dues guard:
  • All operational endpoints reject requests when dues_enabled=0 for the group.
  • enable / disable bypass this guard.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, status

import db as _db
from api.identity import verify_identity_token
from api.schemas.dues import (
    DuesAddAdhocRequest,
    DuesCancelGameRequest,
    DuesCloseGameRequest,
    DuesEnableRequest,
    DuesFundExpenseRequest,
    DuesFundHistoryResponse,
    DuesFundResponse,
    DuesFundTopupRequest,
    DuesMarkPaidRequest,
    DuesMarkPenaltyRequest,
    DuesMemberBalance,
    DuesMyResponse,
    DuesReimburseRequest,
    DuesSetCollectorRequest,
    DuesSettingsPatchRequest,
    DuesSettingsResponse,
    DuesSummaryResponse,
    DuesTiersResponse,
    DuesUpsertTierRequest,
    DuesWaiveRequest,
    DuesEntry,
    FundTransaction,
    PenaltyTier,
)
from rollcall_manager import manager as _mgr
from services import dues as dues_svc

router = APIRouter()
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_chat(group_token: str) -> dict:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    return chat


def _require_identity(id_token: str) -> int:
    user_id = verify_identity_token(id_token)
    if not user_id or user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Verify with Telegram to use dues features.",
        )
    return user_id


def _require_admin(chat_id: int, id_token: str) -> int:
    user_id = _require_identity(id_token)
    if not _db.is_web_admin(chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an admin for this group.",
        )
    return user_id


def _require_dues(chat: dict) -> None:
    if not chat.get("dues_enabled"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dues is not enabled for this group. An admin must run /enable_dues first.",
        )


def _actor_name(chat_id: int, user_id: int) -> str:
    try:
        info = _db.get_member_display_info(chat_id, user_id)
        if info:
            return info.get("first_name") or "(web)"
    except Exception:
        pass
    return "(web)"


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get(
    "/web/group/{group_token}/dues/my",
    response_model=DuesMyResponse,
    summary="Authenticated user's dues balance and recent ledger entries",
)
async def get_my_dues(
    group_token: str = Path(...),
    id_token: str = Query(..., description="Signed identity token"),
) -> DuesMyResponse:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    user_id = _require_identity(id_token)
    chat_id = int(chat["chat_id"])

    result = dues_svc.my_dues(chat_id, user_id)
    settings = dues_svc.get_dues_settings(chat_id)

    return DuesMyResponse(
        balance=result["balance"],
        entries=[DuesEntry(**e) for e in result["entries"]],
        upi_vpa=settings.get("upi_vpa"),
    )


@router.get(
    "/web/group/{group_token}/dues/summary",
    response_model=DuesSummaryResponse,
    summary="All member dues balances (admin only)",
)
async def get_dues_summary(
    group_token: str = Path(...),
    id_token: str = Query(...),
    nonzero_only: bool = Query(False, description="Only return members with a non-zero balance"),
) -> DuesSummaryResponse:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    _require_admin(chat_id, id_token)

    result = dues_svc.all_dues(chat_id, nonzero_only=nonzero_only)
    fund = dues_svc.fund_summary(chat_id)

    return DuesSummaryResponse(
        balances=[DuesMemberBalance(**b) for b in result["balances"]],
        fund_balance=fund["fund_balance"],
    )


@router.get(
    "/web/group/{group_token}/dues/fund",
    response_model=DuesFundResponse,
    summary="Group fund balance (any verified member)",
)
async def get_fund(
    group_token: str = Path(...),
    id_token: str = Query(...),
) -> DuesFundResponse:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    _require_identity(id_token)
    chat_id = int(chat["chat_id"])

    result = dues_svc.fund_summary(chat_id)
    return DuesFundResponse(fund_balance=result["fund_balance"])


@router.get(
    "/web/group/{group_token}/dues/fund/history",
    response_model=DuesFundHistoryResponse,
    summary="Paginated fund transaction history (admin only)",
)
async def get_fund_history(
    group_token: str = Path(...),
    id_token: str = Query(...),
    limit: int = Query(15, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> DuesFundHistoryResponse:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    _require_admin(chat_id, id_token)

    result = dues_svc.fund_history(chat_id, limit=limit, offset=offset)
    fund = dues_svc.fund_summary(chat_id)

    return DuesFundHistoryResponse(
        transactions=[FundTransaction(**t) for t in result["transactions"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        fund_balance=fund["fund_balance"],
    )


@router.get(
    "/web/group/{group_token}/dues/tiers",
    response_model=DuesTiersResponse,
    summary="List penalty tiers for this group (admin only)",
)
async def get_tiers(
    group_token: str = Path(...),
    id_token: str = Query(...),
) -> DuesTiersResponse:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    _require_admin(chat_id, id_token)

    result = dues_svc.list_penalty_tiers(chat_id)
    return DuesTiersResponse(tiers=[PenaltyTier(**t) for t in result["tiers"]])


@router.get(
    "/web/group/{group_token}/dues/settings",
    response_model=DuesSettingsResponse,
    summary="Current dues settings — UPI VPA, round step, enabled flag (admin only)",
)
async def get_settings(
    group_token: str = Path(...),
    id_token: str = Query(...),
) -> DuesSettingsResponse:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    _require_admin(chat_id, id_token)

    settings = dues_svc.get_dues_settings(chat_id)
    return DuesSettingsResponse(
        upi_vpa=settings.get("upi_vpa"),
        dues_round_step=settings.get("dues_round_step") or 10,
        dues_enabled=bool(chat.get("dues_enabled")),
    )


# ── Enable / Disable ──────────────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/dues/enable",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Enable dues for this group and seed default penalty tiers (admin only)",
)
async def enable_dues(
    body: DuesEnableRequest,
    group_token: str = Path(...),
) -> None:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    _db.update_chat_settings(chat_id, dues_enabled=1)
    dues_svc.seed_default_penalty_tiers(chat_id)
    _db.log_admin_action(chat_id, actor_uid, actor_name, "enable_dues")


@router.post(
    "/web/group/{group_token}/dues/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable dues for this group (admin only). Existing ledger data is preserved.",
)
async def disable_dues(
    body: DuesEnableRequest,
    group_token: str = Path(...),
) -> None:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    _db.update_chat_settings(chat_id, dues_enabled=0)
    _db.log_admin_action(chat_id, actor_uid, actor_name, "disable_dues")


# ── Game close & cancel ───────────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/dues/close-game",
    summary="Financially close the most recent game — splits costs and writes ledger entries (admin only)",
)
async def close_game(
    body: DuesCloseGameRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    async with _mgr.get_chat_write_lock(chat_id):
        result = await dues_svc.close_game(
            chat_id,
            subsidy=body.subsidy,
            admin_uid=actor_uid,
            admin_name=actor_name,
            rc_number=body.rc_number,
        )

    return {
        "rollcall_id": result["rollcall_id"],
        "title": result["title"],
        "ground_cost": result["ground_cost"],
        "subsidy": result["subsidy"],
        "per_head": result["per_head"],
        "remainder": result["remainder"],
        "in_count": result["in_count"],
        "fund_balance_after": result["fund_balance_after"],
        "announcement": result["announcement"],
    }


@router.post(
    "/web/group/{group_token}/dues/cancel-game",
    summary="Reverse dues for a closed game via compensating entries (admin only). n_index=0 means the latest.",
)
async def cancel_game_dues(
    body: DuesCancelGameRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    async with _mgr.get_chat_write_lock(chat_id):
        # Fetch inside the lock so a concurrent close_game cannot shift ordering
        # between the lookup and the reversal, and to prevent double-reversal
        # from two concurrent API calls on the same game.
        closure = _db.get_nth_game_closure(chat_id, body.n_index)
        if closure is None:
            raise HTTPException(status_code=404, detail="No game closure found at that position.")
        result = dues_svc.cancel_game_credit(
            chat_id, closure["rollcall_id"], actor_uid, actor_name
        )
    return result


# ── Penalty, payment, and adjustment ops ─────────────────────────────────────

@router.post(
    "/web/group/{group_token}/dues/mark-penalty",
    summary="Charge a named penalty tier to a member (admin only)",
)
async def mark_penalty(
    body: DuesMarkPenaltyRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.mark_penalty(
        chat_id, body.tier_name, body.member_name, actor_uid, actor_name
    )


@router.post(
    "/web/group/{group_token}/dues/mark-paid",
    summary="Record a payment from a member. Allowed for admin or the game's designated collector.",
)
async def mark_paid(
    body: DuesMarkPaidRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_identity(body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)
    is_admin = bool(_db.is_web_admin(chat_id, actor_uid))

    return dues_svc.mark_paid(
        chat_id, body.member_name, actor_uid, actor_name,
        amount=body.amount, is_admin=is_admin,
    )


@router.post(
    "/web/group/{group_token}/dues/waive",
    summary="Waive part or all of a member's outstanding dues (admin only)",
)
async def waive(
    body: DuesWaiveRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.waive(
        chat_id, body.member_name, body.amount, body.reason,
        actor_uid, actor_name,
    )


@router.post(
    "/web/group/{group_token}/dues/reimburse",
    summary="Issue a reimbursement credit to a member (admin only)",
)
async def reimburse(
    body: DuesReimburseRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.reimburse(
        chat_id, body.member_name, body.amount, body.reason,
        actor_uid, actor_name,
    )


@router.post(
    "/web/group/{group_token}/dues/add-adhoc",
    summary="Charge a late joiner the last game's per-head fee (admin only)",
)
async def add_adhoc(
    body: DuesAddAdhocRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.add_adhoc(chat_id, body.member_name, actor_uid, actor_name)


@router.post(
    "/web/group/{group_token}/dues/set-collector",
    summary="Designate a collector for the current or last game (admin only)",
)
async def set_collector(
    body: DuesSetCollectorRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    async with _mgr.get_chat_write_lock(chat_id):
        return dues_svc.set_collector(
            chat_id, body.member_name, body.paid_ground, actor_uid, actor_name
        )


# ── Fund management ───────────────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/dues/fund/expense",
    summary="Log a fund expense, e.g. new shuttles (admin only)",
)
async def log_expense(
    body: DuesFundExpenseRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.log_expense(chat_id, body.amount, body.description, actor_uid, actor_name)


@router.post(
    "/web/group/{group_token}/dues/fund/topup",
    summary="Manually add money to the group fund (admin only)",
)
async def fund_topup(
    body: DuesFundTopupRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _resolve_chat(group_token)
    _require_dues(chat)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.fund_topup(chat_id, body.amount, body.description, actor_uid, actor_name)


# ── Penalty tier management ───────────────────────────────────────────────────

@router.put(
    "/web/group/{group_token}/dues/tiers/{tier_name}",
    summary="Add or update a penalty tier (admin only)",
)
async def upsert_tier(
    body: DuesUpsertTierRequest,
    group_token: str = Path(...),
    tier_name: str = Path(..., description="Tier name, e.g. 'late_short' or 'ditch'"),
) -> dict:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    return dues_svc.add_penalty_tier(
        chat_id, tier_name, body.amount, body.description, actor_uid, actor_name
    )


@router.delete(
    "/web/group/{group_token}/dues/tiers/{tier_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a penalty tier (admin only)",
)
async def delete_tier(
    group_token: str = Path(...),
    tier_name: str = Path(...),
    id_token: str = Query(...),
) -> None:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    dues_svc.remove_penalty_tier(chat_id, tier_name, actor_uid, actor_name)


# ── Settings ──────────────────────────────────────────────────────────────────

@router.patch(
    "/web/group/{group_token}/dues/settings",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Update dues settings — UPI VPA and/or rounding step (admin only)",
)
async def update_settings(
    body: DuesSettingsPatchRequest,
    group_token: str = Path(...),
) -> None:
    chat = _resolve_chat(group_token)
    chat_id = int(chat["chat_id"])
    actor_uid = _require_admin(chat_id, body.id_token)
    actor_name = _actor_name(chat_id, actor_uid)

    if body.upi_vpa is not None:
        dues_svc.set_upi(chat_id, body.upi_vpa, actor_uid, actor_name)
    if body.dues_round_step is not None:
        dues_svc.set_round_step(chat_id, body.dues_round_step, actor_uid, actor_name)
