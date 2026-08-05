from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "prices.csv"
STATE_PATH = ROOT / "data" / "latest.json"
PROFILE_PATH = ROOT / ".browser-profile"
DEBUG_PATH = ROOT / "debug"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets missing; alert skipped")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()


def to_usd(amount: float, currency: str) -> float:
    if currency.upper() == "USD":
        return amount
    response = requests.get(
        "https://api.frankfurter.app/latest",
        params={"amount": amount, "from": currency.upper(), "to": "USD"},
        timeout=30,
    )
    response.raise_for_status()
    return float(response.json()["rates"]["USD"])


def append_rows(rows: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = DATA_PATH.exists()
    fields = [
        "checked_at", "origin", "destination", "departure", "flight_number",
        "currency", "price", "price_usd", "previous_usd", "change_usd",
    ]
    with DATA_PATH.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def save_debug(page: Page, label: str) -> None:
    DEBUG_PATH.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
    try:
        page.screenshot(path=str(DEBUG_PATH / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_PATH / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def accept_cookies(page: Page) -> None:
    for pattern in (r"accept all", r"accept", r"agree", r"allow all"):
        try:
            page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=2500)
            return
        except Exception:
            continue


def fill_first(page: Page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=4000)
            locator.click()
            locator.fill(value)
            return True
        except Exception:
            continue
    return False


def choose_station(page: Page, value: str) -> None:
    options = [
        page.get_by_text(re.compile(rf"\b{re.escape(value)}\b", re.I)),
        page.get_by_role("option", name=re.compile(rf"\b{re.escape(value)}\b", re.I)),
    ]
    for option in options:
        try:
            option.first.click(timeout=5000)
            return
        except Exception:
            continue
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")


def search_from_home(page: Page, origin: str, destination: str, date: str, language: str) -> None:
    page.goto(f"https://wizzair.com/{language}", wait_until="domcontentloaded", timeout=90000)
    accept_cookies(page)

    origin_ok = fill_first(page, [
        'input[placeholder*="Departure" i]',
        'input[aria-label*="Departure" i]',
        'input[name*="departure" i]',
        'input[id*="departure" i]',
    ], origin)
    if not origin_ok:
        raise RuntimeError("Departure input was not found")
    choose_station(page, origin)

    destination_ok = fill_first(page, [
        'input[placeholder*="Arrival" i]',
        'input[placeholder*="Destination" i]',
        'input[aria-label*="Arrival" i]',
        'input[aria-label*="Destination" i]',
        'input[name*="arrival" i]',
        'input[id*="arrival" i]',
    ], destination)
    if not destination_ok:
        raise RuntimeError("Destination input was not found")
    choose_station(page, destination)

    date_ok = fill_first(page, [
        'input[placeholder*="Departure date" i]',
        'input[aria-label*="Departure date" i]',
        'input[name*="date" i]',
    ], date)
    if date_ok:
        page.keyboard.press("Enter")
    else:
        # Wizz often uses a calendar button instead of a writable date field.
        try:
            page.get_by_text(re.compile("departure date", re.I)).first.click(timeout=5000)
            target = datetime.strptime(date, "%Y-%m-%d")
            labels = [
                target.strftime("%A, %B %-d, %Y"),
                target.strftime("%B %-d, %Y"),
                str(target.day),
            ]
            clicked = False
            for label in labels:
                try:
                    page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first.click(timeout=4000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                raise RuntimeError("Target date was not found in calendar")
        except Exception as exc:
            raise RuntimeError(f"Could not choose departure date: {exc}") from exc

    for button_name in (r"search", r"find flights", r"show flights"):
        try:
            page.get_by_role("button", name=re.compile(button_name, re.I)).first.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=90000)
            page.wait_for_timeout(10000)
            return
        except Exception:
            continue
    raise RuntimeError("Search button was not found")


def scrape_visible_fares(page: Page, date: str) -> list[dict[str, Any]]:
    body = page.locator("body").inner_text(timeout=15000)
    currency_patterns = {
        "EUR": [r"€\s*([0-9]+(?:[.,][0-9]{1,2})?)", r"([0-9]+(?:[.,][0-9]{1,2})?)\s*EUR"],
        "USD": [r"\$\s*([0-9]+(?:[.,][0-9]{1,2})?)", r"([0-9]+(?:[.,][0-9]{1,2})?)\s*USD"],
        "AMD": [r"([0-9][0-9\s,.]+)\s*AMD", r"AMD\s*([0-9][0-9\s,.]+)"],
    }
    candidates: list[tuple[float, str]] = []
    for currency, patterns in currency_patterns.items():
        for pattern in patterns:
            for raw in re.findall(pattern, body, flags=re.I):
                cleaned = raw.replace(" ", "").replace(",", ".")
                try:
                    value = float(cleaned)
                except ValueError:
                    continue
                if value > 0:
                    candidates.append((value, currency))

    if not candidates:
        return []

    # The lowest visible ticket-like amount is used as the monitored basic fare.
    amount, currency = min(candidates, key=lambda item: item[0])
    flight_number_match = re.search(r"\bW6\s?\d{3,4}\b", body, flags=re.I)
    return [{
        "departure": date,
        "flight_number": flight_number_match.group(0).replace(" ", "") if flight_number_match else "",
        "price": amount,
        "currency": currency,
    }]


def scrape_route(page: Page, origin: str, destination: str, date: str, language: str) -> list[dict[str, Any]]:
    print(f"Searching {origin}-{destination} {date}")
    search_from_home(page, origin, destination, date, language)
    fares = scrape_visible_fares(page, date)
    if not fares:
        save_debug(page, f"no_fares_{origin}_{destination}_{date}")
    return fares


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    previous = load_json(STATE_PATH, {})
    new_state = dict(previous)
    rows: list[dict[str, Any]] = []
    alerts: list[str] = []
    checked_at = datetime.now(timezone.utc).isoformat()
    threshold = float(config["alert_threshold_usd"])
    headless = env_bool("HEADLESS", True)
    observed = 0

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_PATH),
            headless=headless,
            locale="en-GB",
            timezone_id="Asia/Yerevan",
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        for date in config["dates"]:
            for destination in config["destinations"]:
                try:
                    flights = scrape_route(page, config["origin"], destination, date, config["language_code"])
                except (PlaywrightTimeoutError, RuntimeError, Exception) as exc:
                    print(f"Failed {destination} {date}: {exc}")
                    save_debug(page, f"failed_{config['origin']}_{destination}_{date}")
                    continue

                for flight in flights:
                    observed += 1
                    usd = round(to_usd(float(flight["price"]), flight["currency"]), 2)
                    key = f"{config['origin']}|{destination}|{flight['departure']}|{flight['flight_number']}"
                    old = previous.get(key)
                    old_usd = float(old["price_usd"]) if isinstance(old, dict) and old.get("price_usd") is not None else None
                    change = round(usd - old_usd, 2) if old_usd is not None else None
                    rows.append({
                        "checked_at": checked_at,
                        "origin": config["origin"],
                        "destination": destination,
                        **flight,
                        "price_usd": usd,
                        "previous_usd": "" if old_usd is None else old_usd,
                        "change_usd": "" if change is None else change,
                    })
                    new_state[key] = {
                        "checked_at": checked_at,
                        "origin": config["origin"],
                        "destination": destination,
                        **flight,
                        "price_usd": usd,
                    }
                    if change is not None and abs(change) >= threshold:
                        direction = "dropped" if change < 0 else "increased"
                        alerts.append(
                            f"✈️ Wizz fare {direction}\n"
                            f"{config['origin']} → {destination}\n"
                            f"Departure: {flight['departure']}\n"
                            f"Previous: ${old_usd:.2f}\nCurrent: ${usd:.2f}\nChange: ${change:+.2f}"
                        )
        context.close()

    if observed == 0:
        raise RuntimeError(f"No Wizz fares were detected. Check files in {DEBUG_PATH}.")

    append_rows(rows)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(new_state, indent=2, sort_keys=True), encoding="utf-8")
    for alert in alerts:
        send_telegram(alert)
    print(f"Recorded {observed} fares; sent {len(alerts)} alerts")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
