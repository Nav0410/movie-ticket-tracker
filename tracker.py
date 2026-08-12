#!/usr/bin/env python3
"""
BookMyShow ticket-release tracker for:
- Vishwanath and Sons
- ALLU Cinemas: Kokapet, Hyderabad
- August 15, 2026
- Any released showtime

Telegram behavior:
- Sends NO routine status messages.
- Sends ONE alert when qualifying tickets/showtimes are first detected.
- Sends another alert only if genuinely new showtimes appear later.
- Does not send Telegram messages for no-ticket checks or tracker failures.
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

TARGET_DATE_ISO = os.getenv("TARGET_DATE", "2026-08-15")
TARGET_DATE_BMS = TARGET_DATE_ISO.replace("-", "")

# BookMyShow cinema code for ALLU Cinemas: Kokapet is ALUC.
CINEMA_URL = os.getenv(
    "MOVIE_URL",
    f"https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/"
    f"buytickets/ALUC/{TARGET_DATE_BMS}",
)

TARGET_MOVIE_NAMES = (
    "vishwanath and sons",
    "vishwanath & sons",
    "vishwanath sons",
)

TARGET_THEATRE_NAMES = (
    "allu cinemas: kokapet",
    "allu cinemas kokapet",
    "allu cinemas",
)

STATE_FILE = Path(os.getenv("STATE_FILE", "state/ticket_state.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

TIME_PATTERN = re.compile(
    r"\b(?:0?[1-9]|1[0-2])(?::[0-5][0-9])\s*(?:AM|PM)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("vishwanath-sons-allu-cinemas-tracker")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def parse_time_to_minutes(value: str) -> int:
    match = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*",
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

    return hour * 60 + minute


def unique_sorted_times(values: list[str]) -> list[str]:
    cleaned = {
        normalize(value).upper()
        for value in values
        if TIME_PATTERN.fullmatch(normalize(value))
    }
    return sorted(cleaned, key=parse_time_to_minutes)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "detected_times": [],
            "alert_sent": False,
            "last_alert_at": None,
        }

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read state file: %s", exc)
        return {
            "detected_times": [],
            "alert_sent": False,
            "last_alert_at": None,
        }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is missing. "
            "Add both under GitHub repository Actions secrets."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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
        raise RuntimeError(f"Telegram rejected the message: {payload}")


def visible_text(page: Page) -> str:
    return normalize(page.locator("body").inner_text(timeout=15_000))


def close_common_popups(page: Page) -> None:
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
        "No, Thanks",
    )

    for label in labels:
        try:
            locator = page.get_by_role(
                "button",
                name=re.compile(f"^{re.escape(label)}$", re.I),
            )
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1_500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def scroll_page(page: Page) -> None:
    page.evaluate(
        """
        async () => {
          await new Promise(resolve => {
            let previousHeight = 0;
            let stable = 0;
            const timer = setInterval(() => {
              window.scrollBy(0, 700);
              const h = document.body.scrollHeight;
              if (h === previousHeight) stable += 1;
              else stable = 0;
              previousHeight = h;
              if (window.scrollY + window.innerHeight >= h || stable >= 4) {
                clearInterval(timer);
                resolve();
              }
            }, 180);
          });
        }
        """
    )
    page.wait_for_timeout(1_500)


def extract_movie_section(body_text: str) -> str:
    lower = body_text.lower()
    positions = [
        lower.find(name)
        for name in TARGET_MOVIE_NAMES
        if lower.find(name) >= 0
    ]
    if not positions:
        return ""

    start_index = min(positions)
    start = max(0, start_index - 250)

    # A generous window captures the target movie's language/format/showtimes
    # while avoiding most unrelated movies farther down the cinema page.
    return body_text[start : min(len(body_text), start_index + 4_500)]


def target_date_is_present(body_text: str, current_url: str) -> bool:
    lower = body_text.lower()
    date_tokens = (
        "15 aug",
        "aug 15",
        "15 august",
        "august 15",
        "sat 15",
        "saturday 15",
        "2026-08-15",
        "20260815",
    )
    return (
        TARGET_DATE_BMS in current_url
        or any(token in lower for token in date_tokens)
    )


def inspect_page(page: Page) -> dict[str, Any]:
    page.set_viewport_size({"width": 1440, "height": 1200})
    page.goto(CINEMA_URL, wait_until="domcontentloaded", timeout=45_000)

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        log.info("Network did not become fully idle; continuing.")

    page.wait_for_timeout(4_000)
    close_common_popups(page)
    scroll_page(page)

    body = visible_text(page)
    lower_body = body.lower()
    movie_section = extract_movie_section(body)
    lower_section = movie_section.lower()

    movie_found = any(name in lower_body for name in TARGET_MOVIE_NAMES)
    theatre_found = any(name in lower_body for name in TARGET_THEATRE_NAMES)
    date_found = target_date_is_present(body, page.url)

    show_times = unique_sorted_times(TIME_PATTERN.findall(movie_section))

    blocked_phrases = (
        "access denied",
        "verify you are human",
        "captcha",
        "unusual traffic",
        "security check",
    )
    blocked = any(text in lower_body for text in blocked_phrases)

    unavailable_phrases = (
        "booking not available",
        "tickets not available",
        "no shows available",
        "showtimes unavailable",
        "coming soon",
    )
    unavailable_found = any(text in lower_section for text in unavailable_phrases)

    available = (
        movie_found
        and theatre_found
        and date_found
        and bool(show_times)
        and not blocked
        and not unavailable_found
    )

    return {
        "success": True,
        "available": available,
        "movie": "Vishwanath and Sons",
        "theatre": "ALLU Cinemas: Kokapet, Hyderabad",
        "target_date": TARGET_DATE_ISO,
        "movie_found": movie_found,
        "theatre_found": theatre_found,
        "date_found": date_found,
        "show_times": show_times,
        "blocked": blocked,
        "unavailable_found": unavailable_found,
        "booking_url": page.url or CINEMA_URL,
        "page_title": page.title(),
        "movie_section_preview": movie_section[:3_000],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def save_debug(page: Page | None, result: dict[str, Any]) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    (DEBUG_DIR / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    (DEBUG_DIR / "movie-section.txt").write_text(
        str(result.get("movie_section_preview", "")),
        encoding="utf-8",
    )

    if page is not None:
        try:
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        except Exception as exc:
            log.warning("Unable to save screenshot: %s", exc)


def build_ticket_alert(result: dict[str, Any], new_times: list[str]) -> str:
    return (
        "🚨 VISHWANATH AND SONS TICKETS RELEASED!\n\n"
        "🎬 Movie: Vishwanath and Sons\n"
        "🏢 Theatre: ALLU Cinemas: Kokapet, Hyderabad\n"
        "📅 Date: Saturday, August 15, 2026\n\n"
        f"🕒 Available showtimes:\n{', '.join(result['show_times'])}\n\n"
        f"🆕 Newly detected:\n{', '.join(new_times)}\n\n"
        f"🎟 Book on BookMyShow:\n{result['booking_url']}"
    )


def main() -> int:
    # Manual Telegram test only when explicitly requested from workflow_dispatch.
    if TEST_MODE:
        send_telegram(
            "✅ Vishwanath and Sons / ALLU Cinemas tracker Telegram test successful."
        )
        return 0

    state = load_state()
    previous_times = unique_sorted_times(state.get("detected_times", []))

    page: Page | None = None
    browser: Browser | None = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )

            context = browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            result = inspect_page(page)
            save_debug(page, result)

            log.info("Movie found: %s", result["movie_found"])
            log.info("Theatre found: %s", result["theatre_found"])
            log.info("Target date found: %s", result["date_found"])
            log.info("Detected showtimes: %s", result["show_times"])
            log.info("Tickets available: %s", result["available"])

            # IMPORTANT: no Telegram message when tickets are not released.
            if not result["available"]:
                log.info("No qualifying tickets yet. No Telegram alert sent.")
                return 0

            new_times = [
                show_time
                for show_time in result["show_times"]
                if show_time not in previous_times
            ]

            # If tickets were already detected and nothing new appeared,
            # remain silent to avoid repeated Telegram notifications.
            if not new_times:
                log.info("Showtimes unchanged. No Telegram alert sent.")
                return 0

            send_telegram(build_ticket_alert(result, new_times))

            state.update(
                {
                    "detected_times": result["show_times"],
                    "alert_sent": True,
                    "last_alert_at": datetime.now(timezone.utc).isoformat(),
                    "last_booking_url": result["booking_url"],
                    "movie": "Vishwanath and Sons",
                    "target_date": TARGET_DATE_ISO,
                    "theatre": "ALLU Cinemas: Kokapet, Hyderabad",
                }
            )
            save_state(state)
            log.info("Telegram ticket-release alert sent: %s", new_times)
            return 0

    except Exception as exc:
        # User requested Telegram alerts ONLY when tickets are released,
        # therefore tracker errors are logged/debugged but not sent to Telegram.
        error_result = {
            "success": False,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        log.exception("Tracker failed")
        save_debug(page, error_result)
        return 1

    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
