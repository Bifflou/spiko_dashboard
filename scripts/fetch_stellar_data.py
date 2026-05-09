"""
Fetch supply & holder history for all Spiko tokens on Stellar.
Spiko tokens are Soroban contracts (C... addresses), not classic Stellar assets.

Strategy:
- Current state : stellar.expert contract endpoint
- Historical    : Horizon /accounts/{contract}/operations (Soroban contracts are
                  indexed as accounts in Horizon) + asset_balance_changes parsing
- Holders       : stellar.expert daily snapshot (Soroban holders list)
- FX            : frankfurter.app for non-USD tokens
"""

import json
import os
import requests
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HORIZON      = "https://horizon.stellar.org"
EXPERT_BASE  = "https://api.stellar.expert"
STELLAR_SCALE = 10 ** 7

# (contract_address, native_currency)
TOKENS = {
    'eutbl':    ('CBGV2QFQBBGEQRUKUMCPO3SZOHDDYO6SCP5CH6TW7EALKVHCXTMWDDOF', 'EUR'),
    'ustbl':    ('CARUUX2FZNPH6DGJOEUFSIUQWYHNL5AVDV7PMVSHWL7OBYIBFC76F4TO', 'USD'),
    'uktbl':    ('CDT3KU6TQZNOHKNOHNAFFDQZDURVC3MSTL4ML7TUTZGNOPBZCLABP4FR', 'GBP'),
    'spkcc':    ('CDS2GCAQTNQINSCJUJIVBJXILKBWP5PU7LOBGHMP3X47QCQBFKPMTCNT', 'USD'),
    'eurspkcc': ('CDWOB6T7SVSMMQN5V3P2OPTBAXOP7DAZHGVW3PYTZIKHVFKN6TBSXR6A', 'EUR'),
    'safo':     ('CDGSC6BA4TCAOVSFQCUEHDMOIIHYYVNYBT6YEARS4MX3ITAHUINVGQHX', 'USD'),
    'eursafo':  ('CBOOCGZSVRSZFRE4U2NWR2B4RXYVJWRCBTGOUD2JPI2TDJPWMTJX7FZP', 'EUR'),
    'gbpsafo':  ('CAGYRRKPFSWKM6SJOE4QAAVYMOSHMDS5WOQ4T5A2E6XNCU7LZZKUNQKP', 'GBP'),
    'chfsafo':  ('CAJD2IBSP7VO2VYJQUYJSOGPJINTUYV7MQITINXVPTIH3CCLCUENNMW4', 'CHF'),
}


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)

def iso_to_date(iso):
    return iso[:10] if iso else ''


# ── stellar.expert — current state ─────────────────────────────────────────────

def get_contract_current_state(contract_address):
    """
    Fetch current supply and holder count from stellar.expert.
    Returns (supply_float, holders_int) or (0.0, 0) on failure.
    """
    url = f"{EXPERT_BASE}/explorer/public/contract/{contract_address}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # stellar.expert wraps token info in a 'token' sub-object for contracts
        token = data.get('token') or data
        raw_supply  = token.get('supply', token.get('total_supply', 0)) or 0
        raw_holders = token.get('holders_count', token.get('holders', 0)) or 0
        supply  = float(raw_supply)  / STELLAR_SCALE
        holders = int(raw_holders)
        return round(supply, 7), holders
    except Exception as e:
        print(f'    [stellar.expert] contract info error: {e}')
        return 0.0, 0


def get_contract_holders(contract_address):
    """
    Try the /holders sub-endpoint for more precise holder count.
    Falls back to 0 on error.
    """
    url = f"{EXPERT_BASE}/explorer/public/contract/{contract_address}/holders"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Count records with positive balance
        records = data.get('_embedded', {}).get('records', [])
        if not records and isinstance(data, list):
            records = data
        count = sum(
            1 for r in records
            if float(r.get('balance', r.get('amount', 0)) or 0) > 0
        )
        return count or None  # None → caller falls back to contract info
    except Exception:
        return None


# ── Horizon — historical operations ───────────────────────────────────────────

def get_account_operations_since(account, cursor=None):
    """
    Paginate through all operations for an account (or contract) on Horizon.
    Returns (ops_list, last_op_id).
    """
    all_ops    = []
    last_op_id = cursor
    url        = f"{HORIZON}/accounts/{account}/operations"
    params     = {'order': 'asc', 'limit': 200, 'include_failed': 'false'}
    if cursor:
        params['cursor'] = cursor

    use_params = True
    page = 1
    while True:
        try:
            resp = requests.get(url, params=(params if use_params else None), timeout=30)
            if resp.status_code == 404:
                print(f'    [Horizon] 404 for account {account[:12]}… — no operations indexed')
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f'    [Horizon] error page {page}: {e}')
            break

        data    = resp.json()
        records = data.get('_embedded', {}).get('records', [])
        all_ops.extend(records)
        if records:
            last_op_id = records[-1]['id']
        print(f'    Page {page}: {len(records)} ops (total: {len(all_ops)})')

        next_href = data.get('_links', {}).get('next', {}).get('href')
        if not next_href or not records:
            break

        url        = next_href
        use_params = False
        page      += 1
        time.sleep(0.15)

    return all_ops, last_op_id


# ── Supply reconstruction from operations ──────────────────────────────────────

def process_supply_from_ops(ops):
    """
    Reconstruct daily supply deltas from Horizon operations.
    Works for Soroban SAC tokens (asset_balance_changes) and classic payments.
    For pure SEP-41 tokens without asset_balance_changes, returns empty dict.
    """
    delta_by_date = defaultdict(float)

    for op in ops:
        op_type = op.get('type', '')
        date    = iso_to_date(op.get('created_at', '') or '')
        if not date:
            continue

        if op_type == 'payment':
            # Classic Stellar payment — track if it looks like a mint/burn
            # (src or dst is the contract itself, acting as issuer)
            amount = float(op.get('amount', 0))
            src    = op.get('from', '')
            dst    = op.get('to', '')
            # Mint: from contract; Burn: to contract
            # We don't have a classic issuer here, so skip for now
            pass

        elif op_type == 'invoke_host_function':
            # Soroban invocation — Horizon may decode asset_balance_changes
            changes = op.get('asset_balance_changes') or []
            for change in changes:
                ctype  = change.get('type', '')
                amount = float(change.get('amount', 0))
                if ctype == 'mint':
                    delta_by_date[date] += amount
                elif ctype in ('burn', 'clawback'):
                    delta_by_date[date] -= amount

        elif op_type == 'clawback':
            delta_by_date[date] -= float(op.get('amount', 0))

    return delta_by_date


def build_supply_history(delta_by_date, initial_cumulative=0.0):
    history    = []
    cumulative = initial_cumulative
    for date in sorted(delta_by_date.keys()):
        cumulative += delta_by_date[date]
        history.append({'date': date, 'supply': round(max(0.0, cumulative), 7)})
    return history, cumulative


# ── FX rates & marketcap ───────────────────────────────────────────────────────

def fetch_fx_rates_for_currency(currency, start_date):
    url  = f"https://api.frankfurter.app/{start_date}.."
    resp = requests.get(url, params={'from': currency, 'to': 'USD'}, timeout=30)
    resp.raise_for_status()
    return {d: r['USD'] for d, r in resp.json().get('rates', {}).items()}

def compute_marketcap(supply_history, currency, fx_rates):
    if currency == 'USD':
        return [
            {'date': item['date'], 'marketcap': round(item['supply'], 2), 'supply': round(item['supply'], 2)}
            for item in supply_history
        ]
    result    = []
    last_rate = None
    for item in supply_history:
        date = item['date']
        if date in fx_rates:
            last_rate = fx_rates[date]
        if last_rate is None:
            continue
        result.append({
            'date':      date,
            'marketcap': round(item['supply'] * last_rate, 2),
            'supply':    round(item['supply'], 2),
        })
    return result


# ── Per-token processing ───────────────────────────────────────────────────────

def process_token(token_id, contract_address, currency, today, fx_cache):
    print(f'\n[{token_id.upper()} — {contract_address[:12]}…]')

    state_file   = f'data/{token_id}_stellar_state.json'
    mcap_file    = f'data/{token_id}_stellar_marketcap.json'
    holders_file = f'data/{token_id}_stellar_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    # ── 1. Current authoritative state from stellar.expert ────────────────────
    print('  Fetching current state from stellar.expert...')
    current_supply, current_holders = get_contract_current_state(contract_address)
    time.sleep(0.2)

    # Try dedicated holders endpoint for better accuracy
    precise_holders = get_contract_holders(contract_address)
    if precise_holders is not None:
        current_holders = precise_holders
    time.sleep(0.2)

    print(f'  Current supply: {current_supply:,.2f}  holders: {current_holders}')

    # ── 2. Historical supply from Horizon operations ──────────────────────────
    cursor         = state.get('last_op_id')
    supply_cumul   = float(state.get('supply_cumulative', 0.0))

    print(f'  Fetching Horizon operations (cursor={cursor})...')
    ops, new_cursor = get_account_operations_since(contract_address, cursor=cursor)
    print(f'  {len(ops)} new operations')

    if ops:
        delta_by_date           = process_supply_from_ops(ops)
        new_supply_hist, supply_cumul = build_supply_history(delta_by_date, supply_cumul)
        print(f'  {len(new_supply_hist)} new supply days from operations')

        if new_supply_hist:
            first_new_date  = new_supply_hist[0]['date']
            kept_mcap       = [pt for pt in existing_mcap if pt['date'] < first_new_date]
            merged_raw      = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply_hist
        else:
            merged_raw = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]
    else:
        # No operations indexed — use existing history
        merged_raw = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]

    # ── 3. Override / append today's authoritative snapshot ───────────────────
    if current_supply > 0:
        if merged_raw and merged_raw[-1]['date'] == today:
            merged_raw[-1]['supply'] = current_supply
        else:
            merged_raw.append({'date': today, 'supply': current_supply})

    if not merged_raw:
        print('  No supply data available yet — skipping.')
        return

    # ── 4. FX rates & marketcap ───────────────────────────────────────────────
    if currency not in fx_cache:
        start_date = merged_raw[0]['date']
        if currency == 'USD':
            fx_cache['USD'] = {}
        else:
            print(f'  Fetching {currency}/USD rates from {start_date}...')
            fx_cache[currency] = fetch_fx_rates_for_currency(currency, start_date)

    mcap_history = compute_marketcap(merged_raw, currency, fx_cache.get(currency, {}))

    # ── 5. Holders history ────────────────────────────────────────────────────
    merged_holders = [pt for pt in existing_holders]
    if merged_holders and merged_holders[-1]['date'] == today:
        merged_holders[-1]['holders'] = current_holders
    else:
        merged_holders.append({'date': today, 'holders': current_holders})

    # ── 6. Save ───────────────────────────────────────────────────────────────
    new_state = {
        'last_op_id':         new_cursor or cursor,
        'supply_cumulative':  supply_cumul,
    }
    save_json(state_file,   new_state)
    save_json(mcap_file,    mcap_history)
    save_json(holders_file, merged_holders)
    print(f'  Saved: {len(mcap_history)} mcap pts, {len(merged_holders)} holder pts')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs('data', exist_ok=True)
    today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fx_cache = {}  # currency → {date: usd_rate}

    print(f'=== Fetching Spiko Stellar data — {today} ===')

    for token_id, (contract_address, currency) in TOKENS.items():
        try:
            process_token(token_id, contract_address, currency, today, fx_cache)
        except Exception as e:
            print(f'  ERROR for {token_id}: {e}')
        time.sleep(0.5)

    print('\n=== Done — Stellar ===')


if __name__ == '__main__':
    main()
