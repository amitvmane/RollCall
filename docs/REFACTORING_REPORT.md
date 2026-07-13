# RollCall Refactoring Report

**Generated:** 2026-07-13 at commit `751bdff`.
**Purpose:** Self-contained backlog of code-quality issues and enhancements, written so any
session (including a smaller/cheaper model) can execute items one at a time without
re-exploring the codebase. Every claim below was verified against the code at the commit
above — re-verify file:line anchors if the file has changed since.

---

## How to execute items from this report

1. Pick ONE item per work session. Do not batch unrelated items into one commit.
2. Read the item's **Evidence** locations first; confirm they still match.
3. Apply the **Fix shape** exactly — do not expand scope.
4. Run the item's **Verify** steps, plus always:
   - `.venv312/bin/python -m pytest tests/ -q` (856 tests at time of writing)
   - `.venv312/bin/python -m pytest integration_tests/ -q` (761 tests — MUST be a separate
     pytest invocation; the two suites cannot run together)
   - `.venv312/bin/python scripts/smoke_test.py` (required if touching bot_state.py,
     runner.py, or any import chain)
5. Commit with a `refactor:` / `fix:` prefix. No Co-Authored-By lines. Push to main
   directly (repo convention) and confirm CI green via
   `gh run watch <run-id> --exit-status` (unpiped — piping masks the exit code).

## House rules — intentional patterns, do NOT "fix" these

- `dues_entries` / `fund_transactions` are **append-only**. Never UPDATE/DELETE;
  corrections are compensating entries. (`game_closures` is metadata, not append-only.)
- db.py supports sqlite AND postgres via `ph = "%s" if db_type == "postgresql" else "?"`.
  The f-string-with-`ph` SQL style is deliberate; user data always goes through
  placeholders (audited — no injection found).
- Handlers raise custom exceptions from `exceptions.py`; `reply_error()` renders them.
  Never `bot.send_message(cid, str(e))` for caught exceptions.
- All chat-state mutations run inside `async with manager.get_chat_write_lock(cid):`,
  re-fetching the rollcall inside the lock.
- `commands_registry.py` is the single source of truth for bot commands (all 8 fields).
- `logging.exception("context")` in except blocks; `traceback.format_exc()` is banned.
- `tests/` mocks db+telebot (conftest.py); `integration_tests/` uses real SQLite with
  mocked Telegram API. Intentionally separate.
- Services are platform-agnostic (no telebot imports) — one known violation is item R3.
- The web voting page's avatar palette (`web/app.js` `AV_COLORS`) is a categorical
  palette, distinct from the design tokens in `/shared/tokens.css`. Leave it.

---

# Priority 1 — correctness / durability

## R1. `close_game` writes the financial close non-atomically
- **Files:** `rollCall/services/dues.py` (close_game, ~lines 330–576); the db writers it
  calls in `rollCall/db.py` (`create_game_closure`, `add_dues_entry`,
  `add_fund_transaction`) each open their own connection and `conn.commit()` individually.
- **Problem:** One game close = 1 closure row + N member share entries + collector
  reimbursement + fund transactions, each committed separately. A crash mid-sequence
  leaves a *closed* game (closure row exists, so re-close is blocked by the
  UNIQUE(rollcall_id) constraint) with some or all dues rows missing — silent ledger
  shortfall. Same exposure in `cancel_game_dues` (reversal entries) and
  `close_empty_game` to a lesser degree.
- **Fix shape:** Add a batch writer to db.py (transactions belong in the db layer, not
  services): e.g. `db.write_game_closure_batch(closure_kwargs, dues_entries: list[dict],
  fund_txs: list[dict]) -> int` that opens ONE connection, executes all inserts, commits
  once, rolls back all on any failure, returns closure id. Then `close_game` collects its
  writes into lists and makes a single call. Keep the individual writer functions for all
  other callers (mark_paid etc. are single-row — they're fine).
- **Risk:** needs-care. The batch function must reuse the exact INSERT SQL of the
  existing writers (copy, don't re-derive), handle both sqlite and postgres placeholder
  styles, and preserve `_dues_now()` timestamps. Add an integration test that monkeypatches
  the batch to fail after the closure insert and asserts NO rows landed (rollback proof).
- **Verify:** new integration test + `pytest integration_tests/ -q` green; run a manual
  `/settle_dues` flow via existing tests (`test_dues_scenario.py` must stay green).

## R2. `/rotate_collector` handler: no write lock, bypasses services layer
- **Files:** `rollCall/handlers/dues.py:703` and `:710` — `_db.update_chat_settings(cid,
  collector_rotation=…)` called directly from the handler, outside any
  `manager.get_chat_write_lock(cid)` block.
- **Problem:** Violates both house rules at once (handlers→services→db layering, and
  lock-wrapped mutations). Low practical risk today (idempotent toggle) but it's the only
  dues mutation not behind the lock, and drift like this is how the next race bug lands.
- **Fix shape:** Add `services/dues.py::set_collector_rotation(chat_id, enabled: bool)
  -> dict` (returns `{"enabled": bool, "announcement": str}`); handler wraps the call in
  the write lock and sends `result["announcement"]` via `send_md_fallback`.
- **Risk:** safe-mechanical.
- **Verify:** existing `tests/test_dues_handlers.py` green; add one unit test asserting
  the service function calls `db.update_chat_settings` with the right kwarg.

## R3. Services layer imports from the bot layer (`_esc_md`)
- **Files:** `rollCall/services/dues.py:18` — `from bot_state import _esc_md`; ~20 call
  sites in that file.
- **Problem:** The services layer is documented as platform-agnostic (the whole
  multi-platform plan depends on it), but importing from `bot_state` drags the entire bot
  machinery (AsyncTeleBot construction!) into any context that imports services.
- **Fix shape:** Move `_esc_md` to a neutral module — `rollCall/utils/text.py` (create
  it; utils/ already exists) — keep a re-export in `bot_state.py`
  (`from utils.text import _esc_md`) so all existing handler imports keep working, and
  change services/dues.py to import from `utils.text`.
- **Risk:** safe-mechanical, but MUST run `scripts/smoke_test.py` afterward (import-chain
  change) and check no circular import (utils must not import bot_state or db).
- **Verify:** smoke test + both pytest suites green; `grep -rn "from bot_state" rollCall/services/` returns nothing.

## R4. `_sched_selection` never pruned (slow memory leak)
- **Files:** `rollCall/bot_state.py:98` (`_sched_selection: dict = {}`); consumers in
  `rollCall/handlers/templates.py` (lines ~94, 137, 528–553); prune loop in
  `rollCall/runner.py` (`memory_prune_loop`) covers `_pending_deletes`,
  `_pending_overrides`, `_pending_proxy_add`, `_pending_reconf`, `_pending_subsidy_input`,
  `_pending_payment_input` — but not `_sched_selection`.
- **Problem:** Entries are popped on apply/cancel, but an abandoned scheduling panel
  leaves its entry forever. Keyed per chat_id so growth is bounded by chat count — minor,
  but it's the only session dict outside the prune regime.
- **Fix shape:** NOTE — values are bare `set`s with no `_ts`, so `_prune_pending()`
  cannot be used as-is. Either (a) restructure entries to
  `{"names": set, "_ts": time.time()}` and update templates.py's 5 access sites, then add
  `_prune_pending(_sched_selection)` to the loop, or (b) simpler: in the prune loop just
  `_sched_selection.clear()` if it exceeds e.g. 500 entries. Option (a) is cleaner;
  option (b) is a two-line guard. Either is acceptable.
- **Risk:** safe-mechanical (option b) / needs-care (option a — touches panel state shape).
- **Verify:** `tests/` green; if option (a), manually trace all `_sched_selection`
  accesses in templates.py compile (`python -m py_compile`).

## R5. Rate limiter never deletes empty buckets
- **Files:** `rollCall/api/rate_limit.py` — `bucket = _buckets[key]` (~line 82; it's a
  defaultdict-style dict of deques), trim loop `while bucket and bucket[0] < cutoff:
  bucket.popleft()` — no `del _buckets[key]` anywhere.
- **Problem:** One request from a unique IP/token leaves an empty deque in `_buckets`
  forever. Internet-facing (Cloudflare tunnel), so key cardinality is unbounded.
- **Fix shape:** After the trim loop, before appending the new timestamp is NOT the place
  (bucket is about to be used); instead add an occasional sweep: every Nth request (e.g.
  `if len(_buckets) > 1000:`) iterate and `del` keys whose deques are empty or whose
  newest entry is older than `cutoff`. Keep it inline in the middleware — no new task.
- **Risk:** safe-mechanical.
- **Verify:** `tests/` green (rate-limit tests exist in test_api_routes/test_security_fixes —
  grep to confirm which and run them); add a unit test: fill 1500 keys with stale entries,
  make one request, assert `len(_buckets)` dropped.

---

# Priority 2 — duplication / structure

## R6. Admin-gate check copy-pasted 4×
- **Files:** `rollCall/handlers/payment_panel.py:125` (`_payment_admin_ok`),
  `rollCall/handlers/penalty_panel.py:224` (inline in callback handler),
  `rollCall/handlers/dues.py:113` (`_settle_admin_ok`) and `:629` (inline). All four do:
  `manager.get_admin_rights(cid)` → `bot.get_chat_member(cid, uid)` → status check.
- **Problem:** Four near-identical copies; message text and edge handling already drift.
- **Fix shape:** Add `async def is_chat_admin(cid: int, uid: int) -> bool` to
  `bot_state.py` (it already owns `bot` + imports manager-adjacent helpers). Replace the
  four bodies with calls; keep each site's own error message/alert behavior.
- **Risk:** safe-mechanical, but preserve semantics exactly: the check passes if
  `manager.get_admin_rights(cid)` is falsy OR the member status is
  "administrator"/"creator" — read each site first, they gate in the same direction but
  return/raise differently.
- **Verify:** `tests/test_dues_handlers.py`, `tests/test_penalty_panel.py`, and
  `integration_tests/test_dues_handlers_integration.py` (admin-gate tests exist for the
  payment panel) all green.

## R7. `::N` rollcall-suffix parsing duplicated ~7×
- **Files:** `rollCall/handlers/dues.py:69–79` has the extracted helper
  (`_parse_rc_suffix`); `rollCall/handlers/admin.py:119–124` and `:189–191`, and
  `rollCall/handlers/lists.py` (5 copies: ~35, 65, 95, 125, 210) re-implement it inline
  with divergent error messages.
- **Fix shape:** Move `_parse_rc_suffix` to `utils/text.py` (see R3 — same new module),
  import it in dues.py/admin.py/lists.py, delete the inline copies. Keep the strictest
  existing error behavior (`incorrectParameter` on invalid/zero suffix).
- **Risk:** safe-mechanical. Diff each inline copy against the helper first — if any site
  intentionally accepts a different format, leave that site alone and note it here.
- **Verify:** `tests/` green (list/dues suffix tests exist: grep `::` in tests/).

## R8. `_require_identity` duplicated across API route modules
- **Files:** `rollCall/api/routes/dues.py:71` and `rollCall/api/routes/portal.py:28` —
  identical helper (verify_identity_token → 401 on failure); `rollCall/api/routes/web.py`
  additionally calls `verify_identity_token` inline ~8–12 times with per-site null
  handling.
- **Fix shape:** Two-step, do step 1 only unless step 2 stays trivially mechanical:
  (1) move `_require_identity` into `rollCall/api/identity.py` (it already owns
  verify_identity_token) and import it in dues.py/portal.py. (2) web.py's inline sites
  have per-route nuances (some optional-identity paths) — convert ONLY the sites that are
  exact matches of the strict pattern; leave optional-identity sites untouched.
- **Risk:** step 1 safe-mechanical; step 2 needs-care (read each web.py site — some
  endpoints deliberately allow anonymous access).
- **Verify:** `tests/test_web_routes.py`, `tests/test_identity_security.py`,
  `tests/test_portal.py` green.

## R9. db.py: 151 functions × identical connection boilerplate
- **Files:** `rollCall/db.py` (6,426 lines; ~150 repetitions of
  get_connection/cursor/try/except/finally/release_connection).
- **Problem:** Pure boilerplate mass; plus three coexisting error conventions (mutators
  that raise, e.g. `create_game_closure:5354`; mutators that return False, e.g.
  `update_chat_settings:1675`; readers that swallow and return None, e.g.
  `get_game_closure:5378`).
- **Fix shape:** DO NOT attempt a big-bang rewrite. Add a contextmanager to db.py:
  `@contextmanager def _cursor(commit: bool = False):` yielding a cursor, handling
  close/release/rollback. Migrate functions **incrementally** (10–20 per session, run
  integration_tests after each batch — they hit real SQLite). Do NOT change any
  function's error convention while migrating (raise/False/None stays as-is per function);
  unifying conventions is a separate, needs-design-decision task not covered here.
- **Risk:** needs-care. Highest-volume item in this report; only start it when there's
  appetite for several sessions. Skipping it is acceptable — it's cosmetic mass, not a bug.
- **Verify:** after every batch: `pytest integration_tests/ -q` (real-DB coverage) +
  `pytest tests/ -q` + smoke test.

## R10. `services/stats.py` executes raw SQL directly
- **Files:** `rollCall/services/stats.py:44` and `:106` — `from db import get_connection,
  db_type, release_connection` + inline SQL inside service functions.
- **Problem:** Only service module that bypasses db.py helpers; breaks the layering that
  every other service follows.
- **Fix shape:** Extract the two queries into named db.py functions (e.g.
  `db.find_user_for_stats(chat_id, token)` — copy the SQL verbatim including the `ph`
  pattern), call them from stats.py, drop the get_connection import.
- **Risk:** safe-mechanical.
- **Verify:** `tests/` green (stats handler tests exist); grep
  `get_connection` in `rollCall/services/` returns nothing afterward.

## R11. `lifecycle.py::callback_handler` is a 324-line monolith
- **Files:** `rollCall/handlers/lifecycle.py:554–877` (single function to EOF) —
  dispatches votes, reconfirmation, end-rollcall buttons, waitlist promotion, ghost logic.
- **Fix shape:** Mechanical split into a router + per-action helpers
  (`_cb_vote`, `_cb_end`, `_cb_reconf`, `_cb_promote`…), each taking `(call, cid, …)`.
  Preserve the exact dispatch order and shared local state (several branches share
  early-computed variables — hoist them into the router and pass explicitly).
- **Risk:** needs-design-decision — this is the bot's hottest code path (every panel
  tap). Only do it with the integration suite as a net, and split in 2–3 commits (one
  branch family per commit), not one.
- **Verify:** full `integration_tests/` suite green after each commit (voting flows have
  heavy coverage there); smoke test.

---

# Priority 3 — hygiene / polish

## R12. `datetime.utcnow()` deprecation (Python 3.12 warnings)
- **Files:** `rollCall/db.py` (10 occurrences — includes several via the
  `__import__('datetime')` anti-pattern around lines 4932–4988 and 6135),
  `rollCall/services/dues.py:660`, `rollCall/handlers/dues.py:1407`.
  db.py already has `_utcnow_naive()` (~line 14) used by some call sites.
- **Fix shape:** Replace every `datetime.utcnow()` in db.py with `_utcnow_naive()`;
  remove the `__import__('datetime')` calls entirely. In services/dues.py and
  handlers/dues.py replace with `datetime.datetime.now(datetime.UTC)` **only if** the
  string formatting doesn't change output (both sites strftime immediately — naive vs
  aware doesn't alter the formatted string); otherwise import `_utcnow_naive` from db.
- **Risk:** safe-mechanical.
- **Verify:** both suites green; `pytest tests/ -q 2>&1 | grep -c "utcnow.*deprecated"`
  drops to 0 (currently ~12 warnings).

## R13. config.py: no startup validation of critical env vars
- **Files:** `rollCall/config.py` (~line 20 TELEGRAM_TOKEN, ~29 DATABASE_URL).
- **Fix shape:** Fail fast with clear messages: empty/None TELEGRAM_TOKEN → raise
  ValueError("TELEGRAM_TOKEN / API_KEY not set"); DATABASE_URL not starting with
  sqlite/postgres → raise. CAUTION: tests mock config wholesale (conftest.py replaces the
  module), and smoke_test sets dummy env vars — confirm both still pass, and that the
  dummy token used by smoke test remains accepted (it must contain a colon per telebot's
  own validation — don't add a stricter format check than telebot's).
- **Risk:** safe-mechanical with the caution above.
- **Verify:** both suites + smoke test.

## R14. CI lint job: no pip cache, inline flake8 config
- **Files:** `.github/workflows/ci.yml` (lint job ~lines 55–62 has no cache step;
  flake8 args inline in yaml).
- **Fix shape:** Add the same `actions/cache` step the test job uses; move flake8
  ignores/max-line-length into a `.flake8` file at repo root and have both CI and local
  dev read it.
- **Risk:** safe-mechanical.
- **Verify:** push → `gh run watch <id> --exit-status` green; lint job time should drop.

## R15. Three web.py endpoints return bare `dict` with no response_model
- **Files:** `rollCall/api/routes/web.py:302`, `:349`, `:404` (`-> dict` signatures;
  neighbors correctly use `response_model=`).
- **Fix shape:** Add matching pydantic schemas to `rollCall/api/schemas/web.py`, set
  `response_model=` on the three decorators. Field names must match the currently
  returned dict keys exactly (read the return statements first).
- **Risk:** safe-mechanical.
- **Verify:** `tests/test_web_routes.py` green.

## R16. Silent `except Exception: pass` sites lack justification
- **Files:** `rollCall/handlers/dues.py:667` and `:673`,
  `rollCall/handlers/payment_panel.py:228` (representative — grep
  `except Exception:\n\s*pass` handlers/ for the full list).
- **Fix shape:** For each: if genuinely best-effort (panel refresh, cosmetic edit), add a
  one-line comment stating what's acceptable to lose; otherwise convert to
  `logging.exception("context")`. Do not change control flow.
- **Risk:** safe-mechanical.
- **Verify:** `tests/` green.

## R17. tests/conftest.py db_mock has no spec (silent drift)
- **Files:** `tests/conftest.py:39` — `db_mock = MagicMock()`; ~30 attributes configured
  vs 151 real db functions. A renamed/removed db function keeps "working" in unit tests.
- **Fix shape:** CAUTION — `MagicMock(spec=...)` requires importing real db.py, which the
  conftest deliberately avoids (it mocks before import). A safer increment: add a tiny
  meta-test (`tests/test_conftest_drift.py`) that imports the real `rollCall/db.py` source
  as TEXT, regex-extracts `^def (\w+)` names, and asserts every explicitly-configured
  mock attribute in conftest corresponds to a real function name. Catches deletions/renames
  without changing the mocking architecture.
- **Risk:** needs-care (don't break the mock-before-import ordering).
- **Verify:** new meta-test passes; temporarily misspell one mock attr to confirm it fails.

## R18. Housekeeping: long-lived uncommitted local changes
- **Files:** `dockerfile` (adds a fonts package — looks intentional for card-gen),
  `scripts/merge_db.py`, `.DS_Store` (should be gitignored), plus `.venv*/`,
  `__pycache__/`, `.coverage` untracked noise.
- **Fix shape:** ASK THE USER before committing/reverting — these predate this report and
  weren't authored in the audited sessions. Suggest: gitignore `.DS_Store`, `.coverage`,
  `.venv*/`; then user decides on dockerfile/merge_db.py diffs.
- **Risk:** needs-user-decision.

---

# Considered and rejected (do NOT do these)

- **Announcement-template registry** (dict of lambdas for dues announcement strings):
  the inline f-strings are more readable than an indirection layer; duplication is
  superficial (shared emoji, not shared logic). Rejected.
- **Unifying db.py's three error conventions in one pass:** every caller would need
  auditing simultaneously; only do per-function opportunistically during R9 migration —
  and even then, don't.
- **Centralizing the `require_scope` Depends boilerplate** in API routes: the per-route
  explicitness is arguably a feature (visible auth per endpoint). Leave unless it drifts.
- **Timing-side-channel hardening of the identity-token expiry check:** expiry timestamps
  aren't secrets; `hmac.compare_digest` already guards the signature. No action.
- **Renaming CSS classes / JS-generated markup** during any web work: panels build HTML in
  JS template strings; renames are cross-file risk with zero user value.

# Known-good patterns to imitate (for new code)

- Guided-panel pattern: `handlers/payment_panel.py` (session dataclass keyed by
  (chat_id, message_id), index-based callbacks, pending-input dict + reply capture).
- Double-close guard: `handlers/penalty_panel.py` re-checks `db.get_game_closure(rc)`
  inside the write lock before applying.
- `known_identity` bypass: `services/dues.py::mark_paid` — when a panel already holds the
  concrete identity, skip name re-resolution.
- Fix-verification discipline: when adding a regression test, temporarily revert the fix
  and confirm the test fails (see `dues_export_csv` collision test in
  `tests/test_dues_services.py`).
