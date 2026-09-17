import asyncio
import importlib.util
import logging
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("AUTHORIZED_CHAT_ID", "123")
os.environ.setdefault("PERSONAL_ID", "000000000")
os.environ.setdefault("OTP_CHANNEL", "phone")
os.environ.setdefault("OTP_CONTACT", "0500000000")
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="rehab-test-state-")
spec = importlib.util.spec_from_file_location("bot", ROOT / "rehab_checker_bot.py")
bot = importlib.util.module_from_spec(spec)
sys.modules["bot"] = bot
spec.loader.exec_module(bot)


class FakeLocator:
    def __init__(self, text="", value=""):
        self.text = text
        self.value = value
        self.filled = ""
        self.clicked = False
    async def count(self): return 1
    async def is_visible(self): return True
    async def is_enabled(self): return True
    async def fill(self, value): self.filled = value; self.value = value
    async def input_value(self): return self.value
    async def inner_text(self): return self.text
    async def click(self): self.clicked = True
    async def set_input_files(self, paths): self.paths = paths
    @property
    def first(self): return self


class FakePage:
    def __init__(self, body, textarea, submit):
        self.body, self.textarea, self.submit = body, textarea, submit
        self.url = "https://myshikum.mod.gov.il/universalRequest/1/5/15"
    async def wait_for_timeout(self, _): pass
    async def wait_for_url(self, *_args, **_kwargs):
        self.body.text += " סיכום הפנייה ששלחת פנייה מספר 123-456"
    def locator(self, selector):
        if selector in ("textarea", '[contenteditable="true"]'): return self.textarea
        if selector == "main": return self.body
        if selector == 'input[type="file"]': return FakeLocator()
        raise AssertionError(selector)


class BotTests(unittest.TestCase):
    def test_polling_accepts_callback_queries(self):
        source = (ROOT / "rehab_checker_bot.py").read_text()
        self.assertIn('allowed_updates=["message", "callback_query"]', source)

    def test_all_three_mode_callbacks_are_routed(self):
        source = (ROOT / "rehab_checker_bot.py").read_text()
        for index, label in enumerate(("נושא הפנייה", "גורמים מטפלים", "תצוגה מורחבת")):
            self.assertIn(f'InlineKeyboardButton("{label}", callback_data="ir:mode:{index}")', source)
        self.assertIn('CallbackQueryHandler(inquiry_callback, pattern=r"^ir:")', source)
        self.assertIn('index = int(data.rsplit(":", 1)[1])', source)
        self.assertIn('await click_text_control(run.page, draft.mode)', source)

    def test_new_request_route_and_live_mode_labels_match_supplied_page(self):
        source = (ROOT / "rehab_checker_bot.py").read_text()
        self.assertEqual(bot.NEW_REQUEST_URL, "https://myshikum.mod.gov.il/universalRequest/1")
        self.assertIn('modes = ["נושא הפנייה", "גורמים מטפלים", "תצוגה מורחבת"]', source)
        self.assertIn('get_by_role("radio", name=pattern)', source)

    def test_live_choice_discovery_excludes_modes_and_feedback(self):
        source = (ROOT / "rehab_checker_bot.py").read_text()
        self.assertIn('main [role="button"]:not([role="radio"])', source)
        for label in ("אהבתי", "יש מה לשפר", "יש בעיה טכנית", "לא היה לי נוח", "לא היה לי ברור"):
            self.assertIn(label, source)
        self.assertIn('InlineKeyboardButton("תצוגה מורחבת"', source)

    def test_help_lists_all_commands(self):
        for command in ("/help", "/check", "/new_request", "/review", "/cancel", "/logout", "/whoami", "/autocheck_start", "/autocheck_stop", "/autocheck_status"):
            self.assertIn(command, bot.HELP_TEXT)

    def test_autocheck_interval_rejects_too_fast_and_invalid_values(self):
        original = bot.AUTOCHECK_INTERVAL_MINUTES_RAW
        try:
            for value in ("nope", "0", "14", "-5"):
                bot.AUTOCHECK_INTERVAL_MINUTES_RAW = value
                self.assertIsNone(bot.configured_autocheck_interval())
            bot.AUTOCHECK_INTERVAL_MINUTES_RAW = "15"
            self.assertEqual(bot.configured_autocheck_interval(), 15)
            bot.AUTOCHECK_INTERVAL_MINUTES_RAW = "60"
            self.assertEqual(bot.configured_autocheck_interval(), 60)
        finally:
            bot.AUTOCHECK_INTERVAL_MINUTES_RAW = original

    def test_seen_state_persists_only_digests(self):
        original = bot.SEEN_MESSAGES
        try:
            with tempfile.TemporaryDirectory() as d:
                bot.SEEN_MESSAGES = Path(d) / "seen.json"
                bot.delta_messages([
                    "פנייה באתר רפואה מספר פנייה 123 מצב טיפול פתוח "
                    "תאריך עדכון 01/01/2026 תוכן רפואי פרטי"
                ])
                stored = bot.SEEN_MESSAGES.read_text()
                self.assertIn('"version": 3', stored)
                self.assertIn("raw_digest", stored)
                self.assertNotIn("תוכן רפואי", stored)
                self.assertNotIn("subject", stored)
                self.assertNotIn("events", stored)
        finally:
            bot.SEEN_MESSAGES = original

    def test_autocheck_is_command_only(self):
        example = (ROOT / ".env.example").read_text()
        source = (ROOT / "rehab_checker_bot.py").read_text()
        self.assertNotIn("AUTOCHECK_ENABLED", example)
        self.assertNotIn("AUTOCHECK_ENABLED", source)
        self.assertNotIn("post_init(start_autocheck", source)
        self.assertIn("AUTOCHECK_INTERVAL_MINUTES=60", example)
        self.assertIn('CommandHandler("autocheck_start"', source)
        self.assertIn('CommandHandler("autocheck_stop"', source)

    def test_operational_log_excludes_error_message(self):
        handler = Mock()
        handler.level = logging.NOTSET
        original_handlers = list(bot.LOGGER.handlers)
        original_level = bot.LOGGER.level
        bot.LOGGER.handlers = [handler]
        bot.LOGGER.setLevel(logging.INFO)
        try:
            bot.log_event("browser.failed", level=logging.ERROR, error=RuntimeError("OTP 1234 token secret"))
            record = handler.handle.call_args.args[0]
            rendered = record.getMessage()
            self.assertEqual(rendered, "browser.failed error_type=RuntimeError")
            self.assertNotIn("1234", rendered)
            self.assertNotIn("secret", rendered)
        finally:
            bot.LOGGER.handlers = original_handlers
            bot.LOGGER.setLevel(original_level)

    def test_no_secret_values_in_source(self):
        source = (ROOT / "rehab_checker_bot.py").read_text()
        self.assertNotIn("TELEGRAM_TOKEN=", source)
        self.assertNotRegex(source, r"\b[1-9]\d{8}\b")

    def test_review_lists_exact_route_text_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "report.pdf"; p.write_bytes(b"pdf")
            draft = bot.InquiryDraft(mode="לפי נושא", category="רפואה", subcategory="החזרים", text="טקסט מדויק", attachments=[p])
            summary = bot.inquiry_summary(draft)
            self.assertIn("אגף השיקום - האזור האישי", summary)
            self.assertIn("לפי נושא > רפואה > החזרים", summary)
            self.assertIn("טקסט מדויק", summary)
            self.assertIn("report.pdf", summary)

    def test_submit_mock_requires_success_number(self):
        textarea = FakeLocator(); body = FakeLocator("רפואה החזרים"); submit = FakeLocator()
        page = FakePage(body, textarea, submit)
        original = bot.button_by_names
        bot.button_by_names = AsyncMock(return_value=submit)
        try:
            draft = bot.InquiryDraft(mode="לפי נושא", category="רפואה", subcategory="החזרים", text="שלום")
            number = asyncio.run(bot.submit_inquiry(page, draft))
            self.assertEqual(number, "123-456")
            self.assertTrue(submit.clicked)
            self.assertEqual(textarea.filled, "שלום")
        finally:
            bot.button_by_names = original

    def test_submit_mock_stops_on_route_mismatch(self):
        page = FakePage(FakeLocator("רפואה"), FakeLocator(), FakeLocator())
        draft = bot.InquiryDraft(mode="לפי נושא", category="רפואה", subcategory="החזרים", text="שלום")
        with self.assertRaisesRegex(RuntimeError, "אינו תואם"):
            asyncio.run(bot.submit_inquiry(page, draft))

    def test_diagnostics_are_operational_and_exclude_personal_values(self):
        text = bot.diagnostics_text()
        for label in ("גרסה:", "זמן ריצה:", "Chromium:", "session:", "autocheck:"):
            self.assertIn(label, text)
        for secret in (bot.TOKEN, bot.PERSONAL_ID, bot.OTP_CONTACT, bot.AUTHORIZED_CHAT_ID_RAW):
            self.assertNotIn(secret, text)

    def test_operation_reservation_is_atomic(self):
        original = dict(bot.ACTIVE_OPERATIONS)
        bot.ACTIVE_OPERATIONS.clear()
        try:
            self.assertTrue(asyncio.run(bot.reserve_operation(123, "check")))
            self.assertFalse(asyncio.run(bot.reserve_operation(123, "autocheck")))
            asyncio.run(bot.cleanup(123))
            self.assertTrue(asyncio.run(bot.reserve_operation(123, "autocheck")))
        finally:
            bot.ACTIVE_OPERATIONS.clear()
            bot.ACTIVE_OPERATIONS.update(original)

    def test_healthcheck_rejects_stale_heartbeat(self):
        original = bot.HEALTH_FILE
        try:
            with tempfile.TemporaryDirectory() as d:
                bot.HEALTH_FILE = Path(d) / "heartbeat"
                bot.HEALTH_FILE.touch()
                self.assertEqual(bot.container_healthcheck(), 0)
                os.utime(bot.HEALTH_FILE, (1, 1))
                self.assertEqual(bot.container_healthcheck(), 1)
        finally:
            bot.HEALTH_FILE = original


if __name__ == "__main__":
    unittest.main()
