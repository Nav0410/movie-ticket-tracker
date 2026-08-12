#!/usr/bin/env python3
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
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TARGET_DATE = os.getenv("TARGET_DATE", "2026-08-15").strip()
CINEMA_URL = os.getenv(
    "MOVIE_URL",
    "https://in.bookmyshow.com/cinemas/hyderabad/allu-cinemas-kokapet/buytickets/ALUC/20260815",
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
    r"\b(?:0?[1-9]|1[0-2])(?::[0-5]\d)\s*(?:AM|PM)\b",
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

NO_SHOW_PHRASES = (
    "no shows available",
    "no showtimes",
    "showtimes unavailable",
    "tickets not available",
    "booking not available",
    "booking opens soon",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("vishwanath-allu-bookmyshow")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ")).strip()


def parse_time_to_minutes(value: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*", value, re.I)
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
        return {"detected_times": [], "alert_sent": False}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"detected_times": [], "alert_sent": False}


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
    ):
        try:
            locator = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def scroll_page(page: Page) -> None:
    page.evaluate(
        """
        async () => {
          await new Promise(resolve => {
            let moved = 0;
            const step = 700;
            const timer = setInterval(() => {
              window.scrollBy(0, step);
              moved += step;
              if (moved >= document.body.scrollHeight + 1000) {
                clearInterval(timer);
                window.scrollTo(0, 0);
                resolve();
              }
            }, 180);
          });
        }
        """
    )
    page.wait_for_timeout(1500)


def visible_text(page: Page) -> str:
    return normalize(page.locator("body").inner_text(timeout=15_000))


def extract_target_context(body: str) -> str:
    lower = body.lower()
    positions = []

    for name in TARGET_MOVIE_NAMES + TARGET_THEATRE_NAMES:
        idx = lower.find(name)
        if idx >= 0:
            positions.append(idx)

    if not positions:
        return ""

    start = max(0, min(positions) - 1500)
    end = min(len(body), max(positions) + 8000)
    return body[start:end]


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
    scroll_page(page)

    body = visible_text(page)
    lower = body.lower()
    title = normalize(page.title()).lower()

    blocked = any(
        phrase in title or phrase in lower
        for phrase in BLOCKED_PHRASES
    )

    movie_found = any(name in lower for name in TARGET_MOVIE_NAMES)
    theatre_found = any(name in lower for name in TARGET_THEATRE_NAMES)

    if "/aluc/" in page.url.lower():
        theatre_found = True

    date_found = "20260815" in page.url or any(
        value in lower
        for value in (
            "15 aug",
            "aug 15",
            "15 august",
            "august 15",
            "2026-08-15",
        )
    )

    context = extract_target_context(body)
    search_area = context if context else body

    show_times = unique_sorted_times(
        TIME_PATTERN.findall(search_area)
    )

    unavailable_found = any(
        phrase in search_area.lower()
        for phrase in NO_SHOW_PHRASES
    )

    available = (
        movie_found
        and theatre_found
        and date_found
        and bool(show_times)
        and not unavailable_found
        and not blocked
    )

    return {
        "success": True,
        "available": available,
        "movie": "Vishwanath and Sons",
        "theatre": "ALLU Cinemas: Kokapet, Hyderabad",
        "target_date": TARGET_DATE,
        "movie_found": movie_found,
        "theatre_found": theatre_found,
        "date_found": date_found,
        "show_times": show_times,
        "unavailable_found": unavailable_found,
        "blocked": blocked,
        "booking_url": page.url or CINEMA_URL,
        "page_title": page.title(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "context_preview": context[:4000],
    }


def save_debug(page: Page | None, result: dict[str, Any]) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    (DEBUG_DIR / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    (DEBUG_DIR / "context-preview.txt").write_text(
        str(result.get("context_preview", "")),
        encoding="utf-8",
    )

    if page is not None:
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
        f"🕒 Detected showtimes:\n{', '.join(times)}\n\n"
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
            log.info("Theatre found: %s", result["theatre_found"])
            log.info("Target date found: %s", result["date_found"])
            log.info("Detected showtimes: %s", result["show_times"])
            log.info("Tickets available: %s", result["available"])
            log.info("Blocked: %s", result["blocked"])

            # User requested Telegram only when tickets are released.
            if not result["available"]:
                if result["blocked"]:
                    log.warning(
                        "BookMyShow returned a challenge/block page. "
                        "No Telegram alert sent."
                    )
                else:
                    log.info(
                        "No qualifying tickets yet. No Telegram alert sent."
                    )
                return 0

            current_times = unique_sorted_times(result["show_times"])
            previous_times = unique_sorted_times(
                state.get("detected_times", [])
            )

            new_times = [
                t for t in current_times if t not in previous_times
            ]

            if not state.get("alert_sent"):
                alert_times = current_times
            elif new_times:
                alert_times = new_times
            else:
                log.info(
                    "Tickets already known and showtimes unchanged. "
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

        # No Telegram error messages by design.
        return 1

    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
