"""Response-shape regression informed by live endpoint structure; synthetic values."""
from unittest.mock import patch
import pytest
import coinglass_client as cg
import market_scan as scan


def expiry_fixture():
    return {'data': {'keyList': ['260922', '260923'], 'callOiList': [2.5, 3.1],
                     'putOiList': [4.0, 5.0], 'notionalOiList': [520000, 648000],
                     'putMarketOiList': [100, 200], 'callOi': 5.6},
            'keys': ['20000', '30000', '35000']}


def test_expiry_axis_is_nested_not_top_level_strikes():
    raw = expiry_fixture()
    with patch.object(cg, 'cg_fetch', return_value=raw):
        result = cg.cg_option_expiry()
    assert result['status'] == 'ok'
    assert result['data'] is raw
    assert result['metadata']['axis_path'] == 'data.keyList'
    assert result['metadata']['unit'] == 'mixed_raw_series_units'
    assert len(raw['keys']) != len(raw['data']['keyList'])


@pytest.mark.parametrize('bad', [[], {'keyList': []}, {'keyList': ['260922'], 'callOiList': [], 'putOiList': [2]}])
def test_expiry_rejects_wrong_or_misaligned_series(bad):
    with patch.object(cg, 'cg_fetch', return_value={'data': bad, 'keys': []}):
        with pytest.raises(cg.CoinGlassError) as caught:cg.cg_option_expiry()
    assert caught.value.category == 'schema'


def test_expiry_empty_axis_with_strike_choices_still_no_data():
    raw = {'data': {'keyList': [], 'callOiList': [], 'putOiList': []}, 'keys': ['20000']}
    with patch.object(cg, 'cg_fetch', return_value=raw):
        result = scan.collect('BTC', sources=['cg_option_expiry'])
    assert result['cg_option_expiry']['status'] == 'no_data'


@pytest.mark.parametrize('kwargs', [{'instrument':'BTCUSDT'}, {'instrument':'Binance_BTCUSDT#1#hundredth_depth'}, {'interval':None}, {'interval':True}, {'depth':True}])
def test_depth_bad_inputs_fail_before_network(kwargs):
    with patch.object(cg, 'cg_fetch') as fetch:
        with pytest.raises(ValueError):cg.cg_depth_delta(**kwargs)
        fetch.assert_not_called()
