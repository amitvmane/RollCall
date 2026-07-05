"""Schemas for Dues & Treasury API endpoints."""
from typing import List, Optional
from pydantic import BaseModel, Field


# ── Request bodies ────────────────────────────────────────────────────────────

class DuesCloseGameRequest(BaseModel):
    id_token: str = Field(..., description="Signed identity token of the admin")
    subsidy: int = Field(0, ge=0, description="Amount to subsidise from the group fund (₹)")
    rc_number: int = Field(0, ge=0, description="Which active rollcall to close: 0 = first, 1 = second, … (for groups with multiple simultaneous rollcalls)")


class DuesMarkPenaltyRequest(BaseModel):
    id_token: str
    tier_name: str = Field(..., description="Penalty tier name (e.g. 'ditch', 'late_short')")
    member_name: str = Field(..., description="First name, @username, or proxy name")


class DuesMarkPaidRequest(BaseModel):
    id_token: str
    member_name: str = Field(..., description="First name, @username, or proxy name")
    amount: Optional[int] = Field(None, gt=0, description="Amount paid (₹); omit to use full outstanding balance")


class DuesWaiveRequest(BaseModel):
    id_token: str
    member_name: str
    amount: int = Field(..., gt=0, description="Amount to waive (₹)")
    reason: str = Field("", description="Reason for the waiver (shown in ledger)")


class DuesReimburseRequest(BaseModel):
    id_token: str
    member_name: str
    amount: int = Field(..., gt=0, description="Reimbursement amount (₹)")
    reason: str = ""


class DuesAddAdhocRequest(BaseModel):
    id_token: str
    member_name: str = Field(..., description="Player who joined after the game was closed")


class DuesCancelGameRequest(BaseModel):
    id_token: str
    n_index: int = Field(
        0, ge=0,
        description="Which closure to cancel: 0 = latest, 1 = second-most-recent, …",
    )


class DuesFundExpenseRequest(BaseModel):
    id_token: str
    amount: int = Field(..., gt=0, description="Expense amount (₹)")
    description: str = Field(..., min_length=1, description="What the money was spent on")


class DuesFundTopupRequest(BaseModel):
    id_token: str
    amount: int = Field(..., gt=0, description="Amount added to fund (₹)")
    description: str = Field("", description="Reason for the top-up")


class DuesSetCollectorRequest(BaseModel):
    id_token: str
    member_name: str = Field(..., description="Collector's first name or @username")
    paid_ground: bool = Field(False, description="True if the collector fronted the ground cost")


class DuesUpsertTierRequest(BaseModel):
    id_token: str
    amount: int = Field(..., gt=0, description="Penalty amount in whole rupees (₹)")
    description: str = Field("", description="Human-readable description of when this tier applies")


class DuesSelfPaidRequest(BaseModel):
    id_token: str
    amount: Optional[int] = Field(None, gt=0, description="Amount paid (₹); omit to use full outstanding balance")


class DuesSettingsPatchRequest(BaseModel):
    id_token: str
    upi_vpa: Optional[str] = Field(None, description="UPI VPA for payment instructions, e.g. name@bank")
    dues_round_step: Optional[int] = Field(None, gt=0, description="Rounding step for per-head calculation (₹)")
    dues_self_paid_mode: Optional[str] = Field(None, description="'auto' or 'off'")


class DuesEnableRequest(BaseModel):
    id_token: str


# ── Response models ───────────────────────────────────────────────────────────

class DuesEntry(BaseModel):
    entry_type: str
    amount: int
    memo: Optional[str] = None
    member_name: str
    created_at: Optional[str] = None
    created_by_name: Optional[str] = None

    model_config = {"extra": "ignore"}


class DuesMyResponse(BaseModel):
    balance: int
    entries: List[DuesEntry]
    upi_vpa: Optional[str] = None


class DuesMemberBalance(BaseModel):
    member_name: str
    balance: int

    model_config = {"extra": "ignore"}


class DuesSummaryResponse(BaseModel):
    balances: List[DuesMemberBalance]
    fund_balance: int


class DuesFundResponse(BaseModel):
    fund_balance: int


class FundTransaction(BaseModel):
    txn_type: str
    amount: int
    description: Optional[str] = None
    created_at: Optional[str] = None
    created_by_name: Optional[str] = None
    rollcall_id: Optional[int] = None

    model_config = {"extra": "ignore"}


class DuesFundHistoryResponse(BaseModel):
    transactions: List[FundTransaction]
    total: int
    limit: int
    offset: int
    fund_balance: int


class PenaltyTier(BaseModel):
    name: str
    amount: int
    description: Optional[str] = None

    model_config = {"extra": "ignore"}


class DuesTiersResponse(BaseModel):
    tiers: List[PenaltyTier]


class DuesClosePreviewResponse(BaseModel):
    title: str = ""
    ground_cost: int = 0
    in_count: int = 0
    per_head: int = 0
    remainder: int = 0
    fund_balance: int = 0
    has_active: bool = False
    available: bool = True


class DuesSettingsResponse(BaseModel):
    upi_vpa: Optional[str] = None
    dues_round_step: int
    dues_enabled: bool
    dues_self_paid_mode: str = "auto"
