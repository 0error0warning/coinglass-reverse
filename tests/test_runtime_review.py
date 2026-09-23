"""Independent reviewer regressions: fixtures, never production data."""
import io
import json
import threading
from contextlib import redirect_stdout
from unittest.mock import patch
import pytest
import market_scan as m
import liq_collector as lc
import premium_collector as pc

@pytest.mark.parametrize('source', ['spot', 'sentiment'])
def test_real_source_path_all_network_requests_fail(source, monkeypatch):
    monkeypatch.setattr(m, 'CY', 'non-secret-fixture')
    monkeypatch.setattr(m, 'find_perp', lambda *a, **kw: {'A': 'BTCUSDT_PERP.A'})
    def failed(*a, **kw):
        raise TimeoutError('must-not-leak-signed-url')
    monkeypatch.setattr(m, 'get', failed)
    monkeypatch.setattr(m, 'hist', failed)
    with redirect_stdout(io.StringIO()) as stream:
        rc = m.main(['BTC', '--source', source, '--json'])
    out = json.loads(stream.getvalue())
    assert rc == 1 and out[source]['status'] == 'error'
    assert 'must-not-leak' not in stream.getvalue()

@pytest.mark.parametrize('source', ['spot', 'sentiment', 'liq_map'])
def test_explicit_source_error_wins(source):
    assert m._source(lambda: {'status':'error','error_type':'fixture'}, source)['status'] == 'error'

def test_unavailable_state_directory_reported_in_health(tmp_path):
    path = tmp_path/'blocked'; path.write_text('not a directory')
    collector = lc.Collector(path)
    assert collector.flush_once(now_ms=1000)['errors']['state_dir']
    health = collector.health_snapshot(now_ms=1001)
    assert health['persistence']['flush_errors'] == 1
    assert health['persistence']['last_flush_ms'] == 1000

def test_failed_health_replace_preserves_previous_snapshot(tmp_path):
    collector = lc.Collector(tmp_path)
    collector.write_health(now_ms=1000)
    path = tmp_path/'_health'/'status.json'; before = path.read_bytes()
    with patch.object(lc.os, 'replace', side_effect=OSError('fixture')):
        with pytest.raises(OSError): collector.write_health(now_ms=1001)
    assert path.read_bytes() == before
    assert list(path.parent.glob('*.tmp')) == []

def test_run_return_without_close_callback_marks_disconnected(tmp_path):
    stop = threading.Event()
    class Fake:
        def __init__(self, url, **cb): self.cb=cb
        def send(self, value): pass
        def close(self): pass
        def run_forever(self, **kwargs):
            self.cb['on_open'](self)
            self.cb['on_message'](self, '{"result":null,"id":1}')
            stop.set()
    collector = lc.Collector(tmp_path)
    lc.run_source('binance', collector, stop, ws_factory=Fake)
    assert not collector.health_snapshot()['sources']['binance']['connected']

def test_premium_health_reports_per_symbol_status(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, 'STATE_DIR', tmp_path)
    pc.write_health([{'symbol': 'BTC', 'status': 'ok'},
                     {'symbol': 'ETH', 'status': 'error', 'error_type': 'TimeoutError'}],
                    now_ms=1700000000000)
    data = json.loads((tmp_path / '_health' / 'status.json').read_text())
    assert data['updated_ms'] == 1700000000000
    assert data['symbols']['BTC'] == {'status': 'ok'}
    assert data['symbols']['ETH'] == {'status': 'error', 'error_type': 'TimeoutError'}
    # Failed replace preserves the previous snapshot like the liq collector.
    before = (tmp_path / '_health' / 'status.json').read_bytes()
    with patch.object(pc.os, 'replace', side_effect=OSError('fixture')):
        with pytest.raises(OSError):
            pc.write_health([], now_ms=1700000000001)
    assert (tmp_path / '_health' / 'status.json').read_bytes() == before


def test_premium_term_closes_database(monkeypatch):
    handlers={}; closed=[]
    monkeypatch.setattr(pc.signal,'signal',lambda sig,handler: handlers.update({sig:handler}))
    class Conn:
        def close(self): closed.append(True)
    monkeypatch.setattr(pc,'init_db',Conn)
    monkeypatch.setattr(pc,'collect_once',lambda conn: handlers[pc.signal.SIGTERM](None,None))
    pc.main()
    assert closed == [True]
