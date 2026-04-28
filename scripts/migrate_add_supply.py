"""
One-shot migration: adds 'supply' field to existing marketcap JSON files.
supply = marketcap / eur_usd_rate  (since marketcap = supply × rate)
Fetches historical EUR/USD rates from Frankfurter.app.
"""
import json
import requests

FILES = [
    "data/marketcap.json",
    "data/sol_marketcap.json",
]


def fetch_rates(start_date, end_date):
    url = f"https://api.frankfurter.app/{start_date}..{end_date}"
    resp = requests.get(url, params={"from": "EUR", "to": "USD"}, timeout=30)
    resp.raise_for_status()
    return {d: r["USD"] for d, r in resp.json().get("rates", {}).items()}


for path in FILES:
    with open(path) as f:
        data = json.load(f)

    if not data:
        continue

    start = data[0]["date"]
    end = data[-1]["date"]
    print(f"{path}: fetching rates {start}..{end}")
    rates = fetch_rates(start, end)

    last_rate = None
    updated = 0
    for item in data:
        rate = rates.get(item["date"])
        if rate:
            last_rate = rate
        if last_rate and "supply" not in item:
            item["supply"] = round(item["marketcap"] / last_rate, 2)
            updated += 1

    with open(path, "w") as f:
        json.dump(data, f)

    print(f"  {updated} points updated")
