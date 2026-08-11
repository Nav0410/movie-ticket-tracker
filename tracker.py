#!/usr/bin/env python3
"""
GitHub Actions ticket tracker for:
- Vishwanath & Sons
- SVC Cinemas, City Square Mall, Kurnool
- August 14, 2026
- Any released showtime

Telegram behaviour:
- Sends status when no qualifying tickets are found
- Sends status when showtimes are unchanged
- Sends an alert when new showtimes are detected
- Sends an alert if the tracker fails
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# ============================================================
# TARGET CONFIGURATION
# ============================================================

MOVIE_URL = os.getenv(
    "MOVIE_URL",
    "https://www.district.in/movies/"
    "vishwanath-and-sons-movie-tickets-in-kurnool-MV216343",
)

TARGET_MOVIE_NAMES = (
    "vishwanath and sons",
    "vishwanath & sons",
    "vishwanath sons",
)

TARGET_THEATRE_NAMES = (
    "svc cinemas, city square mall, kurnool",
    "svc cinemas city square mall kurnool",
    "svc cinemas, city square mall",
    "svc cinemas city square mall",
    "svc cinemas",
)

TARGET_DATE_ISO = os.getenv(
    "TARGET_DATE",
    "2026-08-14",
)

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        "state/ticket_state.json",
    )
)

DEBUG_DIR = Path(
    os.getenv(
        "DEBUG_DIR",
        "debug",
    )
)

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()

TEST_MODE = (
    os.getenv(
        "TEST_MODE",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# REGEX / LOGGING
# ============================================================

TIME_PATTERN = re.compile(
    r"\b(?:0?[1-9]|1[0-2])"
    r"(?::[0-5][0-9])\s*"
    r"(?:AM|PM)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(
    "vishwanath-sons-ticket-tracker"
)


# ============================================================
# HELPERS
# ============================================================

def normalize(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        value.replace("\u00a0", " "),
    ).strip()


def parse_time_to_minutes(
    value: str,
) -> int:

    match = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})"
        r"\s*(AM|PM)\s*",
        value,
        re.IGNORECASE,
    )

    if not match:
        return 10_000

    hour = int(match.group(1))
    minute = int(match.group(2))
    period = match.group(3).upper()

    if period == "AM" and hour == 12:
        hour = 0

    elif period == "PM" and hour != 12:
        hour += 12

    return (
        hour * 60
        + minute
    )


def unique_sorted_times(
    values: list[str],
) -> list[str]:

    cleaned = {
        normalize(value).upper()
        for value in values
        if TIME_PATTERN.fullmatch(
            normalize(value)
        )
    }

    return sorted(
        cleaned,
        key=parse_time_to_minutes,
    )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state() -> dict[str, Any]:

    if not STATE_FILE.exists():
        return {
            "detected_times": [],
            "last_alert_at": None,
        }

    try:

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        json.JSONDecodeError,
        OSError,
    ) as exc:

        log.warning(
            "Could not read state file: %s",
            exc,
        )

        return {
            "detected_times": [],
            "last_alert_at": None,
        }


def save_state(
    state: dict[str, Any],
) -> None:

    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(
    message: str,
) -> None:

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        raise RuntimeError(
            "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID "
            "is missing. Add both under "
            "GitHub repository Actions secrets."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=25,
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get("ok"):

        raise RuntimeError(
            "Telegram rejected the message: "
            f"{payload}"
        )


# ============================================================
# PAGE HELPERS
# ============================================================

def visible_text(
    page: Page,
) -> str:

    return normalize(
        page.locator(
            "body"
        ).inner_text(
            timeout=15_000
        )
    )


def close_common_popups(
    page: Page,
) -> None:

    labels = (
        "Allow",
        "Not Now",
        "No Thanks",
        "Skip",
        "Close",
        "Maybe Later",
        "Accept",
        "Accept All",
        "Continue",
        "Got It",
    )

    for label in labels:

        try:

            locator = page.get_by_role(
                "button",
                name=re.compile(
                    f"^{re.escape(label)}$",
                    re.I,
                ),
            )

            if (
                locator.count()
                and locator.first.is_visible()
            ):

                locator.first.click(
                    timeout=1_500
                )

                page.wait_for_timeout(
                    500
                )

        except Exception:
            pass


# ============================================================
# DATE SELECTION
# ============================================================

def click_target_date(
    page: Page,
) -> dict[str, Any]:

    target_date = datetime.strptime(
        TARGET_DATE_ISO,
        "%Y-%m-%d",
    )

    day = str(
        target_date.day
    )

    day2 = (
        f"{target_date.day:02d}"
    )

    month_short = (
        target_date.strftime("%b")
    )

    month_long = (
        target_date.strftime("%B")
    )

    weekday_short = (
        target_date.strftime("%a")
    )

    weekday_long = (
        target_date.strftime("%A")
    )

    patterns = [

        rf"^{day}\s*{weekday_short}$",
        rf"^{weekday_short}\s*{day}$",

        rf"^{day2}\s*{weekday_short}$",
        rf"^{weekday_short}\s*{day2}$",

        rf"^{day}\s*{weekday_long}$",
        rf"^{weekday_long}\s*{day}$",

        rf"^{day}\s*{month_short}$",
        rf"^{month_short}\s*{day}$",

        rf"^{day2}\s*{month_short}$",
        rf"^{month_short}\s*{day2}$",

        rf"^{day}\s*{month_long}$",
        rf"^{month_long}\s*{day}$",

        rf"^{day}$",
        rf"^{day2}$",
    ]

    candidates = page.locator(
        "button, "
        "[role=button], "
        "a, "
        "li"
    )

    count = min(
        candidates.count(),
        700,
    )

    for index in range(
        count
    ):

        candidate = candidates.nth(
            index
        )

        try:

            if not candidate.is_visible():
                continue

            text = normalize(
                candidate.inner_text(
                    timeout=500
                )
            )

            if any(
                re.fullmatch(
                    pattern,
                    text,
                    re.I,
                )
                for pattern in patterns
            ):

                candidate.click(
                    timeout=2_000
                )

                page.wait_for_timeout(
                    4_000
                )

                return {
                    "clicked": True,
                    "text": text,
                }

        except Exception:
            continue

    return {
        "clicked": False,
        "text": "",
    }


# ============================================================
# SCROLL PAGE
# ============================================================

def scroll_page(
    page: Page,
) -> None:

    page.evaluate(
        """
        async () => {

          await new Promise(resolve => {

            let moved = 0;
            const step = 650;

            const timer = setInterval(() => {

              window.scrollBy(
                0,
                step
              );

              moved += step;

              if (
                moved >=
                document.body.scrollHeight + 1000
              ) {

                clearInterval(timer);

                window.scrollTo(
                    0,
                    0
                );

                resolve();
              }

            }, 180);

          });

        }
        """
    )

    page.wait_for_timeout(
        2_000
    )


# ============================================================
# THEATRE SECTION EXTRACTION
# ============================================================

def extract_theatre_section(
    body_text: str,
) -> str:

    lower = body_text.lower()

    indexes = [
        lower.find(name)
        for name in TARGET_THEATRE_NAMES
        if lower.find(name) >= 0
    ]

    if not indexes:
        return ""

    start_index = min(
        indexes
    )

    start = max(
        0,
        start_index - 500,
    )

    end = min(
        len(body_text),
        start_index + 7_500,
    )

    return body_text[
        start:end
    ]


# ============================================================
# PAGE INSPECTION
# ============================================================

def inspect_page(
    page: Page,
) -> dict[str, Any]:

    page.set_viewport_size(
        {
            "width": 1440,
            "height": 1200,
        }
    )

    page.goto(
        MOVIE_URL,
        wait_until="domcontentloaded",
        timeout=45_000,
    )

    try:

        page.wait_for_load_state(
            "networkidle",
            timeout=15_000,
        )

    except PlaywrightTimeoutError:

        log.info(
            "Network did not become "
            "fully idle; continuing."
        )

    page.wait_for_timeout(
        4_000
    )

    close_common_popups(
        page
    )

    date_click = click_target_date(
        page
    )

    scroll_page(
        page
    )

    body = visible_text(
        page
    )

    lower_body = (
        body.lower()
    )

    section = extract_theatre_section(
        body
    )

    lower_section = (
        section.lower()
    )

    movie_found = any(
        name in lower_body
        for name in TARGET_MOVIE_NAMES
    )

    theatre_found = any(
        name in lower_section
        for name in TARGET_THEATRE_NAMES
    )

    city_found = (
        "kurnool"
        in lower_body
    )

    target_date = datetime.strptime(
        TARGET_DATE_ISO,
        "%Y-%m-%d",
    )

    date_variants = (
        "14 aug",
        "aug 14",
        "14 august",
        "august 14",
        "14/08/2026",
        "14-08-2026",
        "2026-08-14",
        "friday 14",
        "fri 14",
    )

    date_found = (
        date_click["clicked"]
        or any(
            value in lower_body
            for value in date_variants
        )
    )

    show_times = (
        unique_sorted_times(
            TIME_PATTERN.findall(
                section
            )
        )
    )

    unavailable_phrases = (
        "no shows available",
        "showtimes unavailable",
        "tickets not available",
        "booking not available",
        "no showtimes",
        "coming soon",
        "booking opens soon",
    )

    unavailable_found = any(
        text in lower_section
        for text in unavailable_phrases
    )

    blocked_phrases = (
        "access denied",
        "verify you are human",
        "captcha",
        "unusual traffic",
        "security check",
    )

    blocked = any(
        text in lower_body
        for text in blocked_phrases
    )

    available = (
        movie_found
        and theatre_found
        and city_found
        and date_found
        and bool(show_times)
        and not unavailable_found
        and not blocked
    )

    return {

        "success": True,

        "available": available,

        "movie": (
            "Vishwanath & Sons"
        ),

        "theatre": (
            "SVC Cinemas, "
            "City Square Mall, "
            "Kurnool"
        ),

        "target_date": (
            TARGET_DATE_ISO
        ),

        "movie_found": (
            movie_found
        ),

        "theatre_found": (
            theatre_found
        ),

        "city_found": (
            city_found
        ),

        "date_found": (
            date_found
        ),

        "date_click": (
            date_click
        ),

        "show_times": (
            show_times
        ),

        "unavailable_found": (
            unavailable_found
        ),

        "blocked": (
            blocked
        ),

        "booking_url": (
            page.url
            or MOVIE_URL
        ),

        "page_title": (
            page.title()
        ),

        "theatre_section_preview": (
            section[:3_000]
        ),

        "checked_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }


# ============================================================
# DEBUG OUTPUT
# ============================================================

def save_debug(
    page: Page | None,
    result: dict[str, Any],
) -> None:

    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        DEBUG_DIR
        / "result.json"
    ).write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (
        DEBUG_DIR
        / "theatre-section.txt"
    ).write_text(
        str(
            result.get(
                "theatre_section_preview",
                "",
            )
        ),
        encoding="utf-8",
    )

    if page is not None:

        try:

            page.screenshot(
                path=str(
                    DEBUG_DIR
                    / "page.png"
                ),
                full_page=True,
            )

        except Exception as exc:

            log.warning(
                "Unable to save screenshot: %s",
                exc,
            )


# ============================================================
# TELEGRAM MESSAGE BUILDERS
# ============================================================

def build_new_ticket_alert(
    result: dict[str, Any],
    new_times: list[str],
) -> str:

    all_times = (
        result["show_times"]
    )

    return (
        "🚨 VISHWANATH & SONS "
        "TICKETS RELEASED!\n\n"

        "🎬 Movie: "
        "Vishwanath & Sons\n"

        "🏢 Theatre: "
        "SVC Cinemas, "
        "City Square Mall, "
        "Kurnool\n"

        "📅 Date: "
        "Friday, August 14, 2026\n\n"

        "🆕 Newly detected showtimes:\n"
        f"{', '.join(new_times)}\n\n"

        "🕒 All detected showtimes:\n"
        f"{', '.join(all_times)}\n\n"

        "🎟 BOOK IMMEDIATELY:\n"
        f"{result['booking_url']}"
    )


def build_no_tickets_message(
    result: dict[str, Any],
) -> str:

    detected_times = (
        ", ".join(
            result["show_times"]
        )
        if result["show_times"]
        else "None"
    )

    checked_time = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    )

    return (
        "✅ VISHWANATH & SONS "
        "TRACKER RAN\n\n"

        "🎬 Vishwanath & Sons\n"

        "🏢 SVC Cinemas, "
        "City Square Mall, "
        "Kurnool\n"

        "📅 Target date: "
        "August 14, 2026\n\n"

        f"Movie found: "
        f"{result['movie_found']}\n"

        f"Theatre found: "
        f"{result['theatre_found']}\n"

        f"August 14 found: "
        f"{result['date_found']}\n"

        f"Detected showtimes: "
        f"{detected_times}\n\n"

        "❌ No qualifying "
        "August 14 tickets found yet.\n\n"

        f"Checked at: "
        f"{checked_time}"
    )


def build_unchanged_message(
    result: dict[str, Any],
) -> str:

    checked_time = (
        datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    )

    return (
        "✅ VISHWANATH & SONS "
        "TRACKER RAN\n\n"

        "🎬 Movie: "
        "Vishwanath & Sons\n"

        "🏢 Theatre: "
        "SVC Cinemas, "
        "City Square Mall, "
        "Kurnool\n"

        "📅 Date: "
        "August 14, 2026\n\n"

        "ℹ️ Showtimes are unchanged.\n\n"

        "🕒 Current showtimes:\n"
        f"{', '.join(result['show_times'])}\n\n"

        "🎟 Booking link:\n"
        f"{result['booking_url']}\n\n"

        f"Checked at: "
        f"{checked_time}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    if TEST_MODE:

        send_telegram(
            "✅ Vishwanath & Sons "
            "ticket tracker test successful.\n\n"
            "Telegram secrets are configured correctly."
        )

        log.info(
            "Test Telegram message sent."
        )

        return 0

    state = load_state()

    previous_times = (
        unique_sorted_times(
            state.get(
                "detected_times",
                [],
            )
        )
    )

    page: Page | None = None
    browser: Browser | None = None

    try:

        with sync_playwright() as playwright:

            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features="
                    "AutomationControlled",
                    "--no-sandbox",
                ],
            )

            context = browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/127.0.0.0 "
                    "Safari/537.36"
                ),
            )

            page = (
                context.new_page()
            )

            result = inspect_page(
                page
            )

            save_debug(
                page,
                result,
            )

            log.info(
                "Movie found: %s",
                result[
                    "movie_found"
                ],
            )

            log.info(
                "Theatre found: %s",
                result[
                    "theatre_found"
                ],
            )

            log.info(
                "Target date found: %s",
                result[
                    "date_found"
                ],
            )

            log.info(
                "Detected showtimes: %s",
                result[
                    "show_times"
                ],
            )

            log.info(
                "Available: %s",
                result[
                    "available"
                ],
            )

            # ------------------------------------------------
            # NO TICKETS YET
            # ------------------------------------------------

            if not result[
                "available"
            ]:

                message = (
                    build_no_tickets_message(
                        result
                    )
                )

                send_telegram(
                    message
                )

                log.info(
                    "No-ticket Telegram "
                    "status sent."
                )

                return 0

            # ------------------------------------------------
            # DETECT NEW SHOWTIMES
            # ------------------------------------------------

            new_times = [
                time
                for time in result[
                    "show_times"
                ]
                if time
                not in previous_times
            ]

            # ------------------------------------------------
            # SHOWTIMES UNCHANGED
            # ------------------------------------------------

            if not new_times:

                message = (
                    build_unchanged_message(
                        result
                    )
                )

                send_telegram(
                    message
                )

                log.info(
                    "Showtimes unchanged "
                    "Telegram status sent."
                )

                return 0

            # ------------------------------------------------
            # NEW TICKETS FOUND
            # ------------------------------------------------

            message = (
                build_new_ticket_alert(
                    result,
                    new_times,
                )
            )

            send_telegram(
                message
            )

            state.update(
                {
                    "detected_times":
                        result[
                            "show_times"
                        ],

                    "last_alert_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),

                    "last_booking_url":
                        result[
                            "booking_url"
                        ],

                    "movie":
                        "Vishwanath & Sons",

                    "target_date":
                        TARGET_DATE_ISO,

                    "theatre":
                        (
                            "SVC Cinemas, "
                            "City Square Mall, "
                            "Kurnool"
                        ),
                }
            )

            save_state(
                state
            )

            log.info(
                "Telegram ticket alert "
                "sent for new showtimes: %s",
                new_times,
            )

            return 0

    except Exception as exc:

        error_result = {

            "success": False,

            "available": False,

            "movie":
                "Vishwanath & Sons",

            "target_date":
                TARGET_DATE_ISO,

            "error":
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),

            "checked_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),
        }

        log.exception(
            "Tracker failed"
        )

        save_debug(
            page,
            error_result,
        )

        try:

            send_telegram(
                "⚠️ VISHWANATH & SONS "
                "TICKET TRACKER FAILED\n\n"

                f"Error: "
                f"{type(exc).__name__}: "
                f"{exc}\n\n"

                "GitHub will try again "
                "on the next run."
            )

        except Exception:

            log.exception(
                "Could not send failure "
                "notification to Telegram."
            )

        return 1

    finally:

        if browser is not None:

            try:
                browser.close()

            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(
        main()
    )
