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
from playwright.sync_api import Response, sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "prices.csv"
STATE_PATH = ROOT / "data" / "latest.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def money_values(node: Any, destination: str, target_date: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        dep = node.get("departureDateTime") or node.get("departureDate") or node.get("departure")
        arr = node.get("arrivalStation") or node.get("arrivalStationCode") or node.get("destination")
        flight = node.get("flightNumber") or node.get("flightCode") or ""
        candidates: list[tuple[float, str]] = []
        for key in ("price", "amount", "basePrice", "totalPrice"):
            value = node.get(key)
            if isinstance(value, (int, float)):
                candidates.append((float(value), str(node.get("currencyCode") or node.get("currency") or "EUR")))
            elif isinstance(value, dict) and isinstance(value.get("amount"), (int, float)):
                candidates.append((float(value["amount"]), str(value.get("currencyCode") or value.get("currency") or "EUR")))
        fares = node.get("fares")
        if isinstance(fares, list):
            for fare in fares:
                if isinstance(fare, dict):
                    price = fare.get("price")
                    if isinstance(price, dict) and isinstance(price.get("amount"), (int, float)):
                        candidates.append((float(price["amount"]), str(price.get("currencyCode") or "EUR")))
                    elif isinstance(price, (int, float)):
                        candidates.append((float(price), str(fare.get("currencyCode") or "EUR")))
        if candidates and isinstance(dep, str) and dep[:10] == target_date and (not arr or str(arr).upper() == destination):
            amount, currency = min(candidates, key=lambda item: item[0])
            found.append({"departure": dep, "flight_number": str(flight), "price": amount, "currency": currency})
        for value in node.values():
            found.extend(money_values(value, destination, target_date))
    elif isinstance(node, list):
        for value in node:
            found.extend(money_values(value, destination, target_date))
    return found


def scrape_route(page, origin: str, destination: str, date: str, language: str) -> list[dict[str, Any]]:
    captured: list[Any] = []

    def handle_response(response: Response) -> None:
        try:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type and response.request.resource_type in {"xhr", "fetch"}:
                captured.append(response.json())
        except Exception:
            pass

    page.on("response", handle_response)
    url = f"https://wizzair.com/{language}/booking/select-flight/{origin}/{destination}/{date}/null/1/0/0/null"
    print(f"Opening {origin}-{destination} {date}")
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.get_by_role("button", name=re.compile("accept|agree", re.I)).first.click(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(12000)

    flights: list[dict[str, Any]] = []
    for payload in captured:
        flights.extend(money_values(payload, destination, date))

    if not flights:
        text = page.locator("body").inner_text(timeout=10000)
        prices = re.findall(r"(?:EUR|€)\s*([0-9]+(?:[.,][0-9]{1,2})?)|([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:EUR|€)", text)
        amounts = [float((a or b).replace(",", ".")) for a, b in prices if (a or b)]
        if amounts:
            flights.append({"departure": date, "flight_number": "", "price": min(amounts), "currency": "EUR"})

    unique: dict[str, dict[str, Any]] = {}
    for flight in flights:
        key = f"{flight['departure']}|{flight['flight_number']}|{flight['price']}"
        unique[key] = flight
    page.remove_listener("response", handle_response)
    return list(unique.values())


def to_usd(amount: float, currency: str) -> float:
    if currency.upper() == "USD":
        return amount
    response = requests.get("https://api.frankfurter.app/latest", params={"amount": amount, "from": currency.upper(), "to": "USD"}, timeout=30)
    response.raise_for_status()
    return float(response.json()["rates"]["USD"])


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets missing; alert skipped")
        return
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=30)
    response.raise_for_status()


def append_rows(rows: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = DATA_PATH.exists()
    fields = ["checked_at", "origin", "destination", "departure", "flight_number", "currency", "price", "price_usd", "previous_usd", "change_usd"]
    with DATA_PATH.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    previous = load_json(STATE_PATH, {})
    new_state = dict(previous)
    rows: list[dict[str, Any]] = []
    alerts: list[str] = []
    checked_at = datetime.now(timezone.utc).isoformat()
    threshold = float(config["alert_threshold_usd"])
    observed = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(locale="en-GB", timezone_id="Asia/Yerevan", viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        for date in config["dates"]:
            for destination in config["destinations"]:
                try:
                    flights = scrape_route(page, config["origin"], destination, date, config["language_code"])
                except Exception as exc:
                    print(f"Failed {destination} {date}: {exc}")
                    continue
                for flight in flights:
                    observed += 1
                    usd = round(to_usd(float(flight["price"]), flight["currency"]), 2)
                    key = f"{config['origin']}|{destination}|{flight['departure']}|{flight['flight_number']}"
                    old = previous.get(key)
                    old_usd = float(old["price_usd"]) if isinstance(old, dict) and old.get("price_usd") is not None else None
                    change = round(usd - old_usd, 2) if old_usd is not None else None
                    rows.append({"checked_at": checked_at, "origin": config["origin"], "destination": destination, **flight, "price_usd": usd, "previous_usd": "" if old_usd is None else old_usd, "change_usd": "" if change is None else change})
                    new_state[key] = {"checked_at": checked_at, "origin": config["origin"], "destination": destination, **flight, "price_usd": usd}
                    if change is not None and abs(change) >= threshold:
                        direction = "dropped" if change < 0 else "increased"
                        alerts.append(f"✈️ Wizz fare {direction}\n{config['origin']} → {destination}\nDeparture: {flight['departure']}\nPrevious: ${old_usd:.2f}\nCurrent: ${usd:.2f}\nChange: ${change:+.2f}")
        browser.close()

    if observed == 0:
        send_telegram("⚠️ Wizz tracker could not read any fares from the booking pages. Check the GitHub Actions log.")
        raise RuntimeError("No Wizz fares were detected")

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
