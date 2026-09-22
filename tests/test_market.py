"""Offline regression fixtures; synthetic values, never captured trades."""
import importlib.util
import json
import pathlib
import sys
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_PATH = pathlib.Path


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('LIQ_STATE_DIR', str(tmp_path/'liq'))
    monkeypatch.setenv('PREMIUM_STATE_DIR', str(tmp_path/'premium'))
    monkeypatch.delenv('COINALYZE_KEY', raising=False)
    monkeypatch.delenv('MARKET_API_KEY_FILE', raising=False)
    sys.path.insert(0, str(ROOT))
    class SafePath(type(REAL_PATH())):
        def __new__(cls, *args, **kwargs):
            if args and str(args[0]).startswith('/var/lib/upi-hermes'):
                args = (str(tmp_path) + str(args[0])[len('/var/lib/upi-hermes'):], *args[1:])
            return super().__new__(cls, *args, **kwargs)
        @classmethod
        def home(cls):
            return cls(tmp_path)
    loaded=[]
    with patch('pathlib.Path', SafePath):
        for name in ('market_scan', 'premium_collector'):
            spec=importlib.util.spec_from_file_location('audit_'+name,ROOT/f'{name}.py')
            assert spec is not None and spec.loader is not None
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded.append(module)
    yield (*loaded,tmp_path)
    sys.path.remove(str(ROOT))


def test_coinalyze_header_and_usd_request(modules, monkeypatch):
    m,_,_=modules; calls=[];m.CY='fixture-key'
    monkeypatch.setattr(m,'get',lambda url,headers=None,timeout=20:(calls.append((url,headers)) or b'[]'))
    m.hist('BTCUSDT_PERP.A','liquidation-history')
    assert calls[0][1]=={'api_key':'fixture-key'}
    assert 'convert_to_usd=true' in calls[0][0]


def test_flat_candle_preserves_volume(modules, monkeypatch):
    m,_,_=modules
    replies=iter([json.dumps([[0,'100','100','100','100','20']]).encode(),b'{"price":"100"}'])
    monkeypatch.setattr(m,'get',lambda *a,**k:next(replies))
    result=m.vpvr()
    assert sum(result['vol'].values())==20
    assert result['poc']==100


def test_vpvr_zero_and_invalid_input(modules):
    m,_,_=modules
    assert m.render_vpvr({'price':100,'step':1,'vol':{100:0},'poc':100})
    with pytest.raises(ValueError):m.vpvr(days=-1)


def test_deribit_expiry_policies(modules, monkeypatch):
    m,_,_=modules
    response={'result':[{'instrument_name':'BTC-01JAN30-100000-C','open_interest':1,'estimated_delivery_price':100000},{'instrument_name':'BTC-01DEC30-100000-P','open_interest':100,'estimated_delivery_price':100000}]}
    monkeypatch.setattr(m,'get',lambda *a,**k:json.dumps(response).encode())
    assert m.deribit_walls(expiry_policy='nearest')['expiry']=='01JAN30'
    assert m.deribit_walls(expiry_policy='max_oi')['expiry']=='01DEC30'
    assert m.deribit_walls(expiry='01JAN30')['expiry']=='01JAN30'
    with pytest.raises(ValueError):m.deribit_walls(expiry='FAKE')


def test_one_source_failure_does_not_abort_collect(modules,monkeypatch):
    m,_,_=modules;m.CY=None
    for name in ('spot_tickers','vpvr','liq_map','spot_premium','premium_trend','coinglass_heatmap','stablecoin','etf_flow'):
        monkeypatch.setattr(m,name,lambda *a,**k:None)
    monkeypatch.setattr(m,'deribit_walls',lambda *a,**k:(_ for _ in ()).throw(TimeoutError('fixture')))
    result=m.collect()
    assert result['deribit']['status']=='error'
    assert 'stablecoin' in result


def test_etf_cli_calls_only_etf(modules,monkeypatch,capsys):
    m,_,_=modules;calls=[]
    monkeypatch.setattr(m,'etf_flow',lambda:(calls.append('etf') or []))
    monkeypatch.setattr(m,'spot_tickers',lambda *a,**k:(_ for _ in ()).throw(AssertionError('unrelated source')))
    assert m.main(['--etf','--json'])==0
    assert calls==['etf']
    assert json.loads(capsys.readouterr().out)['etf']['status']=='no_data'


def test_premium_retention_every_collection(modules,monkeypatch):
    _,p,tmp=modules
    p.DB=tmp/'test.db'
    monkeypatch.setattr(p.time,'time',lambda:100)
    conn=p.init_db()
    conn.execute('INSERT INTO premium VALUES (?,?,?,?,?,?)',('BTC',1,100,100,0,0));conn.commit()
    monkeypatch.setattr(p.time,'time',lambda:40*86400)
    monkeypatch.setattr(p,'SYMBOLS',[])
    p.collect_once(conn)
    assert conn.execute('SELECT count(*) FROM premium').fetchone()[0]==0
    conn.close()


def test_heatmap_zero_and_small_price_display(modules,capsys):
    m,_,_=modules
    m.print_cg_heatmap({'spot':.12,'by_price':{.10:0,.15:0}},'DOGE')
    text=capsys.readouterr().out
    assert '0.1' in text
    assert '阻力' not in text and '支撑' not in text
    assert m.format_percent(None)=='N/A'


def test_liq_map_rejects_legacy_and_excludes_snapshots(modules,monkeypatch):
    m,_,tmp=modules
    state=tmp/'liq';state.mkdir(exist_ok=True)
    (state/'BTCUSDT.json').write_text(json.dumps({'events':[{'ts':1000,'qty':100,'price':100,'side':'long','ex':'okx'}],'updated':1000}))
    monkeypatch.setattr(m,'spot_tickers',lambda *a,**k:{'BTCUSDT':{'p':100}})
    monkeypatch.setattr(m.time,'time',lambda:1000)
    assert m.liq_map()['status']=='legacy_untyped'
    common={'schema_version':2,'symbol':'BTCUSDT','base_asset':'BTC','quote_asset':'USDT','exchange_ts_ms':999000,'received_ts_ms':999010,'price':100,'liquidated_side':'long','base_qty':1,'ex':'bybit','instrument':'BTCUSDT','raw_qty':1,'raw_unit':'base_asset','coverage':{'additive':True},'price_kind':'bankruptcy_price','quantity_kind':'liquidation_quantity'}
    events=[dict(common,event_id='a'),dict(common,event_id='a'),dict(common,event_id='b',base_qty=5,exchange_ts_ms=1001000),dict(common,event_id='c',base_qty=10,coverage={'additive':False},quantity_kind='cumulative_filled_snapshot')]
    (state/'BTCUSDT.json').write_text(json.dumps({'schema_version':2,'events':events,'updated_ms':1000000}))
    result=m.liq_map()
    assert sum(b['1h']['long'] for b in result['buckets'].values())==1
    assert result['excluded_events']>=2
    assert result['unit']=='BTC'


def test_premium_semantics_explicit(modules,monkeypatch):
    m,_,_=modules
    monkeypatch.setattr(m,'get',lambda *a,**k:b'{"price":"100"}')
    result=m.spot_premium()
    assert result['fx_adjusted'] is False
    assert result['metric']=='usd_spot_minus_usdt_perpetual'
