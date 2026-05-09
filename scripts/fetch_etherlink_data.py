"""
Fetch supply & holder history for Spiko tokens on Etherlink (tezos L2).
Etherlink is EVM-compatible but has no Etherscan-equivalent API, so we
use direct JSON-RPC calls to the public endpoint + eth_getLogs pagination.

Tokens on Etherlink: EUTBL, USTBL, UKTBL, SAFO, EURSAFO, GBPSAFO, CHFSAFO
SPKCC and EURSPKCC are NOT deployed on Etherlink.

Usage: python scripts/fetch_etherlink_data.py
"""

import json
import os
import requests
import time
from collections import defaultdict
from datetime import datetime, timezone

RPC_URL       = "https://node.mainnet.etherlink.com"
CHAIN_NAME    = "etherlink"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS  = "0x0000000000000000000000000000000000000000"
LOOKBACK_BLOCKS = 1000   # ~50 min on Etherlink (~3 s/block)
BATCH_SIZE    = 2000     # blocks per eth_getLogs request

TOKENS = {
    'eutbl':   ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
    'ustbl':   ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
    'uktbl':   ('0xa8de1f55aa0e381cb456e1dcc9ff781ea0079068', 'GBP'),
    'safo':    ('0x0bb754d8940e283d9ff6855ab5dafbc14165c059', 'USD'),
    'eursafo': ('0xd879846cbe20751bde8a9342a3cca00a3e56ca47', 'EUR'),
    'gbpsafo': ('0x2f6c0e5e06b43512706a9cdf66cd21f723fe0ec3', 'GBP'),
    'chfsafo': ('0xd9aa2300e126869182dfb6ecf54984e4c687f36b', 'CHF'),
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


# ── JSON-RPC helpers ───────────────────────────────────────────────────────────

_rpc_id = 0

def rpc(method, params):
    global _rpc_id
    _rpc_id += 1
    resp = requests.post(RPC_URL, json={
        'jsonrpc': '2.0', 'id': _rpc_id,
        'method': method, 'params': params,
    }, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if 'error' in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result['result']


def get_latest_block():
    return int(rpc('eth_blockNumber', []), 16)


def get_block_timestamp(block_hex):
    blk = rpc('eth_getBlockByNumber', [block_hex, False])
    return int(blk['timestamp'], 16) if blk else None


def get_token_decimals(token_address):
    result = rpc('eth_call', [{'to': token_address, 'data': '0x313ce567'}, 'latest'])
    return int(result, 16) if result and result != '0x' else 18


def fetch_logs_range(token_address, from_block, to_block):
    """Fetch Transfer logs for a block range, with BATCH_SIZE pagination."""
    all_logs = []
    current  = from_block
    while current <= to_block:
        end = min(current + BATCH_SIZE - 1, to_block)
        logs = rpc('eth_getLogs', [{
            'address':   token_address,
            'topics':    [TRANSFER_TOPIC],
            'fromBlock': hex(current),
            'toBlock':   hex(end),
        }])
        all_logs.extend(logs or [])
        print(f"    Blocks {current}–{end}: {len(logs or [])} logs (total: {len(all_logs)})")
        current = end + 1
        time.sleep(0.15)
    return all_logs


# ── Supply & holders reconstruction ───────────────────────────────────────────

def build_daily_snapshots(logs, balances, decimals, block_ts_cache):
    """
    Apply Transfer events to `balances`, group by date, emit daily snapshots.
    block_ts_cache: {block_hex: timestamp_int} — populated lazily.
    """
    events_by_date = defaultdict(list)
    for log in logs:
        block_hex = log['blockNumber']
        if block_hex not in block_ts_cache:
            ts = get_block_timestamp(block_hex)
            block_ts_cache[block_hex] = ts
            time.sleep(0.05)
        ts   = block_ts_cache[block_hex]
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        events_by_date[date].append(log)

    supply_history  = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        for log in events_by_date[date]:
            from_addr = '0x' + log['topics'][1][-40:]
            to_addr   = '0x' + log['topics'][2][-40:]
            amount    = int(log['data'], 16)
            if from_addr.lower() != ZERO_ADDRESS:
                balances[from_addr.lower()] = balances.get(from_addr.lower(), 0) - amount
            if to_addr.lower() != ZERO_ADDRESS:
                balances[to_addr.lower()] = balances.get(to_addr.lower(), 0) + amount

        supply  = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        holders = sum(1 for v in balances.values() if v > 0)
        supply_history.append({'date': date, 'supply': supply})
        holders_history.append({'date': date, 'holders': holders})

    return supply_history, holders_history


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

def process_token(token_id, token_address, currency, fx_cache):
    print(f'\n[{token_id.upper()} on ETHERLINK — {token_address[:10]}…]')

    state_file   = f'data/{token_id}_{CHAIN_NAME}_state.json'
    mcap_file    = f'data/{token_id}_{CHAIN_NAME}_marketcap.json'
    holders_file = f'data/{token_id}_{CHAIN_NAME}_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    decimals = get_token_decimals(token_address)
    print(f'  Decimals: {decimals}')

    latest_block = get_latest_block()
    print(f'  Latest block: {latest_block}')

    block_ts_cache = {}

    if state.get('last_block') and existing_mcap:
        last_block = int(state['last_block'])
        balances   = {k: int(v) for k, v in state.get('balances', {}).items()}
        from_block = max(0, last_block - LOOKBACK_BLOCKS)
        print(f'  Incremental from block {from_block} (last known: {last_block})')

        fetched_logs   = fetch_logs_range(token_address, from_block, latest_block)
        overlap_logs   = [l for l in fetched_logs if int(l['blockNumber'], 16) <= last_block]
        truly_new_logs = [l for l in fetched_logs if int(l['blockNumber'], 16) >  last_block]
        print(f'  {len(overlap_logs)} overlap, {len(truly_new_logs)} truly new')

        if not truly_new_logs:
            print('  No new logs — skipping.')
            return

        first_new_ts   = block_ts_cache.get(truly_new_logs[0]['blockNumber'])
        if first_new_ts is None:
            first_new_ts = get_block_timestamp(truly_new_logs[0]['blockNumber'])
        first_new_date = datetime.fromtimestamp(first_new_ts, tz=timezone.utc).strftime('%Y-%m-%d')

        new_supply, new_holders = build_daily_snapshots(truly_new_logs, balances, decimals, block_ts_cache)

        kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
        kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
        merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
        merged_hold  = kept_holders + new_holders

    else:
        print('  Full fetch from genesis...')
        balances     = {}
        fetched_logs = fetch_logs_range(token_address, 0, latest_block)
        print(f'  Total: {len(fetched_logs)} Transfer events')

        if not fetched_logs:
            print('  No events found — token may not be active on Etherlink yet.')
            return

        merged_raw, merged_hold = build_daily_snapshots(fetched_logs, balances, decimals, block_ts_cache)

    if not merged_raw:
        print('  No supply data.')
        return

    if currency not in fx_cache:
        start_date = merged_raw[0]['date']
        if currency == 'USD':
            fx_cache['USD'] = {}
        else:
            print(f'  Fetching {currency}/USD rates from {start_date}...')
            fx_cache[currency] = fetch_fx_rates_for_currency(currency, start_date)

    mcap_history   = compute_marketcap(merged_raw, currency, fx_cache.get(currency, {}))
    new_last_block = latest_block

    save_json(state_file, {
        'last_block': new_last_block,
        'balances':   {k: str(v) for k, v in balances.items()},
    })
    save_json(mcap_file,    mcap_history)
    save_json(holders_file, merged_hold)
    print(f'  Saved: {len(mcap_history)} mcap pts, {len(merged_hold)} holder pts, last block {new_last_block}')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs('data', exist_ok=True)
    print(f'=== Fetching Spiko Etherlink data ===')

    fx_cache = {}

    for token_id, (token_address, currency) in TOKENS.items():
        try:
            process_token(token_id, token_address, currency, fx_cache)
        except Exception as e:
            print(f'  ERROR for {token_id}: {e}')
        time.sleep(0.5)

    print('\n=== Done — Etherlink ===')


if __name__ == '__main__':
    main()
