"""
Identity tokens must not require Telegram to exist — and switching the
signing key must not sign everybody out.

Tokens were HMAC'd with the bot token as the secret, so this module could
only authenticate anyone if a Telegram bot token was configured. That is the
first thing an app with no Telegram behind it cannot satisfy: it has neither
the signing key nor the user ids.

IDENTITY_SECRET decouples the key. The subtle half is rotation: minting uses
the primary key, verification accepts every configured key, so a deployment
that sets IDENTITY_SECRET keeps the tokens already sitting in people's
browsers valid for the rest of their 30-day life. Without that, flipping the
env var silently signs out every signed-in browser at once — the kind of
change that looks free in review and generates support messages on deploy.
"""
import importlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
OTHER_BOT_TOKEN = "987654321:A_COMPLETELY_DIFFERENT_BOT_TOKEN"
SECRET = "an-independent-signing-secret-not-from-telegram"
USER = 4242


def _identity():
    from api import identity
    return importlib.reload(identity)


class TestIdentitySecret(unittest.TestCase):

    # ── Default behaviour is unchanged ───────────────────────────────────

    def test_falls_back_to_the_bot_token_when_unset(self):
        """Deployments that set nothing must be completely unaffected."""
        env = {"TELEGRAM_TOKEN": BOT_TOKEN}
        with patch.dict(os.environ, env, clear=True):
            idm = _identity()
            tok = idm.issue_identity_token(USER)
            self.assertEqual(idm.verify_identity_token(tok), USER)

    def test_no_secret_and_no_bot_token_refuses_to_mint(self):
        with patch.dict(os.environ, {}, clear=True):
            idm = _identity()
            with self.assertRaises(idm.IdentityError):
                idm.issue_identity_token(USER)

    # ── The decoupling ───────────────────────────────────────────────────

    def test_works_with_no_telegram_at_all(self):
        """The point of the exercise: mint and verify with no bot token."""
        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            idm = _identity()
            tok = idm.issue_identity_token(USER)
            self.assertEqual(idm.verify_identity_token(tok), USER)

    def test_scoped_tokens_work_without_telegram_too(self):
        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            idm = _identity()
            tok = idm.issue_scoped_token(USER, "dues_qr", ttl_seconds=300)
            self.assertEqual(idm.verify_scoped_token(tok, "dues_qr"), USER)
            self.assertIsNone(idm.verify_scoped_token(tok, "something_else"))
            self.assertIsNone(idm.verify_identity_token(tok))

    # ── Rotation without signing anyone out ──────────────────────────────

    def test_tokens_minted_before_the_switch_still_verify(self):
        """A browser holding a token from before IDENTITY_SECRET was set must
        keep working — this is the whole reason verification tries every key."""
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}, clear=True):
            old_token = _identity().issue_identity_token(USER)

        with patch.dict(os.environ,
                        {"TELEGRAM_TOKEN": BOT_TOKEN, "IDENTITY_SECRET": SECRET},
                        clear=True):
            idm = _identity()
            self.assertEqual(idm.verify_identity_token(old_token), USER,
                             "setting IDENTITY_SECRET signed out every existing session")

    def test_new_tokens_are_minted_under_the_new_key(self):
        """Not just accepted-both — the new key has to be the one in use, or
        the old one can never be retired."""
        with patch.dict(os.environ,
                        {"TELEGRAM_TOKEN": BOT_TOKEN, "IDENTITY_SECRET": SECRET},
                        clear=True):
            new_token = _identity().issue_identity_token(USER)

        # A deployment that has since dropped the bot token entirely must
        # still accept it.
        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            self.assertEqual(_identity().verify_identity_token(new_token), USER)

    def test_old_tokens_die_once_the_old_key_is_removed(self):
        """The transition window has to actually close."""
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}, clear=True):
            old_token = _identity().issue_identity_token(USER)

        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            self.assertIsNone(_identity().verify_identity_token(old_token))

    # ── Still forgery-resistant ──────────────────────────────────────────

    def test_a_token_from_a_different_secret_is_rejected(self):
        with patch.dict(os.environ, {"IDENTITY_SECRET": "attacker-secret"}, clear=True):
            forged = _identity().issue_identity_token(USER)

        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            self.assertIsNone(_identity().verify_identity_token(forged))

    def test_a_token_from_a_different_bot_is_rejected(self):
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": OTHER_BOT_TOKEN}, clear=True):
            forged = _identity().issue_identity_token(USER)

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}, clear=True):
            self.assertIsNone(_identity().verify_identity_token(forged))

    def test_tampered_user_id_is_rejected(self):
        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            idm = _identity()
            tok = idm.issue_identity_token(USER)
            _, exp, sig = tok.split(".")
            self.assertIsNone(idm.verify_identity_token(f"{USER + 1}.{exp}.{sig}"))

    def test_expired_token_is_rejected(self):
        with patch.dict(os.environ, {"IDENTITY_SECRET": SECRET}, clear=True):
            idm = _identity()
            self.assertIsNone(idm.verify_identity_token(idm.issue_identity_token(USER, ttl_seconds=-1)))

    @classmethod
    def tearDownClass(cls):
        # Leave the module in the state the rest of the suite expects.
        os.environ.setdefault("TELEGRAM_TOKEN", BOT_TOKEN)
        os.environ.pop("IDENTITY_SECRET", None)
        _identity()


if __name__ == "__main__":
    unittest.main()
