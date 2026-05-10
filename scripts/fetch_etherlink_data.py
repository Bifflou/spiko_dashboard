"""
Fetch supply & holder history for Spiko tokens on Etherlink (tezos L2).
Uses the Etherlink Blockscout explorer API (Etherscan-compatible, no API key).

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

BLOCKSCOUT_URL = "https://explorer.etherlink.com/api"
CHAIN_NAME     = "etherlink"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS   = "0x0000000000000000000000000000000000000000"
LOOKBACK_BLOCKS = 5000

TOKENS = {
    'eutbl':   ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
    'ustbl':   ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
    'uktbl':   ('0x970e2adc2fdf53aea6b5fa73ca6dc30eafedfe3d', 'GBP'),
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


# ── Blockscout API helpers ─────────────────────────────────────────────────────

def blockscout_get(params):
    resp = requests.get(BLOCKSCOUT_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def get_token_decimals(token_address):
    # Blockscout v2 REST API — proxy/eth_call not supported on Blockscout
    v2_url = BLOCKSCOUT_URL.rstrip('/api').rstrip('/') + f'/api/v2/tokens/{token_address}'
    resp = requests.get(v2_url, timeout=30)
    if resp.ok:
        dec = resp.json().get('decimals')
        return int(dec) if dec is not None else 18
    return 18


# ── Log fetching ───────────────────────────────────────────────────────────────

def fetch_transfer_logs(token_address, from_block):
    all_logs = []
    seen     = set()
    current_from = from_block

    while True:
        page = 1
        last_block_in_batch = None

        while True:
            data = blockscout_get({
                'module': 'logs', 'action': 'getLogs',
                'address': token_address, 'topic0': TRANSFER_TOPIC,
                'fromBlock': current_from, 'toBlock': 'latest',
                'page': page, 'offset': 1000,
            })
            if data['status'] != '1' or not data['result']:
                return all_logs

            logs = data['result']
            new  = 0
            for log in logs:
                key = (log['transactionHash'], log['logIndex'])
                if key not in seen:
                    seen.add(key)
                    all_logs.append(log)
                    new += 1

            last_block_in_batch = int(logs[-1]['blockNumber'], 16)
            print(f"    Page {page} (from {current_from}): {new} new events (total: {len(all_logs)})")

            if len(logs) < 1000:
                return all_logs

            page += 1
            time.sleep(0.25)

            if page > 10:
                current_from = last_block_in_batch
                break

    return all_logs


# ── Supply & holders reconstruction ───────────────────────────────────────────

def build_daily_snapshots(logs, balances, decimals):
    events_by_date = defaultdict(list)
    for log in logs:
        ts   = int(log['timeStamp'], 16)
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

def process_token(token_id, token_address, currency, fx_rates_all):
    print(f'\n[{token_id.upper()} on ETHERLINK — {token_address[:10]}…]')

    state_file   = f'data/{token_id}_{CHAIN_NAME}_state.json'
    mcap_file    = f'data/{token_id}_{CHAIN_NAME}_marketcap.json'
    holders_file = f'data/{token_id}_{CHAIN_NAME}_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    decimals = get_token_decimals(token_address)
    print(f'  Decimals: {decimals}')

    if state.get('last_block') and existing_mcap:
        last_block = int(state['last_block'])
        balances   = {k: int(v) for k, v in state.get('balances', {}).items()}
        from_block = max(0, last_block - LOOKBACK_BLOCKS)
        print(f'  Incremental from block {from_block} (last known: {last_block})')

        fetched_logs   = fetch_transfer_logs(token_address, from_block)
        overlap_logs   = [l for l in fetched_logs if int(l['blockNumber'], 16) <= last_block]
        truly_new_logs = [l for l in fetched_logs if int(l['blockNumber'], 16) >  last_block]
        print(f'  {len(overlap_logs)} overlap, {len(truly_new_logs)} truly new')

        if not truly_new_logs:
            print('  No new logs — carrying forward today.')
            merged_raw  = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]
            merged_hold = list(existing_holders)
            new_last_block = last_block
        else:
            first_new_ts   = int(truly_new_logs[0]['timeStamp'], 16)
            first_new_date = datetime.fromtimestamp(first_new_ts, tz=timezone.utc).strftime('%Y-%m-%d')

            new_supply, new_holders = build_daily_snapshots(truly_new_logs, balances, decimals)

            kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
            kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
            merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
            merged_hold  = kept_holders + new_holders
            new_last_block = max(int(l['blockNumber'], 16) for l in fetched_logs)

    else:
        print('  Full fetch from genesis...')
        balances     = {}
        fetched_logs = fetch_transfer_logs(token_address, 0)
        print(f'  Total: {len(fetched_logs)} Transfer events')

        if not fetched_logs:
            print('  No events found — token may not be active on Etherlink yet.')
            return

        merged_raw, merged_hold = build_daily_snapshots(fetched_logs, balances, decimals)
        new_last_block = max(int(l['blockNumber'], 16) for l in fetched_logs)

    if not merged_raw:
        print('  No supply data.')
        return

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if merged_raw[-1]['date'] < today:
        current_supply  = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        current_holders = sum(1 for v in balances.values() if v > 0)
        merged_raw.append({'date': today, 'supply': round(current_supply, 7)})
        merged_hold.append({'date': today, 'holders': current_holders})

    fx_rates = fx_rates_all.get(currency, {}) if currency != 'USD' else {}
    mcap_history = compute_marketcap(merged_raw, currency, fx_rates)

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
    print('=== Fetching Spiko Etherlink data ===')

    fx_rates_all = load_json('data/fx_rates.json', default={})

    for token_id, (token_address, currency) in TOKENS.items():
        try:
            process_token(token_id, token_address, currency, fx_rates_all)
        except Exception as e:
            print(f'  ERROR for {token_id}: {e}')
        time.sleep(0.5)

    print('\n=== Done — Etherlink ===')


if __name__ == '__main__':
    main()
