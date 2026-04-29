import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
TOKEN_ADDRESS = "0x5422374B27757da72d5265cC745ea906E0446634"
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

        supply_history.append({"date": date, "supply": round(supply_tokens, 2)})
        holders_history.append({"date": date, "holders": holders})

    return supply_history, holders_history


def main():
    os.makedirs("data", exist_ok=True)

    print("USDCV ETH — Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    print("Récupération des Transfer events depuis Etherscan...")
    logs = fetch_all_transfer_logs()
    print(f"Total: {len(logs)} événements Transfer")

    print("Calcul de la supply et du nombre de holders par jour...")
    supply_history, holders_history = process_supply_and_holders(logs, decimals)
    print(f"  {len(supply_history)} jours avec activité")

    with open("data/usdcv_eth_marketcap.json", "w") as f:
        json.dump(supply_history, f)

    with open("data/usdcv_eth_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(supply_history)} points supply, {len(holders_history)} points holders")
    if supply_history:
        print(f"  Dernier point : {supply_history[-1]}")


if __name__ == "__main__":
    main()
