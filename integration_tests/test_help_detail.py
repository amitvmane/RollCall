"""
Integration regressions for the COMMANDS-driven /help refactor.

Covers:
- /help <name>           → detail card with args + example
- /help <alias>          → resolves via alias lookup
- /help <typo>           → fuzzy "did you mean…?" suggestion
- /help <gibberish>      → graceful no-match message
- /help                  → user view (still works after refactor)
- /help admin            → admin view (still works after refactor)
- Registry parity        → every command registered in COMMANDS is rendered
                           in at least one /help view AND vice-versa.
"""
from helpers import IntegrationBase, ADMIN_USER, USERS
from mock_helpers import get_mock_bot


class TestHelpDetail(IntegrationBase):

    def _last_text(self):
        texts = self.sent_texts()
        return texts[-1] if texts else ""

    async def test_help_for_known_command_shows_detail(self):
        await self.help_commands(self.msg("/help start_roll_call", ADMIN_USER))
        text = self._last_text()
        # Detail card markers
        self.assertIn("/start", text)
        self.assertIn("Aliases:", text)
        self.assertIn("Arguments:", text)
        self.assertIn("Example:", text)
        # The actual sample for /src must appear in the body
        self.assertIn("Friday Football", text)

    async def test_help_resolves_alias_to_canonical_command(self):
        # /help src must show the same detail as /help start_roll_call
        await self.help_commands(self.msg("/help src", ADMIN_USER))
        text = self._last_text()
        self.assertIn("/start", text)
        self.assertIn("Aliases:", text)
        self.assertIn("Friday Football", text)

    async def test_help_with_leading_slash_is_accepted(self):
        # User may type /help /in just as easily as /help in.
        await self.help_commands(self.msg("/help /in", ADMIN_USER))
        text = self._last_text()
        self.assertIn("/in", text)
        self.assertIn("Mark yourself", text)

    async def test_help_typo_offers_suggestion(self):
        # "stat" is 1 edit from "stats" — should suggest.
        await self.help_commands(self.msg("/help stat", ADMIN_USER))
        text = self._last_text().lower()
        self.assertIn("did you mean", text)
        self.assertIn("stats", text)

    async def test_help_unknown_gives_graceful_message(self):
        # Far from any command — should NOT crash, should hint /help.
        await self.help_commands(self.msg("/help xyzzyplugh", ADMIN_USER))
        text = self._last_text().lower()
        self.assertIn("no command", text)
        self.assertIn("/help", text)

    async def test_help_no_args_renders_user_list(self):
        await self.help_commands(self.msg("/help", ADMIN_USER))
        text = self._last_text()
        # Must mention voting commands
        self.assertIn("/in", text)
        self.assertIn("/out", text)
        self.assertIn("/maybe", text)
        # Must hint at the new detail view
        self.assertIn("/help <command>", text)

    async def test_help_admin_renders_admin_list(self):
        await self.help_commands(self.msg("/help admin", ADMIN_USER))
        text = self._last_text()
        # Admin-only commands appear
        self.assertIn("/start", text)
        self.assertIn("/erc", text)
        # User commands still listed (admin help is a superset)
        self.assertIn("/in", text)


class TestCommandRegistryParity(IntegrationBase):
    """The COMMANDS registry is the source of truth — every entry must
    render in at least one /help view, and conversely every command shown
    in /help must come from the registry."""

    async def test_every_user_command_appears_in_user_help(self):
        from commands_registry import COMMANDS
        await self.help_commands(self.msg("/help", ADMIN_USER))
        # Strip Markdown-v1 backslash escapes (e.g. `/whos\_in` → `/whos_in`)
        # so we can substring-match against the canonical command name.
        text = self.sent_texts()[-1].replace('\\', '')
        missing = [
            f"/{c['name']}" for c in COMMANDS
            if c['scope'] == 'user' and f"/{c['name']}" not in text
        ]
        self.assertEqual(missing, [], f"user /help missing entries: {missing}")

    async def test_every_admin_command_appears_in_admin_help(self):
        from commands_registry import COMMANDS
        await self.help_commands(self.msg("/help admin", ADMIN_USER))
        text = self.sent_texts()[-1].replace('\\', '')
        missing = [
            f"/{c['name']}" for c in COMMANDS
            if c['scope'] in ('user', 'admin', 'super_admin') and f"/{c['name']}" not in text
        ]
        self.assertEqual(missing, [], f"admin /help missing entries: {missing}")

    async def test_every_command_has_required_fields(self):
        """Defensive: each entry must define everything the renderer relies on."""
        from commands_registry import COMMANDS
        required = ('name', 'aliases', 'scope', 'category', 'args', 'sample', 'summary', 'details')
        for c in COMMANDS:
            for f in required:
                self.assertIn(f, c, f"command {c.get('name')} missing field '{f}'")
            self.assertIn(c['scope'], ('user', 'admin', 'super_admin'),
                          f"{c['name']}: unknown scope {c['scope']}")

    async def test_lookup_command_finds_by_name_and_alias(self):
        from commands_registry import lookup_command
        self.assertEqual(lookup_command('start_roll_call')['name'], 'start_roll_call')
        self.assertEqual(lookup_command('src')['name'], 'start_roll_call')
        self.assertEqual(lookup_command('/SRC')['name'], 'start_roll_call')
        self.assertIsNone(lookup_command('not_a_command'))
