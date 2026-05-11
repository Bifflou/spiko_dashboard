"""
Fetch NAV (Net Asset Value) per token for Spiko funds from Chainlink NAVLink feeds.

Source: Chainlink AggregatorV3Interface (latestRoundData) on Ethereum mainnet.
The NAV is published daily by CACEIS (fund administrator) via Chainlink.

Tokens are accumulating (not rebasing): each token appreciates in value over time
as T-bill / swap yield accrues into the unit price. NAV starts at 1.00 and grows.

Current feeds (Ethereum mainnet):
  EUTBL  → 0xfD628af590c4150A9651C1f4ddD0b4f532B703ae  (EUR, 6 decimals)
  USTBL  → 0x477e363c51Ab0C4D13B22CD6B57D56d4a3Cb7Abe  (USD, 6 decimals)

Oracles deployed but no data yet (will auto-activate when Chainlink starts publishing):
  UKTBL    → 0x903d5990119bc799423e9c25c56518ba7dd19474  (GBP)
  SPKCC    → 0x99f70a0e1786402a6796c6b0aa997ef340a5c6da  (USD)
  eurSPKCC → 0x0e389c83bc1d16d86412476f6103027555c03265  (EUR)

No oracle found yet:
  SAFO, eurSAFO, gbpSAFO, chfSAFO

Output: data/nav.json
  { "eutbl": {"nav": 1.051364, "currency": "EUR", "updated": "2026-05-10"}, ... }
"""

import json
import os
import requests
import time
from datetime import datetime, timezone

ETH_RPC_CANDIDATES = [
    "https://ethereum.publicnode.com",
    "https://rpc.ankr.com/eth",
    "https://cloudflare-eth.com",
]

# Chainlink AggregatorV3Interface selectors
SEL_LATEST_ROUND  = "0xfeaf968c"  # latestRoundData()
SEL_DECIMALS      = "0x313ce567"  # decimals()

# Feed definitions: token_id → (oracle_address, currency)
# Add entries here as new feeds go live.
NAV_FEEDS = {
    "eutbl":    ("0xfD628af590c4150A9651C1f4ddD0b4f532B703ae", "EUR"),
    "ustbl":    ("0x477e363c51Ab0C4D13B22CD6B57D56d4a3Cb7Abe", "USD"),
    # Activate when data is published:
    "uktbl":    ("0x903d5990119bc799423e9c25c56518ba7dd19474", "GBP"),
    "spkcc":    ("0x99f70a0e1786402a6796c6b0aa997ef340a5c6da", "USD"),
    "eurspkcc": ("0x0e389c83bc1d16d86412476f6103027555c03265", "EUR"),
    # SAFO feeds: add addresses here when Spiko deploys them
    # "safo":     ("0x...", "USD"),
    # "eursafo":  ("0x...", "EUR"),
    # "gbpsafo":  ("0x...", "GBP"),
    # "chfsafo":  ("0x...", "CHF"),
}


# ── RPC helpers ────────────────────────────────────────────────────────────────

def pick_rpc():
    """Return the first responsive Ethereum RPC endpoint."""
    test_addr = "0xfD628af590c4150A9651C1f4ddD0b4f532B703ae"
    for rpc in ETH_RPC_CANDIDATES:
        try:
            r = requests.post(
                rpc,
                json={"jsonrpc": "2.0", "method": "eth_call",
                      "params": [{"to": test_addr, "data": SEL_DECIMALS}, "latest"], "id": 1},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            result = r.json().get("result", "")
            if result and result != "0x":
                print(f"  Using RPC: {rpc}")
                return rpc
        except Exception as e:
            print(f"  {rpc}: {e}")
    raise RuntimeError("No Ethereum RPC available")

def eth_call(rpc, to, data):
    r = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "method": "eth_call",
              "params": [{"to": to, "data": data}, "latest"], "id": 1},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("result", "")


# ── Chainlink feed reader ──────────────────────────────────────────────────────

def read_nav(rpc, oracle_addr, currency):
    """
    Read the latest NAV from a Chainlink AggregatorV3 feed.
    Returns dict with nav, currency, updated_at — or None if no data.
    """
    try:
        # decimals()
        dec_hex  = eth_call(rpc, oracle_addr, SEL_DECIMALS)
        decimals = int(dec_hex, 16) if dec_hex and dec_hex != "0x" else 6

        # latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
        res = eth_call(rpc, oracle_addr, SEL_LATEST_ROUND)
        if not res or len(res) <= 2:
            return None

        raw        = bytes.fromhex(res[2:])
        answer_raw = int.from_bytes(raw[32:64], "big", signed=True)
        updated_at = int.from_bytes(raw[96:128], "big")

        if answer_raw <= 0 or updated_at == 0:
            return None

        nav  = round(answer_raw / (10 ** decimals), 6)
        date = datetime.fromtimestamp(updated_at, tz=timezone.utc).strftime("%Y-%m-%d")
        return {"nav": nav, "currency": currency, "updated": date}

    except Exception as e:
        print(f"    Error reading {oracle_addr}: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs("data", exist_ok=True)

    # Load existing data so we can carry forward last known NAV if RPC fails
    nav_file = "data/nav.json"
    existing = {}
    if os.path.exists(nav_file):
        with open(nav_file) as f:
            existing = json.load(f)

    print("=== Fetching Spiko NAV data (Chainlink) ===")
    try:
        rpc = pick_rpc()
    except RuntimeError as e:
        print(f"  {e} — keeping existing data")
        return

    result = {}
    for token_id, (oracle_addr, currency) in NAV_FEEDS.items():
        print(f"  {token_id.upper()} ({currency})…", end=" ", flush=True)
        nav_data = read_nav(rpc, oracle_addr, currency)
        if nav_data:
            print(f"NAV = {nav_data['nav']} {currency} (updated {nav_data['updated']})")
            result[token_id] = nav_data
        else:
            print("no data")
            result[token_id] = None
        time.sleep(0.3)

    # Merge: keep existing entry if new fetch returned nothing
    for token_id in NAV_FEEDS:
        if result.get(token_id) is None and existing.get(token_id):
            result[token_id] = existing[token_id]
            print(f"  {token_id.upper()}: carried forward {existing[token_id]}")

    with open(nav_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Saved {nav_file}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
