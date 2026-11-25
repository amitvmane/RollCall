"""
The human check (services/human_check.py): config, a proof-of-work challenge,
and exchanging a solved challenge (or a Turnstile token) for a 12-hour pass.

Routes that need a pass take `Depends(require_human)` or call
`check_human(...)`; without one they answer 428 Precondition Required, which
the page treats as "run the check, then retry".
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from services import human_check

router = APIRouter()

HUMAN_HEADER = "X-Human-Pass"


class HumanConfig(BaseModel):
    mode: str
    bits: Optional[int] = None
    site_key: Optional[str] = None


class Challenge(BaseModel):
    challenge: str
    bits: int


class VerifyRequest(BaseModel):
    challenge: Optional[str] = Field(None, max_length=200)
    nonce: Optional[str] = Field(None, max_length=32)
    turnstile_token: Optional[str] = Field(None, max_length=4096)


class PassResponse(BaseModel):
    human_pass: str
    expires_in: int = human_check.PASS_TTL_SECONDS


def check_human(human_pass: Optional[str]) -> None:
    if not human_check.use_pass(human_pass):
        raise HTTPException(status_code=428, detail="Please confirm you're a person first.",
                            headers={"X-Human-Check": human_check.mode()})


async def require_human(x_human_pass: Optional[str] = Header(None, alias=HUMAN_HEADER)) -> None:
    check_human(x_human_pass)


@router.get("/human/config", response_model=HumanConfig, summary="Which human check this server uses")
async def human_config() -> HumanConfig:
    return HumanConfig(**human_check.config())


@router.get("/human/challenge", response_model=Challenge, summary="A single-use proof-of-work challenge")
async def human_challenge() -> Challenge:
    return Challenge(**human_check.new_challenge())


@router.post("/human/verify", response_model=PassResponse, summary="Exchange a solved check for a 12-hour pass")
async def human_verify(body: VerifyRequest, request: Request) -> PassResponse:
    try:
        token = await human_check.issue_pass(
            challenge=body.challenge, nonce=body.nonce, turnstile_token=body.turnstile_token,
            client_ip=request.client.host if request.client else None)
    except human_check.HumanCheckFailed as e:
        raise HTTPException(status_code=403, detail=str(e))
    return PassResponse(human_pass=token)
