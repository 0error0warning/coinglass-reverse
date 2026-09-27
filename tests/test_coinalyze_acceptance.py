"""Parent's independent regressions: capability gaps are not transport errors."""
import market_scan as m


def test_unsupported_indicator_does_not_make_healthy_source_fail(monkeypatch):
    monkeypatch.setattr(m, '_markets_cache', [{'exchange':'H', 'symbol':'BTC.H', 'has_long_short_ratio_data':False}])
    monkeypatch.setattr(m, 'find_perp', lambda *a: {'H':'BTC.H'})
    def history(symbol, endpoint, hours):
        if endpoint=='open-interest-history': return [{'o':2}, {'c':3}]
        if endpoint=='funding-rate-history': return [{'c':0}]
        if endpoint=='liquidation-history': return []
        raise AssertionError('Unsupported LS must not be requested')
    monkeypatch.setattr(m, 'hist', history)
    rows = m.scan_sentiment()
    assert rows[0]['metadata']['indicator_statuses']['long_short_ratio']=='unsupported'
    assert rows[0]['long_liq'] is None and rows[0]['fr']==0
    assert rows[0]['status']=='ok'
    assert m._source(lambda: rows, 'sentiment')['status']=='ok'
