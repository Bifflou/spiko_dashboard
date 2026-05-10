"""
Fetch supply & holder history for all 9 Spiko tokens on Starknet.
Uses the public Starknet JSON-RPC (starknet_getEvents) — no API key required.

Key differences from EVM:
- Addresses are felt252 (0x0... padded to 32 bytes)
- Amounts are u256 split as two felts: [low, high]
- Transfer event key: starknet_keccak("Transfer")
- Pagination via continuation_token (not block ranges)
- Block timestamps fetched separately via starknet_getBlockWithTxHashes
"""

import json
import os
import requests
import time
from collections import defaultdict
from datetime import datetime, timezone

RPC_URLS = [
    "https://starknet.drpc.org",
    "https://rpc.starknet.lava.build",
    "https://free-rpc.nethermind.io/mainnet-juno/",
]
CHAIN_NAME = "starknet"
CHUNK_SIZE = 1000

# starknet_keccak("Transfer") — identifies Transfer events in Cairo ERC-20
TRANSFER_KEY = "0x99cd8bde557814842a3121e8ddfd433a539b8c9f14bf31ebf108d12e6196e9"
ZERO_FELT    = "0x0"

# All 9 tokens exist on Starknet
TOKENS = {
    'eutbl':    ('0x04f5e0de717daa6aa8de63b1bf2e8d7823ec5b21a88461b1519d9dbc956fb7f2', 'EUR'),
    'ustbl':    ('0x020ff2f6021ada9edbceaf31b96f9f67b746662a6e6b2bc9d30c0d3e290a71f6', 'USD'),
    'uktbl':    ('0x0153d6e0462080bb2842109e9b64f589ef5aa06bb32b26bbdb894aca92674395', 'GBP'),
    'spkcc':    ('0x04bade88e79a6120f893d64e51006ac6853eceeefa1a50868d19601b1f0a567d', 'USD'),
    'eurspkcc': ('0x06472cabc51a3805975b9c60c7dec63897c9a287f2db173a1d6c589d18dd1e07', 'EUR'),
    'safo':     ('0x035bdc17f7a7d09c45d31ab476a576d4f7aad916676b2948fe172c3bcb33725a', 'USD'),
    'eursafo':  ('0x0128f41ef8017ab56140ffad6439305a3196ed862841ba61ff4d78e380c346a6', 'EUR'),
    'gbpsafo':  ('0x06e8a99926ff6d56f4cb93c37b63286d736cd1f81740d53f88b4875b4cbe7f49', 'GBP'),
    'chfsafo':  ('0x06723dcb428eddb160c5adfc2d0a5e5adc184bf6a7298780c3cbf3fa764f709b', 'CHF'),
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


# ── Starknet JSON-RPC helpers ──────────────────────────────────────────────────

_rpc_id = 0

def rpc(method, params):
    global _rpc_id
    _rpc_id += 1
    payload = {'jsonrpc': '2.0', 'id': _rpc_id, 'method': method, 'params': params}
    last_err = None
    for url in RPC_URLS:
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if 'error' in result:
                raise RuntimeError(f"RPC error ({method}): {result['error']}")
            return result['result']
        except Exception as e:
            last_err = e
            time.sleep(0.5)
            continue
    raise RuntimeError(f"All RPC endpoints failed for {method}: {last_err}")


def normalize_felt(felt_str):
    """Normalize a felt252 hex string to lowercase without leading zeros (except '0x0')."""
    val = int(felt_str, 16)
    return hex(val)


def felt_to_int(felt_str):
    return int(felt_str, 16)


def get_block_timestamp(block_number):
    result = rpc('starknet_getBlockWithTxHashes', [{'block_number': block_number}])
    return result.get('timestamp', 0)


def get_token_decimals(contract_address):
    # starknet_keccak("decimals") entry point selector
    DECIMALS_SELECTOR = "0x04c4fb1ab068f6039d5780c68dd0fa2f8742cceb3426d19667778ca7f3518a9"
    try:
        result = rpc('starknet_call', [{
            'contract_address': contract_address,
            'entry_point_selector': DECIMALS_SELECTOR,
            'calldata': [],
        }, 'latest'])
        return felt_to_int(result[0]) if result else 18
    except Exception:
        return 18  # OZ ERC20 default


def get_latest_block_number():
    result = rpc('starknet_blockNumber', [])
    return result  # returns an integer directly


# ── Event fetching ─────────────────────────────────────────────────────────────

def fetch_transfer_events(contract_address, from_block_number=0, continuation_token=None):
    """
    Fetch all Transfer events for a contract using starknet_getEvents with pagination.
    Returns (events_list, last_continuation_token).

    In Cairo ERC-20 (OZ v0.7+), Transfer event layout:
      keys[0] = TRANSFER_KEY
      keys[1] = from_address (felt252)
      keys[2] = to_address (felt252)
      data[0] = amount.low (felt252)
      data[1] = amount.high (felt252)

    Older contracts (OZ v0.6) put from/to in data instead:
      keys[0] = TRANSFER_KEY
      data[0] = from
      data[1] = to
      data[2] = amount.low
      data[3] = amount.high
    """
    all_events = []
    token_str  = continuation_token
    page       = 1

    while True:
        params = [{
            'from_block':  {'block_number': from_block_number},
            'to_block':    'latest',
            'address':     contract_address,
            'keys':        [[TRANSFER_KEY]],
            'chunk_size':  CHUNK_SIZE,
        }]
        if token_str:
            params[0]['continuation_token'] = token_str

        result = rpc('starknet_getEvents', params)
        events = result.get('events', [])
        all_events.extend(events)
        token_str = result.get('continuation_token')
        print(f"    Page {page}: {len(events)} events (total: {len(all_events)})")

        if not token_str:
            break

        page += 1
        time.sleep(0.2)

    return all_events, token_str


def parse_transfer(event):
    """
    Parse a Transfer event, handling both OZ v0.6 (from/to in data) and
    v0.7+ (from/to in keys[1]/keys[2]).
    Returns (from_addr_int, to_addr_int, amount_int) or None on parse error.
    """
    keys = event.get('keys', [])
    data = event.get('data', [])

    try:
        if len(keys) >= 3:
            # OZ v0.7+: keys = [selector, from, to], data = [low, high]
            from_addr = felt_to_int(keys[1])
            to_addr   = felt_to_int(keys[2])
            low       = felt_to_int(data[0]) if len(data) > 0 else 0
            high      = felt_to_int(data[1]) if len(data) > 1 else 0
        elif len(keys) == 1 and len(data) >= 4:
            # OZ v0.6: keys = [selector], data = [from, to, low, high]
            from_addr = felt_to_int(data[0])
            to_addr   = felt_to_int(data[1])
            low       = felt_to_int(data[2])
            high      = felt_to_int(data[3])
        else:
            return None

        amount = low + (high << 128)
        return from_addr, to_addr, amount
    except Exception:
        return None


# ── Supply & holders reconstruction ───────────────────────────────────────────

def build_daily_snapshots(events, balances, decimals, block_ts_cache):
    """
    Apply Transfer events to balances, grouped by date, emitting daily snapshots.
    block_ts_cache: {block_number: timestamp_int} — shared across calls.
    """
    # Collect unique block numbers we need timestamps for
    missing_blocks = {e['block_number'] for e in events if e['block_number'] not in block_ts_cache}
    if missing_blocks:
        print(f"    Fetching timestamps for {len(missing_blocks)} blocks…")
        for bn in sorted(missing_blocks):
            block_ts_cache[bn] = get_block_timestamp(bn)
            time.sleep(0.05)

    events_by_date = defaultdict(list)
    for event in events:
        ts   = block_ts_cache.get(event['block_number'], 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        events_by_date[date].append(event)

    supply_history  = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        for event in events_by_date[date]:
            parsed = parse_transfer(event)
            if parsed is None:
                continue
            from_addr, to_addr, amount = parsed
            zero = 0  # mint from 0x0, burn to 0x0
            if from_addr != zero:
                balances[from_addr] = balances.get(from_addr, 0) - amount
            if to_addr != zero:
                balances[to_addr] = balances.get(to_addr, 0) + amount

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

def process_token(token_id, contract_address, currency, fx_rates_all, block_ts_cache):
    print(f'\n[{token_id.upper()} on STARKNET — {contract_address[:14]}…]')

    state_file   = f'data/{token_id}_{CHAIN_NAME}_state.json'
    mcap_file    = f'data/{token_id}_{CHAIN_NAME}_marketcap.json'
    holders_file = f'data/{token_id}_{CHAIN_NAME}_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    decimals = get_token_decimals(contract_address)
    print(f'  Decimals: {decimals}')

    has_state = existing_mcap and state.get('last_block') is not None
    if has_state:
        # Incremental: state file exists with last_block — fetch only new events
        saved_token = state.get('continuation_token')  # None = pagination exhausted
        balances    = {int(k): int(v) for k, v in state.get('balances', {}).items()}
        from_block  = state['last_block']

        if saved_token:
            print(f'  Resuming pagination (continuation_token present)…')
            new_events, _ = fetch_transfer_events(contract_address, continuation_token=saved_token)
        else:
            # Pagination exhausted in last run — fetch from last known block
            print(f'  Incremental from block {from_block}…')
            new_events, _ = fetch_transfer_events(contract_address, from_block_number=from_block)

        print(f'  {len(new_events)} new events')

        if not new_events:
            print('  No new events — carrying forward today.')
            merged_raw  = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]
            merged_hold = list(existing_holders)
            last_block  = from_block
        else:
            # Determine cut date for merging
            first_new_bn   = new_events[0]['block_number']
            if first_new_bn not in block_ts_cache:
                block_ts_cache[first_new_bn] = get_block_timestamp(first_new_bn)
            first_new_date = datetime.fromtimestamp(block_ts_cache[first_new_bn], tz=timezone.utc).strftime('%Y-%m-%d')

            new_supply, new_holders = build_daily_snapshots(new_events, balances, decimals, block_ts_cache)

            kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
            kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
            merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
            merged_hold  = kept_holders + new_holders
            last_block   = new_events[-1]['block_number']

    else:
        print('  Full fetch from genesis…')
        balances   = {}
        new_events, _ = fetch_transfer_events(contract_address, from_block_number=0)
        print(f'  Total: {len(new_events)} Transfer events')

        if not new_events:
            print('  No events found — token may not be active on Starknet yet.')
            return

        merged_raw, merged_hold = build_daily_snapshots(new_events, balances, decimals, block_ts_cache)
        last_block = new_events[-1]['block_number'] if new_events else 0

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
        'last_block':          last_block,
        'continuation_token':  None,  # exhausted — next run uses from_block
        'balances':            {str(k): str(v) for k, v in balances.items()},
    })
    save_json(mcap_file,    mcap_history)
    save_json(holders_file, merged_hold)
    print(f'  Saved: {len(mcap_history)} mcap pts, {len(merged_hold)} holder pts, last block {last_block}')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs('data', exist_ok=True)
    print(f'=== Fetching Spiko Starknet data ===')

    fx_rates_all   = load_json('data/fx_rates.json', default={})
    block_ts_cache = {}  # shared across tokens to avoid redundant RPC calls

    for token_id, (contract_address, currency) in TOKENS.items():
        try:
            process_token(token_id, contract_address, currency, fx_rates_all, block_ts_cache)
        except Exception as e:
            print(f'  ERROR for {token_id}: {e}')
        time.sleep(0.5)

    print('\n=== Done — Starknet ===')


if __name__ == '__main__':
    main()
