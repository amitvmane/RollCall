"""
How a REST route names a rollcall — by its permanent id, or (deprecated) by
its display number.

The display number (#1, #2, #3) is a position: when an earlier rollcall ends
or is cancelled, every later one renumbers. A caller who read "#2" and then
acted on "#2" could land on a different rollcall — which is exactly what an
outside app polling and acting on a schedule will eventually do. The bot's
buttons and the web page moved to the database id on 2026-10-19; this does the
same for the REST API.

Every rollcall route is registered twice, on ONE handler:

    /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/...   stable (use this)
    /chats/{chat_id}/rollcalls/{rc_number}/...           deprecated, still works

`rollcall_ref` turns either into an `RcRef` carrying the permanent id. Handlers
then re-find the rollcall BY ID inside the chat write lock (`index_now`), so
even a deprecated number-based call can only ever act on the rollcall that
number pointed to when the request arrived — never on one that slid into its
place while it waited for the lock.

Number-based responses carry `Deprecation: true` and a `Link` to the stable
path; every response carries `X-Rollcall-Id`, so a client can switch over
from what it already receives.
"""

from dataclasses import dataclass
from fastapi import Depends, HTTPException, Path, Request, Response

from services.common import find_rollcall_index


@dataclass
class RcRef:
    chat_id: int
    rollcall_id: int
    number_at_request: int     # 1-based, as it was when the request arrived
    by_number: bool            # came in on a deprecated number-based path

    def index_now(self) -> int:
        """0-based position of this rollcall NOW. Call inside the chat lock."""
        idx = find_rollcall_index(self.chat_id, self.rollcall_id)
        if idx is None:
            from exceptions import rollCallNotStarted
            raise rollCallNotStarted("That rollcall has ended or doesn't exist in this chat.")
        return idx

    def number_now(self) -> int:
        """1-based number for display/mirroring after the action; falls back
        to the number at request time if the rollcall has since closed."""
        idx = find_rollcall_index(self.chat_id, self.rollcall_id)
        return idx + 1 if idx is not None else self.number_at_request


_NUMBER_DOC = ("DEPRECATED — 1-based display number; shifts when an earlier rollcall ends. "
               "Use the /by-id/{rollcall_id} path.")
_ID_DOC = "Permanent rollcall id (the `id` field of every rollcall response)"


def _by_number(rc_number: int = Path(..., ge=1, description=_NUMBER_DOC)) -> int:
    return rc_number


def _by_id(rollcall_id: int = Path(..., ge=1, description=_ID_DOC)) -> int:
    return rollcall_id


# Route-level dependencies: they declare, validate and document the path
# parameter of each registration; `rollcall_ref` then reads whichever is set.
BY_NUMBER = [Depends(_by_number)]
BY_ID = [Depends(_by_id)]


def stable_path(path: str) -> str:
    """The id-based twin of a number-based path."""
    return path.replace("/rollcalls/{rc_number}", "/rollcalls/by-id/{rollcall_id}")


async def rollcall_ref(request: Request, response: Response,
                       chat_id: int = Path(..., description="Chat id")) -> RcRef:
    from rollcall_manager import manager
    from exceptions import rollCallNotStarted
    params = request.path_params
    # The route-level Path(ge=1) dependency documents the parameter, but its
    # validation error is only reported after every dependency has run — so
    # this one checks too, rather than answering first with a 404.
    raw = params.get("rollcall_id", params.get("rc_number"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        raise HTTPException(status_code=422, detail="The rollcall in the path must be a positive integer.")
    if "rollcall_id" in params:
        idx = find_rollcall_index(chat_id, value, manager)
        if idx is None:
            # Also what a rollcall of ANOTHER chat looks like: ids are global,
            # so the chat in the path must own it.
            raise rollCallNotStarted("No open rollcall with that id in this chat.")
        ref = RcRef(chat_id, value, idx + 1, by_number=False)
    else:
        # Exactly the errors this path always gave (404 none open, 422 out of
        # range) — existing clients depend on them.
        from services.common import resolve_rollcall_or_raise
        rc = resolve_rollcall_or_raise(chat_id, value - 1)
        ref = RcRef(chat_id, int(rc.id), value, by_number=True)
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = (f'</api/v1/chats/{chat_id}/rollcalls/by-id/{ref.rollcall_id}>; '
                                    f'rel="successor-version"')
    response.headers["X-Rollcall-Id"] = str(ref.rollcall_id)
    return ref


def dual(router_method, path: str, **kwargs):
    """Register one handler on the stable id path and the deprecated number path.

        @dual(router.post, "/chats/{chat_id}/rollcalls/{rc_number}/votes", ...)
        async def cast_vote(..., ref: RcRef = Depends(rollcall_ref)): ...
    """
    summary = kwargs.pop("summary", None)

    def deco(fn):
        router_method(stable_path(path), dependencies=BY_ID, summary=summary, **kwargs)(fn)
        router_method(path, dependencies=BY_NUMBER, deprecated=True,
                      summary=(f"{summary} (deprecated: by display number)" if summary else None),
                      **kwargs)(fn)
        return fn
    return deco
