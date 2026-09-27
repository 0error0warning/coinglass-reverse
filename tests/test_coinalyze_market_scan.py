"""Synthetic Coinalyze market-selection and sentiment-status regressions."""

import pytest

import market_scan


def market(exchange, symbol, base="BTC", quote="USDT", **overrides):
    record = {
        "exchange": exchange,
        "symbol": symbol,
        "symbol_on_exchange": "deliberately-not-used",
        "base_asset": base,
        "quote_asset": quote,
        "is_perpetual": True,
        "margined": "STABLE",
    }
    record.update(overrides)
    return record


@pytest.fixture(autouse=True)
def reset_markets_cache(monkeypatch):
    monkeypatch.setattr(market_scan, "_markets_cache", None)


def test_find_perp_recovers_four_structured_markets(monkeypatch):
    payload = [
        market("A", "BTCUSDT_PERP.A"),
        market("6", "BTCUSDT_PERP.6"),
        market("3", "BTC-USDT-SWAP"),
        market("H", "BTC/USD", quote="USD"),
    ]
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("BTC", "USDT") == {
        "3": "BTC-USDT-SWAP",
        "6": "BTCUSDT_PERP.6",
        "A": "BTCUSDT_PERP.A",
        "H": "BTC/USD",
    }


def test_find_perp_excludes_wrong_base_quote_margin_and_expiry(monkeypatch):
    payload = [
        market("A", "ETHUSDT_PERP.A", base="ETH"),
        market("6", "BTCUSD_PERP.6", quote="USD"),
        market("3", "BTC-USDT-COIN", margined="COIN"),
        market("H", "BTC-USDT-DEC", is_perpetual=False),
    ]
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("BTC", "USDT") == {}


def test_find_perp_usd_fallback_is_hyperliquid_usdt_only(monkeypatch):
    payload = [
        market("A", "BTCUSD_PERP.A", quote="USD"),
        market("3", "BTC-USD-SWAP", quote="USD"),
        market("H", "BTC/USD", quote="USD"),
    ]
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("BTC", "USDT") == {"H": "BTC/USD"}
    assert market_scan.find_perp("BTC", "USDC") == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_find_perp_prefers_exact_hyperliquid_quote_independent_of_order(monkeypatch, reverse):
    payload = [
        market("H", "BTC/USD", quote="USD"),
        market("H", "BTC/USDT", quote="USDT"),
    ]
    if reverse:
        payload.reverse()
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("BTC", "USDT") == {"H": "BTC/USDT"}


@pytest.mark.parametrize("reverse", [False, True])
def test_find_perp_rejects_same_priority_ambiguity_independent_of_order(monkeypatch, reverse):
    payload = [market("3", "BTC-USDT-SWAP"), market("3", "BTC-USDT-PERP")]
    if reverse:
        payload.reverse()
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    with pytest.raises(ValueError, match="ambiguous"):
        market_scan.find_perp("BTC", "USDT")


def test_find_perp_deduplicates_identical_symbol_metadata(monkeypatch):
    duplicate = market("3", "BTC-USDT-SWAP")
    monkeypatch.setattr(market_scan, "cy_get", lambda path: [duplicate, dict(duplicate)])

    assert market_scan.find_perp("BTC", "USDT") == {"3": "BTC-USDT-SWAP"}


def test_find_perp_normalizes_lowercase_query_but_requires_uppercase_metadata(monkeypatch):
    payload = [
        market("A", "BTCUSDT_PERP.A"),
        market("6", "lowercase-metadata", base="btc", quote="usdt"),
    ]
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("btc", "usdt") == {"A": "BTCUSDT_PERP.A"}


def test_find_perp_defensively_ignores_malformed_records(monkeypatch):
    payload = [
        None,
        {},
        {"exchange": [], "symbol": "BAD", "base_asset": "BTC", "quote_asset": "USDT",
         "is_perpetual": True, "margined": "STABLE"},
        market("A", "BTCUSDT_PERP.A"),
    ]
    monkeypatch.setattr(market_scan, "cy_get", lambda path: payload)

    assert market_scan.find_perp("BTC") == {"A": "BTCUSDT_PERP.A"}


def test_find_perp_rejects_malformed_top_level_payload(monkeypatch):
    monkeypatch.setattr(market_scan, "cy_get", lambda path: {"markets": []})

    with pytest.raises(ValueError, match="not a list"):
        market_scan.find_perp("BTC")


def test_sentiment_metadata_distinguishes_no_data_unsupported_and_zero(monkeypatch):
    selected = market("H", "BTC/USD", quote="USD", has_long_short_ratio_data=False)
    monkeypatch.setattr(market_scan, "_markets_cache", [selected])
    monkeypatch.setattr(market_scan, "find_perp", lambda *args: {"H": "BTC/USD"})

    histories = {
        "open-interest-history": [{"o": 10}, {"c": 10}],
        "liquidation-history": [],
        "funding-rate-history": [{"c": 0}],
    }

    def fake_hist(symbol, endpoint, hours):
        assert endpoint != "long-short-ratio-history"
        return histories[endpoint]

    monkeypatch.setattr(market_scan, "hist", fake_hist)
    row = market_scan.scan_sentiment("BTC")[0]

    assert row["oi_chg"] == 0
    assert row["long_liq"] is None and row["short_liq"] is None
    assert row["ls"] is None and row["lsr"] is None
    assert row["fr"] == 0
    assert row["status"] == "ok"
    assert row["metadata"] == {
        "matched_symbol": "BTC/USD",
        "indicator_statuses": {
            "open_interest": "ok",
            "liquidation": "no_data",
            "long_short_ratio": "unsupported",
            "funding": "ok",
        },
    }


def test_sentiment_unknown_capability_allows_restored_long_short_data(monkeypatch):
    monkeypatch.setattr(market_scan, "find_perp", lambda *args: {"A": "BTCUSDT_PERP.A"})
    histories = {
        "open-interest-history": [],
        "liquidation-history": [],
        "long-short-ratio-history": [{"r": 1.0, "l": 50.0, "s": 50.0}],
        "funding-rate-history": [],
    }
    monkeypatch.setattr(market_scan, "hist", lambda symbol, endpoint, hours: histories[endpoint])

    row = market_scan.scan_sentiment("BTC")[0]

    assert row["ls"] == (50.0, 50.0)
    assert row["lsr"] == 1.0
    assert row["metadata"]["indicator_statuses"]["long_short_ratio"] == "ok"


def test_sentiment_indicator_failure_is_error_not_missing_or_zero(monkeypatch):
    monkeypatch.setattr(market_scan, "find_perp", lambda *args: {"3": "BTC-USDT-SWAP"})
    histories = {
        "open-interest-history": [{"o": 5}, {"c": 10}],
        "liquidation-history": [{"l": 0, "s": 0}],
        "long-short-ratio-history": [],
    }

    def fake_hist(symbol, endpoint, hours):
        if endpoint == "funding-rate-history":
            raise TimeoutError("synthetic fixture failure")
        return histories[endpoint]

    monkeypatch.setattr(market_scan, "hist", fake_hist)
    row = market_scan.scan_sentiment("BTC")[0]

    assert row["oi_chg"] == 100
    assert row["long_liq"] == 0 and row["short_liq"] == 0
    assert row["fr"] is None
    assert row["status"] == "partial"
    assert row["metadata"]["indicator_statuses"]["funding"] == "error"
    assert row["metadata"]["indicator_errors"] == {"funding": "TimeoutError"}
    assert market_scan._source(lambda: [row], "sentiment")["status"] == "partial"

