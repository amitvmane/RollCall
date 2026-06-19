"""
Single source of truth for every bot command.

Read by:
  - runner.register_commands()  → drives the Telegram BotCommand menu
                                  (default scope = user commands;
                                  admin scope = user + admin commands).
  - handlers/core.help_commands() → renders /help, /help admin, and the
                                    per-command detail view /help <name>.

If you add, rename, or remove a command, edit ONLY this file. The
CLAUDE.md command-registry sync rule is satisfied automatically — both
the menu and /help re-render from this list.

Schema per entry:
  name      str           command keyword, no leading slash, lowercase, _ separator
  aliases   list[str]     short aliases (no slash); used for both /help lookup and
                          the user-facing "(also /alias)" hint
  scope     "user" | "admin" | "super_admin"
                          "user"        → everyone can run; appears in user menu + both /help views
                          "admin"       → chat-admin only; appears in admin menu + /help admin
                          "super_admin" → bot-owner only (config.ADMINS); not in any menu,
                                          shown in /help admin
  category  str           heading the command groups under in /help
  args      str           argument format hint, e.g. "[title]" or "name <in|out|maybe>"
  sample    str           one full example invocation, including any args
  summary   str           one-line description for the bot menu and the /help list
  details   str           multi-line explanation for /help <name>. Plain text;
                          escape Markdown specials yourself if you include them.
"""

COMMANDS = [
    # ───────────────────────────── USER ─────────────────────────────
    {
        "name": "in", "aliases": [], "scope": "user", "category": "Vote",
        "args": "[comment]", "sample": "/in running 5 mins late",
        "summary": "Mark yourself as attending",
        "details": (
            "Marks you as IN for the current rollcall. If a comment is given, "
            "it's shown next to your name in the IN list.\n\n"
            "If multiple rollcalls are active, append ::N to target a specific "
            "one (e.g. /in ::2)."
        ),
    },
    {
        "name": "out", "aliases": [], "scope": "user", "category": "Vote",
        "args": "[comment]", "sample": "/out can't make it",
        "summary": "Mark yourself as not attending",
        "details": "Marks you as OUT. Optional comment shown in the OUT list. ::N targets one rollcall.",
    },
    {
        "name": "maybe", "aliases": [], "scope": "user", "category": "Vote",
        "args": "[comment]", "sample": "/maybe will know by 5pm",
        "summary": "Mark yourself as undecided",
        "details": "Marks you as MAYBE. Optional comment shown next to your name. ::N targets one rollcall.",
    },
    {
        "name": "rollcalls", "aliases": ["r"], "scope": "user", "category": "View Lists",
        "args": "", "sample": "/rollcalls",
        "summary": "List all active rollcalls",
        "details": "Lists every currently-open rollcall in this chat with their numbers (used for ::N targeting).",
    },
    {
        "name": "whos_in", "aliases": ["wi"], "scope": "user", "category": "View Lists",
        "args": "", "sample": "/wi",
        "summary": "Show who's IN",
        "details": "Prints the current IN list for the active rollcall. ::N for a specific one.",
    },
    {
        "name": "whos_out", "aliases": ["wo"], "scope": "user", "category": "View Lists",
        "args": "", "sample": "/wo",
        "summary": "Show who's OUT",
        "details": "Prints the OUT list. ::N for a specific rollcall.",
    },
    {
        "name": "whos_maybe", "aliases": ["wm"], "scope": "user", "category": "View Lists",
        "args": "", "sample": "/wm",
        "summary": "Show who's undecided",
        "details": "Prints the MAYBE list. ::N for a specific rollcall.",
    },
    {
        "name": "whos_waiting", "aliases": ["ww"], "scope": "user", "category": "View Lists",
        "args": "", "sample": "/ww",
        "summary": "Show the waitlist",
        "details": "Prints the waitlist (users who voted IN after the IN limit was reached).",
    },
    {
        "name": "stats", "aliases": ["s"], "scope": "user", "category": "Stats & History",
        "args": "[group | top | ghost | @user | name]",
        "sample": "/stats top",
        "summary": "Attendance stats and leaderboard",
        "details": (
            "/stats              — your own attendance, voting, streak\n"
            "/stats group        — chat-wide attendance summary (incl. proxies)\n"
            "/stats top          — top 10 by real attendance (incl. proxies)\n"
            "/stats ghost        — no-show leaderboard\n"
            "/stats @user        — another member's stats (use @username)\n"
            "/stats <Name>       — another member or proxy by display name\n"
            "/stats bot          — bot-wide stats (super-admin only)"
        ),
    },
    {
        "name": "history", "aliases": [], "scope": "user", "category": "Stats & History",
        "args": "[count] [page]", "sample": "/history 5 2",
        "summary": "Past ended rollcalls",
        "details": "Lists previously-ended rollcalls with IN counts. Default 10 per page.",
    },
    {
        "name": "timezone", "aliases": ["tz"], "scope": "user", "category": "Settings",
        "args": "Region/City", "sample": "/timezone Asia/Kolkata",
        "summary": "Set your timezone",
        "details": "Sets the bot's timezone for this chat — used for /when, scheduled templates, and auto-close timing.",
    },
    {
        "name": "help", "aliases": [], "scope": "user", "category": "Settings",
        "args": "[command | admin]", "sample": "/help start_roll_call",
        "summary": "Help — /help <command> for command details",
        "details": (
            "/help          — list user commands\n"
            "/help admin    — list admin commands\n"
            "/help <name>   — detailed help for a single command (args, example, full description)"
        ),
    },
    {
        "name": "version", "aliases": [], "scope": "user", "category": "Settings",
        "args": "", "sample": "/version",
        "summary": "Show bot version",
        "details": "Prints the deployed version of the bot and a short changelog summary.",
    },
    {
        "name": "weblink", "aliases": [], "scope": "user", "category": "Settings",
        "args": "", "sample": "/weblink",
        "summary": "Get web voting links for this group",
        "details": (
            "Returns two types of links:\n\n"
            "📌 Permanent group link — bookmark this once. Always shows the current "
            "active rollcall(s) for this group. Works even when Telegram is down.\n\n"
            "🔗 Per-rollcall links — direct links for each active rollcall. "
            "Expire when the rollcall ends.\n\n"
            "Anyone with a link can vote via their browser — no Telegram account needed. "
            "Requires WEB_BASE_URL to be configured on the server."
        ),
    },

    # ──────────────────────────── ADMIN ─────────────────────────────
    {
        "name": "start_roll_call", "aliases": ["src"], "scope": "admin", "category": "Rollcall",
        "args": "[title]", "sample": "/src Friday Football",
        "summary": "Start a new rollcall",
        "details": (
            "Starts a rollcall in this chat. If title is omitted, defaults to 'Roll Call'. "
            "Sends the inline vote panel immediately so members can vote in one tap. "
            "Admin-only. If multiple rollcalls are active, each is numbered for ::N targeting."
        ),
    },
    {
        "name": "end_roll_call", "aliases": ["erc"], "scope": "admin", "category": "Rollcall",
        "args": "[::N]", "sample": "/erc ::2",
        "summary": "End the active rollcall",
        "details": "Closes the rollcall, prints the final IN/OUT/MAYBE lists, attributes the ender, and triggers the ghost-mark prompt if ghost tracking is on.",
    },
    {
        "name": "panel", "aliases": [], "scope": "admin", "category": "Rollcall",
        "args": "[::N]", "sample": "/panel",
        "summary": "Resend vote panel with buttons",
        "details": "Re-sends the inline vote panel (useful if it scrolled out of view).",
    },
    {
        "name": "set_title", "aliases": ["st"], "scope": "admin", "category": "Settings",
        "args": "title", "sample": '/st "Sunday League W3"',
        "summary": "Set rollcall title",
        "details": "Renames the active rollcall. Shown in vote panels and history.",
    },
    {
        "name": "set_limit", "aliases": ["sl"], "scope": "admin", "category": "Settings",
        "args": "N", "sample": "/sl 14",
        "summary": "Set max IN attendees (0 = unlimited)",
        "details": "Caps the IN list. Extra IN votes go to the waitlist; they auto-promote when someone goes OUT.",
    },
    {
        "name": "set_rollcall_time", "aliases": ["srt"], "scope": "admin", "category": "Settings",
        "args": "DD-MM-YYYY HH:MM", "sample": "/srt 12-06-2026 19:30",
        "summary": "Set rollcall auto-close time",
        "details": "Schedules an auto-close at the given time. Triggers /erc behaviour automatically.",
    },
    {
        "name": "set_rollcall_reminder", "aliases": ["srr"], "scope": "admin", "category": "Settings",
        "args": "hours", "sample": "/srr 2",
        "summary": "Set reminder hours before close",
        "details": "Sends a one-time reminder ping N hours before the scheduled auto-close.",
    },
    {
        "name": "event_fee", "aliases": ["ef"], "scope": "admin", "category": "Settings",
        "args": "amount", "sample": "/ef 1200",
        "summary": "Set total event fee",
        "details": "Sets the total cost of the event. Shown in panels; used by /individual_fee.",
    },
    {
        "name": "individual_fee", "aliases": ["if"], "scope": "admin", "category": "Settings",
        "args": "", "sample": "/if",
        "summary": "Per-person fee split",
        "details": "Divides the configured event fee by the current IN-list size.",
    },
    {
        "name": "location", "aliases": ["loc"], "scope": "admin", "category": "Settings",
        "args": "place", "sample": "/loc Indiranagar Turf 3",
        "summary": "Set event location",
        "details": "Stores a location string shown in panels and reminders.",
    },
    {
        "name": "when", "aliases": ["w"], "scope": "admin", "category": "Settings",
        "args": "", "sample": "/when",
        "summary": "Show rollcall scheduled time",
        "details": "Displays the rollcall's scheduled close time in the chat's timezone.",
    },
    {
        "name": "shh", "aliases": [], "scope": "admin", "category": "Settings",
        "args": "", "sample": "/shh",
        "summary": "Enable silent mode (no ack messages)",
        "details": "Suppresses per-vote acknowledgement messages. Panels still update silently.",
    },
    {
        "name": "louder", "aliases": [], "scope": "admin", "category": "Settings",
        "args": "", "sample": "/louder",
        "summary": "Disable silent mode",
        "details": "Restores per-vote ack messages.",
    },
    {
        "name": "set_in_for", "aliases": ["sif"], "scope": "admin", "category": "Proxy",
        "args": "name [::N]", "sample": "/sif Alex ::1",
        "summary": "Mark a non-Telegram member as IN",
        "details": "Adds a proxy member to the IN list. Useful for members without Telegram. Their attendance is tracked.",
    },
    {
        "name": "set_out_for", "aliases": ["sof"], "scope": "admin", "category": "Proxy",
        "args": "name [::N]", "sample": "/sof Alex",
        "summary": "Mark a non-Telegram member as OUT",
        "details": "Adds or moves a proxy member to the OUT list.",
    },
    {
        "name": "set_maybe_for", "aliases": ["smf"], "scope": "admin", "category": "Proxy",
        "args": "name [::N]", "sample": "/smf Alex",
        "summary": "Mark a non-Telegram member as MAYBE",
        "details": "Adds or moves a proxy member to the MAYBE list.",
    },
    {
        "name": "templates", "aliases": [], "scope": "admin", "category": "Templates",
        "args": "", "sample": "/templates",
        "summary": "List saved templates",
        "details": "Prints all templates saved for this chat, including any active recurring schedules.",
    },
    {
        "name": "set_template", "aliases": [], "scope": "admin", "category": "Templates",
        "args": 'name "Title" [limit=N] [location=X] [fee=X]',
        "sample": '/set_template friday "Friday Football" limit=14 location="Turf 3" fee=200',
        "summary": "Create or update a template",
        "details": "Saves a reusable rollcall config. Use /start_template name to spin one up.",
    },
    {
        "name": "start_template", "aliases": [], "scope": "admin", "category": "Templates",
        "args": "name [title]", "sample": "/start_template friday",
        "summary": "Start a rollcall from a template",
        "details": "Starts a rollcall with the template's settings. Optional title overrides the template's.",
    },
    {
        "name": "delete_template", "aliases": [], "scope": "admin", "category": "Templates",
        "args": "name", "sample": "/delete_template friday",
        "summary": "Delete a template",
        "details": "Removes the named template. Any active schedule on it is cancelled.",
    },
    {
        "name": "schedule_template", "aliases": [], "scope": "admin", "category": "Templates",
        "args": "name <weekday|monthly|biweekly|off> <HH:MM>",
        "sample": "/schedule_template friday friday 18:00",
        "summary": "Schedule auto-start for a template",
        "details": (
            "Weekly:    /schedule_template name <weekday> <HH:MM>\n"
            "Biweekly:  /schedule_template name <weekday> <HH:MM> biweekly\n"
            "Monthly:   /schedule_template name monthly <day> <HH:MM>\n"
            "Disable:   /schedule_template name off"
        ),
    },
    {
        "name": "schedules", "aliases": [], "scope": "admin", "category": "Templates",
        "args": "", "sample": "/schedules",
        "summary": "View and toggle schedules",
        "details": "Lists every scheduled template auto-start and lets you toggle them on/off.",
    },
    {
        "name": "delete_user", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "name [::N]", "sample": "/delete_user Alex",
        "summary": "Remove a user from rollcall (asks confirmation)",
        "details": "Removes a member or proxy from any list. Asks for confirmation. Use @username to disambiguate if two users share a first name.",
    },
    {
        "name": "set_status", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "name <in|out|maybe> [::N]", "sample": "/set_status Alex in",
        "summary": "Override a user's status",
        "details": "Moves a user between IN / OUT / MAYBE. Asks for confirmation. Works for proxies too.",
    },
    {
        "name": "buzz", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "[message] [::N]", "sample": '/buzz "anyone in for tomorrow?"',
        "summary": "Ping members who haven't voted",
        "details": (
            "Pings everyone the bot has seen who hasn't voted on ANY currently-active rollcall. "
            "Optional custom message replaces the default. ::N narrows to a specific rollcall. "
            "30-second per-chat cooldown."
        ),
    },
    {
        "name": "set_admins", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "", "sample": "/set_admins",
        "summary": "Enable admin-only mode",
        "details": "After this, only chat admins can run admin commands.",
    },
    {
        "name": "unset_admins", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "", "sample": "/unset_admins",
        "summary": "Disable admin-only mode",
        "details": "Allow non-admins to run admin commands again.",
    },
    {
        "name": "toggle_ghost_tracking", "aliases": [], "scope": "admin", "category": "Ghost Tracking",
        "args": "[on|off]", "sample": "/toggle_ghost_tracking on",
        "summary": "Enable / disable ghost tracking",
        "details": "Ghost tracking flags IN-list users who didn't show. After enough misses, the bot asks them to reconfirm next time they vote IN.",
    },
    {
        "name": "set_absent_limit", "aliases": [], "scope": "admin", "category": "Ghost Tracking",
        "args": "N", "sample": "/set_absent_limit 2",
        "summary": "Missed sessions before reconfirmation",
        "details": "Sets how many ghosts trigger the reconfirmation prompt. 1 by default.",
    },
    {
        "name": "mark_absent", "aliases": [], "scope": "admin", "category": "Ghost Tracking",
        "args": "", "sample": "/mark_absent",
        "summary": "Review & mark no-shows from a past session",
        "details": "Walks through recently-ended rollcalls and lets you pick who actually didn't show. Resets streaks on selected users.",
    },
    {
        "name": "clear_absent", "aliases": [], "scope": "admin", "category": "Ghost Tracking",
        "args": "name", "sample": "/clear_absent Alex",
        "summary": "Reset ghost count for a user",
        "details": "Clears the user's accumulated ghost count back to zero.",
    },
    {
        "name": "audit_log", "aliases": [], "scope": "admin", "category": "Audit",
        "args": "[N]", "sample": "/audit_log 50",
        "summary": "View admin audit log",
        "details": "Paginated list of admin actions: rollcall starts/ends, buzzes, mode toggles, timezone changes, panel ends. Default 15 per page.",
    },

    # ────────────────────────── SUPER ADMIN ─────────────────────────
    {
        "name": "broadcast", "aliases": [], "scope": "super_admin", "category": "Super Admin",
        "args": '"message"', "sample": '/broadcast "scheduled maintenance tonight"',
        "summary": "Send a message to all bot chats",
        "details": "Bot-owner only. Broadcasts a message to every chat the bot is in. Use sparingly.",
    },
]


# ── Lookup helpers ────────────────────────────────────────────────────────

# Build once at import time.
_BY_NAME = {c["name"]: c for c in COMMANDS}
_BY_ALIAS = {}
for _c in COMMANDS:
    for _a in _c.get("aliases", []):
        _BY_ALIAS[_a] = _c


def lookup_command(name: str):
    """Return the command entry for `name` or any alias, or None.
    Strips a leading slash and lowercases. Handles 'srt' as well as 'set_rollcall_time'."""
    if not name:
        return None
    key = name.strip().lstrip('/').lower()
    return _BY_NAME.get(key) or _BY_ALIAS.get(key)


def all_names_and_aliases():
    """Flat list of every name + alias — used by the fuzzy suggester."""
    out = []
    for c in COMMANDS:
        out.append(c["name"])
        out.extend(c.get("aliases", []))
    return out


# ── Category order (for /help layout) ─────────────────────────────────────

# Order each category appears in the rendered /help. Categories not listed
# here fall to the bottom in COMMANDS order.
USER_CATEGORY_ORDER = ["Vote", "View Lists", "Stats & History", "Settings"]
ADMIN_CATEGORY_ORDER = [
    "Rollcall", "Settings", "Proxy", "Templates",
    "User Management", "Ghost Tracking", "Audit", "Super Admin",
]


def commands_for_scope(scope_set):
    """Return commands whose scope is in `scope_set`, preserving original order
    inside their category. `scope_set` is e.g. {"user"} or {"user", "admin", "super_admin"}."""
    return [c for c in COMMANDS if c["scope"] in scope_set]
