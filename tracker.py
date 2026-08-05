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

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "prices.csv"
STATE_PATH = ROOT / "data" / "latest.json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def discover_api_version() -> str:
    override = os.getenv("WIZZ_API_VERSION")
    if override:
        return override
    html = requests.get("https://wizzair.com/en-gb", headers=HEADERS, timeout=30).text
    patterns = [r"be\.wizzair\.com/(\d+\.\d+\.\d+)/Api", r'apiVersion["\']?\s*[:=]\s*["\'](\d+\.\d+\.\d+)']
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return "24.8.0"


def api_get(version: str, path: str) -> Any:
    url = f"https://be.wizzair.com/{version}/Api/{path.lstrip('/')}"
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.json()


def api_post(version: str, path: str, payload: dict[str, Any]) -> Any:
    url = f"https://be.wizzair.com/{version}/Api/{path.lstrip('/')}"
    response = requests.post(url, headers={**HEADERS, "Content-Type": "application/json"}, json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def find_destinations(node: Any, origin: str) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        dep = node.get("departureStation") or node.get("departureStationCode") or node.get("from")
        arr = node.get("arrivalStation") or node.get("arrivalStationCode") or node.get("to")
        if isinstance(dep, str) and isinstance(arr, str) and dep.upper() == origin:
            found.add(arr.upper())
        if node.get("iata") == origin and isinstance(node.get("connections"), list):
            for item in node["connections"]:
                if isinstance(item, dict):
                    code = item.get("iata") or item.get("code") or item.get("arrivalStation")
                    if isinstance(code, str):
                        found.add(code.upper())
        for value in node.values():
            found |= find_destinations(value, origin)
    elif isinstance(node, list):
        for item in node:
            found |= find_destinations(item, origin)
    return found


def discover_destinations(version: str, language_code: str, origin: str) -> list[str]:
    data = api_get(version, f"asset/map?languageCode={language_code}")
    destinations = sorted(find_destinations(data, origin))
    if not destinations:
        raise RuntimeError(f"Could not discover any Wizz destinations from {origin}.")
    return destinations


def collect_flights(node: Any, origin: str, destination: str, target_date: str) -> list[dict[str, Any]]:
    flights: list[dict[str, Any]] = []
    if isinstance(node, dict):
        dep_time = node.get("departureDateTime") or node.get("departureDate")
        dep_station = node.get("departureStation") or node.get("departureStationCode")
        arr_station = node.get("arrivalStation") or node.get("arrivalStationCode")
        fares = node.get("fares")
        if dep_station == origin and arr_station == destination and isinstance(dep_time, str) and dep_time[:10] == target_date:
            prices: list[tuple[float, str]] = []
            if isinstance(fares, list):
                for fare in fares:
                    if not isinstance(fare, dict):
                        continue
                    price = fare.get("price")
                    if isinstance(price, dict) and isinstance(price.get("amount"), (int, float)):
                        prices.append((float(price["amount"]), str(price.get("currencyCode") or "EUR")))
                    elif isinstance(fare.get("price"), (int, float)):
                        prices.append((float(fare["price"]), str(fare.get("currencyCode") or "EUR")))
            price_obj = node.get("price")
            if isinstance(price_obj, dict) and isinstance(price_obj.get("amount"), (int, float)):
                prices.append((float(price_obj["amount"]), str(price_obj.get("currencyCode") or "EUR")))
            if prices:
                amount, currency = min(prices, key=lambda x: x[0])
                flights.append({
                    "origin": origin,
                    "destination": destination,
                    "departure": dep_time,
                    "flight_number": node.get("flightNumber") or node.get("carrierCode") or "",
                    "price": amount,
                    "currency": currency,
                })
        for value in node.values():
            flights.extend(collect_flights(value, origin, destination, target_date))
    elif isinstance(node, list):
        for item in node:
            flights.extend(collect_flights(item, origin, destination, target_date))
    return flights


def search_route(version: str, origin: str, destination: str, date: str, currency: str) -> list[dict[str, Any]]:
    payload = {
        "flightList": [{"departureStation": origin, "arrivalStation": destination, "departureDate": date}],
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
        "wdc": False,
        "currencyCode": currency,
    }
    data = api_post(version, "search/search", payload)
    return collect_flights(data, origin, destination, date)


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


def load_latest() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_latest(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def append_rows(rows: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = DATA_PATH.exists()
    columns = ["checked_at", "origin", "destination", "departure", "flight_number", "currency", "price", "price_usd", "previous_usd", "change_usd"]
    with DATA_PATH.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets are missing; alert skipped.")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()


def main() -> int:
    config = load_config()
    origin = config["origin"].upper()
    dates = config["dates"]
    threshold = float(config["alert_threshold_usd"])
    checked_at = datetime.now(timezone.utc).isoformat()
    version = discover_api_version()
    print(f"Using Wizz API version {version}")
    destinations = discover_destinations(version, config["language_code"], origin)
    print(f"Discovered {len(destinations)} destinations from {origin}: {', '.join(destinations)}")

    previous = load_latest()
    new_state = dict(previous)
    rows: list[dict[str, Any]] = []
    alerts: list[str] = []
    observed = 0

    for date in dates:
        for destination in destinations:
            try:
                flights = search_route(version, origin, destination, date, config["currency"])
            except requests.RequestException as exc:
                print(f"Search failed for {origin}-{destination} on {date}: {exc}")
                continue
            for flight in flights:
                observed += 1
                usd = round(to_usd(flight["price"], flight["currency"]), 2)
                key = f"{flight['origin']}|{flight['destination']}|{flight['departure']}|{flight['flight_number']}"
                old = previous.get(key)
                old_usd = float(old["price_usd"]) if isinstance(old, dict) and old.get("price_usd") is not None else None
                change = round(usd - old_usd, 2) if old_usd is not None else None
                rows.append({
                    "checked_at": checked_at,
                    **flight,
                    "price_usd": usd,
                    "previous_usd": "" if old_usd is None else old_usd,
                    "change_usd": "" if change is None else change,
                })
                new_state[key] = {"price_usd": usd, "checked_at": checked_at, **flight}
                if change is not None and abs(change) >= threshold:
                    arrow = "⬇️" if change < 0 else "⬆️"
                    alerts.append(
                        f"{arrow} Wizz Air fare changed\n"
                        f"{origin} → {destination}\n"
                        f"Departure: {flight['departure']}\n"
                        f"Previous: ${old_usd:.2f}\nCurrent: ${usd:.2f}\nChange: ${change:+.2f}"
                    )

    if observed == 0:
        send_telegram("⚠️ Wizz fare tracker ran but found no matching flights. Check the GitHub Actions log.")
        raise RuntimeError("No matching Wizz flights were returned.")

    append_rows(rows)
    save_latest(new_state)
    for alert in alerts:
        send_telegram(alert)
    print(f"Recorded {observed} fares; sent {len(alerts)} alerts.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
