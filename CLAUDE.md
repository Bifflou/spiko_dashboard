# Spiko Dashboard — Claude instructions

## Git workflow

Always work toward `main`. At the end of every session (or when the user's request is complete):

1. Merge the current worktree branch into `main` with fast-forward:
   ```
   cd E:/Claude/spiko
   git pull --rebase spiko main
   git merge <worktree-branch> --ff-only
   git push spiko main
   git push spiko --delete <worktree-branch>
   ```
2. Delete the remote worktree branch so GitHub doesn't show stale "Compare & pull request" banners.

Never create pull requests — push directly to `main`.

## Data sources

Only use public RPCs and APIs directly:
- Stellar: `api.stellar.expert` (events), Soroban RPC for contract reads
- EVM (Ethereum, Polygon, Base, Arbitrum): Etherscan / Blockscout APIs
- Etherlink: Blockscout API
- Starknet: public Starknet RPC
- FX rates: `api.frankfurter.dev/v1/` — fetched once by `fetch_fx_rates.py` into `data/fx_rates.json`, shared by all fetch scripts

Never call DeFiLlama, rwa.xyz, or other aggregator sites in fetch scripts. Those are only used manually for cross-checking.

## Marketcap calculation

Supply is denominated in the token's native currency (1:1 backing):
- EUR tokens (eutbl, eurspkcc, eursafo): supply in EUR → × EUR/USD from fx_rates.json
- GBP tokens (uktbl, gbpsafo): supply in GBP → × GBP/USD from fx_rates.json
- CHF tokens (chfsafo): supply in CHF → × CHF/USD from fx_rates.json
- USD tokens (ustbl, spkcc, safo): supply = marketcap directly (no FX needed)

## Stellar token decimal precision

All 9 Spiko Stellar tokens are Stellar Asset Contracts (SAC) with **5 decimal places** (`10^5`).
Confirmed 2026-05-10 via Soroban RPC `getLedgerEntries` reading each contract's METADATA storage entry.
`STELLAR_SCALE_DEFAULT = 10 ** 5` in `scripts/fetch_stellar_data.py`.
