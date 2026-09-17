#!/usr/bin/env python3
"""Telegram + Playwright checker for the Rehabilitation Department personal area.

Verified public routes (2026-09-15):
  personal area: https://myshikum.mod.gov.il/
  applications: https://myshikum.mod.gov.il/applies/

Install:
  python -m pip install "python-telegram-bot>=20,<23" "playwright>=1.45" "python-dotenv>=1,<2"
  playwright install chromium

Required environment variables:
  TELEGRAM_TOKEN       Token from BotFather (never put it in this file)
  AUTHORIZED_CHAT_ID   Your numeric Telegram chat id. Run /whoami first if needed.
  PERSONAL_ID          Israeli ID, exactly 9 digits
  OTP_CHANNEL          phone or email
  OTP_CONTACT          Full mobile number (10 digits) or email registered with the department

Optional:
  HEADLESS=false       Run a visible browser (requires a desktop or xvfb-run on a server)
  BROWSER_EXECUTABLE   Optional absolute path to a working Chromium/Chrome binary
  STATE_DIR=./.rehab_checker_state

Commands: /check, /new_request, /review, /cancel, /logout, /whoami

Security notes:
- Only AUTHORIZED_CHAT_ID may run a check or submit an OTP.
- An OTP is accepted only during a five-minute login window, is never logged, and the
  Telegram message containing it is deleted when Telegram permits deletion.
- Browser cookies and sessionStorage are stored locally with owner-only permissions.
  /logout deletes them. Protect the machine and do not sync STATE_DIR.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import random
import shutil
import stat
import time
import tempfile
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BASE_URL = "https://myshikum.mod.gov.il/"
APPLIES_URL = "https://myshikum.mod.gov.il/applies/"
NEW_REQUEST_URL = "https://myshikum.mod.gov.il/universalRequest"
MAX_INQUIRY_TEXT = 450
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_SUFFIXES = {".doc", ".docx", ".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff"}
CONFIRM_TTL_SECONDS = 10 * 60
OTP_TTL_SECONDS = 5 * 60  # The official guide says the four-digit code is valid for 5 minutes.
MAX_OTP_ATTEMPTS = 3
NAVIGATION_TIMEOUT_MS = 45_000

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
AUTHORIZED_CHAT_ID_RAW = os.getenv("AUTHORIZED_CHAT_ID", "").strip()
PERSONAL_ID = re.sub(r"\D", "", os.getenv("PERSONAL_ID", ""))
OTP_CHANNEL = os.getenv("OTP_CHANNEL", "phone").strip().lower()
OTP_CONTACT = os.getenv("OTP_CONTACT", "").strip()
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
BROWSER_EXECUTABLE = os.getenv("BROWSER_EXECUTABLE", "").strip()
STATE_DIR = Path(os.getenv("STATE_DIR", ".rehab_checker_state")).expanduser().resolve()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
AUTOCHECK_INTERVAL_MINUTES_RAW = os.getenv("AUTOCHECK_INTERVAL_MINUTES", "60").strip()
MIN_AUTOCHECK_INTERVAL_MINUTES = 15
MAX_AUTOCHECK_BACKOFF_SECONDS = 6 * 60 * 60
BROWSER_STATE = STATE_DIR / "browser_state.json"
SESSION_STORAGE = STATE_DIR / "session_storage.json"
SEEN_MESSAGES = STATE_DIR / "seen_messages.json"


LOGGER = logging.getLogger("myshikum_assistant")


def configure_logging() -> None:
    """Configure operational logs that never include user-controlled or secret data."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s event=%(message)s"
    ))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False
    # Third-party debug logs may contain request URLs or browser data.
    for name in ("telegram", "httpx", "httpcore", "playwright", "asyncio"):
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = False
        third_party.disabled = True


def log_event(event: str, *, level: int = logging.INFO, error: BaseException | None = None) -> None:
    """Log only a fixed event name and, when useful, the exception class."""
    safe_event = re.sub(r"[^a-z0-9_.-]", "_", event.lower())[:80]
    if error is None:
        LOGGER.log(level, safe_event)
    else:
        LOGGER.log(level, "%s error_type=%s", safe_event, type(error).__name__)


HELP_TEXT = """פקודות זמינות:
/check - בדיקת שינויים בפניות
/new_request - הכנת פנייה חדשה
/review - הצגת הטיוטה ואישור מפורש לפני שליחה
/cancel - ביטול הפעולה ומחיקת קבצים זמניים
/logout - מחיקת מצב ההתחברות המקומי
/whoami - הצגת מזהה הצ'אט להגדרה
/autocheck_start - הפעלת בדיקות אוטומטיות עד לאתחול הבא
/autocheck_stop - עצירת בדיקות אוטומטיות עד לאתחול הבא
/autocheck_status - הצגת הגדרת ומצב הבדיקות האוטומטיות
/help - הצגת העזרה הזו

הבוט לא שולח פנייה בלי לחיצה על אישור ושליחה במסך הסיכום."""


def configured_autocheck_interval() -> int | None:
    try:
        value = int(AUTOCHECK_INTERVAL_MINUTES_RAW)
    except ValueError:
        return None
    return value if value >= MIN_AUTOCHECK_INTERVAL_MINUTES else None


def configured_chat_id() -> int | None:
    try:
        return int(AUTHORIZED_CHAT_ID_RAW) if AUTHORIZED_CHAT_ID_RAW else None
    except ValueError:
        return None


def secure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, stat.S_IRWXU)


def secure_write_json(path: Path, value: object) -> None:
    secure_state_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def valid_config() -> list[str]:
    missing: list[str] = []
    if not TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if configured_chat_id() is None:
        missing.append("AUTHORIZED_CHAT_ID")
    if not re.fullmatch(r"\d{9}", PERSONAL_ID):
        missing.append("PERSONAL_ID (9 digits)")
    if OTP_CHANNEL not in {"phone", "email"}:
        missing.append("OTP_CHANNEL (phone/email)")
    if OTP_CHANNEL == "phone" and not re.fullmatch(r"\d{10}", re.sub(r"\D", "", OTP_CONTACT)):
        missing.append("OTP_CONTACT (10-digit mobile)")
    if OTP_CHANNEL == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", OTP_CONTACT):
        missing.append("OTP_CONTACT (email)")
    return missing


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_lines(text: str) -> list[str]:
    """Keep useful innerText boundaries without depending on generated CSS."""
    return [normalize(line) for line in text.replace("\r", "\n").split("\n") if normalize(line)]


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return normalize(match.group(1)) if match else ""


def split_sender_and_content(segment: str) -> tuple[str, str]:
    """Split a timeline entry conservatively; return blanks rather than guesses."""
    segment = normalize(segment)
    if not segment:
        return "", ""

    # In the supplied page the sender is rendered on its own innerText line.
    lines = clean_lines(segment)
    if len(lines) >= 2 and len(lines[0]) <= 80:
        return lines[0], normalize(" ".join(lines[1:]))

    # normalize() may have flattened the DOM. These are content markers observed
    # in the real /applies/ card supplied by the user, not mutable CSS classes.
    content_markers = (
        "פירוט המענה:", "רצינו לעדכן", "קיבלנו ממך", "הפנייה שלך",
        "הבקשה שלך", "עדכון בנוגע", "ברצוננו לעדכן",
    )
    positions = [segment.find(marker) for marker in content_markers if segment.find(marker) > 0]
    if positions:
        cut = min(positions)
        sender = normalize(segment[:cut])
        if len(sender) <= 80:
            return sender, normalize(segment[cut:])

    # If there is no reliable boundary, keep everything as content. This avoids
    # presenting guessed words as the sender.
    return "", segment


def parse_application(raw_text: str) -> dict[str, object]:
    """Parse one expanded application card without inventing missing fields."""
    text = normalize(raw_text)
    number_match = re.search(r"מספר\s+פנייה\s*[:\-]?\s*([0-9][0-9\-]*)", text)
    number = number_match.group(1) if number_match else ""

    prefix = normalize(text[:number_match.start()]) if number_match else ""
    subject = prefix
    # The first phrase is a section/category label in the observed HTML.
    if prefix.startswith("פנייה באתר "):
        remainder = prefix[len("פנייה באתר "):]
        # Prefer the explicit "פנייה ... בנושא ..." title when present.
        explicit = re.search(r"(פנייה\s+.+?\s+בנושא\s+.+)$", remainder)
        subject = normalize(explicit.group(1) if explicit else remainder)

    status = first_match(
        r"מצב\s+טיפול\s*[:\-]?\s*(.+?)(?=תאריך\s+(?:עדכון|פתיחת(?:\s+ה)?פנייה)|$)",
        text,
    )
    opening_date = first_match(
        r"תאריך\s+פתיחת(?:\s+ה)?פנייה\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        text,
    )

    event_pattern = re.compile(
        r"תאריך\s+עדכון\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        re.I,
    )
    matches = list(event_pattern.finditer(text))
    summary_updated_date = matches[0].group(1) if matches else ""
    events: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        # The first update date can be card metadata immediately followed by
        # "מצב טיפול". It is not a timeline event.
        if re.match(r"\s*מצב\s+טיפול", segment, re.I):
            continue
        # Opening-date metadata belongs to the card, not to the event content.
        segment = re.split(r"תאריך\s+פתיחת(?:\s+ה)?פנייה", segment, maxsplit=1)[0]
        sender, content = split_sender_and_content(segment)
        if sender or content:
            events.append({"date": match.group(1), "sender": sender, "content": content})

    return {
        "subject": subject,
        "number": number,
        "status": status,
        "opening_date": opening_date,
        "updated_date": summary_updated_date,
        "events": events,
    }


def format_application(raw_text: str) -> str:
    """Produce compact, readable plain text that Telegram renders well in Hebrew."""
    parsed = parse_application(raw_text)
    lines = ["📌 פנייה"]
    if parsed["subject"]:
        lines[0] += f": {parsed['subject']}"
    if parsed["number"]:
        lines.append(f"מספר פנייה: {parsed['number']}")
    if parsed["status"]:
        lines.append(f"סטטוס: {parsed['status']}")
    if parsed["opening_date"]:
        lines.append(f"תאריך פתיחה: {parsed['opening_date']}")
    if parsed["updated_date"]:
        lines.append(f"עדכון אחרון: {parsed['updated_date']}")

    events = parsed["events"]
    if events:
        lines.extend(["", "🕒 ציר זמן"])
        for event in events:
            lines.extend(["", f"• {event['date']}"])
            if event["sender"]:
                lines.append(f"מאת: {event['sender']}")
            if event["content"]:
                lines.append(event["content"])

    # If the structure changes, preserve the real card text instead of returning
    # an empty or fabricated record.
    if len(lines) == 1 and lines[0] == "📌 פנייה":
        return "📌 פנייה\n" + normalize(raw_text)
    return "\n".join(lines)


def split_telegram_messages(heading: str, cards: list[str], limit: int = 3900) -> list[str]:
    """Keep card/event boundaries where possible and stay below Telegram's limit."""
    chunks: list[str] = []
    current = heading
    for card in cards:
        block = ("\n\n━━━━━━━━━━━━\n\n" if current != heading else "\n\n") + card
        if len(current) + len(block) <= limit:
            current += block
            continue
        if current:
            chunks.append(current)
        # A very long card is split at paragraph boundaries, never discarded.
        remaining = card
        while len(remaining) > limit:
            cut = remaining.rfind("\n\n", 0, limit)
            if cut < limit // 2:
                cut = remaining.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        current = remaining
    if current:
        chunks.append(current)
    return chunks


@dataclass
class InquiryDraft:
    stage: str = "choose_mode"
    mode: str = ""
    category: str = ""
    subcategory: str = ""
    text: str = ""
    attachments: list[Path] = field(default_factory=list)
    choices: list[str] = field(default_factory=list)
    confirmation_nonce: str = ""
    confirmation_deadline: float = 0


@dataclass
class LoginRun:
    pw: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    action: Literal["check", "new_request"] = "check"
    otp_deadline: float = 0
    otp_attempts: int = 0
    inquiry: InquiryDraft | None = None


RUNS: dict[int, LoginRun] = {}
RUNS_LOCK = asyncio.Lock()
AUTOCHECK_TASK: asyncio.Task[None] | None = None
AUTOCHECK_STOP: asyncio.Event | None = None
AUTOCHECK_LAST_SUCCESS: float | None = None
AUTOCHECK_LAST_FAILURE: str | None = None


def authorized(update: Update) -> bool:
    expected = configured_chat_id()
    return expected is not None and update.effective_chat is not None and update.effective_chat.id == expected


async def deny(update: Update) -> None:
    log_event("authorization.denied", level=logging.WARNING)
    if update.effective_message:
        await update.effective_message.reply_text("הבוט הזה מוגבל לצ'אט שהוגדר מראש.")


async def first_visible(page: Page, selectors: Iterable[str], timeout_ms: int = 8_000):
    deadline = time.monotonic() + timeout_ms / 1000
    selectors = list(selectors)
    while time.monotonic() < deadline:
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        await page.wait_for_timeout(200)
    return None


async def button_by_names(page: Page, names: Iterable[str], timeout_ms: int = 10_000):
    deadline = time.monotonic() + timeout_ms / 1000
    patterns = [re.compile(name, re.I) for name in names]
    while time.monotonic() < deadline:
        for pattern in patterns:
            loc = page.get_by_role("button", name=pattern).first
            try:
                if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                    return loc
            except Exception:
                continue
        await page.wait_for_timeout(200)
    return None


async def click_button(page: Page, names: Iterable[str], label: str) -> None:
    button = await button_by_names(page, names)
    if not button:
        raise RuntimeError(f"לא נמצא כפתור {label}. כתובת הדף: {page.url}")
    await button.click()


async def fill_first(page: Page, selectors: Iterable[str], value: str, label: str) -> None:
    field = await first_visible(page, selectors)
    if not field:
        raise RuntimeError(f"לא נמצא שדה {label}. כתובת הדף: {page.url}")
    await field.fill(value)


async def restore_session_storage(context: BrowserContext) -> None:
    if not SESSION_STORAGE.exists():
        return
    try:
        data = json.loads(SESSION_STORAGE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    # add_init_script has no `arg` parameter in Playwright Python. Embed a JSON
    # literal generated by json.dumps, and run before the site's JavaScript.
    storage_json = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    script = f"""(() => {{
      const storage = {storage_json};
      if (location.origin !== storage.origin) return;
      for (const [key, value] of Object.entries(storage.items || {{}})) {{
        if (typeof value === "string") sessionStorage.setItem(key, value);
      }}
    }})()"""
    await context.add_init_script(script=script)


async def save_browser_session(run: LoginRun) -> None:
    secure_state_dir()
    await run.context.storage_state(path=str(BROWSER_STATE))
    os.chmod(BROWSER_STATE, stat.S_IRUSR | stat.S_IWUSR)
    storage = await run.page.evaluate(
        """() => ({origin: location.origin, items: Object.fromEntries(
          Array.from({length: sessionStorage.length}, (_, i) => {
            const key = sessionStorage.key(i); return [key, sessionStorage.getItem(key)];
          })
        )})"""
    )
    secure_write_json(SESSION_STORAGE, storage)


def browser_launch_candidates() -> list[tuple[str, dict[str, object]]]:
    """Prefer full Chromium (new headless) over chromium_headless_shell.

    On Linux ARM64 the small headless-shell binary can exit during launch even though
    the full Chromium build works. `channel="chromium"` tells Playwright to use the
    full bundled browser and its newer headless implementation.
    """
    common: dict[str, object] = {
        "headless": HEADLESS,
        "args": ["--disable-dev-shm-usage"],
    }
    if os.geteuid() == 0:
        # Chromium refuses its sandbox when the bot is run as root. Running the bot as
        # an ordinary user is still preferable; this keeps containers usable.
        common["args"] = ["--disable-dev-shm-usage", "--no-sandbox"]

    candidates: list[tuple[str, dict[str, object]]] = []
    if BROWSER_EXECUTABLE:
        candidates.append((f"BROWSER_EXECUTABLE={BROWSER_EXECUTABLE}", {
            **common, "executable_path": BROWSER_EXECUTABLE,
        }))

    # This is the important ARM64 fix: full bundled Chromium, not
    # chromium_headless_shell-*/chrome-headless-shell-linux-arm64.
    candidates.append(('Playwright channel "chromium"', {**common, "channel": "chromium"}))

    # If Playwright's full build is unavailable, try a distro-installed browser.
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        executable = shutil.which(name)
        if executable and executable != BROWSER_EXECUTABLE:
            candidates.append((f"system browser {executable}", {
                **common, "executable_path": executable,
            }))
    return candidates


async def start_browser() -> LoginRun:
    log_event("browser.starting")
    pw = await async_playwright().start()
    browser: Browser | None = None
    failures: list[str] = []
    try:
        for label, launch_options in browser_launch_candidates():
            try:
                browser = await pw.chromium.launch(**launch_options)
                log_event("browser.started")
                break
            except Exception as exc:
                failures.append(f"{label}: {normalize(str(exc))[:500]}")
        if browser is None:
            system = f"{platform.system()} {platform.machine()} / Python {platform.python_version()}"
            details = " | ".join(failures)
            raise RuntimeError(
                "Chromium לא הצליח לעלות. " + system + ". " + details +
                " | הרץ: python -m playwright install --with-deps chromium"
            )

        kwargs: dict[str, object] = {"locale": "he-IL"}
        if BROWSER_STATE.exists():
            kwargs["storage_state"] = str(BROWSER_STATE)
        context = await browser.new_context(**kwargs)
        await restore_session_storage(context)
        page = await context.new_page()
        page.set_default_timeout(12_000)
        return LoginRun(pw=pw, browser=browser, context=context, page=page)
    except Exception:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()
        log_event("browser.start_failed", level=logging.ERROR)
        raise


async def cleanup(chat_id: int) -> None:
    async with RUNS_LOCK:
        run = RUNS.pop(chat_id, None)
    if not run:
        return
    log_event("run.cleanup")
    if run.inquiry:
        temp_dirs: set[Path] = set()
        for path in run.inquiry.attachments:
            temp_dirs.add(path.parent)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for temp_dir in temp_dirs:
            try:
                temp_dir.rmdir()
            except OSError:
                pass
    try:
        await run.context.close()
    except Exception:
        pass
    try:
        await run.browser.close()
    except Exception:
        pass
    try:
        await run.pw.stop()
    except Exception:
        pass


async def looks_logged_out(page: Page) -> bool:
    url = page.url.lower()
    if "login.myshikum" in url or "signin" in url or "otp" in url:
        return True
    login_controls = page.get_by_text(re.compile(r"התחברות עם קוד חד.?פעמי|כניסה לאזור האישי", re.I))
    try:
        return bool(await login_controls.count() and await login_controls.first.is_visible())
    except Exception:
        return False


async def goto_applies(page: Page) -> bool:
    await page.goto(APPLIES_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    return not await looks_logged_out(page) and "/applies" in page.url.lower()


async def begin_otp_login(page: Page) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass

    # The public guide confirms this order: ID -> "login with one-time code" ->
    # phone/email details -> four-digit code. Prefer labels/names over CSS classes.
    await fill_first(
        page,
        [
            'input[name="IsrId"]', 'input[name="id"]', 'input[autocomplete="username"]',
            'input[inputmode="numeric"]', 'input[type="tel"]'
        ],
        PERSONAL_ID,
        "תעודת זהות",
    )
    await click_button(page, [r"התחברות עם קוד חד.?פעמי", r"קוד חד.?פעמי", r"המשך"], "התחברות עם קוד חד פעמי")

    if OTP_CHANNEL == "email":
        option = page.get_by_text(re.compile(r"דואר אלקטרוני|אימייל", re.I)).first
        if await option.count() and await option.is_visible():
            await option.click()
        await fill_first(
            page,
            ['input[name="email"]', 'input[type="email"]', 'input[autocomplete="email"]'],
            OTP_CONTACT,
            "דואר אלקטרוני",
        )
    else:
        phone = re.sub(r"\D", "", OTP_CONTACT)
        # Some versions split the prefix and seven-digit subscriber number. Try the
        # full phone field first, then a visible generic tel field.
        await fill_first(
            page,
            ['input[name="phone"]', 'input[name="phoneNumber"]', 'input[autocomplete="tel"]', 'input[type="tel"]'],
            phone,
            "טלפון נייד",
        )

    await click_button(page, [r"להמשך", r"המשך", r"שליחה", r"שלחו.*קוד", r"קבלת הקוד"], "שליחת הקוד")

    otp = await first_visible(
        page,
        ['input[autocomplete="one-time-code"]', 'input[aria-label*="קוד חד"]', 'input[inputmode="numeric"]'],
        timeout_ms=15_000,
    )
    if not otp:
        raise RuntimeError(f"האתר לא עבר למסך הקוד. בדוק שהפרטים תואמים לרישום באגף. כתובת: {page.url}")


async def application_texts(page: Page, limit: int = 50) -> list[str]:
    """Open each read-only application accordion and return its full text.

    The live /applies/ HTML uses MUI Accordion summary buttons.  The stable
    hooks are the button role, aria-expanded, and the Hebrew application
    number label.  Generated css-* class names are intentionally not used.
    Only the accordion summary itself is clicked; attachment, feedback and
    other action buttons inside an open application are never touched.
    """
    await page.wait_for_timeout(1_000)

    summaries = page.locator(
        'main button.MuiAccordionSummary-root[aria-expanded]'
    ).filter(has_text=re.compile(r"מספר\s+פנייה"))
    count = await summaries.count()

    # Fallback for a future MUI build that drops the descriptive class but
    # keeps the accessible accordion contract observed in the supplied HTML.
    if not count:
        summaries = page.locator(
            'main button[aria-expanded="false"], main button[aria-expanded="true"]'
        ).filter(has_text=re.compile(r"מספר\s+פנייה"))
        count = await summaries.count()

    applications: list[str] = []
    for index in range(min(count, limit)):
        # Re-query every time because expanding an accordion changes the DOM.
        summary = summaries.nth(index)
        try:
            await summary.scroll_into_view_if_needed()
            was_open = (await summary.get_attribute("aria-expanded")) == "true"
            if not was_open:
                await summary.click()
                await summary.wait_for(state="visible")
                await page.wait_for_function(
                    "el => el.getAttribute('aria-expanded') === 'true'",
                    arg=await summary.element_handle(),
                    timeout=8_000,
                )

            accordion = summary.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' MuiAccordion-root ')][1]"
            )
            region = accordion.locator('[role="region"]').first
            if await region.count():
                await region.wait_for(state="visible", timeout=8_000)

            # The accordion root contains the summary fields and the opened
            # timeline/messages, including dates and attachment names.
            # Keep innerText line boundaries: they help distinguish sender from
            # event content. Parsing still has a safe flattened-text fallback.
            text = (await accordion.inner_text()).strip()
            if normalize(text) and text not in applications:
                applications.append(text[:16_000])

            # Always return to the list with this card collapsed.  This click
            # only toggles the read-only MUI accordion; it cannot submit a form.
            summary = summaries.nth(index)
            if (await summary.get_attribute("aria-expanded")) == "true":
                await summary.click()
                await page.wait_for_function(
                    "el => el.getAttribute('aria-expanded') === 'false'",
                    arg=await summary.element_handle(),
                    timeout=8_000,
                )
        except Exception as exc:
            # A single stale/malformed card must not hide all other records.
            try:
                summary = summaries.nth(index)
                header = normalize(await summary.inner_text())
                if header and header not in applications:
                    applications.append(header + " [לא ניתן היה לפתוח את פרטי הכרטיס: " + normalize(str(exc))[:180] + "]")
                if (await summary.get_attribute("aria-expanded")) == "true":
                    await summary.click(timeout=3_000)
            except Exception:
                pass

    if applications:
        return applications

    main = page.locator("main").first
    if await main.count() and await main.is_visible():
        text = normalize(await main.inner_text())
        empty_markers = (
            "אין בקשות", "לא נמצאו בקשות", "אין פניות",
            "לא נמצאו פניות", "אין תביעות", "לא נמצאו תוצאות",
        )
        if any(marker in text for marker in empty_markers):
            return []
        if text:
            return ["[תצוגת דף - לא זוהו כרטיסי פנייה] " + text[:3_500]]
    return []


def normalized_event(event: dict[str, str]) -> dict[str, str]:
    """Return only stable, user-visible event fields."""
    return {
        "date": normalize(str(event.get("date", ""))),
        "sender": normalize(str(event.get("sender", ""))),
        "content": normalize(str(event.get("content", ""))),
    }


def event_id(event: dict[str, str]) -> str:
    stable = normalized_event(event)
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def application_id(raw_text: str, parsed: dict[str, object]) -> tuple[str, bool]:
    """Build an ID that does not change when status or timeline text changes."""
    number = normalize(str(parsed.get("number", "")))
    if number:
        return "number:" + number, True

    subject = normalize(str(parsed.get("subject", "")))
    opening_date = normalize(str(parsed.get("opening_date", "")))
    if subject and opening_date:
        seed = subject + "\n" + opening_date
        return "subject-date:" + hashlib.sha256(seed.encode("utf-8")).hexdigest(), True

    # Safe fallback: use the card header before mutable status/timeline fields.
    # This normally stays fixed even if parsing a new site layout is incomplete.
    header = re.split(
        r"תאריך\s+עדכון|מצב\s+טיפול|תאריך\s+פתיחת(?:\s+ה)?פנייה",
        normalize(raw_text), maxsplit=1, flags=re.I,
    )[0]
    if header:
        return "header:" + hashlib.sha256(header.encode("utf-8")).hexdigest(), False
    return "raw:" + hashlib.sha256(normalize(raw_text).encode("utf-8")).hexdigest(), False


def application_snapshot(raw_text: str) -> tuple[str, dict[str, object]]:
    parsed = parse_application(raw_text)
    app_id, parsed_reliably = application_id(raw_text, parsed)
    events: list[dict[str, str]] = []
    seen_event_ids: set[str] = set()
    for raw_event in parsed.get("events", []):
        event = normalized_event(raw_event)
        digest = event_id(event)
        if digest not in seen_event_ids:
            events.append({**event, "id": digest})
            seen_event_ids.add(digest)
    snapshot: dict[str, object] = {
        "subject": normalize(str(parsed.get("subject", ""))),
        "number": normalize(str(parsed.get("number", ""))),
        "status": normalize(str(parsed.get("status", ""))),
        "opening_date": normalize(str(parsed.get("opening_date", ""))),
        "updated_date": normalize(str(parsed.get("updated_date", ""))),
        "events": events,
        "raw_digest": hashlib.sha256(normalize(raw_text).encode("utf-8")).hexdigest(),
        "parsed_reliably": parsed_reliably,
    }
    return app_id, snapshot


def load_seen_state() -> tuple[str, dict[str, object]]:
    """Return (kind, state). Old hash-list files are recognized for migration."""
    if not SEEN_MESSAGES.exists():
        return "missing", {}
    try:
        value = json.loads(SEEN_MESSAGES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid", {}
    if isinstance(value, list):
        return "legacy", {}
    if isinstance(value, dict) and value.get("version") in {2, 3} and isinstance(value.get("applications"), dict):
        return "v2" if value.get("version") == 2 else "v3", value
    return "invalid", {}


def format_event_update(snapshot: dict[str, object], events: list[dict[str, str]]) -> str:
    title = str(snapshot.get("subject") or "פנייה")
    lines = [f"🆕 עדכון חדש בפנייה: {title}"]
    if snapshot.get("number"):
        lines.append(f"מספר פנייה: {snapshot['number']}")
    for event in events:
        lines.extend(["", f"• {event.get('date') or 'תאריך לא זוהה'}"])
        if event.get("sender"):
            lines.append(f"מאת: {event['sender']}")
        if event.get("content"):
            lines.append(str(event["content"]))
    return "\n".join(lines)


def format_status_update(snapshot: dict[str, object], old_status: str) -> str:
    title = str(snapshot.get("subject") or "פנייה")
    lines = [f"🔄 שינוי סטטוס בפנייה: {title}"]
    if snapshot.get("number"):
        lines.append(f"מספר פנייה: {snapshot['number']}")
    if old_status:
        lines.append(f"סטטוס קודם: {old_status}")
    lines.append(f"סטטוס חדש: {snapshot.get('status') or 'לא זוהה'}")
    if snapshot.get("updated_date"):
        lines.append(f"תאריך עדכון: {snapshot['updated_date']}")
    return "\n".join(lines)


def delta_messages(applications: list[str]) -> tuple[list[str], str]:
    """Persist per-application state and return only changed applications.

    The application number is the primary identity. A changed existing card is
    emitted once, in full, including its timeline. Other unchanged applications
    are not emitted. Legacy whole-page/card hash lists are migrated quietly.
    """
    state_kind, previous_state = load_seen_state()
    previous_apps = previous_state.get("applications", {}) if state_kind in {"v2", "v3"} else {}
    current_apps: dict[str, dict[str, object]] = {}
    raw_by_id: dict[str, str] = {}

    for raw in applications:
        app_id, snapshot = application_snapshot(raw)
        base_id = app_id
        suffix = 2
        while app_id in current_apps:
            app_id = f"{base_id}#{suffix}"
            suffix += 1
        current_apps[app_id] = snapshot
        raw_by_id[app_id] = raw

    # Persist only opaque application identifiers and content digests. Human-readable
    # subjects, statuses, inquiry text, events, and attachment data stay in memory.
    persisted_apps = {
        app_id: {"raw_digest": snapshot["raw_digest"]}
        for app_id, snapshot in current_apps.items()
    }
    secure_write_json(SEEN_MESSAGES, {"version": 3, "applications": persisted_apps})

    if state_kind in {"legacy", "invalid"}:
        return [], "migrated"
    if state_kind == "missing":
        return [format_application(raw) for raw in applications], "first"

    output: list[str] = []
    for app_id, current in current_apps.items():
        previous = previous_apps.get(app_id)
        if not isinstance(previous, dict):
            # A genuinely new application is sent in full.
            output.append(format_application(raw_by_id[app_id]))
            continue

        # Compare within the stable application identity. Any real card change
        # sends this application once in full, and never the other applications.
        if current.get("raw_digest") != previous.get("raw_digest"):
            output.append(format_application(raw_by_id[app_id]))

    return output, "delta"



async def goto_new_request(page: Page) -> bool:
    await page.goto(NEW_REQUEST_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    return not await looks_logged_out(page) and "/universalrequest" in page.url.lower()


async def click_text_control(page: Page, text: str) -> None:
    """Click a live UI control by its exact visible label, never by a stored site ID."""
    pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)
    candidates = [
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.locator('main [role="button"], main [tabindex="0"]').filter(has_text=pattern),
    ]
    for candidate in candidates:
        for index in range(await candidate.count()):
            loc = candidate.nth(index)
            try:
                if await loc.is_visible() and await loc.is_enabled():
                    await loc.click()
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue
    raise RuntimeError(f"לא נמצאה אפשרות פעילה בשם {text!r}. האתר אולי השתנה.")


async def live_choices(page: Page) -> list[str]:
    """Read category/subcategory labels from the current official UI."""
    controls = page.locator('main button, main a[href], main [role="button"], main [tabindex="0"]')
    ignored = re.compile(
        r"^(חיפוש|חזרה|שליחה|המשך|ביטול|נקה|פתיחה|סגירה|העלאת קובץ|צירוף קובץ|"
        r"לפי נושא|לפי גורם מטפל|תצוגה מורחבת)$",
        re.I,
    )
    result: list[str] = []
    for index in range(min(await controls.count(), 300)):
        loc = controls.nth(index)
        try:
            if not await loc.is_visible():
                continue
            text = normalize(await loc.inner_text())
        except Exception:
            continue
        if not text or len(text) > 120 or ignored.fullmatch(text) or text in result:
            continue
        if text.startswith("אגף השיקום") or text in {"לאזור האישי", "יציאה"}:
            continue
        result.append(text)
    return result


def choice_keyboard(kind: str, choices: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, title in enumerate(choices):
        rows.append([InlineKeyboardButton(title[:64], callback_data=f"ir:{kind}:{index}")])
    rows.append([InlineKeyboardButton("ביטול", callback_data="ir:cancel")])
    return InlineKeyboardMarkup(rows)


async def start_inquiry_ui(chat_id: int, update: Update, run: LoginRun) -> None:
    if not await goto_new_request(run.page):
        raise RuntimeError(f"ההתחברות הסתיימה אך טופס הפנייה לא נפתח: {run.page.url}")
    run.inquiry = InquiryDraft()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("לפי נושא", callback_data="ir:mode:0")],
        [InlineKeyboardButton("לפי גורם מטפל", callback_data="ir:mode:1")],
        [InlineKeyboardButton("ביטול", callback_data="ir:cancel")],
    ])
    await update.effective_chat.send_message(
        "פתיחת פנייה חדשה. איך לבחור את היעד? הקטגוריות ייקראו בזמן אמת מהאתר הרשמי.",
        reply_markup=keyboard,
    )


async def inquiry_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not authorized(update) or not update.effective_chat:
        return
    await query.answer()
    chat_id = update.effective_chat.id
    run = RUNS.get(chat_id)
    if not run or not run.inquiry:
        await query.edit_message_text("הפעולה כבר לא פעילה. הפעל /new_request מחדש.")
        return
    draft = run.inquiry
    data = query.data or ""
    if data == "ir:cancel":
        await cleanup(chat_id)
        await query.edit_message_text("הפנייה בוטלה. לא נשלח דבר.")
        return
    if data == "ir:edit":
        draft.text = ""
        draft.confirmation_nonce = ""
        draft.stage = "awaiting_text"
        await query.edit_message_text("שלח מחדש את הטקסט המדויק של הפנייה.")
        return
    if data.startswith("ir:mode:"):
        index = int(data.rsplit(":", 1)[1])
        modes = ["לפי נושא", "לפי גורם מטפל"]
        if index >= len(modes):
            return
        draft.mode = modes[index]
        await click_text_control(run.page, draft.mode)
        draft.choices = await live_choices(run.page)
        if not draft.choices:
            raise RuntimeError("לא נמצאו קטגוריות פעילות בטופס הרשמי.")
        draft.stage = "choose_category"
        await query.edit_message_text("בחר קטגוריה:", reply_markup=choice_keyboard("category", draft.choices))
        return
    if data.startswith("ir:category:"):
        index = int(data.rsplit(":", 1)[1])
        if draft.stage != "choose_category" or index >= len(draft.choices):
            return
        draft.category = draft.choices[index]
        await click_text_control(run.page, draft.category)
        draft.choices = await live_choices(run.page)
        if not draft.choices:
            raise RuntimeError("לא נמצאו תתי-קטגוריות פעילות בטופס הרשמי.")
        draft.stage = "choose_subcategory"
        await query.edit_message_text(
            f"קטגוריה: {draft.category}\nבחר תת-קטגוריה:",
            reply_markup=choice_keyboard("subcategory", draft.choices),
        )
        return
    if data.startswith("ir:subcategory:"):
        index = int(data.rsplit(":", 1)[1])
        if draft.stage != "choose_subcategory" or index >= len(draft.choices):
            return
        draft.subcategory = draft.choices[index]
        await click_text_control(run.page, draft.subcategory)
        draft.stage = "awaiting_text"
        await query.edit_message_text(
            f"היעד שנבחר: {draft.mode} > {draft.category} > {draft.subcategory}\n"
            f"שלח עכשיו את הטקסט המדויק של הפנייה (עד {MAX_INQUIRY_TEXT} תווים)."
        )
        return
    if data.startswith("ir:send:"):
        nonce = data.rsplit(":", 1)[1]
        if (
            draft.stage != "review" or not secrets.compare_digest(nonce, draft.confirmation_nonce)
            or time.monotonic() > draft.confirmation_deadline
        ):
            await query.edit_message_text("האישור פג או כבר נוצל. הפעל /review לקבלת אישור חדש.")
            return
        # Consume before any browser mutation so a double tap cannot submit twice.
        draft.confirmation_nonce = ""
        draft.stage = "submitting"
        await query.edit_message_text("האישור התקבל. שולח דרך האזור האישי...")
        try:
            number = await submit_inquiry(run.page, draft)
            await save_browser_session(run)
            await cleanup(chat_id)
            log_event("inquiry.submitted")
            await update.effective_chat.send_message(f"הפנייה נשלחה. מספר הפנייה: {number}")
        except Exception as exc:
            log_event("inquiry.submit_uncertain", level=logging.ERROR, error=exc)
            # Never retry automatically. The request may have reached the server.
            await cleanup(chat_id)
            await update.effective_chat.send_message(
                "לא ניתן לאשר בוודאות אם הפנייה נשלחה. לא ניסיתי שוב כדי למנוע כפילות. "
                "בדוק בריכוז הפניות. פרטי השגיאה: " + normalize(str(exc))[:600]
            )


async def inquiry_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update) or not update.effective_chat or not update.effective_message:
        return
    run = RUNS.get(update.effective_chat.id)
    if not run or not run.inquiry or run.inquiry.stage != "awaiting_text":
        return
    text = (update.effective_message.text or "").strip()
    if not text or len(text) > MAX_INQUIRY_TEXT:
        await update.effective_message.reply_text(f"נדרש טקסט של 1 עד {MAX_INQUIRY_TEXT} תווים.")
        return
    run.inquiry.text = text
    run.inquiry.stage = "collecting_files"
    await update.effective_message.reply_text(
        "הטקסט נשמר. אפשר לצרף מסמכים נתמכים (עד 20MB כל אחד), או להפעיל /review. "
        "הקבצים יישמרו זמנית בלבד ויימחקו בסיום או בביטול."
    )


async def inquiry_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update) or not update.effective_chat or not update.effective_message:
        return
    run = RUNS.get(update.effective_chat.id)
    if not run or not run.inquiry or run.inquiry.stage != "collecting_files":
        return
    doc = update.effective_message.document
    photo = update.effective_message.photo[-1] if update.effective_message.photo else None
    item = doc or photo
    if not item:
        return
    name = (doc.file_name if doc and doc.file_name else f"photo-{int(time.time())}.jpg")
    suffix = Path(name).suffix.lower()
    size = item.file_size or 0
    if suffix not in ALLOWED_ATTACHMENT_SUFFIXES or size > MAX_ATTACHMENT_BYTES:
        await update.effective_message.reply_text("הקובץ לא נתמך או גדול מ-20MB. לא שמרתי אותו.")
        return
    temp_dir = Path(tempfile.mkdtemp(prefix="rehab-inquiry-"))
    os.chmod(temp_dir, 0o700)
    target = temp_dir / Path(name).name
    telegram_file = await item.get_file()
    await telegram_file.download_to_drive(custom_path=str(target))
    os.chmod(target, 0o600)
    run.inquiry.attachments.append(target)
    await update.effective_message.reply_text(f"צורף זמנית: {target.name}. אפשר לצרף עוד או להפעיל /review.")


def inquiry_summary(draft: InquiryDraft) -> str:
    files = "\n".join(f"• {p.name} ({p.stat().st_size / 1024:.1f}KB)" for p in draft.attachments) or "ללא"
    return (
        "נא לבדוק לפני שליחה:\n\n"
        "ערוץ/נמען: אגף השיקום - האזור האישי\n"
        f"מסלול: {draft.mode} > {draft.category} > {draft.subcategory}\n\n"
        f"תוכן הפנייה:\n{draft.text}\n\n"
        f"קבצים:\n{files}\n\n"
        "שום דבר לא יישלח עד ללחיצה על אישור ושליחה."
    )


async def review_inquiry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command.review")
    if not authorized(update) or not update.effective_chat or not update.effective_message:
        return
    run = RUNS.get(update.effective_chat.id)
    if not run or not run.inquiry or run.inquiry.stage not in {"collecting_files", "review"}:
        await update.effective_message.reply_text("אין טיוטת פנייה מוכנה לבדיקה. הפעל /new_request.")
        return
    draft = run.inquiry
    draft.confirmation_nonce = secrets.token_urlsafe(8)
    draft.confirmation_deadline = time.monotonic() + CONFIRM_TTL_SECONDS
    draft.stage = "review"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("אישור ושליחה", callback_data=f"ir:send:{draft.confirmation_nonce}")],
        [InlineKeyboardButton("עריכת הטקסט", callback_data="ir:edit")],
        [InlineKeyboardButton("ביטול", callback_data="ir:cancel")],
    ])
    await update.effective_message.reply_text(inquiry_summary(draft), reply_markup=keyboard)


async def submit_inquiry(page: Page, draft: InquiryDraft) -> str:
    """Fill the reviewed values through the official UI and click submit once."""
    note = await first_visible(page, ['textarea', '[contenteditable="true"]'])
    if not note:
        raise RuntimeError("לא נמצא שדה תוכן הפנייה.")
    await note.fill(draft.text)
    if (await note.input_value()).strip() != draft.text:
        raise RuntimeError("הטקסט באתר אינו תואם לטקסט שאושר.")

    if draft.attachments:
        file_input = page.locator('input[type="file"]').first
        if not await file_input.count():
            raise RuntimeError("נבחרו קבצים אך לא נמצא שדה העלאה בטופס.")
        await file_input.set_input_files([str(path) for path in draft.attachments])
        await page.wait_for_timeout(500)
        body = normalize(await page.locator("main").inner_text())
        missing = [path.name for path in draft.attachments if path.name not in body]
        if missing:
            raise RuntimeError("האתר לא הציג את הקבצים שנבחרו: " + ", ".join(missing))

    body = normalize(await page.locator("main").inner_text())
    for expected in (draft.category, draft.subcategory):
        if expected not in body:
            raise RuntimeError(f"המסלול באתר אינו תואם לאישור: {expected}")

    submit = await button_by_names(page, [r"^שליחה$", r"^שליחת הבקשה$"])
    if not submit:
        raise RuntimeError("לא נמצא כפתור שליחה פעיל. ייתכן שחסר מסמך חובה.")
    await submit.click()
    try:
        await page.wait_for_url(re.compile(r"/success(?:[/?#]|$)", re.I), timeout=30_000)
    except PlaywrightTimeoutError:
        pass
    main_text = normalize(await page.locator("main").inner_text())
    number = first_match(r"פנייה\s+מספר\s*[:\-]?\s*([0-9][0-9\-]*)", main_text)
    if not number:
        raise RuntimeError("לא התקבל מסך הצלחה עם מספר פנייה.")
    return number


async def new_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command.new_request")
    if not authorized(update):
        await deny(update)
        return
    missing = valid_config()
    if missing:
        await update.effective_message.reply_text("חסרה הגדרה: " + ", ".join(missing))
        return
    chat_id = update.effective_chat.id
    async with RUNS_LOCK:
        if chat_id in RUNS:
            await update.effective_message.reply_text("כבר מתבצעת פעולה. סיים אותה או הפעל /cancel.")
            return
    await update.effective_message.reply_text("פותח את טופס הפנייה הרשמי...")
    try:
        run = await start_browser()
        run.action = "new_request"
        async with RUNS_LOCK:
            RUNS[chat_id] = run
        if await goto_new_request(run.page):
            await start_inquiry_ui(chat_id, update, run)
            return
        await begin_otp_login(run.page)
        run.otp_deadline = time.monotonic() + OTP_TTL_SECONDS
        await update.effective_message.reply_text(
            "נשלח קוד חד פעמי בן 4 ספרות. שלח אותו כאן בתוך 5 דקות. "
            "ההודעה עם הקוד תימחק אם Telegram יאפשר זאת."
        )
    except Exception as exc:
        log_event("inquiry.open_failed", level=logging.ERROR, error=exc)
        await cleanup(chat_id)
        await update.effective_message.reply_text("לא הצלחתי לפתוח את טופס הפנייה: " + normalize(str(exc))[:800])


async def send_results(update: Update, run: LoginRun) -> None:
    await save_browser_session(run)
    applications = await application_texts(run.page)
    if not applications:
        await update.effective_message.reply_text("התחברתי לדף הבקשות, אך לא מצאתי רשומות בתצוגה הנוכחית.")
        return
    messages, mode = delta_messages(applications)
    if not messages:
        if mode == "migrated":
            await update.effective_message.reply_text(
                "התחברתי. מצב המעקב שודרג בלי לשלוח מחדש את היסטוריית הפניות."
            )
        else:
            await update.effective_message.reply_text("התחברתי. אין שינוי ברשומות מאז הבדיקה הקודמת.")
        return
    heading = "הבקשות האחרונות:" if mode == "first" else "נמצאו עדכונים חדשים:"
    for message in split_telegram_messages(heading, messages):
        await update.effective_message.reply_text(message)


async def periodic_check_once(app: Application) -> tuple[bool, str]:
    """Run one read-only cycle. Return success and a privacy-safe result code."""
    global AUTOCHECK_LAST_SUCCESS, AUTOCHECK_LAST_FAILURE
    chat_id = configured_chat_id()
    if chat_id is None:
        return False, "invalid_config"
    async with RUNS_LOCK:
        if chat_id in RUNS:
            return True, "busy_skipped"
        run = await start_browser()
        RUNS[chat_id] = run
    try:
        if not await goto_applies(run.page):
            return False, "login_required"
        await save_browser_session(run)
        applications = await application_texts(run.page)
        if not applications:
            return False, "page_unreadable"
        messages, mode = delta_messages(applications)
        if messages and mode not in {"first", "migrated"}:
            heading = "נמצאו עדכונים חדשים:"
            for message in split_telegram_messages(heading, messages):
                await app.bot.send_message(chat_id=chat_id, text=message)
        AUTOCHECK_LAST_SUCCESS = time.time()
        AUTOCHECK_LAST_FAILURE = None
        return True, "changes_sent" if messages and mode == "delta" else "baseline_or_no_change"
    finally:
        await cleanup(chat_id)


async def autocheck_loop(app: Application) -> None:
    """Run serialized checks with jitter and bounded exponential failure backoff."""
    global AUTOCHECK_LAST_FAILURE
    interval = configured_autocheck_interval()
    if interval is None:
        log_event("autocheck.invalid_interval", level=logging.ERROR)
        return
    stop = AUTOCHECK_STOP
    if stop is None:
        return
    failure_count = 0
    log_event("autocheck.started")
    while not stop.is_set():
        result = "unexpected_failure"
        try:
            success, result = await periodic_check_once(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            success = False
            result = type(exc).__name__
            log_event("autocheck.cycle_failed", level=logging.ERROR, error=exc)
        if success:
            failure_count = 0
        else:
            failure_count += 1
            # Notify once per failure kind. Do not echo exception text or private data.
            if result != AUTOCHECK_LAST_FAILURE:
                AUTOCHECK_LAST_FAILURE = result
                if result == "login_required":
                    text = "הבדיקה האוטומטית זקוקה להתחברות מחדש. הפעל /check והזן את הקוד החד-פעמי."
                elif result == "invalid_config":
                    text = "הבדיקה האוטומטית נעצרה בגלל הגדרה לא תקינה. בדוק את משתני הסביבה והפעל מחדש."
                elif result == "page_unreadable":
                    text = "הבדיקה האוטומטית לא הצליחה לקרוא את דף הפניות. ייתכן שהאתר השתנה; נסה /check ידנית."
                else:
                    text = "הבדיקה האוטומטית נכשלה. אנסה שוב בהשהיה; אם זה נמשך, נסה /check ידנית."
                chat_id = configured_chat_id()
                if chat_id is not None:
                    await app.bot.send_message(chat_id=chat_id, text=text)
        base_seconds = interval * 60
        if failure_count:
            base_seconds = min(base_seconds * (2 ** min(failure_count - 1, 5)), MAX_AUTOCHECK_BACKOFF_SECONDS)
        delay = base_seconds * random.uniform(0.9, 1.1)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    log_event("autocheck.stopped")


def autocheck_running() -> bool:
    return AUTOCHECK_TASK is not None and not AUTOCHECK_TASK.done()


async def start_autocheck(app: Application) -> bool:
    global AUTOCHECK_TASK, AUTOCHECK_STOP
    if autocheck_running():
        return True
    if configured_autocheck_interval() is None:
        return False
    AUTOCHECK_STOP = asyncio.Event()
    AUTOCHECK_TASK = asyncio.create_task(autocheck_loop(app), name="autocheck")
    return True


async def stop_autocheck() -> None:
    global AUTOCHECK_TASK, AUTOCHECK_STOP
    if AUTOCHECK_STOP is not None:
        AUTOCHECK_STOP.set()
    task = AUTOCHECK_TASK
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass
    AUTOCHECK_TASK = None
    AUTOCHECK_STOP = None


async def autocheck_start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    if configured_autocheck_interval() is None:
        await update.effective_message.reply_text(
            f"AUTOCHECK_INTERVAL_MINUTES חייב להיות מספר של לפחות {MIN_AUTOCHECK_INTERVAL_MINUTES}."
        )
        return
    already = autocheck_running()
    await start_autocheck(ctx.application)
    text = "הבדיקה האוטומטית כבר פעילה." if already else "הבדיקה האוטומטית הופעלה לזמן הריצה הנוכחי."
    await update.effective_message.reply_text(text)


async def autocheck_stop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    was_running = autocheck_running()
    await stop_autocheck()
    await update.effective_message.reply_text(
        "הבדיקה האוטומטית נעצרה." if was_running else "הבדיקה האוטומטית אינה פעילה."
    )


async def autocheck_status_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    interval = configured_autocheck_interval()
    running = "פעילה" if autocheck_running() else "לא פעילה"
    interval_text = str(interval) if interval is not None else "לא תקין"
    await update.effective_message.reply_text(
        f"הפעלה באתחול: כבויה תמיד\nמצב נוכחי: {running}\nמרווח בסיסי: {interval_text} דקות"
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        log_event("command.help")
        await update.effective_message.reply_text(HELP_TEXT)


async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_message:
        await update.effective_message.reply_text(f"מזהה הצ'אט: {update.effective_chat.id}")


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command.check")
    if not authorized(update):
        await deny(update)
        return
    missing = valid_config()
    if missing:
        await update.effective_message.reply_text("חסרה הגדרה: " + ", ".join(missing))
        return
    chat_id = update.effective_chat.id
    async with RUNS_LOCK:
        if chat_id in RUNS:
            await update.effective_message.reply_text("כבר מתבצעת בדיקה. שלח את הקוד בן 4 הספרות, או /cancel.")
            return
    await update.effective_message.reply_text("בודק את האזור האישי...")
    try:
        run = await start_browser()
        async with RUNS_LOCK:
            RUNS[chat_id] = run
        if await goto_applies(run.page):
            await send_results(update, run)
            await cleanup(chat_id)
            return
        await begin_otp_login(run.page)
        run.otp_deadline = time.monotonic() + OTP_TTL_SECONDS
        await update.effective_message.reply_text(
            "נשלח קוד חד פעמי בן 4 ספרות. שלח אותו כאן בתוך 5 דקות. "
            "ההודעה עם הקוד תימחק מהצ'אט אם Telegram יאפשר זאת."
        )
    except Exception as exc:
        log_event("check.failed", level=logging.ERROR, error=exc)
        await cleanup(chat_id)
        await update.effective_message.reply_text(
            "לא הצלחתי להגיע למסך הקוד. " + normalize(str(exc))[:800] +
            "\nאם זו קריסת דפדפן, הרץ את פקודות האבחון המצורפות. HEADLESS=false דורש שולחן עבודה או xvfb-run."
        )


async def handle_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update) or not update.effective_message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    run = RUNS.get(chat_id)
    if not run or run.action != "check" and run.inquiry is not None:
        return

    raw = update.effective_message.text or ""
    code = re.sub(r"\D", "", raw)
    if not re.fullmatch(r"\d{4}", code):
        await update.effective_message.reply_text("במסך הזה נדרש קוד של 4 ספרות בלבד.")
        return
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if time.monotonic() > run.otp_deadline:
        await cleanup(chat_id)
        await update.effective_chat.send_message("הקוד פג. הפעל /check כדי לבקש קוד חדש.")
        return
    run.otp_attempts += 1
    if run.otp_attempts > MAX_OTP_ATTEMPTS:
        await cleanup(chat_id)
        await update.effective_chat.send_message("עצרתי אחרי יותר מדי ניסיונות. הפעל /check לקבלת קוד חדש.")
        return

    try:
        await fill_first(
            run.page,
            ['input[autocomplete="one-time-code"]', 'input[aria-label*="קוד חד"]', 'input[inputmode="numeric"]'],
            code,
            "קוד חד פעמי",
        )
        await click_button(run.page, [r"התחברות", r"אישור", r"להמשך", r"המשך"], "אישור הקוד")
        try:
            await run.page.wait_for_url(re.compile(r"^https://myshikum\.mod\.gov\.il/(?!.*(?:login|otp))"), timeout=30_000)
        except PlaywrightTimeoutError:
            pass
        if await looks_logged_out(run.page):
            if run.otp_attempts >= MAX_OTP_ATTEMPTS:
                await cleanup(chat_id)
                await update.effective_chat.send_message("הקוד לא התקבל. הפעל /check לקבלת קוד חדש.")
            else:
                await update.effective_chat.send_message("הקוד לא התקבל. אפשר לנסות שוב כל עוד לא חלפו 5 דקות.")
            return
        if run.action == "new_request":
            await start_inquiry_ui(chat_id, update, run)
            return
        if not await goto_applies(run.page):
            raise RuntimeError(f"ההתחברות הסתיימה אך דף הבקשות /applies/ לא נפתח: {run.page.url}")
        await send_results(update, run)
        await cleanup(chat_id)
    except Exception as exc:
        log_event("otp.check_failed", level=logging.ERROR, error=exc)
        await cleanup(chat_id)
        await update.effective_chat.send_message("הבדיקה נכשלה אחרי הקוד: " + normalize(str(exc))[:800])


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command.cancel")
    if not authorized(update):
        await deny(update)
        return
    await cleanup(update.effective_chat.id)
    await update.effective_message.reply_text("הפעולה בוטלה. לא נשלח דבר.")


async def logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command.logout")
    if not authorized(update):
        await deny(update)
        return
    await cleanup(update.effective_chat.id)
    for path in (BROWSER_STATE, SESSION_STORAGE, SEEN_MESSAGES):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    await update.effective_message.reply_text("פרטי ההתחברות המקומיים נמחקו.")


async def shutdown(app: Application) -> None:
    await stop_autocheck()
    for chat_id in list(RUNS):
        await cleanup(chat_id)


def main() -> None:
    configure_logging()
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN in the environment; never paste it into the source file.")
    secure_state_dir()
    app = ApplicationBuilder().token(TOKEN).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("autocheck_start", autocheck_start_command))
    app.add_handler(CommandHandler("autocheck_stop", autocheck_stop_command))
    app.add_handler(CommandHandler("autocheck_status", autocheck_status_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("new_request", new_request))
    app.add_handler(CommandHandler("review", review_inquiry))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CallbackQueryHandler(inquiry_callback, pattern=r"^ir:"))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, inquiry_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, inquiry_text), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_otp), group=1)
    log_event("bot.started")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
