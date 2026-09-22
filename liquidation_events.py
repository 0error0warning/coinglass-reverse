"""Schema-2 liquidation adapters. Pure functions; no network/filesystem access.

Fingerprint identity is best-effort: identical no-ID events can collide. Binance
cumulative snapshots are NEVER additive. All feeds have coverage limitations.
"""
import hashlib
import json
import logging
import math
import re
import time

log = logging.getLogger('liq.normalization')
_rejections = {}


def _warn(source, reason):
    # Bounded keys; retain first diagnostic and exponentially spaced reminders.
    count = _rejections.get(source, 0) + 1
    _rejections[source] = count
    if count == 1 or count & (count - 1) == 0:
        log.warning('%s rejected events=%s: %s', source, count, str(reason)[:160])


DEFAULT_SYMBOLS = frozenset(x + 'USDT' for x in ('BTC','ETH','SOL','UNI','ZEC','TAO','SUI','HYPE'))


def _positive(value):
    if isinstance(value, bool):
        raise ValueError('boolean numeric value')
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError('nonpositive/nonfinite value')
    return n


def _timestamp(value):
    n = _positive(value)
    if n != int(n):
        raise ValueError('timestamp must be integer milliseconds')
    return int(n)


def _pair(symbol, symbols):
    if symbol not in symbols:
        raise ValueError('instrument not whitelisted')
    for quote in ('USDT','USDC','USD'):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)], quote
    raise ValueError('unknown quote unit')


def _event(ex, instrument, base, quote, qty, unit, base_qty, price,
           price_kind, quantity_kind, side, ts, received, additive, limitation):
    if side not in ('long','short'):
        raise ValueError('unknown liquidated side')
    result = dict(schema_version=2, symbol=base+quote, ex=ex, instrument=instrument,
                  base_asset=base, quote_asset=quote, raw_qty=_positive(qty),
                  raw_unit=unit, base_qty=_positive(base_qty), price=_positive(price),
                  price_kind=price_kind, quantity_kind=quantity_kind,
                  liquidated_side=side, exchange_ts_ms=_timestamp(ts),
                  received_ts_ms=_timestamp(received),
                  coverage={'sampled': True, 'additive': additive,
                            'limitation': limitation, 'identity': 'content_fingerprint_best_effort'})
    identity = {k:v for k,v in result.items() if k not in ('received_ts_ms','coverage')}
    result['event_id'] = ex + ':' + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',',':')).encode()).hexdigest()
    return result


def _received(value):
    return int(time.time()*1000) if value is None else value


def normalize_binance(payload, *, received_ts_ms=None, symbols=DEFAULT_SYMBOLS):
    """UM only; st=2 CM contracts cannot be treated as base asset quantities."""
    out = []
    payload = payload.get('data', payload) if isinstance(payload, dict) else payload
    for item in payload if isinstance(payload, list) else [payload]:
        try:
            o = item['o']
            if str(o.get('st')) != '1':
                raise ValueError('missing/unsupported Binance instrument type')
            base, quote = _pair(o['s'], symbols)
            out.append(_event('binance', o['s'], base, quote, o['z'], 'base_asset', o['z'], o['ap'],
                              'average_filled_price', 'cumulative_filled_snapshot',
                              {'BUY':'short','SELL':'long'}[o['S']], o['T'], _received(received_ts_ms),
                              False, 'latest order snapshot per symbol per 1000ms; not incremental fills'))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            _warn('binance', exc)
    return out


def normalize_bybit(payload, *, received_ts_ms=None, symbols=DEFAULT_SYMBOLS):
    out = []
    if not isinstance(payload, dict) or not str(payload.get('topic','')).startswith('allLiquidation.'):
        return out
    rows = payload.get('data')
    if not isinstance(rows, list):
        _warn('bybit', 'data must be a list'); return out
    for item in rows:
        try:
            base, quote = _pair(item['s'], symbols)
            if payload['topic'] != 'allLiquidation.' + item['s']:
                raise ValueError('topic/instrument mismatch')
            if quote not in ('USDT','USDC'):
                raise ValueError('Bybit adapter supports linear instruments only')
            out.append(_event('bybit', item['s'], base, quote, item['v'], 'base_asset', item['v'], item['p'],
                              'bankruptcy_price', 'liquidation_quantity', {'Buy':'long','Sell':'short'}[item['S']],
                              item['T'], _received(received_ts_ms), True,
                              'allLiquidation public feed; connection gaps and fingerprint collisions possible'))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            _warn('bybit', exc)
    return out


def normalize_okx(payload, instruments, *, received_ts_ms=None, symbols=DEFAULT_SYMBOLS):
    out = []
    if not isinstance(payload, dict) or payload.get('arg',{}).get('channel') != 'liquidation-orders':
        return out
    rows = payload.get('data', [])
    if not isinstance(rows, list):
        _warn('okx', 'data must be a list'); return out
    for item in rows:
        try:
            inst = item['instId']
            base, quote, kind = inst.split('-')
            if kind != 'SWAP' or base+quote not in symbols:
                raise ValueError('instrument not whitelisted')
            meta = instruments[inst]
            if meta.get('instId',inst) != inst:
                raise ValueError('metadata instrument mismatch')
            val = _positive(meta['ctVal']) * _positive(meta['ctMult'])
        except (KeyError, TypeError, ValueError) as exc:
            _warn('okx', exc); continue
        details = item.get('details', [])
        if not isinstance(details, list):
            _warn('okx', 'details must be a list'); continue
        for detail in details:
            try:
                qty, price = _positive(detail['sz']), _positive(detail['bkPx'])
                if meta['ctType'] == 'linear' and meta['ctValCcy'] == base:
                    base_qty = qty * val
                elif meta['ctType'] == 'inverse' and meta['ctValCcy'] == quote:
                    base_qty = qty * val / price
                else:
                    raise ValueError('unsupported contract unit')
                side = detail['posSide']
                if side == 'net': side = {'buy':'short','sell':'long'}[detail['side']]
                out.append(_event('okx', inst, base, quote, qty, 'contracts', base_qty, price,
                                  'liquidation_transfer_price', 'liquidation_quantity', side, detail['ts'],
                                  _received(received_ts_ms), True,
                                  'public liquidation-orders is incomplete; transfer price is not market execution'))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                _warn('okx', exc)
    return out


def valid_event(event):
    """Strict enough to reject legacy/mixed-unit records at persistence boundaries."""
    try:
        if event['schema_version'] != 2 or event['ex'] not in ('binance','bybit','okx'):
            return False
        if not re.fullmatch(r'[A-Z0-9]+', event['symbol']): return False
        if event['symbol'] != event['base_asset'] + event['quote_asset']: return False
        if not event['instrument'] or not event['event_id']: return False
        if event['liquidated_side'] not in ('long','short'): return False
        if event['raw_unit'] not in ('base_asset','contracts'): return False
        if event['quote_asset'] not in ('USDT','USDC','USD'): return False
        if event['ex'] in ('binance','bybit'):
            if event['instrument'] != event['symbol'] or event['raw_unit'] != 'base_asset': return False
            if event['raw_qty'] != event['base_qty']: return False
        elif (event['raw_unit'] != 'contracts' or event['instrument'] !=
              event['base_asset'] + '-' + event['quote_asset'] + '-SWAP'):
            return False
        for key in ('raw_qty','base_qty','price'): _positive(event[key])
        for key in ('exchange_ts_ms','received_ts_ms'): _timestamp(event[key])
        if not isinstance(event['coverage']['additive'], bool): return False
        if event['ex'] == 'binance':
            return (event['quantity_kind'] == 'cumulative_filled_snapshot'
                    and event['price_kind'] == 'average_filled_price'
                    and event['coverage']['additive'] is False)
        return event['quantity_kind'] == 'liquidation_quantity' and event['price_kind'] in ('bankruptcy_price','liquidation_transfer_price')
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
