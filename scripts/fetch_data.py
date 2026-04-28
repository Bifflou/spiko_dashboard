import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
TOKEN_ADDRESS = "0x5F7827FDeb7c20b443265Fc2F40845B715385Ff2"
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def etherscan_get(params):
    params["chainid"] = CHAIN_ID
    params["apikey"] = ETHERSCAN_API_KEY
    resp = requests.get(ETHERSCAN_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def get_token_decimals():
    data = etherscan_get({
        "module": "proxy",
        "action": "eth_call",
        "to": TOKEN_ADDRESS,
        "data": "0x313ce567",
        "tag": "latest",
    })
    result = data.get("result", "0x12")
    return int(result, 16)


def fetch_all_transfer_logs():
    """Fetch all Transfer logs, handling Etherscan's 10-page cap via block-range splitting."""
    all_logs = []
    seen = set()
    from_block = 0

    while True:
        page = 1
        last_block_in_batch = None

        while True:
            data = etherscan_get({
                "module": "logs",
                "action": "getLogs",
                "address": TOKEN_ADDRESS,
                "topic0": TRANSFER_TOPIC,
                "fromBlock": from_block,
                "toBlock": "latest",
                "page": page,
                "offset": 1000,
            })

            if data["status"] != "1" or not data["result"]:
                return all_logs

            logs = data["result"]
            new = 0
            for log in logs:
                key = (log["transactionHash"], log["logIndex"])
                if key not in seen:
                    seen.add(key)
                    all_logs.append(log)
                    new += 1

            last_block_in_batch = int(logs[-1]["blockNumber"], 16)
            print(f"  Page {page} (from block {from_block}): {new} nouveaux events (total: {len(all_logs)})")

            if len(logs) < 1000:
                return all_logs

            page += 1
            time.sleep(0.25)

            if page > 10:
                from_block = last_block_in_batch
                break

    return all_logs


def process_supply_and_holders(logs, decimals):
    """Replay all Transfer events to compute daily supply (in tokens) and holder count."""
    balances = defaultdict(int)
    events_by_date = defaultdict(list)

    for log in logs:
        ts = int(log["timeStamp"], 16)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        events_by_date[date].append(log)

    supply_history = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        for log in events_by_date[date]:
            from_addr = "0x" + log["topics"][1][-40:]
            to_addr = "0x" + log["topics"][2][-40:]
            amount = int(log["data"], 16)

            if from_addr.lower() != ZERO_ADDRESS:
                balances[from_addr.lower()] -= amount
            if to_addr.lower() != ZERO_ADDRESS:
                balances[to_addr.lower()] += amount

        supply_tokens = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        holders = sum(1 for v in balances.values() if v > 0)

        supply_history.append({"date": date, "supply": supply_tokens})
        holders_history.append({"date": date, "holders": holders})

    return supply_history, holders_history


def fetch_eur_usd_rates(start_date):
    """Fetch all EUR/USD daily rates from frankfurter.app (ECB data, free, no key)."""
    url = f"https://api.frankfurter.app/{start_date}.."
    resp = requests.get(url, params={"from": "EUR", "to": "USD"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    rates = {}
    for date, rate_data in data.get("rates", {}).items():
        rates[date] = rate_data["USD"]
    return rates


def compute_marketcap(supply_history, eur_usd_rates):
    """Market cap = supply × EUR/USD rate (forward-fill for weekends/holidays)."""
    marketcap_history = []
    last_rate = None

    for item in supply_history:
        date = item["date"]
        if date in eur_usd_rates:
            last_rate = eur_usd_rates[date]

        if last_rate is None:
            continue

        mcap = round(item["supply"] * last_rate, 2)
        marketcap_history.append({"date": date, "marketcap": mcap})

    return marketcap_history


def main():
    os.makedirs("data", exist_ok=True)

    print("Récupération des decimals du token...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    print("Récupération des Transfer events depuis Etherscan...")
    logs = fetch_all_transfer_logs()
    print(f"Total: {len(logs)} événements Transfer")

    print("Calcul de la supply et du nombre de holders par jour...")
    supply_history, holders_history = process_supply_and_holders(logs, decimals)
    print(f"  {len(supply_history)} jours avec activité")

    start_date = supply_history[0]["date"]
    print(f"Récupération des taux EUR/USD depuis le {start_date} (frankfurter.app)...")
    eur_usd_rates = fetch_eur_usd_rates(start_date)
    print(f"  {len(eur_usd_rates)} taux récupérés")

    print("Calcul de la market cap...")
    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    with open("data/holders.json", "w") as f:
        json.dump(holders_history, f)

    with open("data/marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, {len(holders_history)} points holders")


if __name__ == "__main__":
    main()
