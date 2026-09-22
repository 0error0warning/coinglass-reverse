# CoinGlass Reverse Engineering Toolkit

Read-only adapters for selected CoinGlass web data, plus a multi-source market scanner and optional liquidation/premium collectors. Internal website APIs are **undocumented, versioned and permission/rate-limit dependent**. This is not a complete CoinGlass SDK, an official API equivalent, or a trading signal service.

## Install and test

Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

Tests are offline and use synthetic fixtures. Public live checks are opt-in; no account keys are needed for the tested anonymous CoinGlass paths. A ticker search result does not guarantee heatmap access. Login/Prime gates must not be treated as unsupported symbols or bypassed.

## Heatmap quick start

```python
from market_scan import coinglass_heatmap
from coinglass_decrypt import CoinGlassError

try:
    h = coinglass_heatmap(
        "BTC", "Binance", model=1, scope="pair", window="24h",
        original_symbol="BTCUSDT",  # exact exchange instrument, not a guessed suffix
    )
    print("Contract candle close:", h["reference_contract_price"])
    print("Latest-slice price clusters:", h["top_below"][:3], h["top_above"][:3])
    print("Raw cells:", len(h["raw"]["liq"]))
    print("Provenance:", h["metadata"])
except CoinGlassError as exc:
    print("Data unavailable:", exc.category, exc.code)
```

Omit `original_symbol` to resolve the exchange/quote instrument from CoinGlass's public ticker metadata. Ambiguous or missing matches fail explicitly; no fallback manufactures `COINUSDT`. Models 1/2/3 and pair/aggregate scopes use separate endpoints. Legacy v3 is an explicit compatibility mode, not a numbered current model.

- `by_price` and `top_above/top_below` use **only the final time slice**, not sums of historical snapshots.
- `raw` preserves the original axes, candles, cells and instrument metadata.
- `spot` is only a deprecated alias for the selected contract's final candle close. It is **not a separately fetched spot/index/mark price**.
- Heatmap values are **estimated liquidation intensity with an unverified unit**, not observed liquidation executions or guaranteed USD notional. Price clusters are not proven support/resistance.
- `window` cannot be combined with explicit `interval`/`limit`. Positional `(symbol, exchange, interval, limit)` remains available for granular requests; no silent model fallback.

### Website window presets

| Window | Model 1/2 interval | limit | Model 3 range |
|---|---|---:|---|
| 12h | 5 | 144 | 12h |
| 24h | 5 | 288 | 24h |
| 48h | 15 | 192 | 48h |
| 3d | 15 | 288 | 72h |
| 1w | 30 | 336 | 7d |
| 2w | 30 | 672 | 14d |
| 1mo | h2 | 372 | 30d |
| 3mo | h6 | 360 | 90d |
| 6mo | h12 | 360 | 180d |
| 1y | h24 | 360 | 365d |
| 2y | d1 | 720 | 720d |

Presets reproduce website requests, not exact calendar periods or guaranteed access. Model 3 uses `range` and `cp=false`, not interval/limit. Color palettes, liquidity threshold and chart style are display-only controls, not invented API parameters.

## Scanner CLI

```bash
python market_scan.py BTC --json
python market_scan.py --etf --json
python market_scan.py BTC --source cg_heatmap --model 2 --window 48h --json
python market_scan.py BTC --source cg_heatmap --model 3 --scope aggregate --window 3d
python market_scan.py ETH --source premium --source deribit
```

JSON reports have `schema_version=2`; each source has `status`, `data` or a typed `error`, and `as_of`. A failing source does not erase the other sources. `as_of` records the observation attempt, not necessarily the upstream data update time. Missing data is not zero. Nonzero CLI exit status indicates one or more failed sources, while JSON remains readable.

`--etf` selects only the **Farside BTC ETF HTML table (USD millions)**. This is distinct from `cg_etf_flow`, whose raw `changeUsd` is already USD. Other source names are listed in `--help`.

Coinalyze is optional: set `COINALYZE_KEY` or an explicitly chosen `MARKET_API_KEY_FILE`. Nothing automatically reads a particular user's secret directory. Liquidation/open-interest histories request USD conversion; unavailable quantities remain unknown.

## Units and scope

- **ETF:** CoinGlass `changeUsd` is USD; `change` is asset quantity. `617600000` → `617.60M` is formatting only. Missing issuer fields are not confirmed zero. BTC and ETH use different endpoints.
- **Funding:** a raw value of `0.01` means `0.01%`. Linear annualization needs the actual settlement interval; sampling `m5` is not a five-minute funding settlement. It is not realized/compound yield.
- **Open interest:** `currency=USD/BTC` requests denomination, not collateral type. Do not convert a historical series with today's price or assume a single chart price reconstructs all venue values exactly.
- **Long/short:** taker volume, account shares and position ratios are distinct, not counts of people.
- **Options:** Deribit output is OI distribution, not gamma exposure/dealer positioning. `deribit_walls(expiry_policy="max_oi")` honestly selects maximum OI; `nearest` and an explicit `expiry` are alternatives.
- **Premium:** Coinbase USD spot minus Binance USDT perpetual is an **unadjusted cross-market, cross-quote difference**, not identified ETF/geographic buying. Existing stored values retain that definition.
- **Time:** heatmap candle times are seconds, update times may be milliseconds; Fear & Greed uses milliseconds, AHR dates are strings. Do not globally multiply every timestamp by 1000.
- **VPVR:** uniform allocation across hourly OHLC ranges is an approximation, not executed volume-at-price.

See [migration and event contracts](docs/migration.md) and [audit remediation](docs/audit-remediation.md). Named helpers cover selected verified routes only. All 413 discovered site routes are **not** implemented or behaviorally verified.

## Optional collectors

Collectors are never started on import or installation:

```bash
LIQ_STATE_DIR=/path/to/new-liq-state python liq_collector.py
PREMIUM_STATE_DIR=/path/to/premium-state python premium_collector.py
```

Default locations are under `~/.local/state/coinglass/`. Set paths explicitly when migrating a deployment. No automatic service installation or production migration is included.

Liquidation schema v2 stores raw units, normalized base quantity, exchange/receive times, position direction and price/quantity semantics. **Legacy mixed-unit files must not be silently converted.** Binance cumulative filled snapshots without reliable order identity are retained as non-additive observations and excluded from economic totals; sampled feeds cannot prove a complete liquidation ledger. OKX quantities require instrument metadata; unknown contracts fail closed. See the migration guide before pointing a new collector at an existing directory.

## Protocol and verification boundaries

Request signing uses a six-digit TOTP string and **AES-256-ECB** (32 UTF-8 key bytes). Response decryption is a separate version-specific AES/gzip process. Business errors, malformed encryption headers and schema failures are not successful market data. Signing constants reproduced from public client code are protocol details, not account credentials.

Offline tests prove the local behavior for specified fixtures, not endpoint availability. Live source access, subscription gates, historical coverage and rates can change. Respect upstream terms and rate limits. No login or payment credentials are included.

Original decryption research: [xeronsh/coinglass-decrypt](https://github.com/xeronsh/coinglass-decrypt). Official reference: [CoinGlass API documentation](https://docs.coinglass.com).
