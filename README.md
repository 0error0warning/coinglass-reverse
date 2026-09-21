# CoinGlass Reverse Engineering Toolkit

Free access to CoinGlass liquidation heatmap data via internal API reverse engineering.

## Features

- **Multi-coin**: BTC, ETH, SOL, HYPE, DOGE, XRP, BNB, WIF, LTC, ADA, NEAR, ENA, LINK (19+ coins)
- **Multi-exchange**: Binance, Bybit, OKX, Bitget, Gate (5 exchanges)
- **Multi-timeframe**: 5m, 15m, 30m, 2h, 6h, 12h, 24h, 1d (8 intervals)
- **Structured data**: Raw `[x_idx, y_idx, amount_usd]` + `y_axis` + `prices` — same as official API
- **Zero cost**: No API key, no subscription, no browser required

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  CoinGlass Web  │────▶│  capi.coinglass  │────▶│  AES-128-ECB   │
│  (React SPA)    │     │  /api/index/v3   │     │  + gzip + TOTP │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                                               │
         │                                               ▼
         │                                        ┌─────────────────┐
         │                                        │  coinglass-     │
         │                                        │  decrypt.py     │
         │                                        │  (this repo)    │
         │                                        └─────────────────┘
         │                                               │
         ▼                                               ▼
┌─────────────────┐                            ┌─────────────────┐
│  Canvas/ECharts │                            │  Structured JSON│
│  (rendering)    │                            │  {liq, y, prices}│
└─────────────────┘                            └─────────────────┘
```

## Quick Start

```bash
pip install -r requirements.txt
```

```python
from coinglass_decrypt import fetch_and_decrypt
from market_scan import coinglass_heatmap

# Get BTC liquidation heatmap (Binance, 5min interval)
h = coinglass_heatmap('BTC', 'Binance', '5', 288)
print(f"Spot: ${h['spot']:,.0f}")
print(f"Top support: {h['top_below'][:3]}")
print(f"Top resistance: {h['top_above'][:3]}")
```

## Data Structure

```json
{
  "instrument": {"instrumentId": "BTCUSDT", "exName": "Binance"},
  "liq": [[0, 1, 1666629.61], ...],  // [x_idx, y_idx, amount_usd]
  "y": [77507.89, 77586.88, ...],     // price axis
  "prices": [[1789917300, "80720.3", "80855.5", "80720.2", "80795.2", "35855198.1842"], ...],
  "rangeHigh": 95596.5,
  "rangeLow": 71899.6
}
```

## Capability Matrix

| Coin | Binance | Bybit | OKX | Bitget | Gate |
|------|---------|-------|-----|--------|------|
| BTC  | ✅      | ✅    | ✅  | ✅     | ✅   |
| ETH  | ✅      | ✅    | ✅  | ✅     | ✅   |
| SOL  | ✅      | ✅    | ✅  | ✅     | ✅   |
| UNI  | ⚠️      | ⚠️    | ⚠️  | ⚠️     | ⚠️   |
| ZEC  | ⚠️      | ⚠️    | ⚠️  | ⚠️     | ⚠️   |
| TAO  | ⚠️      | ⚠️    | ⚠️  | ⚠️     | ⚠️   |
| SUI  | ✅      | ✅    | ✅  | ✅     | ✅   |
| HYPE | ✅      | ✅    | ✅  | ✅     | ✅   |
| DOGE | ✅      | ✅    | ✅  | ✅     | ✅   |
| XRP  | ✅      | ✅    | ✅  | ✅     | ✅   |
| BNB  | ✅      | ✅    | ✅  | ✅     | ✅   |
| ARB  | ⚠️      | ⚠️    | ⚠️  | ⚠️     | ⚠️   |
| WIF  | ✅      | ✅    | ✅  | ✅     | ✅   |
| LTC  | ✅      | ✅    | ✅  | ✅     | ✅   |
| ADA  | ✅      | ✅    | ✅  | ✅     | ✅   |
| NEAR | ✅      | ✅    | ✅  | ✅     | ✅   |
| ENA  | ✅      | ✅    | ✅  | ✅     | ✅   |
| LINK | ✅      | ✅    | ✅  | ✅     | ✅   |

⚠️ = intermittent (some intervals may return empty)

| Interval | Stability | Notes |
|----------|-----------|-------|
| `5` (5min) | ⚠️ | BTC stable, others intermittent |
| `15` (15min) | ⚠️ | BTC stable, others intermittent |
| `30` (30min) | ⚠️ | BTC only |
| `h2` (2h) | ⚠️ | BTC only |
| `h6` (6h) | ✅ | Most coins stable |
| `h12` (12h) | ✅ | Most coins stable |
| `h24` (24h) | ✅ | Most coins stable |
| `d1` (1d) | ✅ | Most coins stable |

## Authentication

CoinGlass uses **TOTP + AES-128-ECB** for request signing:

1. Generate TOTP code (secret: `I65VU7K5ZQL7WB4E`, step: 30s)
2. Plaintext: `{unix_timestamp},{totp_code}`
3. AES-128-ECB encrypt with key `1f68efd73f8d4921acc0dead41dd39bc`
4. Base64 encode → `data` parameter

Response uses **AES-128-ECB + gzip** for encryption:
1. `Base64(path)[:16]` → first layer key
2. Decrypt `user` header → gunzip → real AES key
3. Decrypt `data` → gunzip → JSON

## Files

| File | Purpose |
|------|---------|
| `coinglass_decrypt.py` | Core decryption (AES+TOTP+gzip) |
| `market_scan.py` | Full market data aggregator (10 blocks) |
| `liq_collector.py` | Real-time liquidation stream collector |
| `premium_collector.py` | Spot-future premium collector |
| `requirements.txt` | Dependencies |

## Limitations

- `data` parameter expires every 30s (TOTP-based)
- Some coins have intermittent coverage (UNI/ZEC/TAO)
- No official documentation — parameters may change
- Rate limits unknown (use responsibly)

## Related

- [xeronsh/coinglass-decrypt](https://github.com/xeronsh/coinglass-decrypt) — Original decryption research
- [CoinGlass API Docs](https://docs.coinglass.com) — Official paid API reference
