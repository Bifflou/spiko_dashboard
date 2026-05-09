"""
Fetch supply & holder history for all Spiko tokens on Stellar.
Spiko tokens are Soroban SEP-41 contracts (C... addresses).

Strategy:
- Events: stellar.expert /contract/{address}/events (cursor pagination)
  Each event has: ts (timestamp), topics (decoded), bodyXdr (i128 amount)
- Event types: transfer, mint, burn, clawback — applied to a balance map
- Supply = sum of positive balances after all events
- Holders = count of addresses with positive balance
- FX: frankfurter.app for EUR/GBP/CHF → USD
"""

import base64
import json
import os
import requests
import struct
import time
from collections import defaultdict
from datetime import datetime, timezone

EXPERT_BASE   = "https://api.stellar.expert"
STELLAR_SCALE = 10 ** 7

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


# ── XDR amount decoding ────────────────────────────────────────────────────────

def decode_amount(body_xdr):
    """
    Decode a Soroban SCVal (base64 XDR) to a Python int.
    SEP-41 token amounts are encoded as SCV_I128 (type=10) or SCV_U128 (type=9).
    Layout: 4 bytes type | 8 bytes hi (int64) | 8 bytes lo (uint64)
    """
    try:
        data = base64.b64decode(body_xdr)
        if len(data) < 4:
            return 0
        val_type = struct.unpack('>I', data[0:4])[0]
        if val_type in (9, 10) and len(data) >= 20:  # U128 or I128
            hi = struct.unpack('>Q', data[4:12])[0]
            lo = struct.unpack('>Q', data[12:20])[0]
            return lo + hi * (2 ** 64)
        if val_type == 5 and len(data) >= 12:  # SCV_U64
            return struct.unpack('>Q', data[4:12])[0]
        if val_type == 6 and len(data) >= 12:  # SCV_I64
            return struct.unpack('>q', data[4:12])[0]
    except Exception:
        pass
    return 0


# ── stellar.expert events ──────────────────────────────────────────────────────

def fetch_all_events(contract_address, cursor=None):
    """
    Fetch all contract events from stellar.expert in ascending order.
    Resumes from `cursor` if provided.
    Returns (events_list, last_paging_token).
    """
    all_events     = []
    last_token     = cursor
    url            = f"{EXPERT_BASE}/explorer/public/contract/{contract_address}/events"
    params         = {'order': 'asc', 'limit': 200}
    if cursor:
        params['cursor'] = cursor

    use_params = True
    page       = 1

    while True:
        try:
            resp = requests.get(url, params=(params if use_params else None), timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f'    [stellar.expert] error page {page}: {e}')
            break

        data    = resp.json()
        records = data.get('_embedded', {}).get('records', [])
        all_events.extend(records)
        if records:
            last_token = records[-1]['paging_token']
        print(f'    Page {page}: {len(records)} events (total: {len(all_events)})')

        next_href = data.get('_links', {}).get('next', {}).get('href')
        if not next_href or not records:
            break

        url        = f"{EXPERT_BASE}{next_href}"
        use_params = False
        page      += 1
        time.sleep(0.15)

    return all_events, last_token


# ── Balance & supply reconstruction ───────────────────────────────────────────

def apply_events_to_balances(events, balances):
    """
    Apply contract events to a balance map {address: raw_int}.
    SEP-41 event types:
      transfer(from, to, amount)  → topics[0]='transfer', [1]=from, [2]=to
      mint(admin, to, amount)     → topics[0]='mint',     [1]=admin,[2]=to
      burn(from, amount)          → topics[0]='burn',     [1]=from
      burn_from(spender,from,amt) → topics[0]='burn_from',[1]=spender,[2]=from
      clawback(admin, from, amt)  → topics[0]='clawback', [1]=admin, [2]=from
    """
    for event in events:
        topics = event.get('topics', [])
        if not topics:
            continue
        etype  = topics[0]
        amount = decode_amount(event.get('bodyXdr', ''))
        if amount <= 0:
            continue

        if etype == 'transfer' and len(topics) >= 3:
            frm = topics[1]; to = topics[2]
            balances[frm] = balances.get(frm, 0) - amount
            balances[to]  = balances.get(to,  0) + amount

        elif etype == 'mint' and len(topics) >= 3:
            to = topics[2]
            balances[to] = balances.get(to, 0) + amount

        elif etype == 'burn' and len(topics) >= 2:
            frm = topics[1]
            balances[frm] = balances.get(frm, 0) - amount

        elif etype == 'burn_from' and len(topics) >= 3:
            frm = topics[2]
            balances[frm] = balances.get(frm, 0) - amount

        elif etype == 'clawback' and len(topics) >= 3:
            frm = topics[2]
            balances[frm] = balances.get(frm, 0) - amount


def build_daily_snapshots(events, balances):
    """
    Apply events chronologically, grouped by date, and emit daily supply/holders.
    """
    events_by_date = defaultdict(list)
    for event in events:
        ts   = event.get('ts', 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        events_by_date[date].append(event)

    supply_history  = []
    holders_history = []

    for date in sorted(events_by_date.keys()):
        apply_events_to_balances(events_by_date[date], balances)
        supply  = sum(v for v in balances.values() if v > 0) / STELLAR_SCALE
        holders = sum(1 for v in balances.values() if v > 0)
        supply_history.append({'date': date, 'supply': round(supply, 7)})
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

def process_token(token_id, contract_address, currency, today, fx_cache):
    print(f'\n[{token_id.upper()} — {contract_address[:12]}…]')

    state_file   = f'data/{token_id}_stellar_state.json'
    mcap_file    = f'data/{token_id}_stellar_marketcap.json'
    holders_file = f'data/{token_id}_stellar_holders.json'

    state            = load_json(state_file, default={})
    existing_mcap    = load_json(mcap_file,    default=[])
    existing_holders = load_json(holders_file, default=[])

    cursor   = state.get('last_paging_token')
    balances = {k: int(v) for k, v in state.get('balances', {}).items()}

    print(f'  Fetching events from stellar.expert (cursor={cursor})…')
    new_events, new_cursor = fetch_all_events(contract_address, cursor=cursor)
    print(f'  {len(new_events)} new events')

    if not new_events and not existing_mcap:
        print('  No events and no existing data — skipping.')
        return

    if new_events:
        first_new_date = datetime.fromtimestamp(
            new_events[0].get('ts', 0), tz=timezone.utc
        ).strftime('%Y-%m-%d')

        new_supply, new_holders = build_daily_snapshots(new_events, balances)

        kept_mcap    = [pt for pt in existing_mcap    if pt['date'] < first_new_date]
        kept_holders = [pt for pt in existing_holders if pt['date'] < first_new_date]
        merged_raw   = [{'date': pt['date'], 'supply': pt['supply']} for pt in kept_mcap] + new_supply
        merged_hold  = kept_holders + new_holders
    else:
        merged_raw  = [{'date': pt['date'], 'supply': pt['supply']} for pt in existing_mcap]
        merged_hold = list(existing_holders)

    if not merged_raw:
        print('  No supply data yet.')
        return

    # FX rates
    if currency not in fx_cache:
        start_date = merged_raw[0]['date']
        if currency == 'USD':
            fx_cache['USD'] = {}
        else:
            print(f'  Fetching {currency}/USD rates from {start_date}…')
            fx_cache[currency] = fetch_fx_rates_for_currency(currency, start_date)

    mcap_history = compute_marketcap(merged_raw, currency, fx_cache.get(currency, {}))

    save_json(state_file, {
        'last_paging_token': new_cursor or cursor,
        'balances':          {k: str(v) for k, v in balances.items()},
    })
    save_json(mcap_file,    mcap_history)
    save_json(holders_file, merged_hold)
    print(f'  Saved: {len(mcap_history)} mcap pts, {len(merged_hold)} holder pts')


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    os.makedirs('data', exist_ok=True)
    today    = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    fx_cache = {}

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
