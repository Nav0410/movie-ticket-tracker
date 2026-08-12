#!/usr/bin/env python3
"""
Vishwanath and Sons ticket-release tracker

Target:
- Movie: Vishwanath and Sons
- Cinema: ALLU Cinemas: Kokapet, Hyderabad
- Date: August 15, 2026
- Source: BookMyShow
- Telegram: alert ONLY when matching showtimes/tickets are detected

Important:
- This version does NOT use Playwright.
- It fetches BookMyShow with normal HTTPS requests and parses visible HTML +
  embedded JSON/Next.js data.
- If BookMyShow returns a Cloudflare/challenge page, the run exits safely
  without sending a Telegram message.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

TARGET_DATE = os.getenv("TARGET_DATE", "2026-08-15").strip()

CINEMA_URL = os.getenv(
    "MOVIE_URL",
    "https://in.bookmyshow.com/cinemas/hyderabad/"
    "allu-cinemas-kokapet/buytickets/ALUC/20260815",
).strip()

# Fallback page. It can help if the cinema page layout changes.
MOVIE_URL = os.getenv(
    "BMS_MOVIE_URL",
    "https://in.bookmyshow.com/movies/hyderabad/vishwanath-and-sons/",
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

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("vishwanath-allu-bookmyshow")

TIME_PATTERN = re.compile(
    r"\b(?:0?[1-9]|1[0-2])(?::[0-5]\d)\s*(?:AM|PM)\b",
    re.IGNORECASE,
)

TIME_24_PATTERN = re.compile(
    r'(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)'
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


# ---------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------

def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def canonical(value: str) -> str:
    value = normalize(value).lower()
    value = value.replace("&amp;", "&")
    return value


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


def time24_to_12(value: str) -> str:
    hour, minute = map(int, value.split(":"))
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period}"


def unique_sorted_times(values: Iterable[str]) -> list[str]:
    cleaned: set[str] = set()

    for raw in values:
        value = normalize(raw).upper()

        if TIME_PATTERN.fullmatch(value):
            cleaned.add(value)
            continue

        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            cleaned.add(time24_to_12(value))

    return sorted(cleaned, key=parse_time_to_minutes)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"detected_times": [], "alert_sent": False}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Unable to read state file: %s", exc)
        return {"detected_times": [], "alert_sent": False}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_debug(result: dict[str, Any], body: str = "") -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    (DEBUG_DIR / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Save only a limited response preview.
    (DEBUG_DIR / "response-preview.txt").write_text(
        body[:25_000],
        encoding="utf-8",
    )


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID is missing from GitHub secrets."
        )

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


# ---------------------------------------------------------------------
# BookMyShow fetch + parsing
# ---------------------------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()

    # Normal browser-like request headers; no Cloudflare bypassing.
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-IN,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


def fetch_page(session: requests.Session, url: str) -> tuple[str, str, int]:
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    return response.text, response.url, response.status_code


def page_is_blocked(text: str, title: str = "") -> bool:
    haystack = canonical(f"{title} {text[:10_000]}")
    return any(phrase in haystack for phrase in BLOCKED_PHRASES)


def flatten_json_strings(value: Any) -> list[str]:
    values: list[str] = []

    if isinstance(value, dict):
        for key, item in value.items():
            values.append(str(key))
            values.extend(flatten_json_strings(item))

    elif isinstance(value, list):
        for item in value:
            values.extend(flatten_json_strings(item))

    elif isinstance(value, (str, int, float, bool)):
        values.append(str(value))

    return values


def extract_search_text_and_json(raw_html: str) -> tuple[str, list[str], str]:
    soup = BeautifulSoup(raw_html, "html.parser")

    title = normalize(soup.title.get_text(" ", strip=True)) if soup.title else ""

    visible_text = normalize(soup.get_text(" ", strip=True))

    embedded_strings: list[str] = []

    # Parse JSON/JSON-LD/Next.js style script data if present.
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = (script.get("id") or "").lower()
        content = script.string or script.get_text() or ""
        content = content.strip()

        if not content:
            continue

        should_try_json = (
            "json" in script_type
            or script_id == "__next_data__"
            or content.startswith("{")
            or content.startswith("[")
        )

        if should_try_json:
            try:
                data = json.loads(content)
                embedded_strings.extend(flatten_json_strings(data))
            except Exception:
                pass

    combined = normalize(
        visible_text + " " + " ".join(embedded_strings)
    )

    return combined, embedded_strings, title


def target_date_variants() -> tuple[str, ...]:
    target = datetime.strptime(TARGET_DATE, "%Y-%m-%d")

    return (
        TARGET_DATE.lower(),
        target.strftime("%Y%m%d").lower(),
        target.strftime("%d-%m-%Y").lower(),
        target.strftime("%d/%m/%Y").lower(),
        target.strftime("%d %b %Y").lower(),
        target.strftime("%d %B %Y").lower(),
        target.strftime("%b %d").lower(),
        target.strftime("%B %d").lower(),
        target.strftime("%d %b").lower(),
        target.strftime("%d %B").lower(),
    )


def extract_times(text: str) -> list[str]:
    values: list[str] = []

    values.extend(TIME_PATTERN.findall(text))

    # Find 24-hour times too, but be conservative.
    for m in TIME_24_PATTERN.finditer(text):
        value = f"{m.group(1)}:{m.group(2)}"

        # Avoid obvious date fragments / timestamps where possible.
        start = max(0, m.start() - 25)
        end = min(len(text), m.end() + 25)
        context = canonical(text[start:end])

        if "http" in context:
            continue

        values.append(value)

    return unique_sorted_times(values)


def find_target_context(text: str) -> str:
    """
    Return a local text window around the target movie/theatre when possible.
    This reduces accidental extraction of unrelated showtimes elsewhere.
    """
    lower = canonical(text)

    movie_positions = [
        lower.find(name)
        for name in TARGET_MOVIE_NAMES
        if lower.find(name) >= 0
    ]

    theatre_positions = [
        lower.find(name)
        for name in TARGET_THEATRE_NAMES
        if lower.find(name) >= 0
    ]

    positions = movie_positions + theatre_positions

    if not positions:
        return ""

    start = max(0, min(positions) - 2_000)
    end = min(len(text), max(positions) + 10_000)

    return text[start:end]


def inspect_bookmyshow() -> dict[str, Any]:
    session = make_session()

    attempts: list[dict[str, Any]] = []
    best_body = ""

    for source_name, url in (
        ("cinema", CINEMA_URL),
        ("movie", MOVIE_URL),
    ):
        try:
            raw_html, final_url, status = fetch_page(session, url)

            searchable, _json_strings, title = extract_search_text_and_json(raw_html)

            blocked = page_is_blocked(raw_html, title)

            attempt = {
                "source": source_name,
                "requested_url": url,
                "final_url": final_url,
                "status_code": status,
                "page_title": title,
                "blocked": blocked,
            }
            attempts.append(attempt)

            if len(raw_html) > len(best_body):
                best_body = raw_html

            if blocked:
                log.warning(
                    "BookMyShow %s page returned a challenge/block page.",
                    source_name,
                )
                continue

            lower = canonical(searchable)

            movie_found = any(name in lower for name in TARGET_MOVIE_NAMES)
            theatre_found = any(name in lower for name in TARGET_THEATRE_NAMES)
            date_found = any(v in lower for v in target_date_variants())

            # The cinema URL itself is explicitly for ALUC/20260815.
            if source_name == "cinema":
                theatre_found = theatre_found or "/aluc/" in final_url.lower()
                date_found = date_found or "20260815" in final_url

            context = find_target_context(searchable)

            # Only extract showtimes from a target-related context.
            show_times = extract_times(context) if context else []

            unavailable_found = any(
                phrase in canonical(context or searchable)
                for phrase in NO_SHOW_PHRASES
            )

            available = (
                movie_found
                and theatre_found
                and date_found
                and bool(show_times)
                and not unavailable_found
            )

            result = {
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
                "blocked": False,
                "booking_url": CINEMA_URL,
                "source_used": source_name,
                "page_title": title,
                "attempts": attempts,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

            # If we found actual availability, return immediately.
            if available:
                save_debug(result, raw_html)
                return result

            # Keep this non-available result in case the other source is blocked.
            last_nonblocked_result = result

        except Exception as exc:
            attempts.append(
                {
                    "source": source_name,
                    "requested_url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            log.warning("BookMyShow %s fetch failed: %s", source_name, exc)

    # Return the best non-blocked inspection result if one existed.
    if "last_nonblocked_result" in locals():
        last_nonblocked_result["attempts"] = attempts
        save_debug(last_nonblocked_result, best_body)
        return last_nonblocked_result

    # Both sources were blocked/failed.
    result = {
        "success": False,
        "available": False,
        "movie": "Vishwanath and Sons",
        "theatre": "ALLU Cinemas: Kokapet, Hyderabad",
        "target_date": TARGET_DATE,
        "movie_found": False,
        "theatre_found": False,
        "date_found": False,
        "show_times": [],
        "blocked": True,
        "booking_url": CINEMA_URL,
        "attempts": attempts,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    save_debug(result, best_body)
    return result


# ---------------------------------------------------------------------
# Alert logic
# ---------------------------------------------------------------------

def build_ticket_alert(result: dict[str, Any], new_times: list[str]) -> str:
    times = new_times or result["show_times"]

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
    try:
        result = inspect_bookmyshow()

        log.info("Movie found: %s", result.get("movie_found"))
        log.info("Theatre found: %s", result.get("theatre_found"))
        log.info("Target date found: %s", result.get("date_found"))
        log.info("Detected showtimes: %s", result.get("show_times"))
        log.info("Tickets available: %s", result.get("available"))
        log.info("Blocked: %s", result.get("blocked"))

        # User requested NO Telegram messages unless tickets are released.
        if not result.get("available"):
            if result.get("blocked"):
                log.warning(
                    "BookMyShow could not be inspected because the request "
                    "was blocked/challenged. No Telegram alert sent."
                )
            else:
                log.info("No qualifying tickets yet. No Telegram alert sent.")
            return 0

        state = load_state()

        previous_times = unique_sorted_times(
            state.get("detected_times", [])
        )

        current_times = unique_sorted_times(
            result.get("show_times", [])
        )

        new_times = [
            value
            for value in current_times
            if value not in previous_times
        ]

        # First release -> alert.
        # Later only newly-added showtimes -> alert again.
        if not state.get("alert_sent") or new_times:
            send_telegram(
                build_ticket_alert(
                    result,
                    new_times if state.get("alert_sent") else current_times,
                )
            )

            state.update(
                {
                    "alert_sent": True,
                    "detected_times": current_times,
                    "last_alert_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "booking_url": result["booking_url"],
                }
            )

            save_state(state)

            log.info("Telegram ticket-release alert sent.")
        else:
            log.info(
                "Tickets are already known and showtimes are unchanged. "
                "No Telegram alert sent."
            )

        return 0

    except Exception as exc:
        log.exception("Tracker failed: %s", exc)

        # User requested alerts ONLY for ticket release,
        # so errors intentionally do not produce Telegram notifications.
        result = {
            "success": False,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        save_debug(result)
        return 1


if __name__ == "__main__":
    sys.exit(main())
