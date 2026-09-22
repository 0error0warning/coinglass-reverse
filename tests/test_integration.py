"""Synthetic wire-shaped fixtures exercise producer -> disk -> consumer."""
import json
from unittest.mock import patch

import coinglass_client as cg
import market_scan as market
from liq_collector import Collector
from liquidation_events import normalize_okx, normalize_bybit, normalize_binance


def test_heatmap_wire_numeric_strings():
    raw = {'instrument': {}, 'y': [90, 110], 'prices': [
        [1700000000, '100.0', '101', '99', '100', '1'],
        [1700000300, '100', '102', '99', '101', '0']],
        'liq': [[0, 0, 999], [1, 0, 2], [1, 1, 3]],
        'updateTime': 1700000300000}
    with patch.object(cg, 'cg_fetch', return_value=raw):
        result = cg.coinglass_heatmap(original_symbol='BTCUSDT')
    assert result['reference_contract_price'] == 101
    assert result['by_price'] == {90: 2, 110: 3}
    assert raw['prices'][-1][4] == '101'  # Preserve untouched wire evidence.


def test_normalize_persist_read_base_units(tmp_path, monkeypatch):
    monkeypatch.setenv('LIQ_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(market.time, 'time', lambda: 1700000001)
    monkeypatch.setattr(market, 'spot_tickers', lambda *a: {'BTCUSDT': {'p': 100}})
    okx = normalize_okx({'arg': {'channel': 'liquidation-orders'}, 'data': [{
        'instId': 'BTC-USDT-SWAP', 'details': [{'sz': '100', 'bkPx': '100',
        'posSide': 'long', 'side': 'sell', 'ts': '1700000000000'}]}]},
        {'BTC-USDT-SWAP': {'ctVal': '0.01', 'ctMult': '1', 'ctType': 'linear', 'ctValCcy': 'BTC'}})
    bybit = normalize_bybit({'topic': 'allLiquidation.BTCUSDT', 'data': [{
        's': 'BTCUSDT', 'v': '1', 'p': '100', 'S': 'Buy', 'T': 1700000000000}]})
    binance = normalize_binance({'o': {'st': '1', 's': 'BTCUSDT', 'z': '500',
        'ap': '100', 'S': 'SELL', 'T': 1700000000000}})
    assert len(okx) == len(bybit) == len(binance) == 1
    collector = Collector(tmp_path)
    for event in okx + bybit + binance:
        assert collector.record(event)
    assert not collector.flush_once(now_ms=1700000001000)['errors']
    result = market.liq_map()
    assert result['accepted_events'] == 2
    assert result['excluded_events'] == 1
    assert result['unit'] == 'BTC'
    assert sum(b['1h']['long'] for b in result['buckets'].values()) == 2
