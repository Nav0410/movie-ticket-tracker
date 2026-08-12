#!/usr/bin/env python3
"""
STRICT BookMyShow tracker for:
- Vishwanath and Sons
- ALLU Cinemas: Kokapet, Hyderabad
- August 15, 2026

Telegram is sent ONLY when:
1) BookMyShow page is not blocked/challenged
2) the exact target movie is found
3) the date URL is August 15, 2026
4) showtime-like controls are found INSIDE the same movie card/container
5) those showtime controls are clickable/bookable elements (button/a/[role=button])

This avoids the previous false positive where unrelated times elsewhere on the
page were picked up.
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
from playwright.sync_api import Browser, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TARGET_DATE = os.getenv("TARGET_DATE", "2026-08-15").strip()
CINEMA_URL = os.getenv(
    "MOVIE_URL",
    "https://in.bookmyshow.com/cinemas/hyderabad/"
    "allu-cinemas-kokapet/buytickets/ALUC/20260815",
).strip()

TARGET_MOVIE_NAMES = (
    "vishwanath and sons",
    "vishwanath & sons",
    "vishwanath sons",
)

TARGET_THEATRE_NAMES = (
    "allu cinemas: kokapet",
    "allu cinemas kokapet",
    "allu cinemas",
    "kokapet",
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path(os.getenv("STATE_FILE", "state/ticket_state.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))

TIME_PATTERN = re.compile(
    r"^(?:0?[1-9]|1[0-2])(?::[0-5]\d)\s*(?:AM|PM)$",
    re.IGNORECASE,
)

BLOCKED_PHRASES = (
    "attention required",
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "access denied",
    "captcha",
    "unusual traffic",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("vishwanath-allu-bookmyshow-strict")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def parse_time_to_minutes(value: str) -> int:
    m = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*",
        value,
        re.IGNORECASE,
    )
    if not m:
        return 10_000

    hour = int(m.group(1))
    minute = int(m.group(2))
    period = m.group(3).upper()

    if period == "AM" and hour == 12:
        hour = 0
    elif period == "PM" and hour != 12:
        hour += 12

    return hour * 60 + minute


def unique_sorted_times(values: list[str]) -> list[str]:
    cleaned = {
        normalize(v).upper()
        for v in values
        if TIME_PATTERN.fullmatch(normalize(v))
    }
    return sorted(cleaned, key=parse_time_to_minutes)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "target": "vishwanath-and-sons|ALUC|2026-08-15",
            "detected_times": [],
            "alert_sent": False,
        }

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "target": "vishwanath-and-sons|ALUC|2026-08-15",
            "detected_times": [],
            "alert_sent": False,
        }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {payload}")


def close_common_popups(page: Page) -> None:
    for label in (
        "Allow", "Not Now", "No Thanks", "Skip", "Close",
        "Maybe Later", "Accept", "Accept All", "Continue", "Got It",
    ):
        try:
            locator = page.get_by_role(
                "button",
                name=re.compile(f"^{re.escape(label)}$", re.I),
            )
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1200)
                page.wait_for_timeout(300)
        except Exception:
            pass


def visible_text(page: Page) -> str:
    return normalize(page.locator("body").inner_text(timeout=15_000))


def page_is_blocked(page: Page, body: str) -> bool:
    title = normalize(page.title()).lower()
    lower = body.lower()
    return any(x in title or x in lower for x in BLOCKED_PHRASES)


def exact_movie_locator(page: Page) -> Locator:
    """
    Find visible elements whose text is exactly (or almost exactly) the target
    movie name. This is intentionally strict so recommendations/metadata do not
    cause a ticket-release alert.
    """
    selectors = []

    for name in TARGET_MOVIE_NAMES:
        selectors.append(
            page.get_by_text(
                re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
            )
        )

    # Merge by picking first selector with visible matches.
    for locator in selectors:
        try:
            count = min(locator.count(), 20)
            for i in range(count):
                item = locator.nth(i)
                if item.is_visible():
                    return item
        except Exception:
            continue

    return page.locator("__bookmyshow_target_movie_not_found__")


def find_movie_container(movie_node: Locator) -> Locator | None:
    """
    Walk upward from exact movie title and choose the smallest ancestor that
    contains BOTH the movie title and at least one time-like clickable control.
    """
    for level in range(1, 9):
        try:
            container = movie_node.locator(f"xpath=ancestor::*[{level}]")

            if not container.count():
                continue

            text = normalize(container.first.inner_text(timeout=1200))
            lower = text.lower()

            if not any(name in lower for name in TARGET_MOVIE_NAMES):
                continue

            clickable = container.first.locator(
                "button, a, [role='button']"
            )

            clickable_count = min(clickable.count(), 150)

            for i in range(clickable_count):
                try:
                    txt = normalize(clickable.nth(i).inner_text(timeout=400))
                    if TIME_PATTERN.fullmatch(txt):
                        return container.first
                except Exception:
                    continue

        except Exception:
            continue

    return None


def extract_clickable_showtimes(container: Locator) -> list[str]:
    """
    Only accepts showtimes from clickable elements inside the target movie
    container. Plain text times elsewhere are ignored.
    """
    values: list[str] = []

    clickable = container.locator(
        "button, a, [role='button']"
    )

    count = min(clickable.count(), 250)

    for i in range(count):
        item = clickable.nth(i)

        try:
            if not item.is_visible():
                continue

            text = normalize(item.inner_text(timeout=500))

            if not TIME_PATTERN.fullmatch(text):
                continue

            # Do not count disabled controls as released/bookable.
            disabled = item.get_attribute("disabled") is not None
            aria_disabled = (item.get_attribute("aria-disabled") or "").lower() == "true"

            if disabled or aria_disabled:
                continue

            values.append(text)

        except Exception:
            continue

    return unique_sorted_times(values)


def inspect_page(page: Page) -> dict[str, Any]:
    page.set_viewport_size({"width": 1440, "height": 1200})

    page.goto(
        CINEMA_URL,
        wait_until="domcontentloaded",
        timeout=45_000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        log.info("Network did not become fully idle; continuing.")

    page.wait_for_timeout(4000)
    close_common_popups(page)

    body = visible_text(page)
    lower = body.lower()

    blocked = page_is_blocked(page, body)

    date_found = (
        "20260815" in page.url
        or "2026-08-15" in lower
        or "15 aug" in lower
        or "aug 15" in lower
        or "15 august" in lower
        or "august 15" in lower
    )

    theatre_found = (
        "/aluc/" in page.url.lower()
        and any(name in lower for name in TARGET_THEATRE_NAMES)
    )

    movie_node = exact_movie_locator(page)
    movie_found = False
    movie_container_found = False
    show_times: list[str] = []

    if movie_node.count():
        try:
            movie_found = movie_node.first.is_visible()
        except Exception:
            movie_found = False

    if movie_found:
        container = find_movie_container(movie_node.first)
        if container is not None:
            movie_container_found = True
            show_times = extract_clickable_showtimes(container)

    # STRICT RULE:
    # no alert unless exact target movie + exact theatre + exact date +
    # clickable showtimes inside the target movie's own container.
    available = (
        not blocked
        and movie_found
        and movie_container_found
        and theatre_found
        and date_found
        and bool(show_times)
    )

    return {
        "success": True,
        "available": available,
        "movie": "Vishwanath and Sons",
        "theatre": "ALLU Cinemas: Kokapet, Hyderabad",
        "target_date": TARGET_DATE,
        "movie_found": movie_found,
        "movie_container_found": movie_container_found,
        "theatre_found": theatre_found,
        "date_found": date_found,
        "show_times": show_times,
        "blocked": blocked,
        "booking_url": page.url or CINEMA_URL,
        "page_title": page.title(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def save_debug(page: Page | None, result: dict[str, Any]) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    (DEBUG_DIR / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if page is not None:
        try:
            (DEBUG_DIR / "body.txt").write_text(
                visible_text(page)[:100_000],
                encoding="utf-8",
            )
        except Exception:
            pass

        try:
            page.screenshot(
                path=str(DEBUG_DIR / "page.png"),
                full_page=True,
            )
        except Exception as exc:
            log.warning("Could not save screenshot: %s", exc)


def build_ticket_alert(result: dict[str, Any], times: list[str]) -> str:
    return (
        "🚨 VISHWANATH AND SONS TICKETS RELEASED!\n\n"
        "🎬 Movie: Vishwanath and Sons\n"
        "🏢 Cinema: ALLU Cinemas: Kokapet, Hyderabad\n"
        "📅 Date: Saturday, August 15, 2026\n\n"
        f"🕒 Bookable showtimes:\n{', '.join(times)}\n\n"
        "🎟 Book on BookMyShow:\n"
        f"{result['booking_url']}"
    )


def main() -> int:
    state = load_state()
    page: Page | None = None
    browser: Browser | None = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
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
            log.info("Movie container found: %s", result["movie_container_found"])
            log.info("Theatre found: %s", result["theatre_found"])
            log.info("Target date found: %s", result["date_found"])
            log.info("Detected BOOKABLE showtimes: %s", result["show_times"])
            log.info("Tickets available: %s", result["available"])
            log.info("Blocked: %s", result["blocked"])

            if not result["available"]:
                log.info(
                    "No confirmed bookable Vishwanath and Sons showtimes "
                    "for Aug 15 at ALLU Cinemas. No Telegram alert sent."
                )
                return 0

            current_times = unique_sorted_times(result["show_times"])
            previous_times = unique_sorted_times(
                state.get("detected_times", [])
            )

            new_times = [
                t for t in current_times
                if t not in previous_times
            ]

            if not state.get("alert_sent"):
                alert_times = current_times
            elif new_times:
                alert_times = new_times
            else:
                log.info(
                    "Bookable tickets already known and showtimes unchanged. "
                    "No Telegram alert sent."
                )
                return 0

            send_telegram(
                build_ticket_alert(
                    result,
                    alert_times,
                )
            )

            state.update(
                {
                    "target": "vishwanath-and-sons|ALUC|2026-08-15",
                    "alert_sent": True,
                    "detected_times": current_times,
                    "last_alert_at": datetime.now(timezone.utc).isoformat(),
                    "booking_url": result["booking_url"],
                }
            )
            save_state(state)

            log.info("Telegram ticket-release alert sent.")
            return 0

    except Exception as exc:
        log.exception("Tracker failed: %s", exc)

        save_debug(
            page,
            {
                "success": False,
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # User requested Telegram only for ticket release.
        return 1

    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
