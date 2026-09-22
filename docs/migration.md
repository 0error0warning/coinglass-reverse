# Migration: explicit units and schema v2

## Do not reuse mixed-unit liquidation files blindly

Old files stored `qty` without its unit and used local receipt time. Some OKX values are contracts, other exchange values are base coins. An old timestamp cannot recover the original exchange timestamp. Rewriting every old record with a new schema version does not repair these problems.

1. Stop your existing collector using your deployment's service manager (not performed by this patch).
2. Preserve the old directory read-only for historical inspection.
3. Configure `LIQ_STATE_DIR` to a **new directory** and start the corrected collector explicitly.
4. Point the scanner to the same new directory.
5. Validate source subscription ACK/error state, instrument metadata and fresh typed events. No events in a quiet interval do not alone indicate failure.

The reader excludes untyped legacy data. The collector must refuse to overwrite legacy/corrupt files and retain pending events on a flush error; use a clean path rather than losing or guessing old data.

Operational health is written atomically to `LIQ_STATE_DIR/_health/status.json`. The `_health` subdirectory is deliberate so scanners that read root `*.json` symbol event files never mistake health for market data. Use this health file to check whether each source is connected and acknowledged, whether errors are increasing, whether genuine events have been accepted, and whether persistence is flushing. `last_accepted_event_ms` is absent/null until a real normalized event is recorded; it is not fabricated during quiet market periods.

## Event contract

- `schema_version`: 2 on the file and event.
- `symbol`, `instrument`, `ex`, `base_asset`, `quote_asset`: preserve source identity.
- `raw_qty`, `raw_unit`: what the venue sent; never discard contract counts.
- `base_qty`: normalized base-asset quantity using verified contract metadata where necessary.
- `exchange_ts_ms`, `received_ts_ms`: separate millisecond times. The reader uses validated exchange time and excludes future/expired events.
- `liquidated_side`: explicitly `long` or `short`, not generic order `BUY/SELL`.
- `price_kind`: venue-specific semantics; Bybit bankruptcy price and OKX liquidation transfer price are not ordinary spot/execution prices.
- `quantity_kind`, `additive`, `coverage`: state what can be totaled and what the feed omits.
- `event_id`: deterministic observation identity for duplicate/replay suppression, not an invented exchange order identifier.

An exact repeated observation is deduplicated across flush/restart. Without upstream unique IDs, two genuinely distinct events identical on all observed fields cannot always be distinguished; no lossless/full-ledger claim is made.

### Binance snapshots

`q/p` are original order quantity/price. The normalized observation uses `z/ap` (cumulative filled size/average price) with trade time `T`. `st=2` inverse/coin-margined instruments must not be interpreted as base quantity without contract metadata.

The public force-order stream samples the latest liquidation order per symbol per interval. It does not supply a universally reliable order ID for incremental reconstruction. Cumulative filled snapshots are therefore **non-additive**: they are retained for inspection but excluded from aggregate liquidation quantities. This intentionally reduces the apparent total instead of publishing an invented economic sum. Deriving incremental fills is deferred until a verifiable identity contract exists.

### OKX and Bybit

OKX swap `sz` is contracts; normalize through `ctVal/ctMult/ctValCcy` and the linear/inverse contract definition. Missing/unknown metadata is an explicit exclusion, never a 1:1 conversion. `posSide=net` requires order side to infer which position was closed.

Bybit `allLiquidation.*` has array data. Its `S` is position side (`Buy` means long liquidated), unlike an exchange's forced execution order side. A shared generic BUY→short rule is unsafe.

Even additive observations are only the events covered by that upstream feed. Bucket units are base assets, not assumed USD; source prices remain venue-specific liquidation prices.

## Other compatibility changes

- `collect()` / CLI JSON now use source envelopes: `status`, `data/error`, `as_of`. Update code that directly indexed `report['cg_heatmap']['by_price']` to inspect status then `report['cg_heatmap']['data']['by_price']`.
- `spot` and `sentiment` use explicit collection status aggregation: all usable rows are `ok`, usable rows mixed with failed/no-data rows are `partial`, all empty/no-data rows are `no_data`, and all failed/no usable rows are `error`. CLI exits nonzero for `error` or `partial`; `not_configured` remains an optional skip.
- `coinglass_heatmap()` retains the first four positional inputs. Current model 1 is default; select legacy explicitly if required. Latest-slice profile replaces historical summation. Raw response and provenance are preserved.
- `spot` in the heatmap remains only a documented compatibility alias for contract candle close.
- API business/transport/schema errors are explicit exceptions, not silently `None` or successful error dictionaries.
- State paths default to portable `~/.local/state/coinglass/{liq_map,premium}`. Existing private-host paths are not automatically read or migrated. Use environment overrides deliberately.
- The premium database keeps its existing six-column format and **unadjusted USD-spot/USDT-perpetual definition**. Its 30-day retention now runs on every collection cycle. There is no silent change to same-currency spot-vs-spot data.
- `--etf` actually runs only the Farside ETF source. CoinGlass ETF values use a different unit contract.
- Deribit `max_oi` is the honest default expiry policy; request `nearest` explicitly. No gamma/dealer-exposure claim remains.

No service restart, production data rewrite, credential installation or trading operation is part of this change.
