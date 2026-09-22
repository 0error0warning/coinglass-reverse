"""Audited public web adapters, not an official/stable SDK. No import I/O.

Heatmap intensity units remain unverified. `spot` is only a compatibility
alias for the reference contract's last candle close, never a spot quote.
"""
import base64
import hashlib
import hmac
import math
import re
import struct
import time
from collections import defaultdict
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from coinglass_decrypt import CoinGlassError, check_business, fetch_and_decrypt

WINDOWS = {
    '12h': ('5', 144, '12h'), '24h': ('5', 288, '24h'),
    '48h': ('15', 192, '48h'), '3d': ('15', 288, '72h'),
    '1w': ('30', 336, '7d'), '2w': ('30', 672, '14d'),
    '1mo': ('h2', 372, '30d'), '3mo': ('h6', 360, '90d'),
    '6mo': ('h12', 360, '180d'), '1y': ('h24', 360, '365d'),
    '2y': ('d1', 720, '720d'),
}
ROUTES = {
    (1, 'pair'): ('capi', '/api/index/v2/liqHeatMap'),
    (1, 'aggregate'): ('capi', '/api/index/aggregate/liqHeatMap'),
    (2, 'pair'): ('capi', '/api/index/v5/liqHeatMap'),
    (2, 'aggregate'): ('capi', '/api/index/v3/aggregate/liqHeatMap'),
    (3, 'pair'): ('fapi', '/api/index/v6/liqHeatMap'),
    (3, 'aggregate'): ('fapi', '/api/index/v4/aggregate/liqHeatMap'),
    ('legacy', 'pair'): ('capi', '/api/index/v3/liqHeatMap'),
}


def _cg_totp(secret, t=None, step=30):
    if t is None:
        t = int(time.time())
    digest = hmac.new(base64.b32decode(secret), struct.pack('>Q', int(t) // step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000


def _cg_data_param():
    """Public web protocol AES-256-ECB signing; zero-pad the TOTP."""
    now = int(time.time())
    plain = f'{now},{_cg_totp("I65VU7K5ZQL7WB4E", now):06d}'
    key = b'1f68efd73f8d4921acc0dead41dd39bc'
    return base64.b64encode(AES.new(key, AES.MODE_ECB).encrypt(pad(plain.encode(), 16))).decode()


def cg_fetch(path, params, *, host='capi'):
    if host not in ('capi', 'fapi') or not isinstance(path, str) or not re.fullmatch(r'/api/[A-Za-z0-9/_-]+', path):
        raise ValueError('Invalid CoinGlass host/path')
    signed = dict(params)
    signed['data'] = _cg_data_param()
    try:
        value = check_business(fetch_and_decrypt(f'https://{host}.coinglass.com{path}', signed))
    except CoinGlassError:
        raise
    except Exception:
        raise CoinGlassError('transport', 'request_failed', 'Request failed') from None
    if isinstance(value, dict) and 'data' in value and ('code' in value or 'success' in value):
        value = value['data']
    if not isinstance(value, (dict, list)):
        raise CoinGlassError('schema', 'invalid_payload', 'Expected object or array')
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise CoinGlassError('schema', 'invalid_number', f'Invalid {name}')
    return value


def _wire_number(value, name, *, positive=False):
    """OHLCV arrives as decimal strings; do not mutate the raw response."""
    if isinstance(value, str):
        try:
            value = float(value)
        except (ValueError, OverflowError):
            raise CoinGlassError('schema', 'invalid_number', f'Invalid {name}') from None
    return _number(value, name, positive=positive)


def _token(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', value):
        raise ValueError(f'Invalid {name}')
    return value



def _resolve_pair_ticker(matches, symbol, quote):
    """Pick one pair instrument from ticker search hits.

    Live CoinGlass ticker search returns perpetual + dated deliveries for the
    same exchange/base/quote (e.g. BTCUSDT plus BTCUSDT_260925). Prefer the
    conventional `{symbol}{quote}` originalSymbol (BTCUSDT) — the same ticker
    the 6/6 live pair smoke used — then a unique type==1 perpetual. Zero hits
    or remaining ambiguity stay fail-closed.
    """
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CoinGlassError('instrument', 'ambiguous_or_unknown', 'Expected one matching instrument')
    conventional = f'{symbol}{quote}'
    exact = [r for r in matches if r.get('originalSymbol') == conventional]
    if len(exact) == 1:
        return exact[0]
    perps = [r for r in matches if r.get('type') == 1]
    if len(perps) == 1:
        return perps[0]
    raise CoinGlassError('instrument', 'ambiguous_or_unknown', 'Expected one matching instrument')


def coinglass_heatmap(symbol='BTC', exchange='Binance', interval=None, limit=None,
                      *, model=1, scope='pair', window=None, quote='USDT', original_symbol=None):
    if isinstance(model, bool) or (model, scope) not in ROUTES:
        raise ValueError('Unsupported model/scope')
    _token(symbol, 'symbol'); _token(exchange, 'exchange'); _token(quote, 'quote')
    if window is not None and (interval is not None or limit is not None):
        raise ValueError('window conflicts with interval/limit')
    if (interval is None) != (limit is None):
        raise ValueError('interval and limit must be supplied together')
    if window is None and interval is None:
        window = '24h'
    if window is not None and window not in WINDOWS:
        raise ValueError('Unsupported window')
    if model == 3 and interval is not None:
        raise ValueError('Model3 requires a window, not interval/limit')
    if interval is not None:
        interval = str(interval)
        _positive_int(limit, 'limit')
        if interval not in {v[0] for v in WINDOWS.values()}:
            raise ValueError('Unsupported interval')
    if model == 3 and scope == 'pair' and exchange not in ('Binance', 'Bybit', 'OKX', 'Bitget', 'Hyperliquid'):
        raise ValueError('Exchange not in Model3 frontend whitelist')
    ticker = None
    if original_symbol is not None:
        _token(original_symbol, 'original_symbol')
        if scope != 'pair':
            raise ValueError('original_symbol is only valid for pair scope')
    if scope == 'pair':
        if original_symbol is None:
            rows = cg_fetch('/api/futures/select/coins/tickers', {'keyword': symbol})
            if not isinstance(rows, list):
                raise CoinGlassError('schema', 'invalid_tickers', 'Ticker response must be an array')
            matches = [r for r in rows if isinstance(r, dict) and r.get('exchangeName') == exchange
                       and r.get('symbol') == symbol and r.get('quoteCurrency') == quote]
            ticker = _resolve_pair_ticker(matches, symbol, quote)
            original_symbol = _token(ticker.get('originalSymbol'), 'original_symbol')
        request_symbol = f'{exchange}_{original_symbol}'
    else:
        request_symbol = symbol
    params = {'symbol': request_symbol, 'merge': 'true'}
    if model == 3:
        params.update(cp='false', range=WINDOWS[window][2])
    else:
        if window is not None:
            interval, limit, _ = WINDOWS[window]
        params.update(interval=str(interval), limit=str(limit))
    host, endpoint = ROUTES[model, scope]
    raw = cg_fetch(endpoint, params, host=host)
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), list) for k in ('y', 'prices', 'liq')) or not isinstance(raw.get('instrument'), dict):
        raise CoinGlassError('schema', 'invalid_heatmap', 'Invalid heatmap structure')
    y, prices, liq = raw['y'], raw['prices'], raw['liq']
    for price in y:
        _number(price, 'y price', positive=True)
    previous = -1
    for candle in prices:
        if not isinstance(candle, (list, tuple)) or len(candle) != 6:
            raise CoinGlassError('schema', 'invalid_candle', 'Expected timestamp/OHLC/volume')
        ts = _number(candle[0], 'candle timestamp', positive=True)
        if ts <= previous or ts >= 100000000000:
            raise CoinGlassError('schema', 'invalid_time', 'Candle timestamps must increase in seconds')
        previous = ts
        for v in candle[1:5]:
            _wire_number(v, 'OHLC', positive=True)
        _wire_number(candle[5], 'volume')
    by_price = defaultdict(float)
    for cell in liq:
        if not isinstance(cell, (list, tuple)) or len(cell) != 3:
            raise CoinGlassError('schema', 'invalid_cell', 'Expected x/y/intensity')
        x, yi, intensity = cell
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (x, yi)) or not (0 <= x < len(prices) and 0 <= yi < len(y)):
            raise CoinGlassError('schema', 'invalid_index', 'Heatmap index out of bounds')
        _number(intensity, 'intensity')
        if x == len(prices) - 1:
            by_price[y[yi]] += intensity
            _number(by_price[y[yi]], 'summed intensity')
    updated = raw.get('updateTime')
    if updated is not None:
        _number(updated, 'updateTime', positive=True)
    for key in ('rangeLow', 'rangeHigh'):
        if raw.get(key) is not None:
            _number(raw[key], key)
    reference = _wire_number(prices[-1][4], 'close', positive=True) if prices else None
    ranked = sorted(by_price.items(), key=lambda p: (-p[1], p[0]))
    return {'raw': raw, 'by_price': dict(by_price), 'y_axis': y,
            'top_above': [p for p in ranked if reference is not None and p[0] > reference],
            'top_below': [p for p in ranked if reference is not None and p[0] <= reference],
            'reference_contract_price': reference, 'last_candle_close': reference, 'spot': reference,
            'range': (raw.get('rangeLow'), raw.get('rangeHigh')), 'updateTime': updated,
            'instrument': raw['instrument'], 'metadata': {
                'model': model, 'scope': scope, 'host': host, 'endpoint': endpoint,
                'params': dict(params), 'quote': quote, 'window': window,
                'unit': 'unverified_liquidation_intensity', 'ticker': ticker,
                'latest_x': len(prices) - 1 if prices else None,
                'timestamps': {'last_candle_seconds': prices[-1][0] if prices else None,
                               'updateTime_ms': updated, 'received_ms': int(time.time() * 1000)}}}


def cg_home_stats():
    return cg_fetch('/api/futures/home/statistics', {})


def cg_oi_change_rank(limit=10, *, sort='h4OiChangePercent', order='desc', ex='all', page_num=1):
    """Locally capped to limit; server may return fewer (observed fixed ten)."""
    _positive_int(limit, 'limit'); _positive_int(page_num, 'page_num')
    if order not in ('asc', 'desc'):
        raise ValueError('Invalid order')
    data = cg_fetch('/api/home/oi/changeRank', {'sort': sort, 'order': order, 'ex': ex,
                    'pageNum': str(page_num), 'pageSize': str(limit)})
    if not isinstance(data, list):
        raise CoinGlassError('schema', 'invalid_rank', 'Expected rank array')
    return data[:limit]


def cg_coin_markets(limit=20, *, page_num=1, sort='h4PriceChangePercent', order='desc',
                    ex='all', keyword='', tags='', symbols='', filter=''):
    _positive_int(limit, 'limit'); _positive_int(page_num, 'page_num')
    if order not in ('', 'asc', 'desc'):
        raise ValueError('Invalid order')
    return cg_fetch('/api/home/v2/coinMarkets', {'pageNum': str(page_num), 'pageSize': str(limit),
        'sort': sort, 'order': order, 'ex': ex, 'keyword': keyword, 'tags': tags, 'symbols': symbols, 'filter': filter})


def cg_funding_chart(symbol='BTC', *, mode='snapshot', type='U', interval='h8'):
    """Rates already percent; sampling interval is NOT settlement interval."""
    if mode == 'snapshot':
        return cg_fetch('/api/fundingRate/chart', {'symbol': symbol})
    if mode == 'home':
        return cg_fetch('/api/fundingRate/v2/home', {})
    if mode != 'history' or type not in ('U', 'C') or interval not in ('m1', 'm5', 'h8'):
        raise ValueError('Invalid funding mode/type/interval')
    return cg_fetch('/api/fundingRate/v2/history/chart', {'symbol': symbol, 'type': type, 'interval': interval})


def annualize_funding_percent(rate_percent, funding_interval_hours):
    """Simple percent extrapolation, not compounded/realized yield. Missing stays unknown."""
    if rate_percent is None or funding_interval_hours is None:
        return None
    if isinstance(rate_percent, bool) or not isinstance(rate_percent, (int, float)) or not math.isfinite(rate_percent):
        raise ValueError('Invalid funding rate')
    _number(funding_interval_hours, 'funding interval hours', positive=True)
    return rate_percent * 24 * 365 / funding_interval_hours


def cg_liquidation_chart(symbol='BTC', *, window='90d', time_type=None, range=None):
    mapping = {'1d': 10, '7d': 2, '30d': 1, '90d': 4, 'all': 0}
    if range is not None:
        window = range
    if window not in mapping or (time_type is not None and time_type != mapping[window]):
        raise ValueError('Invalid liquidation timeType/range')
    return cg_fetch('/api/futures/liquidation/chart', {'symbol': symbol, 'timeType': mapping[window], 'range': window})


def cg_liquidation_info(*, time='h4', symbol='', mode='current'):
    if mode == 'legacy':
        return cg_fetch('/api/futures/liquidation/info', {})
    if mode != 'current' or time not in ('h1', 'h4', 'h12', 'h24'):
        raise ValueError('Invalid liquidation mode/time')
    return cg_fetch('/api/futures/liquidation/ex/info', {'time': time, 'symbol': symbol})


def cg_etf_flow(asset='BTC'):
    """Preserve changeUsd in USD and change in asset units; never multiply by million."""
    if asset not in ('BTC', 'ETH'):
        raise ValueError('Only BTC/ETH ETF endpoints verified')
    return cg_fetch('/api/etf/flow' if asset == 'BTC' else '/api/etf/eth/flow', {})


def cg_ahr999(*, legacy=False):
    """Index dimensionless; value/avg USD; date strings, variable history length."""
    return cg_fetch('/api/index/ahr999' if legacy else '/api/index/v2/ahr999', {})


def cg_fear_greed(*, legacy=False, size=None):
    """Aligned epoch-ms dates, 0–100 index points, USD prices."""
    if size is not None:
        _positive_int(size, 'size')
    data = cg_fetch('/api/index/fearGreed' if legacy else '/api/index/history', {} if legacy else {'size': '' if size is None else size})
    if not legacy:
        if not isinstance(data, list) or not data:
            raise CoinGlassError('schema', 'invalid_fear', 'Expected history array')
        data = data[0]
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in ('dates', 'values', 'prices')) or len({len(data[k]) for k in ('dates', 'values', 'prices')}) != 1:
        raise CoinGlassError('schema', 'unaligned_fear', 'Unaligned fear/greed series')
    return data


def cg_open_interest_chart(symbol='BTC', *, currency='USD', time_type=0, type=0):
    return cg_fetch('/api/openInterest/v3/chart', {'symbol': symbol, 'currency': currency,
                    'timeType': time_type, 'type': type, 'exchangeName': ''})


def cg_option_chart(symbol='BTC', *, ex='Deribit', type='Delivery', subtype='ALL', currency='USD'):
    if type not in ('Delivery', 'Strike'):
        raise ValueError('type must be Delivery/Strike, not oi/vol')
    return cg_fetch('/api/option/v2/chart', {'symbol': symbol, 'ex': ex, 'type': type, 'subtype': subtype, 'currency': currency})


def cg_option_max_pain(symbol='BTC', *, ex='Deribit'):
    return cg_fetch('/api/option/strike_pain', {'symbol': symbol, 'ex': ex})


def cg_exchange_balance(symbol='BTC', *, ex_name='all'):
    """Chain balance in asset units, not USD trade flow."""
    return cg_fetch('/api/exchange/chain/v3/balance', {'symbol': symbol, 'exName': ex_name})


def cg_spot_markets(limit=20, *, page_num=1, sort='', order='', keyword=''):
    _positive_int(limit, 'limit'); _positive_int(page_num, 'page_num')
    return cg_fetch('/api/spot/coin/markets', {'pageSize': limit, 'pageNum': page_num, 'sort': sort, 'order': order, 'keyword': keyword})


def cg_rsi(*, scope='spot', limit=500, page_num=1):
    if scope == 'futures':
        return cg_fetch('/api/index/rsiMap', {})
    if scope != 'spot':
        raise ValueError('scope must be spot/futures')
    _positive_int(limit, 'limit'); _positive_int(page_num, 'page_num')
    return cg_fetch('/api/spot/rsi/list', {'pageSize': limit, 'pageNum': page_num})
