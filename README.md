# Solana Ecosystem Pulse

`solana-ecosystem-pulse` is a read-only, dependency-free collector and dashboard for the current Solana mainnet-beta state. It produces three synchronized artifacts:

- `report.json` for machines and downstream agents;
- `report.md` for a concise human report; and
- `dashboard.html`, a self-contained dark-theme dashboard with no CDN or API-key dependency.

The collector never signs, sends, or simulates a transaction. It does not request a wallet, private key, API key, login, or user funds.

## Run

Requires Python 3.10+ and network access to public endpoints.

```sh
python3 report.py --out-dir out
open out/dashboard.html
```

For unattended refreshes:

```sh
python3 report.py --out-dir out --interval 900
```

Use a custom RPC endpoint without changing the code:

```sh
SOLANA_RPC_URL=https://your-read-only-rpc.example python3 report.py --out-dir out
```

To use only Solana RPC and skip optional off-chain sources:

```sh
python3 report.py --out-dir out --no-offchain
```

## Data sources

The Solana RPC collector calls `getHealth`, `getSlot`, `getBlockHeight`, `getBlockTime`, `getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`, and `getSupply`. The default endpoint is the public Solana mainnet-beta RPC.

Optional best-effort enrichments use public, unauthenticated endpoints from CoinGecko, DeFiLlama, and Stablecoins by DeFiLlama. A source can be rate-limited or unavailable; each source's status is retained in `report.json` and missing values remain `n/a`. X/Twitter data is intentionally not scraped because the official API requires credentials.

## Metrics and signals

- Network: health, slot, block height, epoch progress, and block time.
- Performance: recent TPS and slot time from `getRecentPerformanceSamples`.
- Validators: active/delinquent counts, delinquency rate, activated stake, top ten stake share, and commission.
- Economics: SOL price, Solana TVL, DEX volume, and stablecoin supply when optional sources respond.
- Signals: a watch flag for a 25% TPS or slot-time shift against the median of the previous samples, a 1% validator-delinquency threshold, and an RPC-health error.

Signals are screening aids, not investment advice or proof of a protocol incident. The report preserves source failures instead of guessing.

## Test

```sh
python3 -m unittest -v
```

## License

MIT. See `LICENSE`.
