"""
Fetch and cache FX rates (EUR, GBP, CHF → USD) from frankfurter.app.
Saves data/fx_rates.json used by fetch_evm_data.py and the Stellar script.
"""

import json
import os
import requests
import time
from datetime import datetime, timezone, timedelta

START_DATE   = '2023-01-01'   # covers all Spiko token histories
OUTPUT_FILE  = 'data/fx_rates.json'
CURRENCIES   = ['EUR', 'GBP', 'CHF']


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)


def fetch_rates_since(currency, since_date):
    url  = f"https://api.frankfurter.dev/v1/{since_date}.."
    resp = requests.get(url, params={'from': currency, 'to': 'USD'}, timeout=30)
    resp.raise_for_status()
    return {d: r['USD'] for d, r in resp.json().get('rates', {}).items()}


def main():
    os.makedirs('data', exist_ok=True)

    existing = load_json(OUTPUT_FILE, default={})
    today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Determine start date for incremental fetch (go back 7 days for safety)
    incremental_start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')

    for currency in CURRENCIES:
        prev = existing.get(currency, {})
        fetch_from = incremental_start if prev else START_DATE
        print(f'Fetching {currency}/USD from {fetch_from}...')

        try:
            new_rates = fetch_rates_since(currency, fetch_from)
            prev.update(new_rates)
            existing[currency] = prev
            print(f'  {len(new_rates)} new entries, {len(prev)} total')
        except Exception as e:
            print(f'  ERROR: {e}')

        time.sleep(0.2)

    save_json(OUTPUT_FILE, existing)
    print(f'Saved {OUTPUT_FILE}')


if __name__ == '__main__':
    main()
