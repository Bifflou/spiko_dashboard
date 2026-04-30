import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
MINT_ADDRESS = "8smindLdDuySY6i2bStQX9o8DVhALCXCMbNxD98unx35"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SPL_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}

MAX_RETRIES = 8
BATCH_SIZE  = 3    # transactions par batch JSON-RPC (Helius limite la taille du payload)


def rpc(method, params):
    """Appel JSON-RPC unique avec retry + backoff exponentiel sur 429."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    wait = 2.0
    for attempt in range(MAX_RETRIES):
        resp = requests.post(HELIUS_RPC, json=payload, timeout=30)
        if resp.status_code == 429:
            print(f"  [429] attente {wait:.0f}s (tentative {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")
    raise RuntimeError(f"Échec après {MAX_RETRIES} tentatives pour {method}")


def rpc_batch(requests_list):
    """Envoie plusieurs requêtes JSON-RPC en un seul appel HTTP (batch)."""
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": r["method"], "params": r["params"]}
        for i, r in enumerate(requests_list)
    ]
    wait = 2.0
    for attempt in range(MAX_RETRIES):
        resp = requests.post(HELIUS_RPC, json=payload, timeout=60)
        if resp.status_code == 429:
            print(f"  [429 batch] attente {wait:.0f}s (tentative {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        resp.raise_for_status()
        results = resp.json()
        results.sort(key=lambda r: r.get("id", 0))
        return [r.get("result") for r in results]
    raise RuntimeError(f"Échec batch après {MAX_RETRIES} tentatives")


def get_token_decimals():
    result = rpc("getAccountInfo", [MINT_ADDRESS, {"encoding": "jsonParsed"}])
    return result["value"]["data"]["parsed"]["info"]["decimals"]


def get_mint_signatures():
    all_sigs = []
    before = None

    while True:
        params = {"limit": 1000}
        if before:
            params["before"] = before

        result = rpc("getSignaturesForAddress", [MINT_ADDRESS, params])
        if not result:
            break

        all_sigs.extend(result)
        print(f"  {len(result)} signatures (total: {len(all_sigs)})")

        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.15)

    return list(reversed(all_sigs))


def extract_mint_burn(parsed_tx):
    events = []
    if not parsed_tx:
        return events

    def scan_instructions(instructions):
        for ix in instructions:
            if ix.get("programId") not in SPL_PROGRAMS:
                continue
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            ix_type = parsed.get("type", "")
            info = parsed.get("info", {})
            if info.get("mint") != MINT_ADDRESS:
                continue
            if ix_type in ("mintTo", "mintToChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "mint", "amount": int(amt)})
            elif ix_type in ("burn", "burnChecked"):
                amt = info.get("amount") or info.get("tokenAmount", {}).get("amount", 0)
                events.append({"type": "burn", "amount": int(amt)})

    tx = parsed_tx.get("transaction", {})
    msg = tx.get("message", {})
    scan_instructions(msg.get("instructions", []))

    for inner in parsed_tx.get("meta", {}).get("innerInstructions", []):
        scan_instructions(inner.get("instructions", []))

    return events


def reconstruct_supply(signatures, decimals):
    delta_by_date = defaultdict(int)
    found = 0
    total = len(signatures)

    for batch_start in range(0, total, BATCH_SIZE):
        batch = signatures[batch_start:batch_start + BATCH_SIZE]

        reqs = [
            {"method": "getTransaction", "params": [s["signature"], {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            }]}
            for s in batch
        ]

        results = rpc_batch(reqs)

        for sig_info, parsed in zip(batch, results):
            ts = sig_info.get("blockTime", 0)
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
            for ev in extract_mint_burn(parsed):
                if date:
                    delta_by_date[date] += ev["amount"] if ev["type"] == "mint" else -ev["amount"]
                    found += 1

        done = min(batch_start + BATCH_SIZE, total)
        print(f"  {done}/{total} tx analysées, {found} mint/burn trouvés")
        time.sleep(0.6)  # pause entre batches

    supply_history = []
    cumulative = 0
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(cumulative / (10 ** decimals), 2)})

    return supply_history


def get_token_accounts():
    all_accounts = []
    cursor = None

    while True:
        params = {"mint": MINT_ADDRESS, "limit": 1000}
        if cursor:
            params["cursor"] = cursor

        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts", "params": params}
        resp = requests.post(HELIUS_RPC, json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json().get("result", {})

        accounts = result.get("token_accounts", [])
        all_accounts.extend(accounts)
        print(f"  {len(all_accounts)} comptes token récupérés")

        cursor = result.get("cursor")
        if not cursor or len(accounts) < 1000:
            break

        time.sleep(0.2)

    return all_accounts


def get_first_seen_date(address):
    before = None
    last_sig = None

    while True:
        params = {"limit": 1000}
        if before:
            params["before"] = before

        result = rpc("getSignaturesForAddress", [address, params])
        if not result:
            break

        last_sig = result[-1]

        if len(result) < 1000:
            break

        before = result[-1]["signature"]
        time.sleep(0.1)

    if last_sig and last_sig.get("blockTime"):
        return datetime.fromtimestamp(last_sig["blockTime"], tz=timezone.utc).strftime("%Y-%m-%d")
    return None


def reconstruct_holders(token_accounts):
    active = [a for a in token_accounts if float(a.get("amount", 0)) > 0]
    print(f"  {len(active)} comptes actifs à dater")

    events_by_date = defaultdict(int)

    for i, acc in enumerate(active):
        address = acc.get("address", "")
        if not address:
            continue

        date = get_first_seen_date(address)
        if date:
            events_by_date[date] += 1

        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(active)} comptes datés")

        time.sleep(0.3)

    holders_history = []
    cumulative = 0

    for date in sorted(events_by_date.keys()):
        cumulative += events_by_date[date]
        holders_history.append({"date": date, "holders": cumulative})

    return holders_history


def main():
    os.makedirs("data", exist_ok=True)

    print("USDCV SOL — Récupération des decimals...")
    decimals = get_token_decimals()
    print(f"  Decimals: {decimals}")

    print("Récupération des signatures du compte mint...")
    signatures = get_mint_signatures()
    print(f"  {len(signatures)} signatures trouvées")

    print("Analyse des transactions (mintTo / burn)...")
    supply_history = reconstruct_supply(signatures, decimals)
    print(f"  {len(supply_history)} jours avec activité supply")

    if not supply_history:
        print("Aucun mintTo/burn trouvé.")
        return

    print("Récupération des comptes token (holders)...")
    token_accounts = get_token_accounts()

    print("Reconstruction de l'historique des holders...")
    holders_history = reconstruct_holders(token_accounts)

    with open("data/usdcv_sol_marketcap.json", "w") as f:
        json.dump(supply_history, f)

    with open("data/usdcv_sol_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(supply_history)} points supply, {len(holders_history)} points holders")
    if supply_history:
        print(f"  Dernier point : {supply_history[-1]}")


if __name__ == "__main__":
    main()
