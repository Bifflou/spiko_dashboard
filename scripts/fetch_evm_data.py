"""
Fetch supply & holder history for all Spiko tokens on one EVM chain.
Usage: python scripts/fetch_evm_data.py <chain>
       chain: eth | polygon | base | arb

Uses the Etherscan v2 unified API (single ETHERSCAN_API_KEY covers all chains).
Data files: data/{token}_{chain}_marketcap.json, data/{token}_{chain}_holders.json
State files: data/{token}_{chain}_state.json
"""

import sys
import requests
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
ETHERSCAN_V2_URL  = "https://api.etherscan.io/v2/api"
TRANSFER_TOPIC    = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS      = "0x0000000000000000000000000000000000000000"
LOOKBACK_BLOCKS   = 1000  # safety overlap (~3 h on Ethereum, proportionally less on faster chains)

# (address, native_currency)
# api_base=None → Etherscan v2 (requires ETHERSCAN_API_KEY)
# api_base=URL  → Blockscout-compatible (no key needed)
CHAIN_CONFIGS = {
    'eth': {
        'chain_id': 1,
        'api_base': None,
        'tokens': {
            'eutbl':    ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
            'ustbl':    ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
            'uktbl':    ('0xf695df6c0f3bb45918a7a82e83348fc59517734e', 'GBP'),
            'spkcc':    ('0x4f33acf823e6eeb697180d553ce0c710124c8d59', 'USD'),
            'eurspkcc': ('0x3868d4e336d14d38031cf680329d31e4712e11cc', 'EUR'),
            'safo':     ('0xcbade7d9bdee88411cb6cbcbb29952b742036992', 'USD'),
            'eursafo':  ('0x0990b149e915cb08e2143a5c6f669c907eddc8b0', 'EUR'),
            'gbpsafo':  ('0xc273986a91e4bfc543610a5cb5860b7cfefb6cc0', 'GBP'),
            'chfsafo':  ('0x18b5c15e5196a38a162b1787875295b76e4313fb', 'CHF'),
        },
    },
    'polygon': {
        'chain_id': 137,
        'api_base': None,
        'tokens': {
            'eutbl':    ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
            'ustbl':    ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
            'uktbl':    ('0x970e2adc2fdf53aea6b5fa73ca6dc30eafedfe3d', 'GBP'),
            'spkcc':    ('0x903d5990119bc799423e9c25c56518ba7dd19474', 'USD'),
            'eurspkcc': ('0x99f70a0e1786402a6796c6b0aa997ef340a5c6da', 'EUR'),
            'safo':     ('0x6f64f47f95cf656f21b40e14798f6b49f80b3dc5', 'USD'),
            'eursafo':  ('0x272ea767712cc4839f4a27ee35eb73116158c8a2', 'EUR'),
            'gbpsafo':  ('0x4fe515c67eeeadb3282780325f09bb7c244fe774', 'GBP'),
            'chfsafo':  ('0x9de2b2dcdcf43540e47143f28484b6d15118f089', 'CHF'),
        },
    },
    'base': {
        'chain_id': 8453,
        'api_base': 'https://base.blockscout.com/api',
        'tokens': {
            'eutbl':    ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
            'ustbl':    ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
            'uktbl':    ('0xa8de1f55aa0e381cb456e1dcc9ff781ea0079068', 'GBP'),
            'spkcc':    ('0xf695df6c0f3bb45918a7a82e83348fc59517734e', 'USD'),
            'eurspkcc': ('0x4f33acf823e6eeb697180d553ce0c710124c8d59', 'EUR'),
            'safo':     ('0x0bb754d8940e283d9ff6855ab5dafbc14165c059', 'USD'),
            'eursafo':  ('0xd879846cbe20751bde8a9342a3cca00a3e56ca47', 'EUR'),
            'gbpsafo':  ('0x2f6c0e5e06b43512706a9cdf66cd21f723fe0ec3', 'GBP'),
            'chfsafo':  ('0xd9aa2300e126869182dfb6ecf54984e4c687f36b', 'CHF'),
        },
    },
    'arb': {
        'chain_id': 42161,
        'api_base': None,
        'tokens': {
            'eutbl':    ('0xcbeb19549054cc0a6257a77736fc78c367216ce7', 'EUR'),
            'ustbl':    ('0x021289588cd81dc1ac87ea91e91607eef68303f5', 'USD'),
            'uktbl':    ('0x903d5990119bc799423e9c25c56518ba7dd19474', 'GBP'),
            'spkcc':    ('0x99f70a0e1786402a6796c6b0aa997ef340a5c6da', 'USD'),
            'eurspkcc': ('0x0e389c83bc1d16d86412476f6103027555c03265', 'EUR'),
            'safo':     ('0x0c709396739b9cfb72bcea6ac691ce0ddf66479c', 'USD'),
            'eursafo':  ('0x1412632f2b89e87bfa20c1318a43ced25f1d7b76', 'EUR'),
            'gbpsafo':  ('0xbe023308ac2ef7e1c3799f4e6a3003ee6d342635', 'GBP'),
            'chfsafo':  ('0x97e7962bcd091e7ecfb583fc96289b1e1553ac6e', 'CHF'),
        },
    },
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


# ── Explorer API helpers ───────────────────────────────────────────────────────

def explorer_get(cfg, params):
    """Unified call: Etherscan v2 (api_base=None) or Blockscout (api_base=URL)."""
    api_base = cfg.get('api_base')
    if api_base is None:
        url    = ETHERSCAN_V2_URL
        params = dict(params)
        params['chainid'] = cfg['chain_id']
        params['apikey']  = ETHERSCAN_API_KEY
    else:
        url = api_base
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def get_token_decimals(cfg, token_address):
    data = explorer_get(cfg, {
        'module': 'proxy', 'action': 'eth_call',
        'to': token_address, 'data': '0x313ce567', 'tag': 'latest',
    })
    return int(data.get('result', '0x12'), 16)


# ── Log fetching ───────────────────────────────────────────────────────────────

def fetch_transfer_logs(cfg, token_address, from_block):
    all_logs = []
    seen     = set()
    current_from = from_block

    while True:
        page = 1
        last_block_in_batch = None

        while True:
            data = explorer_get(cfg, {
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

def fetch_fx_rates_for_currency(currency, start_date):
    """Returns {date: usd_rate} for the given non-USD currency."""
    url  = f"https://api.frankfurter.app/{start_date}.."
    resp = requests.get(url, params={'from': currency, 'to': 'USD'}, timeout=30)
    resp.raise_for_status()
    return {d: r['USD'] for d, r in resp.json().get('rates', {}).items()}

def compute_marketcap(supply_history, currency, fx_rates_for_currency):
    if currency == 'USD':
        return [
            {'date': item['date'], 'marketcap': round(item['supply'], 2), 'supply': round(item['supply'], 2)}
            for item in supply_history
        ]

    result    = []
    last_rate = None
    for item in supply_history:
        date = item['date']
        if date in fx_rates_for_currency:
            last_rate = fx_rates_for_currency[date]
        if last_rate is None:
            continue
        result.append({
            'date':      date,
            'marketcap': round(item['supply'] * last_rate, 2),
            'supply':    round(item['supply'], 2),
        })
    return result


# ── Per-token processing ───────────────────────────────────────────────────────

def process_token(cfg, chain_name, token_id, token_address, currency, fx_cache):
    state_file   = f'data/{token_id}_{chain_name}_state.json'
    mcap_file    = f'data/{token_id}_{chain_name}_marketcap.json'
    holders_file = f'data/{token_id}_{chain_name}_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    decimals = get_token_decimals(cfg, token_address)
    print(f'    Decimals: {decimals}')

    if state.get('last_block') and existing_mcap:
        last_block = int(state['last_block'])
        balances   = {k: int(v) for k, v in state.get('balances', {}).items()}
        from_block = max(0, last_block - LOOKBACK_BLOCKS)
        print(f'    Incremental from block {from_block} (last known: {last_block})')

        fetched_logs   = fetch_transfer_logs(cfg, token_address, from_block)
        overlap_logs   = [l for l in fetched_logs if int(l['blockNumber'], 16) <= last_block]
        truly_new_logs = [l for l in fetched_logs if int(l['blockNumber'], 16) >  last_block]
        print(f'    {len(overlap_logs)} overlap, {len(truly_new_logs)} truly new')

        if not truly_new_logs:
            print(f'    No new logs — skipping.')
            return

        first_new_ts   = int(truly_new_logs[0]['timeStamp'], 16)
        first_new_date = datetime.fromtimestamp(first_new_ts, tz=timezone.utc).strftime('%Y-%m-%d')
        new_supply, new_holders = build_daily_snapshots(truly_new_logs, balances, decimals)

        kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
        kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
        merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
        merged_hold  = kept_holders + new_holders

        new_last_block = max(int(l['blockNumber'], 16) for l in fetched_logs)

    else:
        print(f'    Full fetch from genesis...')
        balances     = {}
        fetched_logs = fetch_transfer_logs(cfg, token_address, 0)
        print(f'    Total: {len(fetched_logs)} Transfer events')

        if not fetched_logs:
            print(f'    No events found — token may not be active on this chain yet.')
            return

        merged_raw, merged_hold = build_daily_snapshots(fetched_logs, balances, decimals)
        all_blocks     = [int(l['blockNumber'], 16) for l in fetched_logs]
        new_last_block = max(all_blocks)

    if not merged_raw:
        print(f'    No supply data.')
        return

    # Fetch FX rates if not already cached for this currency
    if currency not in fx_cache:
        start_date = merged_raw[0]['date']
        print(f'    Fetching {currency}/USD rates from {start_date}...')
        if currency == 'USD':
            fx_cache['USD'] = {}
        else:
            fx_cache[currency] = fetch_fx_rates_for_currency(currency, start_date)
        print(f'    {len(fx_cache.get(currency, {}))} rate entries')

    mcap_history = compute_marketcap(merged_raw, currency, fx_cache.get(currency, {}))

    save_json(state_file, {
        'last_block': new_last_block,
        'balances':   {k: str(v) for k, v in balances.items()},
    })
    save_json(mcap_file,    mcap_history)
    save_json(holders_file, merged_hold)
    print(f'    Saved: {len(mcap_history)} mcap pts, {len(merged_hold)} holder pts, last block {new_last_block}')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CHAIN_CONFIGS:
        print(f'Usage: python {sys.argv[0]} <chain>')
        print(f'Supported chains: {list(CHAIN_CONFIGS.keys())}')
        sys.exit(1)

    chain_name = sys.argv[1]
    cfg        = CHAIN_CONFIGS[chain_name]
    tokens     = cfg['tokens']

    print(f'=== Fetching Spiko data — {chain_name.upper()} (chain_id={cfg["chain_id"]}) ===')
    os.makedirs('data', exist_ok=True)

    fx_cache = {}  # currency → {date: usd_rate}, populated lazily per token

    for token_id, (token_address, currency) in tokens.items():
        print(f'\n[{token_id.upper()} on {chain_name.upper()}]')
        try:
            process_token(cfg, chain_name, token_id, token_address, currency, fx_cache)
        except Exception as e:
            print(f'  ERROR processing {token_id}: {e}')
        time.sleep(0.5)

    print(f'\n=== Done — {chain_name.upper()} ===')


if __name__ == '__main__':
    main()
