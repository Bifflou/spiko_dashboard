"""
Fetch NAV (Net Asset Value) per token history from Chainlink NAVLink feeds.

Tokens are accumulating: yield accrues into the unit price (not rebased).
NAV starts at ~1.0 on launch day and grows over time.

Outputs:
  data/nav_history.json  – full daily NAV series used by chain scripts to compute
                           accurate marketcap = supply × NAV × FX_rate
  data/nav.json          – latest NAV per token (for dashboard table display)
  data/nav_state.json    – last fetched aggregatorRoundId per token

Chainlink NAVLink feeds (Ethereum mainnet, phaseId=1, 6 decimals, daily publication):
  EUTBL  → 0xfD628af590c4150A9651C1f4ddD0b4f532B703ae  EUR
  USTBL  → 0x477e363c51Ab0C4D13B22CD6B57D56d4a3Cb7Abe  USD

Other oracles deployed but no data published yet (will activate automatically):
  UKTBL    → 0x903d5990119bc799423e9c25c56518ba7dd19474  GBP
  SPKCC    → 0x99f70a0e1786402a6796c6b0aa997ef340a5c6da  USD
  eurSPKCC → 0x0e389c83bc1d16d86412476f6103027555c03265  EUR
  SAFO / eurSAFO / gbpSAFO / chfSAFO → oracle addresses TBD

Pre-Chainlink gap filling (linear interpolation):
  For tokens where the Chainlink feed started after fund launch, we interpolate
  NAV linearly from (launch_date, 1.0) to (first_chainlink_date, first_chainlink_nav).
  Money market fund NAV growth is quasi-linear (daily accrual ≈ constant yield),
  so this is an accurate approximation for the gap period.
"""

import json
import os
import requests
import time
from datetime import datetime, timedelta, timezone

ETH_RPC_CANDIDATES = [
    "https://ethereum.publicnode.com",
    "https://rpc.ankr.com/eth",
    "https://cloudflare-eth.com",
]

SEL_LATEST_ROUND = "0xfeaf968c"   # latestRoundData()
SEL_DECIMALS     = "0x313ce567"   # decimals()
SEL_GET_ROUND    = "0x9a6fc8f5"   # getRoundData(uint80)

# (oracle_address, currency, phase_id)
# Add entries here as feeds go live. phase_id=1 for all current Spiko NAVLink feeds.
NAV_FEEDS = {
    "eutbl": ("0xfD628af590c4150A9651C1f4ddD0b4f532B703ae", "EUR", 1),
    "ustbl": ("0x477e363c51Ab0C4D13B22CD6B57D56d4a3Cb7Abe", "USD", 1),
    # Uncomment + add oracle address when data is published:
    # "uktbl":    ("0x903d5990119bc799423e9c25c56518ba7dd19474", "GBP", 1),
    # "spkcc":    ("0x99f70a0e1786402a6796c6b0aa997ef340a5c6da", "USD", 1),
    # "eurspkcc": ("0x0e389c83bc1d16d86412476f6103027555c03265", "EUR", 1),
    # "safo":     ("0x...", "USD", 1),
    # "eursafo":  ("0x...", "EUR", 1),
    # "gbpsafo":  ("0x...", "GBP", 1),
    # "chfsafo":  ("0x...", "CHF", 1),
}

BATCH_SIZE = 50   # getRoundData calls per JSON-RPC batch request

# Fund launch dates for pre-Chainlink/pre-CoinGecko gap filling.
# NAV at launch is 1.0 by definition (subscription at par).
LAUNCH_DATES = {
    "eutbl":    "2024-04-30",
    "ustbl":    "2024-05-14",
    "uktbl":    "2025-10-15",
    "spkcc":    "2025-07-24",
    "eurspkcc": "2025-09-29",
    "safo":     "2026-03-19",
    "eursafo":  "2026-03-19",
    "gbpsafo":  "2026-03-19",
    "chfsafo":  "2026-03-19",
}

# CoinGecko IDs for tokens not covered by Chainlink NAVLink feeds.
# Preference: Chainlink > CoinGecko (Chainlink is more accurate/official).
# currency must be a valid CoinGecko vs_currency string.
CG_BASE_URL = "https://api.coingecko.com/api/v3"
CG_FEEDS = {
    "uktbl":    ("spiko-uk-t-bills-money-market-fund",                    "gbp"),
    "spkcc":    ("spiko-digital-assets-cash-carry-fund",                  "usd"),
    "eurspkcc": ("spiko-digital-assets-cash-carry-fund-euro-share-class", "eur"),
    "safo":     ("safo",                                                  "usd"),
    "eursafo":  ("spiko-amundi-overnight-swap-fund-eur",                  "eur"),
    "gbpsafo":  ("spiko-amundi-overnight-swap-fund-gbp",                  "gbp"),
    "chfsafo":  ("spiko-amundi-overnight-swap-fund-chf",                  "chf"),
}
CG_HEADERS = {"accept": "application/json", "User-Agent": "Mozilla/5.0"}
CG_DELAY   = 2.5   # seconds between CoinGecko calls (free tier: 30 req/min)


# ── NAV gap-filling ────────────────────────────────────────────────────────────

def build_interpolated_prefix(launch_date_str, first_chainlink_date_str, first_chainlink_nav):
    """
    Return a list of {date, nav} entries covering [launch_date, first_chainlink_date)
    using linear interpolation from 1.0 → first_chainlink_nav.

    The first_chainlink_date itself is NOT included (it comes from the on-chain series).
    If launch_date >= first_chainlink_date there is no gap → returns [].
    """
    t0 = datetime.strptime(launch_date_str,        "%Y-%m-%d").date()
    t1 = datetime.strptime(first_chainlink_date_str, "%Y-%m-%d").date()
    if t0 >= t1:
        return []

    total_days = (t1 - t0).days          # e.g. 217 for EUTBL
    delta_nav  = first_chainlink_nav - 1.0

    entries = []
    for i in range(total_days):           # i=0 → launch, i=total_days-1 → day before first on-chain
        d   = t0 + timedelta(days=i)
        nav = round(1.0 + delta_nav * i / total_days, 6)
        entries.append({"date": d.strftime("%Y-%m-%d"), "nav": nav})
    return entries


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── RPC helpers ────────────────────────────────────────────────────────────────

def pick_rpc():
    """Return the first responsive public Ethereum RPC."""
    probe_addr = "0xfD628af590c4150A9651C1f4ddD0b4f532B703ae"
    for rpc in ETH_RPC_CANDIDATES:
        try:
            r = requests.post(
                rpc,
                json={"jsonrpc": "2.0", "method": "eth_call",
                      "params": [{"to": probe_addr, "data": SEL_DECIMALS}, "latest"], "id": 1},
                headers={"Content-Type": "application/json"}, timeout=10,
            )
            res = r.json().get("result", "")
            if res and res != "0x":
                print(f"  RPC: {rpc}")
                return rpc
        except Exception as e:
            print(f"  {rpc}: {e}")
    raise RuntimeError("No responsive Ethereum RPC found")

def eth_call(rpc, to, data):
    r = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "method": "eth_call",
              "params": [{"to": to, "data": data}, "latest"], "id": 1},
        headers={"Content-Type": "application/json"}, timeout=15,
    )
    r.raise_for_status()
    return r.json().get("result", "")

def batch_get_rounds(rpc, oracle_addr, round_ids):
    """
    Batch-fetch getRoundData for a list of full round IDs.
    Returns {round_id: (answer_raw, updated_at_unix)}.
    """
    results = {}
    for i in range(0, len(round_ids), BATCH_SIZE):
        chunk = round_ids[i : i + BATCH_SIZE]
        batch_req = [
            {
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": oracle_addr,
                             "data": SEL_GET_ROUND + hex(rid)[2:].zfill(64)},
                            "latest"],
                "id": rid,
            }
            for rid in chunk
        ]
        resp = requests.post(
            rpc, json=batch_req,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        resp.raise_for_status()
        for item in resp.json():
            rid = item["id"]
            res = item.get("result", "")
            if res and len(res) > 2:
                raw    = bytes.fromhex(res[2:])
                answer = int.from_bytes(raw[32:64], "big", signed=True)
                upd    = int.from_bytes(raw[96:128], "big")
                if answer > 0 and upd > 0:
                    results[rid] = (answer, upd)
        time.sleep(0.2)
    return results


# ── Per-feed processing ────────────────────────────────────────────────────────

def process_feed(rpc, token_id, oracle_addr, currency, phase_id,
                 last_aggr_round, existing_series):
    """
    Fetch new rounds since last_aggr_round and merge with existing_series.
    Returns (merged_series, new_last_aggr_round, current_nav_entry | None).
    existing_series: list of {"date": str, "nav": float}
    Pre-Chainlink gap filling is handled by ensure_launch_prefix() in main().
    """
    print(f"\n[{token_id.upper()} — {currency}]")

    # decimals()
    dec_hex  = eth_call(rpc, oracle_addr, SEL_DECIMALS)
    decimals = int(dec_hex, 16) if dec_hex and dec_hex != "0x" else 6

    # latestRoundData()
    res = eth_call(rpc, oracle_addr, SEL_LATEST_ROUND)
    if not res or len(res) <= 2:
        print("  No data from feed")
        return existing_series, last_aggr_round, None

    raw             = bytes.fromhex(res[2:])
    latest_round_id = int.from_bytes(raw[0:32],   "big")
    latest_answer   = int.from_bytes(raw[32:64],  "big", signed=True)
    latest_updated  = int.from_bytes(raw[96:128], "big")

    if latest_answer <= 0 or latest_updated == 0:
        print("  Feed returned invalid data")
        return existing_series, last_aggr_round, None

    latest_aggr_id = latest_round_id & 0xFFFFFFFFFFFFFFFF
    base           = phase_id << 64
    current_nav    = round(latest_answer / (10 ** decimals), 6)
    current_date   = datetime.fromtimestamp(latest_updated, tz=timezone.utc).strftime("%Y-%m-%d")

    current_entry = {"nav": current_nav, "currency": currency, "updated": current_date}

    if last_aggr_round >= latest_aggr_id:
        print(f"  Up to date — round {latest_aggr_id}, NAV={current_nav} {currency}")
        return existing_series, last_aggr_round, current_entry

    # Rounds to fetch: from last_known+1 to latest
    start_aggr = max(1, last_aggr_round + 1)
    new_aggr_ids = list(range(start_aggr, latest_aggr_id + 1))
    full_ids     = [base + i for i in new_aggr_ids]
    print(f"  Fetching rounds {start_aggr}..{latest_aggr_id} ({len(new_aggr_ids)} rounds)")

    raw_rounds = batch_get_rounds(rpc, oracle_addr, full_ids)

    # Deduplicate to daily series: keep last round per date
    by_date = {}  # date → (aggr_id, nav)
    for aggr_id in new_aggr_ids:
        full_id = base + aggr_id
        if full_id in raw_rounds:
            answer, upd = raw_rounds[full_id]
            nav  = round(answer / (10 ** decimals), 6)
            date = datetime.fromtimestamp(upd, tz=timezone.utc).strftime("%Y-%m-%d")
            if date not in by_date or aggr_id > by_date[date][0]:
                by_date[date] = (aggr_id, nav)

    new_entries = [{"date": d, "nav": by_date[d][1]} for d in sorted(by_date.keys())]
    print(f"  {len(new_entries)} new daily entries (latest: {current_nav} {currency} on {current_date})")

    # Merge: drop existing entries on/after first new date, then append new
    if new_entries:
        cutoff = new_entries[0]["date"]
        kept   = [e for e in existing_series if e["date"] < cutoff]
        merged = kept + new_entries
    else:
        merged = list(existing_series)

    return merged, latest_aggr_id, current_entry


def ensure_launch_prefix(token_id, series, launch_date):
    """
    If `series` doesn't yet start at `launch_date`, prepend an interpolated
    prefix from (launch_date, 1.0) → (series[0]['date'], series[0]['nav']).
    Returns the (possibly extended) series. Idempotent: once the prefix exists
    it won't be re-added because series[0]['date'] == launch_date.
    """
    if not series or not launch_date:
        return series
    first_date = series[0]["date"]
    if first_date <= launch_date:
        return series          # already starts at / before launch
    prefix = build_interpolated_prefix(launch_date, first_date, series[0]["nav"])
    if prefix:
        print(f"  [{token_id.upper()}] Prepended {len(prefix)} interpolated entries "
              f"({launch_date} -> {first_date}, pre-Chainlink gap)")
    return prefix + series


# ── CoinGecko NAV fetching ─────────────────────────────────────────────────────

def cg_fetch_history(cg_id, currency, last_date=None):
    """
    Fetch daily NAV history from CoinGecko for a token.
    Uses market_chart with days=365 (free-tier max).
    Returns list of {"date": str, "nav": float}, deduplicated to one entry per day.

    last_date: if set, only returns entries AFTER this date (incremental).
    """
    url  = f"{CG_BASE_URL}/coins/{cg_id}/market_chart"
    resp = requests.get(
        url,
        params={"vs_currency": currency, "days": 365, "interval": "daily"},
        headers=CG_HEADERS, timeout=30,
    )
    if resp.status_code == 429:
        print(f"    CoinGecko rate limit hit — sleeping 60s")
        time.sleep(60)
        resp = requests.get(url, params={"vs_currency": currency, "days": 365, "interval": "daily"},
                            headers=CG_HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"    CoinGecko error {resp.status_code} for {cg_id}")
        return []

    raw_prices = resp.json().get("prices", [])
    if not raw_prices:
        return []

    # Deduplicate: one entry per UTC date, keep the last price of the day
    by_date = {}
    for ts_ms, price in raw_prices:
        date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_date[date] = round(price, 6)

    entries = [{"date": d, "nav": by_date[d]} for d in sorted(by_date.keys())]

    # Incremental: drop already-known dates
    if last_date:
        entries = [e for e in entries if e["date"] > last_date]

    return entries


def cg_fetch_current(cg_id, currency):
    """
    Return current NAV from CoinGecko /coins/{id} (more up-to-date than market_chart).
    Returns {"nav": float, "currency": str, "updated": date_str} or None.
    """
    resp = requests.get(f"{CG_BASE_URL}/coins/{cg_id}", headers=CG_HEADERS, timeout=20,
                        params={"localization": "false", "tickers": "false",
                                "community_data": "false", "developer_data": "false"})
    if resp.status_code != 200:
        return None
    data      = resp.json()
    price     = data.get("market_data", {}).get("current_price", {}).get(currency)
    updated   = data.get("last_updated", "")[:10]   # "2026-05-11T..."
    if price is None:
        return None
    return {"nav": round(price, 6), "currency": currency.upper(), "updated": updated}


def process_cg_feed(token_id, cg_id, currency, existing_series):
    """
    Fetch CoinGecko history, merge with existing_series, apply launch prefix.
    Returns (merged_series, current_nav_entry | None).
    """
    print(f"\n[{token_id.upper()} — CoinGecko / {currency.upper()}]")
    launch_date = LAUNCH_DATES.get(token_id)

    last_date = existing_series[-1]["date"] if existing_series else None
    new_entries = cg_fetch_history(cg_id, currency, last_date=last_date)
    print(f"  {len(new_entries)} new entries from CoinGecko "
          f"(after {last_date or 'beginning'})")

    if new_entries:
        merged = existing_series + new_entries
    else:
        merged = list(existing_series)

    if not merged:
        print("  No data available yet.")
        return [], None

    # Prepend interpolated prefix from launch if gap exists
    merged = ensure_launch_prefix(token_id, merged, launch_date)

    # Current price (separate call, more up-to-date)
    time.sleep(CG_DELAY)
    current = cg_fetch_current(cg_id, currency)
    if current:
        print(f"  Current: {current['nav']} {currency.upper()} (updated {current['updated']})")
        # Ensure latest point is in the series
        if current["updated"] and (not merged or merged[-1]["date"] < current["updated"]):
            merged.append({"date": current["updated"], "nav": current["nav"]})
    else:
        current = ({"nav": merged[-1]["nav"], "currency": currency.upper(),
                    "updated": merged[-1]["date"]} if merged else None)

    return merged, current


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    history_file = "data/nav_history.json"
    state_file   = "data/nav_state.json"
    nav_file     = "data/nav.json"

    existing_history = load_json(history_file, default={})  # {token_id: [{date, nav}]}
    state            = load_json(state_file,   default={})  # {token_id: {last_aggr_round}}
    existing_nav     = load_json(nav_file,     default={})  # {token_id: {nav, currency, updated}}

    print("=== Fetching Spiko NAV history (Chainlink) ===")
    try:
        rpc = pick_rpc()
    except RuntimeError as e:
        print(f"  {e} — keeping existing data")
        return

    new_history = dict(existing_history)
    new_nav     = dict(existing_nav)
    new_state   = dict(state)

    for token_id, (oracle_addr, currency, phase_id) in NAV_FEEDS.items():
        try:
            last_aggr   = state.get(token_id, {}).get("last_aggr_round", 0)
            series      = existing_history.get(token_id, [])
            launch_date = LAUNCH_DATES.get(token_id)

            merged, last_aggr_new, current_entry = process_feed(
                rpc, token_id, oracle_addr, currency, phase_id, last_aggr, series,
            )

            # Always ensure the series starts from the fund's launch date
            merged = ensure_launch_prefix(token_id, merged, launch_date)

            new_history[token_id] = merged
            new_state[token_id]   = {"last_aggr_round": last_aggr_new}
            if current_entry:
                new_nav[token_id] = current_entry
            elif token_id not in new_nav:
                new_nav[token_id] = None

        except Exception as e:
            import traceback
            print(f"  ERROR for {token_id}: {e}")
            traceback.print_exc()

    # ── CoinGecko section (tokens without Chainlink feed) ──────────────────────
    print("\n=== Fetching Spiko NAV history (CoinGecko) ===")
    for token_id, (cg_id, cg_currency) in CG_FEEDS.items():
        try:
            series = new_history.get(token_id, [])
            merged, current_entry = process_cg_feed(
                token_id, cg_id, cg_currency, series
            )
            if merged:
                new_history[token_id] = merged
            if current_entry:
                new_nav[token_id] = current_entry
            elif token_id not in new_nav:
                new_nav[token_id] = None
        except Exception as e:
            import traceback
            print(f"  ERROR for {token_id}: {e}")
            traceback.print_exc()
        time.sleep(CG_DELAY)

    save_json(history_file, new_history)
    save_json(state_file,   new_state)
    save_json(nav_file,     new_nav)

    for tid, series in new_history.items():
        print(f"  {tid.upper()}: {len(series)} daily NAV points saved")

    print("=== Done — NAV ===")


if __name__ == "__main__":
    main()
