import json
import os
import requests
from datetime import date

DATA_FILE = "data/eurusd_rates.json"
START_DATE = "2023-10-01"  # before earliest token data


def load_existing():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def fetch_rates(from_date):
    url = f"https://api.frankfurter.app/{from_date}.."
    resp = requests.get(url, params={"from": "EUR", "to": "USD"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {d: r["USD"] for d, r in data.get("rates", {}).items()}


rates = load_existing()

stored_dates = sorted(rates.keys())
last_stored = stored_dates[-1] if stored_dates else None
fetch_from = last_stored if last_stored else START_DATE

print(f"Fetching EUR/USD from {fetch_from}..")
new_rates = fetch_rates(fetch_from)
rates.update(new_rates)
print(f"  {len(new_rates)} new dates fetched, {len(rates)} total")

with open(DATA_FILE, "w") as f:
    json.dump(rates, f, sort_keys=True)
