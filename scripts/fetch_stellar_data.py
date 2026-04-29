import base64
import json
import os
import requests
import struct
import time
from collections import defaultdict
from datetime import datetime, timezone

ISSUER        = "GCEYGIVOLAVBF2TG2RUSGTUJCIN75KEX3NGLMY4VPL4GFE5L355AXW3G"
ADMIN         = "GCYYFR4SR4RDSWTN64LSE4BGF2UQEDYZ32QTD7TMQXO6TXSGEDWP652D"
CONTRACT      = "CANKBYNNAYKEZXLB655F2UPNTAZFK5HILZUXL7ZTFR3NF6LKDSVY7KFH"
ASSET_CODE    = "EURCV"
HORIZON       = "https://horizon.stellar.org"
EXPERT_BASE   = "https://api.stellar.expert"
EXPERT        = f"{EXPERT_BASE}/explorer/public/asset/{ASSET_CODE}-{ISSUER}"
STELLAR_SCALE = 10 ** 7   # Stellar uses 7 decimal places


def iso_to_date(iso):
    return iso[:10]


# ── XDR / ScVal decoding (no external deps) ────────────────────────────────────

# Soroban SCValType discriminants
SCV_I128   = 10
SCV_U128   = 9
SCV_SYMBOL = 15

def decode_scval(b64):
    """
    Decode a base64-encoded Soroban ScVal.
    Returns (type_str, python_value) or (None, None) on failure.
    """
    if not b64:
        return None, None
    try:
        raw = base64.b64decode(b64)
        discriminant = struct.unpack('>I', raw[:4])[0]

        if discriminant == SCV_SYMBOL:
            length = struct.unpack('>I', raw[4:8])[0]
            return 'symbol', raw[8:8 + length].decode('utf-8')

        elif discriminant == SCV_I128:
            hi = struct.unpack('>q', raw[4:12])[0]   # signed int64
            lo = struct.unpack('>Q', raw[12:20])[0]  # unsigned int64
            return 'i128', (hi << 64) | lo

        elif discriminant == SCV_U128:
            hi = struct.unpack('>Q', raw[4:12])[0]
            lo = struct.unpack('>Q', raw[12:20])[0]
            return 'u128', (hi << 64) | lo

    except Exception:
        pass
    return None, None


# ── Current state (stellar.expert) ─────────────────────────────────────────────

def get_asset_info():
    resp = requests.get(EXPERT, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_circulating_supply_and_holders():
    """stellar.expert /holders — sum of non-zero balances = supply, count = holders."""
    total_supply = 0.0
    holder_count = 0
    url = f"{EXPERT}/holders"

    while True:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])

        for r in records:
            bal = float(r.get("balance", 0)) / STELLAR_SCALE
            if bal > 0:
                total_supply += bal
                holder_count += 1

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break
        if next_href.startswith("/"):
            next_href = EXPERT_BASE + next_href
        url = next_href
        time.sleep(0.1)

    return round(total_supply, 7), holder_count


# ── Historical operations (Horizon) ────────────────────────────────────────────

def get_account_operations(account, label="account"):
    """Paginate all operations on an account, oldest first."""
    all_ops = []
    url = f"{HORIZON}/accounts/{account}/operations"
    params = {"order": "asc", "limit": 200}
    use_params = True
    page = 1

    while True:
        resp = requests.get(url, params=(params if use_params else None), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("_embedded", {}).get("records", [])
        all_ops.extend(records)
        print(f"  [{label}] Page {page}: {len(records)} ops (total: {len(all_ops)})")

        next_href = data.get("_links", {}).get("next", {}).get("href")
        if not next_href or not records:
            break
        url = next_href
        use_params = False
        page += 1
        time.sleep(0.15)

    return all_ops


def process_operations(ops):
    """
    Reconstruct daily supply delta from issuer operations:

    Classic payments
      FROM issuer → holder   : mint   (+)
      FROM holder → issuer   : burn   (-)

    Soroban invoke_host_function (Soroban SEP-41 token)
      function "mint"            : mint   (+)  params[-1] = i128 amount
      function "mint_to_account" : mint   (+)  params[-1] = i128 amount
      function "burn"            : burn   (-)
      function "clawback"        : burn   (-)

    Holder first-seen is tracked from classic payment recipients only.
    """
    delta_by_date = defaultdict(float)
    holder_first_seen = {}

    for op in ops:
        op_type = op.get("type", "")
        date = iso_to_date(op.get("created_at", "") or "")
        if not date:
            continue

        # ── Classic payment ──────────────────────────────────────────────────
        if op_type == "payment":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            amount = float(op.get("amount", 0))
            src = op.get("from", "")
            dst = op.get("to", "")

            if src == ISSUER:
                delta_by_date[date] += amount
                if dst and dst != ISSUER and dst not in holder_first_seen:
                    holder_first_seen[dst] = date
            elif dst == ISSUER:
                delta_by_date[date] -= amount

        # ── Soroban invoke_host_function ─────────────────────────────────────
        elif op_type == "invoke_host_function":
            if not op.get("function", "").endswith("InvokeContract"):
                continue

            params = op.get("parameters", [])
            # params layout: [contract_address, fn_name, arg0, arg1, ..., amount]
            if len(params) < 3:
                continue

            # Decode function name (index 1)
            _, fn_name = decode_scval(params[1].get("value", ""))
            if fn_name not in ("mint", "mint_to_account", "burn", "clawback"):
                continue

            # Amount is always the last parameter
            _, raw_amount = decode_scval(params[-1].get("value", ""))
            if raw_amount is None:
                print(f"  [warn] could not decode amount for {fn_name} on {date}")
                continue

            amount = raw_amount / STELLAR_SCALE
            print(f"  Soroban {fn_name}: {amount:,.2f} EURCV on {date}")

            if fn_name in ("mint", "mint_to_account"):
                delta_by_date[date] += amount
            else:
                delta_by_date[date] -= amount

        # ── Clawback (classic) ───────────────────────────────────────────────
        elif op_type == "clawback":
            if op.get("asset_code") != ASSET_CODE or op.get("asset_issuer") != ISSUER:
                continue
            delta_by_date[date] -= float(op.get("amount", 0))

    # ── Cumulative supply ────────────────────────────────────────────────────
    supply_history = []
    cumulative = 0.0
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        supply_history.append({"date": date, "supply": round(max(0.0, cumulative), 2)})

    # ── Holders (first-seen per address) ────────────────────────────────────
    events_by_date = defaultdict(int)
    for date in holder_first_seen.values():
        events_by_date[date] += 1

    holders_history = []
    count = 0
    for date in sorted(events_by_date.keys()):
        count += events_by_date[date]
        holders_history.append({"date": date, "holders": count})

    return supply_history, holders_history


# ── EUR/USD + market cap ──────────────────────────────────────────────────────

def fetch_eur_usd_rates(start_date):
    url = f"https://api.frankfurter.app/{start_date}.."
    resp = requests.get(url, params={"from": "EUR", "to": "USD"}, timeout=30)
    resp.raise_for_status()
    return {d: r["USD"] for d, r in resp.json().get("rates", {}).items()}


def compute_marketcap(supply_history, eur_usd_rates):
    result = []
    last_rate = None
    for item in supply_history:
        date = item["date"]
        if date in eur_usd_rates:
            last_rate = eur_usd_rates[date]
        if last_rate is None:
            continue
        result.append({
            "date": date,
            "marketcap": round(item["supply"] * last_rate, 2),
            "supply": round(item["supply"], 2),
        })
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    print("Récupération métadonnées asset via stellar.expert...")
    asset_info = get_asset_info()
    created_ts = int(asset_info.get("created", 0))
    created_date = (
        datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if created_ts else None
    )
    print(f"  Asset créé le : {created_date}")

    print("Récupération supply + holders via stellar.expert /holders...")
    current_supply, current_holders = get_circulating_supply_and_holders()
    print(f"  Circulating supply : {current_supply:,.2f} EURCV")
    print(f"  Holders actifs     : {current_holders}")

    print("Récupération des opérations issuer Stellar (Horizon)...")
    ops_issuer = get_account_operations(ISSUER, label="issuer")
    print(f"  Issuer: {len(ops_issuer)} opérations")

    print("Récupération des opérations admin Stellar (Horizon)...")
    ops_admin = get_account_operations(ADMIN, label="admin")
    print(f"  Admin: {len(ops_admin)} opérations")

    # Merge and deduplicate by operation id, sort by created_at
    seen_ids = set()
    ops = []
    for op in ops_issuer + ops_admin:
        oid = op.get("id")
        if oid not in seen_ids:
            seen_ids.add(oid)
            ops.append(op)
    ops.sort(key=lambda o: o.get("created_at", ""))
    print(f"Total: {len(ops)} opérations (dédupliquées)")

    supply_history, holders_history = process_operations(ops)
    print(f"  {len(supply_history)} jours avec activité supply")
    print(f"  {len(holders_history)} jours avec activité holders")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Override/append today with authoritative current_supply from stellar.expert
    if supply_history and supply_history[-1]["date"] == today:
        supply_history[-1]["supply"] = current_supply
    else:
        supply_history.append({"date": today, "supply": current_supply})

    # Fallback: if no ops found at all, flat line from creation date
    if len(supply_history) == 1 and created_date and created_date < today:
        print("  Avertissement: aucune opération trouvée — ligne plate utilisée.")
        supply_history = [
            {"date": created_date, "supply": current_supply},
            {"date": today,        "supply": current_supply},
        ]

    # Override/append today's holders
    if holders_history and holders_history[-1]["date"] == today:
        holders_history[-1]["holders"] = current_holders
    else:
        holders_history.append({"date": today, "holders": current_holders})

    if not supply_history:
        print("Aucune donnée supply trouvée.")
        return

    from datetime import timedelta
    start_date = supply_history[0]["date"]
    rates_start = (
        datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=7)
    ).strftime("%Y-%m-%d")
    print(f"Récupération taux EUR/USD depuis le {rates_start}...")
    eur_usd_rates = fetch_eur_usd_rates(rates_start)

    # Pre-seed latest rate for any supply date missing a rate
    if eur_usd_rates:
        seed_rate = eur_usd_rates[max(eur_usd_rates.keys())]
        for item in supply_history:
            if item["date"] not in eur_usd_rates:
                eur_usd_rates[item["date"]] = seed_rate

    marketcap_history = compute_marketcap(supply_history, eur_usd_rates)

    with open("data/stellar_marketcap.json", "w") as f:
        json.dump(marketcap_history, f)

    with open("data/stellar_holders.json", "w") as f:
        json.dump(holders_history, f)

    print(f"Sauvegardé : {len(marketcap_history)} points market cap, "
          f"{len(holders_history)} points holders")
    for pt in marketcap_history:
        print(f"  {pt['date']} : supply={pt['supply']:,.2f}  mcap=${pt['marketcap']:,.2f}")


if __name__ == "__main__":
    main()
