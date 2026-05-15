"""
Fetch supply & holder history for Spiko tokens on Etherlink (tezos L2).
Uses the Blockscout v2 REST API (cursor-based pagination, no cap issues).

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

BLOCKSCOUT_V2   = "https://explorer.etherlink.com/api/v2"
CHAIN_NAME      = "etherlink"
ZERO_ADDRESS    = "0x0000000000000000000000000000000000000000"
LOOKBACK_BLOCKS = 5000

TOKENS = {
    'eutbl':   ('0xa0769f7a8fc65e47de93797b4e21c073c117fc80', 'EUR'),
    'ustbl':   ('0xe4880249745eac5f1ed9d8f7df844792d560e750', 'USD'),
    'uktbl':   ('0x970e2adc2fdf53aea6b5fa73ca6dc30eafedfe3d', 'GBP'),
    'safo':    ('0x5677a4dc7484762ffCCEe13cbA20b5c979DeF446', 'USD'),
    'eursafo': ('0x35DFEC1813C43d82E6B87c682F560bbB8EA0C121', 'EUR'),
    'gbpsafo': ('0xFE20eBe3881491b2e158b9D10cB95bcFa652262D', 'GBP'),
    'chfsafo': ('0xEf53E7D17822B641C6481837238A64A688709301', 'CHF'),
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

def fill_daily_gaps(series, value_key):
    """Forward-fill missing dates so there are no gaps in daily series."""
    if not series:
        return series
    from datetime import timedelta
    result = []
    by_date = {pt['date']: pt for pt in series}
    d = datetime.strptime(series[0]['date'], '%Y-%m-%d').date()
    end = datetime.strptime(series[-1]['date'], '%Y-%m-%d').date()
    last_val = None
    while d <= end:
        ds = d.strftime('%Y-%m-%d')
        if ds in by_date:
            result.append(by_date[ds])
            last_val = by_date[ds][value_key]
        elif last_val is not None:
            result.append({'date': ds, value_key: last_val})
        d += timedelta(days=1)
    return result


# ── Blockscout v2 helpers ──────────────────────────────────────────────────────

def get_token_info(token_address):
    """Return (decimals, holders_count, total_supply_raw) from Blockscout v2."""
    resp = requests.get(f"{BLOCKSCOUT_V2}/tokens/{token_address}", timeout=30)
    if resp.ok:
        d = resp.json()
        dec     = int(d.get('decimals') or 18)
        holders = int(d.get('holders_count') or 0)
        supply  = int(d.get('total_supply')  or 0)
        return dec, holders, supply
    return 18, None, None


# ── Transfer fetching (v2 cursor pagination) ───────────────────────────────────

def fetch_transfers_v2(token_address, from_block=0):
    """
    Fetch all ERC-20 Transfer events via Blockscout v2 REST API.
    Uses cursor-based pagination (block_number + index) — no 100-result cap issues.
    Returns list of normalised transfer dicts sorted ascending by (block_number, log_index).
    If from_block > 0, only transfers with block_number > from_block are returned.
    """
    url = f"{BLOCKSCOUT_V2}/tokens/{token_address}/transfers"
    all_items = []
    params    = {}
    page      = 0

    while True:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data  = resp.json()
        items = data.get('items', [])

        if not items:
            break

        page += 1
        stop = False

        for item in items:
            bn = item.get('block_number', 0)
            if bn <= from_block:
                # Items are newest-first; once we pass from_block we're done
                stop = True
                break
            from_obj  = item.get('from')  or {}
            to_obj    = item.get('to')    or {}
            total_obj = item.get('total') or {}
            all_items.append({
                'block_number': bn,
                'log_index':    item.get('log_index', 0),
                'from_addr':    from_obj.get('hash', '').lower(),
                'to_addr':      to_obj.get('hash', '').lower(),
                'amount':       int(total_obj.get('value', '0') or '0'),
                'timestamp':    item.get('timestamp', ''),
            })

        last_bn = items[-1].get('block_number', 0) if items else 0
        print(f"    page {page}: {len(items)} items (last block {last_bn}, kept {len(all_items)} total)")

        if stop:
            break

        next_page = data.get('next_page_params')
        if not next_page:
            break

        params = next_page
        time.sleep(0.25)

    # Return in chronological order
    all_items.sort(key=lambda x: (x['block_number'], x['log_index']))
    return all_items


# ── Supply & holders reconstruction ───────────────────────────────────────────

def build_daily_snapshots(transfers, balances, decimals):
    """Replay Transfer events to build daily supply & holder-count series."""
    events_by_date = defaultdict(list)

    for t in transfers:
        ts_str = t.get('timestamp', '')
        try:
            date = ts_str[:10]   # 'YYYY-MM-DD' prefix of ISO timestamp
            datetime.strptime(date, '%Y-%m-%d')  # validate
        except Exception:
            continue
        events_by_date[date].append(t)

    supply_history  = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        day_events = sorted(events_by_date[date],
                            key=lambda x: (x['block_number'], x['log_index']))
        for t in day_events:
            fa = t['from_addr']
            ta = t['to_addr']
            amt = t['amount']
            if fa and fa != ZERO_ADDRESS:
                balances[fa] = balances.get(fa, 0) - amt
            if ta and ta != ZERO_ADDRESS:
                balances[ta] = balances.get(ta, 0) + amt

        supply  = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        holders = sum(1 for v in balances.values() if v > 0)
        supply_history.append({'date': date, 'supply': supply})
        holders_history.append({'date': date, 'holders': holders})

    return supply_history, holders_history


# ── FX rates & marketcap ───────────────────────────────────────────────────────

def compute_marketcap(supply_history, currency, fx_rates, nav_lookup=None):
    result    = []
    last_rate = None
    last_nav  = None
    for item in supply_history:
        date   = item['date']
        supply = item['supply']
        if nav_lookup and date in nav_lookup:
            last_nav = nav_lookup[date]
        nav = last_nav if last_nav is not None else 1.0

        if currency == 'USD':
            result.append({'date': date, 'marketcap': round(supply * nav, 2), 'supply': round(supply, 2)})
        else:
            if date in fx_rates:
                last_rate = fx_rates[date]
            if last_rate is None:
                continue
            result.append({'date': date, 'marketcap': round(supply * nav * last_rate, 2), 'supply': round(supply, 2)})
    return result


# ── Per-token processing ───────────────────────────────────────────────────────

def process_token(token_id, token_address, currency, fx_rates_all, nav_lookup=None):
    print(f'\n[{token_id.upper()} on ETHERLINK — {token_address[:10]}…]')

    state_file   = f'data/{token_id}_{CHAIN_NAME}_state.json'
    mcap_file    = f'data/{token_id}_{CHAIN_NAME}_marketcap.json'
    holders_file = f'data/{token_id}_{CHAIN_NAME}_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    decimals, api_holders_now, api_supply_now = get_token_info(token_address)
    print(f'  Decimals: {decimals}  |  API holders: {api_holders_now}  |  API supply raw: {api_supply_now}')

    if state.get('last_block') and existing_mcap:
        last_block = int(state['last_block'])
        balances   = {k: int(v) for k, v in state.get('balances', {}).items()}
        from_block = max(0, last_block - LOOKBACK_BLOCKS)
        print(f'  Incremental: fetching transfers after block {from_block} (last known: {last_block})')

        new_transfers = fetch_transfers_v2(token_address, from_block=from_block)
        truly_new = [t for t in new_transfers if t['block_number'] > last_block]
        print(f'  {len(new_transfers) - len(truly_new)} overlap, {len(truly_new)} truly new')

        if not truly_new:
            print('  No new transfers — carrying forward today.')
            merged_raw  = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]
            merged_hold = list(existing_holders)
            new_last_block = last_block
        else:
            first_new_date = truly_new[0]['timestamp'][:10]

            new_supply, new_holders = build_daily_snapshots(truly_new, balances, decimals)

            kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
            kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
            merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
            merged_hold  = kept_holders + new_holders
            new_last_block = max(t['block_number'] for t in new_transfers)

    else:
        print('  Full fetch from genesis…')
        balances      = {}
        all_transfers = fetch_transfers_v2(token_address, from_block=0)
        print(f'  Total: {len(all_transfers)} Transfer events')

        if not all_transfers:
            print('  No events found — token may not be active on Etherlink yet.')
            return

        merged_raw, merged_hold = build_daily_snapshots(all_transfers, balances, decimals)
        new_last_block = max(t['block_number'] for t in all_transfers)

    if not merged_raw:
        print('  No supply data.')
        return

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if merged_raw[-1]['date'] < today:
        current_supply  = sum(v for v in balances.values() if v > 0) / (10 ** decimals)
        current_holders = sum(1 for v in balances.values() if v > 0)
        merged_raw.append({'date': today, 'supply': round(current_supply, 7)})
        merged_hold.append({'date': today, 'holders': current_holders})

    # ── Override today's snapshot with live API data ───────────────────────────
    # The Spiko EUTBL token (and potentially others) uses non-standard Transfer
    # semantics: transfers to the redemption address emit an event but do NOT
    # change balanceOf. Event-based reconstruction therefore undercounts holders
    # and supply. We override the current-day entry with the values read directly
    # from the contract via the Blockscout API (holders_count, total_supply).
    if api_holders_now is not None and api_supply_now is not None:
        api_supply = round(api_supply_now / (10 ** decimals), 7)
        if merged_raw  and merged_raw[-1]['date']  == today:
            merged_raw[-1]['supply']    = api_supply
        if merged_hold and merged_hold[-1]['date'] == today:
            merged_hold[-1]['holders']  = api_holders_now
        print(f'  API override today: {api_holders_now} holders, supply {api_supply}')

    merged_raw  = fill_daily_gaps(merged_raw,  'supply')
    merged_hold = fill_daily_gaps(merged_hold, 'holders')

    fx_rates     = fx_rates_all.get(currency, {}) if currency != 'USD' else {}
    mcap_history = compute_marketcap(merged_raw, currency, fx_rates, nav_lookup=nav_lookup)

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

    fx_rates_all    = load_json('data/fx_rates.json',    default={})
    nav_history_all = load_json('data/nav_history.json', default={})

    def build_nav_lookup(tid):
        s = nav_history_all.get(tid)
        return {e['date']: e['nav'] for e in s} if s else None

    for token_id, (token_address, currency) in TOKENS.items():
        try:
            process_token(token_id, token_address, currency, fx_rates_all,
                          nav_lookup=build_nav_lookup(token_id))
        except Exception as e:
            print(f'  ERROR for {token_id}: {e}')
        time.sleep(0.5)

    print('\n=== Done — Etherlink ===')


if __name__ == '__main__':
    main()
