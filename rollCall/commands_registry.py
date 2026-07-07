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
        "name": "summary", "aliases": [], "scope": "user", "category": "Stats & History",
        "args": "[days]", "sample": "/summary 14",
        "summary": "Recap of recent sessions",
        "details": (
            "Shows a recap of the last 7 days (or a custom period up to 90 days): "
            "number of sessions, average attendance, top 3 attendees, and a per-session list."
        ),
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
        "name": "version", "aliases": ["v"], "scope": "user", "category": "Settings",
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
        "name": "weblogin", "aliases": [], "scope": "admin", "category": "Settings",
        "args": "<name or @username>",
        "sample": "/weblogin Amit",
        "summary": "Generate a one-time web login link for a member",
        "details": (
            "Creates a single-use login URL for a group member who cannot go through "
            "the normal Telegram verification flow — for example when Telegram is down "
            "or the member doesn't have the app installed.\n\n"
            "The link expires in 7 days and can only be used once. Share it via "
            "WhatsApp, SMS, or email. When the member opens it, they are automatically "
            "authenticated on the group web page.\n\n"
            "The member must have previously chatted in the group so the bot knows "
            "their account (run /weblink first to register yourself).\n\n"
            "Example: /weblogin @Amit_Shah or /weblogin Ravi"
        ),
    },
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
        "name": "repeat", "aliases": ["rpt"], "scope": "admin", "category": "Rollcall",
        "args": "", "sample": "/repeat",
        "summary": "Start a new rollcall cloned from the last one",
        "details": (
            "Clones the last ended rollcall's title, IN-list limit, location, and event fee "
            "into a fresh rollcall — no retyping the same settings every week. "
            "Finalize time is NOT carried over; set it with /srt if needed."
        ),
    },
    {
        "name": "end_roll_call", "aliases": ["erc"], "scope": "admin", "category": "Rollcall",
        "args": "[::N]", "sample": "/erc ::2",
        "summary": "End the active rollcall",
        "details": "Closes the rollcall, prints the final IN/OUT/MAYBE lists, attributes the ender, and triggers the ghost-mark prompt if ghost tracking is on.",
    },
    {
        "name": "cancel_roll_call", "aliases": ["xrc"], "scope": "admin", "category": "Rollcall",
        "args": "[reason] [::N]", "sample": "/xrc rain",
        "summary": "Cancel a rollcall — no stats recorded",
        "details": (
            "Cancels the rollcall without recording any attendance stats. "
            "Use this when the session didn't happen (bad weather, venue unavailable, too few players, etc.).\n\n"
            "Cancelled sessions:\n"
            "• Are stored in history as CANCELLED\n"
            "• Are excluded from attendance rate and total session counts\n"
            "• Do NOT break streaks — they are treated as if the session never happened\n"
            "• Do NOT trigger ghost tracking\n\n"
            "An optional reason can be added: /xrc rain    /xrc venue closed\n\n"
            "Use /erc to end a session normally (stats recorded)."
        ),
    },
    {
        "name": "panel", "aliases": [], "scope": "admin", "category": "Rollcall",
        "args": "[::N]", "sample": "/panel",
        "summary": "Resend vote panel with buttons",
        "details": "Re-sends the inline vote panel (useful if it scrolled out of view).",
    },
    {
        "name": "card", "aliases": ["mc"], "scope": "admin", "category": "Rollcall",
        "args": "[::N]", "sample": "/card",
        "summary": "Share match-day player card",
        "details": (
            "Generates a shareable image card showing the current IN list — "
            "great for posting to WhatsApp or other group chats.\n\n"
            "Use `::N` to pick from multiple active rollcalls."
        ),
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
        "name": "auto_buzz", "aliases": [], "scope": "admin", "category": "User Management",
        "args": "<hours | off>", "sample": "/auto_buzz 3",
        "summary": "Auto-ping non-voters before close time",
        "details": (
            "Automatically pings members who haven't voted N hours (1-48) before a "
            "rollcall's scheduled close time. Fires once per rollcall, survives bot "
            "restarts, and is skipped when the IN list is already full. "
            "Only applies to rollcalls with a close time set (/srt or scheduled templates). "
            "/auto_buzz off disables. /auto_buzz alone shows current status."
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
    {
        "name": "gentoken", "aliases": [], "scope": "admin", "category": "API Access",
        "args": "", "sample": "/gentoken",
        "summary": "Generate an API token for the admin dashboard",
        "details": (
            "Issues a personal API token scoped to this group (read + vote + admin), "
            "valid for 1 year. The token is sent to you via private DM — never posted in the group.\n\n"
            "Only Telegram group admins (administrator or creator) can run this command.\n\n"
            "Use the token to log in to the admin dashboard at /admin/ on the bot server.\n\n"
            "When your token expires, run /gentoken again to get a fresh one."
        ),
    },

    # ──────────────────────── DUES & FUND (user) ────────────────────
    {
        "name": "my_dues", "aliases": ["md"], "scope": "user", "category": "Dues & Fund",
        "args": "", "sample": "/my_dues",
        "summary": "Your outstanding dues and payment history",
        "details": (
            "Shows your current balance and the 5 most recent ledger entries.\n\n"
            "If a UPI address is configured, a pay link is shown when you owe money."
        ),
    },
    {
        "name": "fund", "aliases": [], "scope": "user", "category": "Dues & Fund",
        "args": "", "sample": "/fund",
        "summary": "Show the group fund balance",
        "details": (
            "Displays the total group fund balance — the running total of all "
            "rounding remainders, penalties, subsidies, expenses and top-ups.\n\n"
            "Balance = booked amount; it may not equal actual cash on hand."
        ),
    },
    {
        "name": "fund_history", "aliases": ["fh"], "scope": "user", "category": "Dues & Fund",
        "args": "[page]", "sample": "/fh 2",
        "summary": "Paginated fund transaction history",
        "details": "Lists fund in/out entries newest first. Pass a page number for older entries.",
    },
    {
        "name": "mark_paid", "aliases": ["paid"], "scope": "user", "category": "Dues & Fund",
        "args": "name [amount]", "sample": "/paid Alice",
        "summary": "Record a payment received from a member",
        "details": (
            "Records a payment, clearing dues.\n\n"
            "• Admin: can record payment for anyone.\n"
            "• Designated collector (set via /set_collector): can record payment too.\n"
            "• Amount defaults to the member's full outstanding balance.\n"
            "  Overpayments are accepted and appear as negative balance (credit).\n\n"
            "Example: /paid Alice 90"
        ),
    },
    # ──────────────────────── DUES & FUND (admin) ───────────────────
    {
        "name": "close_game", "aliases": ["cg"], "scope": "admin", "category": "Dues & Fund",
        "args": "[subsidy] [::N]", "sample": "/cg",
        "summary": "Financially close a game — split costs and record shares",
        "details": (
            "Closes the current (or most-recent ended) game:\n"
            "  1. Reads player count from the IN list.\n"
            "  2. Reads ground cost from /ef (first digit group).\n"
            "  3. Splits cost equally; rounds each share UP to the nearest step "
            "(set via /set_round_step, default ₹10).\n"
            "  4. Rounding surplus goes to the group fund.\n"
            "  5. Optional [subsidy] deducts from fund balance before splitting.\n\n"
            "If an active rollcall is open, it's ended first (stats recorded).\n\n"
            "Examples: /cg  |  /cg 60  |  /cg ::2"
        ),
    },
    {
        "name": "mark_late", "aliases": ["ml"], "scope": "admin", "category": "Dues & Fund",
        "args": "player_name minutes", "sample": "/mark_late Alice 20",
        "summary": "Assess a late penalty — tier auto-chosen from minutes",
        "details": (
            "Picks the configured tier whose threshold is ≤ the given minutes "
            "and charges it to the player.\n\n"
            "Configure thresholds with /add_penalty mins:<N>.\n\n"
            "Examples: /mark_late Alice 20  |  /ml Bob 8"
        ),
    },
    {
        "name": "mark_ditch", "aliases": ["mdt"], "scope": "admin", "category": "Dues & Fund",
        "args": "player_name", "sample": "/mark_ditch Bob",
        "summary": "Assess the no-show (ditch) penalty",
        "details": (
            "Uses whichever tier is configured as the ditch tier for this group.\n\n"
            "Set one with: /add_penalty <name> <amount> ditch <description>\n\n"
            "Examples: /mark_ditch Bob  |  /mdt Carol"
        ),
    },
    {
        "name": "mark_penalty", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "tier_name player_name", "sample": "/mark_penalty late_short Alice",
        "summary": "Manual fallback — charge any named tier to a player",
        "details": (
            "Manual fallback when /mark_late or /mark_ditch don't fit.\n\n"
            "Use /penalties to see all defined tiers.\n"
            "Use /add_penalty to create or update a tier.\n\n"
            "Examples: /mark_penalty late_short Alice  |  /mark_penalty no_show Bob"
        ),
    },
    {
        "name": "waive", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name amount [reason]", "sample": "/waive Alice 75 injured",
        "summary": "Waive part or all of a member's dues",
        "details": (
            "Writes a compensating credit entry. Original entries are never deleted "
            "(append-only ledger).\n\n"
            "Example: /waive Alice 75 injury"
        ),
    },
    {
        "name": "set_collector", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name [paid] [::N]", "sample": "/set_collector Ravi paid",
        "summary": "Designate a member as the game's cash collector",
        "details": (
            "Sets the collector for this game. Adding 'paid' means the collector "
            "fronted the ground cost and should be reimbursed when the game is closed.\n\n"
            "The designated collector can also run /mark_paid without being a chat admin.\n\n"
            "Examples: /set_collector Ravi  |  /set_collector Ravi paid  |  /set_collector Ravi paid ::2"
        ),
    },
    {
        "name": "pick_collector", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "[::N]", "sample": "/pick_collector",
        "summary": "Pick the collector from a button panel",
        "details": (
            "Shows the active rollcall's IN members as inline buttons — tap one to "
            "make them the collector. Only real Telegram users are shown (proxies "
            "can't receive payments). Same effect as /set_collector, one tap instead "
            "of typing a name."
        ),
    },
    {
        "name": "rotate_collector", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "[on | off]", "sample": "/rotate_collector on",
        "summary": "Round-robin collector auto-assignment",
        "details": (
            "When on, /close_game with no staged collector automatically assigns "
            "the next IN member in rotation (cycling through real users in a fixed "
            "order) and announces it in the close summary. Manual /set_collector or "
            "/pick_collector always take priority over the rotation. Off by default. "
            "/rotate_collector alone shows current status."
        ),
    },
    {
        "name": "reimburse", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name amount [reason]", "sample": "/reimburse Ravi 600 fronted ground",
        "summary": "Issue a reimbursement credit to a member",
        "details": "Writes a negative dues entry (credit) for the given amount.",
    },
    {
        "name": "add_adhoc", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name", "sample": "/add_adhoc NewPlayer",
        "summary": "Charge a late-joining player the last game's per-head fee",
        "details": (
            "Adds the most recent closure's per-head as an 'adhoc' charge for a player "
            "who joined after /close_game was run. Also credits the group fund."
        ),
    },
    {
        "name": "cancel_game_dues", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "[::N]", "sample": "/cancel_game_dues",
        "summary": "Reverse all share entries for a closed game",
        "details": (
            "Writes compensating cancel_credit entries to undo share/adhoc charges. "
            "Payments already recorded remain as credits (they genuinely happened). "
            "Use /close_game again to re-close after fixing the event fee or player count.\n\n"
            "Defaults to the most recently closed game.\n"
            "Use ::N to target an older game: /cancel_game_dues ::2 cancels the second most recent."
        ),
    },
    {
        "name": "dues", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "", "sample": "/dues",
        "summary": "Full group dues ledger (admin view)",
        "details": "Shows every member's current balance. Use /my_dues for your own balance.",
    },
    {
        "name": "log_expense", "aliases": ["le"], "scope": "admin", "category": "Dues & Fund",
        "args": "amount description", "sample": "/le 150 new balls",
        "summary": "Log a fund expenditure",
        "details": "Deducts from the group fund. Example: /le 150 new balls and bibs",
    },
    {
        "name": "fund_topup", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "amount [description]", "sample": "/fund_topup 500 donations",
        "summary": "Manually add money to the group fund",
        "details": "Credits the group fund. Use for special contributions or corrections.",
    },
    {
        "name": "remind_dues", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "", "sample": "/remind_dues",
        "summary": "Post a dues reminder for all members with outstanding balances",
        "details": "Lists everyone who owes money with a UPI pay link if configured.",
    },
    {
        "name": "set_upi", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "vpa@bank", "sample": "/set_upi amit@upi",
        "summary": "Set the group UPI VPA for payment instructions",
        "details": (
            "Configures the UPI address shown in /close_game summaries, "
            "/my_dues, and /remind_dues.\n\n"
            "Format: name@bankname  e.g. amit@upi, 9876543210@paytm, squad@hdfc"
        ),
    },
    {
        "name": "penalties", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "", "sample": "/penalties",
        "summary": "List all defined penalty tiers",
        "details": "Shows all named penalty tiers and their amounts. Use /add_penalty to create or update one.",
    },
    {
        "name": "add_penalty", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name amount [mins:N] [ditch] [description]",
        "sample": "/add_penalty very_late 100 mins:20 significantly late",
        "summary": "Add or update a penalty tier with optional auto-trigger thresholds",
        "details": (
            "Creates or updates a named penalty tier.\n\n"
            "  mins:<N>  — auto-select via /mark_late when player is ≥N min late\n"
            "  ditch     — mark as the no-show tier used by /mark_ditch\n\n"
            "When updating an existing tier, omitting mins: or ditch preserves "
            "the current values.\n\n"
            "Examples:\n"
            "  /add_penalty slightly_late 50 mins:1 under 15 min late\n"
            "  /add_penalty very_late 100 mins:20 significantly late\n"
            "  /add_penalty no_show 200 ditch missed the game\n"
            "  /add_penalty custom_fine 75 one-off manual tier"
        ),
    },
    {
        "name": "remove_penalty", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "name", "sample": "/remove_penalty early_leave",
        "summary": "Remove a penalty tier",
        "details": "Deletes the named tier. Existing ledger entries using this tier are unaffected.",
    },
    {
        "name": "set_round_step", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "step", "sample": "/set_round_step 5",
        "summary": "Set the per-head fee rounding step",
        "details": (
            "Per-head fees are rounded UP to the nearest multiple of this step "
            "(default ₹10). The overage goes to the group fund.\n\n"
            "Example: /set_round_step 5 rounds to the nearest ₹5."
        ),
    },
    {
        "name": "enable_dues", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "", "sample": "/enable_dues",
        "summary": "Enable Dues & Treasury for this group",
        "details": (
            "Turns on the Dues & Treasury feature for this group. All dues commands "
            "(/close_game, /my_dues, /fund, etc.) are blocked until this is run.\n\n"
            "Configure settings first: /set_upi, /set_penalties, /set_round_step.\n"
            "Existing ledger data is preserved if re-enabled after /disable_dues."
        ),
    },
    {
        "name": "dues_nudges", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "[on | off]", "sample": "/dues_nudges on",
        "summary": "Weekly automatic dues reminders",
        "details": (
            "When on, every Sunday evening members with outstanding dues get a "
            "group summary and an individual DM with the amount and UPI details "
            "— the same output as a manual /remind_dues. Nothing is sent on weeks "
            "when everyone is settled. Off by default. "
            "/dues_nudges alone shows current status."
        ),
    },
    {
        "name": "disable_dues", "aliases": [], "scope": "admin", "category": "Dues & Fund",
        "args": "", "sample": "/disable_dues",
        "summary": "Disable Dues & Treasury for this group",
        "details": (
            "Turns off all Dues & Treasury commands for this group. "
            "Existing ledger data is preserved — nothing is deleted. "
            "Re-enable at any time with /enable_dues."
        ),
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
USER_CATEGORY_ORDER = ["Vote", "View Lists", "Stats & History", "Dues & Fund", "Settings"]
ADMIN_CATEGORY_ORDER = [
    "Rollcall", "Settings", "Proxy", "Templates",
    "User Management", "Ghost Tracking", "Dues & Fund", "Audit", "API Access", "Super Admin",
]


def commands_for_scope(scope_set):
    """Return commands whose scope is in `scope_set`, preserving original order
    inside their category. `scope_set` is e.g. {"user"} or {"user", "admin", "super_admin"}."""
    return [c for c in COMMANDS if c["scope"] in scope_set]
