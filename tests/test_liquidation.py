"""Offline synthetic fixtures, not captured exchange traffic."""
import importlib
import json
import threading
import pytest


def normalizers():
    return importlib.import_module('liquidation_events')


def bn(z='2', st=1):
    return {'e': 'forceOrder', 'o': {'s': 'BTCUSDT', 'st': st, 'S': 'SELL', 'q': '10', 'p': '99000', 'z': z, 'ap': '100000', 'T': 1700000000000}}


def test_binance_filled_snapshot_not_order_quantity():
    m = normalizers()
    e = m.normalize_binance(bn(), received_ts_ms=1700000000001)[0]
    assert e['base_qty'] == 2 and e['price'] == 100000
    assert e['quantity_kind'] == 'cumulative_filled_snapshot'
    assert e['coverage']['additive'] is False
    assert e['exchange_ts_ms'] == 1700000000000
    assert e['liquidated_side'] == 'long'
    assert e['event_id'] == m.normalize_binance(bn(), received_ts_ms=1700000000002)[0]['event_id']
    assert m.normalize_binance(bn(st=2)) == []


def test_bybit_position_side_and_bankruptcy():
    e = normalizers().normalize_bybit({'topic':'allLiquidation.BTCUSDT', 'data':[{'s':'BTCUSDT','S':'Buy','v':'2','p':'99000','T':1700000000000}]})[0]
    assert e['liquidated_side'] == 'long'
    assert e['price_kind'] == 'bankruptcy_price'
    assert e['raw_unit'] == 'base_asset'


def okx(cttype='linear', ccy='BTC', val='0.01'):
    meta = {'BTC-USDT-SWAP': {'instId':'BTC-USDT-SWAP','ctType':cttype,'ctVal':val,'ctMult':'1','ctValCcy':ccy,'baseCcy':'','quoteCcy':''}}
    payload = {'arg':{'channel':'liquidation-orders','instType':'SWAP'}, 'data':[{'instId':'BTC-USDT-SWAP','details':[{'posSide':'net','side':'buy','sz':'100','bkPx':'100000','ts':'1700000000000'}]}]}
    return payload, meta


def test_okx_contracts_metadata_sides_and_price():
    p, meta = okx()
    e = normalizers().normalize_okx(p, meta)[0]
    assert e['base_qty'] == 1 and e['raw_qty'] == 100
    assert e['raw_unit'] == 'contracts' and e['liquidated_side'] == 'short'
    assert e['price_kind'] == 'liquidation_transfer_price'
    assert normalizers().normalize_okx(p, {}) == []
    p, meta = okx('inverse', 'USDT', '100')
    assert normalizers().normalize_okx(p, meta)[0]['base_qty'] == .1


@pytest.mark.parametrize('field,value', [('S','bad'),('z','NaN'),('ap','0'),('T',0),('s','UNKNOWN'),('st',3)])
def test_invalid_binance_fail_closed(field,value):
    p = bn(); p['o'][field] = value
    assert normalizers().normalize_binance(p) == []


def test_persistence_retry_restart_dedup_prune_and_legacy(tmp_path, monkeypatch):
    c = importlib.import_module('liq_collector')
    collector = c.Collector(tmp_path)
    event = normalizers().normalize_binance(bn(), received_ts_ms=1700000000001)[0]
    assert collector.record(event)
    event['base_qty'] = 999  # cannot mutate recorded copy
    real_replace = c.os.replace
    monkeypatch.setattr(c.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    assert collector.flush_once(now_ms=1700000000002)['errors']
    assert collector.pending_count == 1
    monkeypatch.setattr(c.os, 'replace', real_replace)
    assert not collector.flush_once(now_ms=1700000000002)['errors']
    f = tmp_path/'BTCUSDT.json'
    stored = json.loads(f.read_text())
    assert stored['schema_version'] == 2 and stored['events'][0]['base_qty'] == 2
    restarted = c.Collector(tmp_path)
    restarted.record(normalizers().normalize_binance(bn())[0])
    restarted.flush_once(now_ms=1700000000003)
    assert len(json.loads(f.read_text())['events']) == 1
    restarted.flush_once(now_ms=1700000000000 + 73*3600000)
    assert json.loads(f.read_text())['events'] == []
    legacy = tmp_path/'ETHUSDT.json'; legacy.write_text('{"events":[{"qty":9}]}')
    before = legacy.read_bytes()
    assert restarted.flush_once(now_ms=1700000000003)['errors']
    assert legacy.read_bytes() == before


@pytest.mark.parametrize('key,value', [('raw_unit','contracts'), ('instrument','ETHUSDT'), ('quote_asset','USD')])
def test_validator_rejects_inconsistent_event_contract(key, value):
    e = normalizers().normalize_binance(bn())[0]
    e[key] = value
    assert not normalizers().valid_event(e)


def test_bybit_topic_must_match_instrument():
    assert normalizers().normalize_bybit({'topic':'allLiquidation.ETHUSDT', 'data':[{'s':'BTCUSDT','S':'Buy','v':'2','p':'99','T':1700000000000}]}) == []


def test_malformed_okx_batch_is_fail_closed():
    assert normalizers().normalize_okx({'arg':{'channel':'liquidation-orders'}, 'data':None}, {}) == []


def test_flush_isolates_corrupt_file_and_preserves_it(tmp_path):
    c = importlib.import_module('liq_collector').Collector(tmp_path)
    (tmp_path/'ETHUSDT.json').write_text('broken')
    c.record(normalizers().normalize_binance(bn())[0])
    result = c.flush_once(now_ms=1700000000001)
    assert result['written'] == ['BTCUSDT']
    assert 'ETHUSDT' in result['errors']
    assert (tmp_path/'ETHUSDT.json').read_text() == 'broken'


def test_import_does_not_create_state_or_logs(tmp_path):
    import os
    import subprocess
    import sys
    target = tmp_path/'not-created'
    result = subprocess.run([sys.executable, '-c', 'import liq_collector'], env={**os.environ, 'LIQ_STATE_DIR':str(target)}, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_connected_source_can_stop_and_observes_server_error(tmp_path):
    c = importlib.import_module('liq_collector')
    stop = threading.Event(); closed = threading.Event()
    class Fake:
        def __init__(self, url, **callbacks): self.cb = callbacks
        def send(self, msg): pass
        def close(self): closed.set()
        def run_forever(self, **kwargs):
            self.cb['on_open'](self)
            self.cb['on_message'](self, '{"event":"error","code":"60012","msg":"bad subscribe"}')
            stop.set()
            assert closed.wait(2)
    collector = c.Collector(tmp_path)
    c.run_source('okx', collector, stop, instruments={}, ws_factory=Fake)
    assert collector.status['okx']['errors'] == 1
    assert collector.status['okx']['acknowledged'] is False


def test_ack_timeout_closes_connection(tmp_path):
    c = importlib.import_module('liq_collector')
    stop = threading.Event(); closed = threading.Event()
    class Fake:
        def __init__(self, url, **callbacks): self.cb = callbacks
        def send(self, msg): pass
        def close(self): closed.set()
        def run_forever(self, **kwargs):
            self.cb['on_open'](self)
            assert closed.wait(2)
            stop.set()
    collector = c.Collector(tmp_path)
    c.run_source('bybit', collector, stop, ws_factory=Fake, ack_timeout=0)
    assert collector.status['bybit']['errors'] == 1


def test_stop_prevents_ws_connection(tmp_path):
    c = importlib.import_module('liq_collector')
    stop = threading.Event(); stop.set()
    c.run_source('binance', c.Collector(tmp_path), stop, ws_factory=lambda *a, **k: pytest.fail('connected'))


def test_ack_subscription_and_iterative_reconnect(tmp_path):
    c = importlib.import_module('liq_collector')
    stop = threading.Event(); seen = []
    class Fake:
        def __init__(self, url, **callbacks): self.cb = callbacks; seen.append(self)
        def send(self, msg): self.subscription = json.loads(msg)
        def close(self): pass
        def run_forever(self, **kwargs):
            self.cb['on_open'](self)
            self.cb['on_message'](self, '{"result":null,"id":1}')
            self.cb['on_close'](self, 1000, 'done')
            if len(seen) == 3: stop.set()
    collector = c.Collector(tmp_path)
    c.run_source('binance', collector, stop, ws_factory=Fake, backoff=0)
    assert len(seen) == 3
    assert seen[0].subscription['params'] == ['!forceOrder@arr']
    assert collector.status['binance']['acknowledged'] is True
