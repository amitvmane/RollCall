"""chat_outbox: burst-safe group delivery and panel coalescing.

Everything runs on a fake clock against a fake Telegram that enforces a real
rate limit (a burst of 3, then one message per second) and answers 429 with
retry_after — the behaviour that lost messages when ten scheduled /in
commands fired at 9:01.
"""
import asyncio
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import chat_outbox  # noqa: E402


class _Refused(Exception):
    error_code = 429

    def __init__(self, retry_after):
        super().__init__(f"Too Many Requests: retry after {retry_after}")
        self.result_json = {"ok": False, "error_code": 429,
                            "parameters": {"retry_after": retry_after}}


class FakeTelegram:
    """Per-chat token bucket: `burst` messages at once, then 1 per second."""

    def __init__(self, clock, burst=3, per_second=1.0):
        self.clock, self.burst, self.rate = clock, burst, per_second
        self.tokens, self.updated = {}, {}
        self.posted = []        # (chat, text) in the order Telegram accepted them
        self.refused = 0

    def send(self, chat, text):
        async def _call():
            now = self.clock.now
            t = self.tokens.get(chat, self.burst)
            t = min(self.burst, t + (now - self.updated.get(chat, now)) * self.rate)
            self.updated[chat] = now
            if t < 1:
                self.tokens[chat] = t
                self.refused += 1
                raise _Refused(math.ceil((1 - t) / self.rate))
            self.tokens[chat] = t - 1
            self.posted.append((chat, text))
            return text
        return _call


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += max(0.0, seconds)
        self.slept += max(0.0, seconds)
        await asyncio.sleep(0)      # let other tasks run, like a real sleep would


class OutboxTestBase(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self._saved = (chat_outbox._clock, chat_outbox._sleep)
        chat_outbox._clock, chat_outbox._sleep = self.clock, self.clock.sleep
        for table in (chat_outbox._blocked_until, chat_outbox._chat_locks,
                      chat_outbox._queued, chat_outbox._inflight):
            table.clear()

    def tearDown(self):
        chat_outbox._clock, chat_outbox._sleep = self._saved

    def assertTablesEmpty(self):
        self.assertEqual(chat_outbox._blocked_until, {})
        self.assertEqual(chat_outbox._chat_locks, {})
        self.assertEqual(chat_outbox._queued, {})
        self.assertEqual(chat_outbox._inflight, {})


class TestDeliver(OutboxTestBase):

    def test_normal_send_is_immediate_and_single(self):
        tg = FakeTelegram(self.clock)
        result = asyncio.run(chat_outbox.deliver(-1, tg.send(-1, "hi")))
        self.assertEqual(result, "hi")
        self.assertEqual(self.clock.slept, 0.0, "a normal send must not wait")
        self.assertEqual(tg.posted, [(-1, "hi")])
        self.assertTablesEmpty()

    def test_burst_of_ten_all_delivered_in_order(self):
        """Ten confirmations at once: Telegram takes 3, refuses the rest —
        every one must still be posted, in the order they were sent."""
        tg = FakeTelegram(self.clock)

        async def run():
            await asyncio.gather(*(chat_outbox.deliver(-1, tg.send(-1, f"v{i}"))
                                   for i in range(10)))
        asyncio.run(run())
        self.assertEqual([t for _, t in tg.posted], [f"v{i}" for i in range(10)])
        self.assertGreater(tg.refused, 0, "the fake must actually have throttled")
        self.assertLess(self.clock.slept, chat_outbox.MAX_WAIT)
        self.assertTablesEmpty()

    def test_retry_waits_what_telegram_asked(self):
        tg = FakeTelegram(self.clock, burst=1)

        async def run():
            await chat_outbox.deliver(-1, tg.send(-1, "a"))
            await chat_outbox.deliver(-1, tg.send(-1, "b"))     # refused once, retry after 1s
        asyncio.run(run())
        self.assertEqual([t for _, t in tg.posted], ["a", "b"])
        self.assertGreaterEqual(self.clock.slept, 1.0)

    def test_other_errors_propagate_without_retry(self):
        calls = []

        def factory():
            async def _c():
                calls.append(1)
                raise ValueError("Bad Request: chat not found")
            return _c()

        with self.assertRaises(ValueError):
            asyncio.run(chat_outbox.deliver(-1, factory))
        self.assertEqual(len(calls), 1, "only a 429 means 'not posted, try again'")
        self.assertTablesEmpty()

    def test_gives_up_after_max_wait(self):
        def factory():
            async def _c():
                raise _Refused(25)
            return _c()

        with self.assertRaises(Exception) as cm:
            asyncio.run(chat_outbox.deliver(-1, factory, max_wait=30))
        self.assertTrue(chat_outbox.is_rate_limited(cm.exception))
        self.assertLessEqual(self.clock.slept, 30)
        self.assertTablesEmpty()

    def test_chats_are_independent(self):
        tg = FakeTelegram(self.clock, burst=1)

        async def run():
            await chat_outbox.deliver(-1, tg.send(-1, "a1"))
            t = asyncio.ensure_future(chat_outbox.deliver(-1, tg.send(-1, "a2")))  # A throttled
            await asyncio.sleep(0)
            before = self.clock.now
            await chat_outbox.deliver(-2, tg.send(-2, "b1"))   # B unaffected
            self.assertEqual(self.clock.now, before, "chat B waited on chat A's limit")
            await t
        asyncio.run(run())

    def test_retry_after_is_at_least_one_second(self):
        e = _Refused(0)
        self.assertEqual(chat_outbox.retry_after(e), 1.0)
        e.result_json = {"parameters": {"retry_after": "junk"}}
        self.assertGreaterEqual(chat_outbox.retry_after(e), 1.0)


class TestCoalesce(OutboxTestBase):

    def test_lone_refresh_runs_now_and_once(self):
        drawn = []

        async def run():
            await chat_outbox.coalesce("k", lambda: _draw(drawn, "lead"), lambda: _draw(drawn, "trail"))
            await chat_outbox.drain()
        asyncio.run(run())
        self.assertEqual(drawn, ["lead"])
        self.assertEqual(self.clock.slept, 0.0)
        self.assertTablesEmpty()

    def test_overlapping_refreshes_fold_into_one_final_redraw(self):
        state = {"votes": 1}
        drawn = []

        async def run():
            sent = asyncio.Event()          # the first redraw's Telegram call

            async def lead():
                drawn.append(("lead", state["votes"]))
                await sent.wait()           # still in flight while 9 more votes land

            async def trail():
                drawn.append(("trail", state["votes"]))

            async def vote_and_refresh():
                state["votes"] += 1
                await chat_outbox.coalesce("k", lead, trail)

            first = asyncio.ensure_future(chat_outbox.coalesce("k", lead, trail))
            await asyncio.sleep(0)
            await asyncio.gather(*(vote_and_refresh() for _ in range(9)))
            sent.set()
            await first
            await chat_outbox.drain()
        asyncio.run(run())
        self.assertEqual(drawn, [("lead", 1), ("trail", 10)],
                         "one immediate draw, one final draw showing all 10")
        self.assertTablesEmpty()

    def test_change_during_trailing_redraw_gets_another_round(self):
        state = {"votes": 0}
        drawn = []

        async def lead():
            await asyncio.sleep(0)

        async def trail():
            drawn.append(state["votes"])
            if state["votes"] == 1:
                state["votes"] = 2                       # a vote lands mid-redraw…
                await chat_outbox.coalesce("k", lead, trail)  # …and asks for a refresh

        async def run():
            t = asyncio.ensure_future(chat_outbox.coalesce("k", lead, trail))
            await asyncio.sleep(0)
            state["votes"] = 1
            await chat_outbox.coalesce("k", lead, trail)
            await t
            await chat_outbox.drain()
        asyncio.run(run())
        self.assertEqual(drawn, [1, 2], "the vote that landed mid-redraw was left out")
        self.assertTablesEmpty()

    def test_leading_error_still_propagates_and_trailing_still_runs(self):
        drawn = []

        async def lead():
            await asyncio.sleep(0)
            raise RuntimeError("edit failed")

        async def run():
            t = asyncio.ensure_future(chat_outbox.coalesce("k", lead, lambda: _draw(drawn, "trail")))
            await asyncio.sleep(0)
            await chat_outbox.coalesce("k", lead, lambda: _draw(drawn, "trail"))
            with self.assertRaises(RuntimeError):
                await t
            await chat_outbox.drain()
        asyncio.run(run())
        self.assertEqual(drawn, ["trail"])
        self.assertTablesEmpty()


async def _draw(log, what):
    log.append(what)


if __name__ == "__main__":
    unittest.main()


class TestDrain(OutboxTestBase):

    def test_drain_returns_when_tasks_finished_but_callbacks_pending(self):
        """A finished task's done-callback (which removes it from the set) is
        only scheduled; drain() used to re-await it forever without yielding."""
        async def run():
            t = asyncio.ensure_future(asyncio.sleep(0))
            chat_outbox._trailing_tasks.add(t)
            t.add_done_callback(chat_outbox._trailing_tasks.discard)
            await asyncio.sleep(0)
            await asyncio.sleep(0)       # t is done now; its callback may still be queued
            await asyncio.wait_for(chat_outbox.drain(), 2)
        asyncio.run(run())
        self.assertEqual(chat_outbox._trailing_tasks, set())
