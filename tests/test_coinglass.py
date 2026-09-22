"""Offline synthetic fixtures; not captured market data."""
import base64
import copy
import gzip
import importlib.util
import json
import unittest
from unittest.mock import Mock, patch
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import coinglass_decrypt as dec


class BoundaryTests(unittest.TestCase):
    def test_partial_crypto_headers_fail_closed(self):
        response = Mock(headers={'user': 'token'}, text='{"data": []}')
        response.json.return_value = {'data': []}
        with patch('requests.get', return_value=response):
            with self.assertRaises(Exception):
                dec.fetch_and_decrypt('https://capi.coinglass.com/api/test')

    def test_client_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('coinglass_client'))


import coinglass_client as cg


def fixture():
    return {'y': [90, 110], 'prices': [[1700000000, 100, 101, 99, 100, 1],
            [1700000300, 100, 101, 99, 100, 0]], 'liq': [[0, 0, 999], [1, 0, 2], [1, 1, 0]],
            'instrument': {'instrumentId': 'BTCUSDT', 'contractType': 'PERPETUAL'},
            'updateTime': 1700000300000, 'rangeLow': 90, 'rangeHigh': 110}


class ClientTests(unittest.TestCase):
    def test_all_routes_and_windows(self):
        for model in (1, 2, 3):
            for scope in ('pair', 'aggregate'):
                for window, (interval, limit, range_) in cg.WINDOWS.items():
                    with self.subTest(model=model, scope=scope, window=window):
                        raw = fixture()
                        with patch.object(cg, 'cg_fetch', return_value=raw) as fetch:
                            result = cg.coinglass_heatmap(model=model, scope=scope, window=window,
                                     original_symbol='BTCUSDT' if scope == 'pair' else None)
                        host, path = cg.ROUTES[model, scope]
                        args, kwargs = fetch.call_args
                        self.assertEqual(args[0], path)
                        self.assertEqual(kwargs['host'], host)
                        self.assertEqual(args[1]['symbol'], 'Binance_BTCUSDT' if scope == 'pair' else 'BTC')
                        if model == 3:
                            self.assertEqual(args[1]['range'], range_)
                            self.assertEqual(args[1]['cp'], 'false')
                            self.assertNotIn('interval', args[1])
                        else:
                            self.assertEqual((args[1]['interval'], args[1]['limit']), (interval, str(limit)))
                        self.assertIs(result['raw'], raw)
                        self.assertEqual(result['by_price'], {90: 2, 110: 0})
                        self.assertEqual(result['top_below'], [(90, 2)])
                        self.assertEqual(result['top_above'], [(110, 0)])
                        self.assertEqual(result['instrument'], raw['instrument'])
                        self.assertEqual(result['reference_contract_price'], 100)
                        self.assertEqual(result['metadata']['unit'], 'unverified_liquidation_intensity')

    def test_bad_heatmap_cells(self):
        for cell in ([-1, 0, 1], [2, 0, 1], [0, -1, 1], [0, 2, 1], [0.5, 0, 1],
                     [0, 0, -1], [0, 0, float('nan')], [0, 0, float('inf')], [True, 0, 1]):
            raw = fixture(); raw['liq'] = [cell]
            with self.subTest(cell=cell), patch.object(cg, 'cg_fetch', return_value=raw):
                with self.assertRaises(dec.CoinGlassError):
                    cg.coinglass_heatmap(original_symbol='BTCUSDT')

    def test_bad_candles(self):
        for replacement in ([1], [1700000000000, 1, 1, 1, 1, 1],
                            [1700000000, 1, 1, 1, float('inf'), 1]):
            raw = fixture(); raw['prices'][1] = replacement
            with patch.object(cg, 'cg_fetch', return_value=raw):
                with self.assertRaises(dec.CoinGlassError):
                    cg.coinglass_heatmap(original_symbol='BTCUSDT')

    def test_empty_and_zero(self):
        raw = fixture(); raw.update(liq=[], prices=[], y=[])
        with patch.object(cg, 'cg_fetch', return_value=raw):
            result = cg.coinglass_heatmap(original_symbol='BTCUSDT')
        self.assertIsNone(result['spot']); self.assertEqual(result['by_price'], {})

    def test_configuration_rejects_before_network(self):
        for kw in ({'window': '24h', 'interval': '5', 'limit': 288}, {'limit': 2},
                   {'window': 'bad'}, {'original_symbol': ''}, {'original_symbol': 'foo&data=x'},
                   {'model': 3, 'exchange': 'Gate'}, {'model': 3, 'interval': '5', 'limit': 288},
                   {'model': True}, {'scope': 'aggregate', 'original_symbol': 'BTCUSDT'}):
            with self.subTest(kw=kw), patch.object(cg, 'cg_fetch') as fetch:
                with self.assertRaises(ValueError): cg.coinglass_heatmap(**kw)
                fetch.assert_not_called()

    def test_ticker_uses_real_original_symbol(self):
        ticker = {'symbol': 'BTC', 'exchangeName': 'KuCoin', 'quoteCurrency': 'USDT', 'originalSymbol': 'XBTUSDTM'}
        with patch.object(cg, 'cg_fetch', side_effect=[[ticker], fixture()]) as fetch:
            result = cg.coinglass_heatmap(exchange='KuCoin')
        self.assertEqual(fetch.call_args.args[1]['symbol'], 'KuCoin_XBTUSDTM')
        self.assertEqual(result['metadata']['ticker'], ticker)
        for rows in ([], [ticker, ticker]):
            with patch.object(cg, 'cg_fetch', return_value=rows):
                with self.assertRaises(dec.CoinGlassError): cg.coinglass_heatmap(exchange='KuCoin')

    def test_default_pair_prefers_conventional_perpetual_ticker(self):
        """Binance BTC/USDT search returns perp + deliveries; prefer BTCUSDT like live 6/6 smoke."""
        rows = [
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDT',
             'originalSymbol': 'BTCUSDT_261225', 'type': 5},
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDT',
             'originalSymbol': 'BTCUSDT', 'type': 1},
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDT',
             'originalSymbol': 'BTCUSDT_260925', 'type': 4},
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDC',
             'originalSymbol': 'BTCUSDC', 'type': 1},
        ]
        with patch.object(cg, 'cg_fetch', side_effect=[rows, fixture()]) as fetch:
            result = cg.coinglass_heatmap()
        self.assertEqual(fetch.call_args.args[1]['symbol'], 'Binance_BTCUSDT')
        self.assertEqual(result['metadata']['ticker']['originalSymbol'], 'BTCUSDT')
        # No conventional symbol and multiple type==1 -> still fail closed.
        amb = [
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDT',
             'originalSymbol': 'BTCUSDT_A', 'type': 1},
            {'symbol': 'BTC', 'exchangeName': 'Binance', 'quoteCurrency': 'USDT',
             'originalSymbol': 'BTCUSDT_B', 'type': 1},
        ]
        with patch.object(cg, 'cg_fetch', return_value=amb):
            with self.assertRaises(dec.CoinGlassError) as caught:
                cg.coinglass_heatmap()
            self.assertEqual(caught.exception.code, 'ambiguous_or_unknown')

    def test_positional_and_explicit_legacy(self):
        with patch.object(cg, 'cg_fetch', return_value=fixture()) as fetch:
            cg.coinglass_heatmap('BTC', 'Binance', '15', 288, original_symbol='BTCUSDT')
            self.assertEqual(fetch.call_args.args[0], '/api/index/v2/liqHeatMap')
            cg.coinglass_heatmap(model='legacy', original_symbol='BTCUSDT')
            self.assertEqual(fetch.call_args.args[0], '/api/index/v3/liqHeatMap')

    def test_zero_padded_totp(self):
        with patch.object(cg, '_cg_totp', return_value=123), patch.object(cg.time, 'time', return_value=1700000000):
            ciphertext = base64.b64decode(cg._cg_data_param())
        plain = unpad(AES.new(b'1f68efd73f8d4921acc0dead41dd39bc', AES.MODE_ECB).decrypt(ciphertext), 16)
        self.assertEqual(plain, b'1700000000,000123')

    def test_envelopes_and_safe_transport(self):
        for code in (0, '0', 200, '200'):
            with patch.object(cg, 'fetch_and_decrypt', return_value={'code': code, 'data': [1]}):
                self.assertEqual(cg.cg_fetch('/api/test', {}), [1])
        for obj in ({'code': 40000}, {'code': '40003'}, {'code': 50001}, {'code': 0, 'success': False}):
            with patch.object(cg, 'fetch_and_decrypt', return_value=obj):
                with self.assertRaises(dec.CoinGlassError): cg.cg_fetch('/api/test', {})
        with patch.object(cg, 'fetch_and_decrypt', side_effect=RuntimeError('secret-data-in-url')):
            with self.assertRaises(dec.CoinGlassError) as caught: cg.cg_fetch('/api/test', {})
            self.assertNotIn('secret', str(caught.exception))

    def test_units_wrappers(self):
        self.assertAlmostEqual(cg.annualize_funding_percent(.01, 8), 10.95)
        self.assertIsNone(cg.annualize_funding_percent(.01, None))
        self.assertIsNone(cg.annualize_funding_percent(None, 8))
        with self.assertRaises(ValueError): cg.annualize_funding_percent(.01, 0)
        rows = [{'changeUsd': 617600000, 'change': 7611.16}]
        with patch.object(cg, 'cg_fetch', return_value=rows) as fetch:
            self.assertIs(cg.cg_etf_flow('ETH'), rows)
            self.assertEqual(fetch.call_args.args[0], '/api/etf/eth/flow')
        with patch.object(cg, 'cg_fetch', return_value=list(range(10))):
            self.assertEqual(cg.cg_oi_change_rank(2), [0, 1])
            self.assertEqual(len(cg.cg_oi_change_rank(20)), 10)
        for n in (0, -1, True, 1.5):
            with self.assertRaises(ValueError): cg.cg_coin_markets(n)

    def test_exact_adapter_params(self):
        with patch.object(cg, 'cg_fetch', return_value=[]) as fetch:
            cg.cg_coin_markets(50, page_num=2, keyword='BTC', tags='crypto', symbols='BTC,ETH', filter='oi,1,')
            self.assertEqual(fetch.call_args.args[1]['pageNum'], '2')
            self.assertEqual(fetch.call_args.args[1]['filter'], 'oi,1,')
            cg.cg_funding_chart('ETH', mode='history', type='C', interval='m5')
            self.assertEqual(fetch.call_args.args, ('/api/fundingRate/v2/history/chart', {'symbol': 'ETH', 'type': 'C', 'interval': 'm5'}))
            for window, tt in [('1d',10), ('7d',2), ('30d',1), ('90d',4), ('all',0)]:
                cg.cg_liquidation_chart(window=window)
                self.assertEqual(fetch.call_args.args[1]['timeType'], tt)
            cg.cg_option_chart(type='Strike', subtype='260925')
            self.assertEqual(fetch.call_args.args[1]['subtype'], '260925')
            cg.cg_exchange_balance(ex_name='Binance')
            self.assertEqual(fetch.call_args.args[1]['exName'], 'Binance')

    def test_fear_alignment(self):
        raw = {'dates': [1], 'values': [20], 'prices': [100]}
        with patch.object(cg, 'cg_fetch', return_value=[raw]): self.assertIs(cg.cg_fear_greed(), raw)
        raw['dates'] = []
        with patch.object(cg, 'cg_fetch', return_value=[raw]):
            with self.assertRaises(dec.CoinGlassError): cg.cg_fear_greed()


class DecryptionTests(unittest.TestCase):
    def test_six_versions(self):
        url = 'https://capi.coinglass.com/api/index/v2/liqHeatMap'
        for version in ('0', '1', '2', '55', '66', '77'):
            with self.subTest(version=version):
                key0 = dec._derive_key0(version, url, cache_ts='1700000000000', time_header='1700000000001')
                actual = b'0123456789abcdef'
                token = base64.b64encode(AES.new(key0.encode(), AES.MODE_ECB).encrypt(pad(gzip.compress(actual), 16))).decode()
                payload = base64.b64encode(AES.new(actual, AES.MODE_ECB).encrypt(pad(gzip.compress(b'{"value": 7}'), 16))).decode()
                self.assertEqual(dec.decrypt(json.dumps({'code': '0', 'data': payload}), token, version, url,
                    cache_ts='1700000000000', time_header='1700000000001'), {'value': 7})

    def test_plain_and_missing_headers(self):
        for headers, body, fails in [({}, {'code': 0, 'data': []}, False),
                                     ({'v': '1'}, {}, True), ({}, {'data': 'encrypted'}, True),
                                     ({}, {'code': 40003}, True)]:
            response = Mock(headers=headers); response.json.return_value = body
            with patch('requests.get', return_value=response):
                if fails:
                    with self.assertRaises(dec.CoinGlassError): dec.fetch_and_decrypt('https://capi.coinglass.com/api/test')
                else:
                    self.assertEqual(dec.fetch_and_decrypt('https://capi.coinglass.com/api/test'), body)

    def test_malformed_crypto_is_structured(self):
        with self.assertRaises(dec.CoinGlassError) as caught:
            dec.decrypt('{"data":"bad"}', 'bad', '1', 'https://example.com/?data=private')
        self.assertEqual(caught.exception.category, 'decrypt')
        self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
