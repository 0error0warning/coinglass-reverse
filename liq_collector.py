#!/usr/bin/env python3
"""Public liquidation collector, schema 2. Import has no filesystem/network effects.

LIQ_STATE_DIR defaults to ~/.local/state/coinglass/liq_map. Legacy/corrupt files
are preserved and block replacement; move them aside explicitly to start fresh.
Single writer per state directory. Fingerprint dedup is not exchange-ID identity.
"""
import copy
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from urllib.request import urlopen
from urllib.request import Request
from collections import defaultdict
from liquidation_events import (DEFAULT_SYMBOLS, normalize_binance,
                                normalize_bybit, normalize_okx, valid_event)

STATE_DIR = Path(os.environ.get('LIQ_STATE_DIR', str(Path.home()/'.local/state/coinglass/liq_map')))
SYMBOLS = sorted(DEFAULT_SYMBOLS)
MAX_EVENTS_PER_SYM = 50000
log = logging.getLogger('liq')


class Collector:
    def __init__(self, state_dir=None, *, retention_ms=72*3600000, max_events=MAX_EVENTS_PER_SYM):
        self.state_dir = Path(state_dir) if state_dir is not None else STATE_DIR
        self.retention_ms = retention_ms
        self.max_events = max_events
        self._store = defaultdict(list)
        self._lock = threading.RLock()
        self.status = {}
        self._records_accepted = 0
        self._flush_errors = 0
        self._last_persisted_ms = None
        self._last_flush_ms = None

    @property
    def pending_count(self):
        with self._lock: return sum(map(len, self._store.values()))

    def record(self, event):
        if not valid_event(event):
            log.warning('reject invalid schema-2 event'); return False
        with self._lock:
            events = self._store[event['symbol']]
            if any(e['event_id'] == event['event_id'] for e in events): return False
            if len(events) >= self.max_events:
                log.error('buffer full for %s; incoming event rejected', event['symbol']); return False
            events.append(copy.deepcopy(event))
            self._records_accepted += 1
        return True

    def update_source(self, source, **fields):
        with self._lock:
            status = self.status.setdefault(source, {'acknowledged':False, 'connected':False, 'errors':0})
            status.update(fields)

    def increment_source_error(self, source):
        with self._lock:
            status = self.status.setdefault(source, {'acknowledged':False, 'connected':False, 'errors':0})
            status['errors'] = int(status.get('errors') or 0) + 1

    def note_source_event(self, source, event_ms):
        with self._lock:
            status = self.status.setdefault(source, {'acknowledged':False, 'connected':False, 'errors':0})
            status['last_accepted_event_ms'] = int(event_ms)

    def health_snapshot(self, *, now_ms=None):
        now = int(time.time()*1000) if now_ms is None else int(now_ms)
        with self._lock:
            sources = {name:{'connected':bool(status.get('connected')),
                             'acknowledged':bool(status.get('acknowledged')),
                             'errors':int(status.get('errors') or 0),
                             'last_accepted_event_ms':status.get('last_accepted_event_ms')}
                       for name,status in sorted(self.status.items())}
            return {'schema_version':1, 'updated_ms':now, 'pending_count':sum(map(len, self._store.values())),
                    'sources':sources, 'persistence':{'records_accepted':self._records_accepted,
                    'flush_errors':self._flush_errors, 'last_persisted_ms':self._last_persisted_ms,
                    'last_flush_ms':self._last_flush_ms}}

    def write_health(self, *, now_ms=None):
        health_dir = self.state_dir / '_health'
        now = int(time.time()*1000) if now_ms is None else int(now_ms)
        health_dir.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=health_dir, prefix='.status', suffix='.tmp', delete=False) as f:
                tmp = f.name
                json.dump(self.health_snapshot(now_ms=now), f, allow_nan=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, health_dir/'status.json')
        finally:
            if tmp and os.path.exists(tmp):
                try: os.unlink(tmp)
                except OSError: log.warning('cannot clean temporary health file %s', tmp)

    def flush_once(self, *, now_ms=None):
        now = int(time.time()*1000) if now_ms is None else int(now_ms)
        result = {'written': [], 'errors': {}}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            with self._lock:
                self._flush_errors += 1
                self._last_flush_ms = now
            log.error('state directory unavailable: %s', exc)
            result['errors']['state_dir'] = str(exc); return result
        with self._lock:
            symbols = set(self._store) | {p.stem for p in self.state_dir.glob('*.json')}
            for sym in sorted(symbols):
                path = self.state_dir / (sym + '.json')
                tmp = None
                try:
                    old = []
                    if path.exists():
                        data = json.loads(path.read_text())
                        if not isinstance(data, dict) or data.get('schema_version') != 2 or not isinstance(data.get('events'), list):
                            raise ValueError('legacy/corrupt file preserved; explicit migration required')
                        if any(not valid_event(e) or e['symbol'] != sym for e in data['events']):
                            raise ValueError('invalid schema-2 records; original file preserved')
                        old = data['events']
                    merged = {}
                    for event in old + self._store[sym]:
                        if now-self.retention_ms <= event['exchange_ts_ms'] <= now:
                            merged.setdefault(event['event_id'], event)
                    events = sorted(merged.values(), key=lambda e:e['exchange_ts_ms'])
                    if len(events) > self.max_events:
                        log.warning('disk retention capped for %s; oldest events removed', sym)
                        events = events[-self.max_events:]
                    with tempfile.NamedTemporaryFile('w', dir=self.state_dir, prefix='.'+sym, suffix='.tmp', delete=False) as f:
                        tmp = f.name
                        json.dump({'schema_version':2, 'events':events, 'updated_ms':now}, f, allow_nan=False)
                        f.flush(); os.fsync(f.fileno())
                    os.replace(tmp, path)
                    self._store.pop(sym, None)
                    result['written'].append(sym)
                    self._last_persisted_ms = now
                except (OSError, ValueError, TypeError) as exc:
                    log.error('flush %s failed; buffer retained: %s', sym, exc)
                    result['errors'][sym] = str(exc)
                    self._flush_errors += 1
                finally:
                    if tmp and os.path.exists(tmp):
                        try: os.unlink(tmp)
                        except OSError: log.warning('cannot clean temporary file %s', tmp)
            self._last_flush_ms = now
            pending = sum(map(len, self._store.values()))
        log.info('flush summary written=%d errors=%d pending=%d', len(result['written']), len(result['errors']), pending)
        return result

    def flush(self, stop_event, interval=60):
        while not stop_event.wait(interval):
            self.flush_once()
            try: self.write_health()
            except OSError as exc: log.error('health write failed: %s', exc)
        self.flush_once()
        try: self.write_health()
        except OSError as exc: log.error('health write failed: %s', exc)


def load_okx_instruments(symbols=DEFAULT_SYMBOLS):
    """Public metadata lookup outside callbacks, refreshed each reconnection."""
    request = Request('https://www.okx.com/api/v5/public/instruments?instType=SWAP',
                      headers={'User-Agent':'Mozilla/5.0'})
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if str(data.get('code')) != '0': raise ValueError('OKX instruments request failed')
    return {r['instId']:r for r in data['data'] if r['instId'].replace('-SWAP','').replace('-','') in symbols}


def run_source(source, collector, stop_event, *, instruments=None, ws_factory=None, backoff=5, ack_timeout=15):
    if stop_event.is_set(): return
    if ws_factory is None:
        import websocket
        websocket.setdefaulttimeout(15)
        ws_factory = websocket.WebSocketApp
    urls = {'binance':'wss://fstream.binance.com/market/ws',
            'bybit':'wss://stream.bybit.com/v5/public/linear',
            'okx':'wss://ws.okx.com:8443/ws/v5/public'}
    subscription = {'binance':{'method':'SUBSCRIBE','params':['!forceOrder@arr'],'id':1},
                    'bybit':{'op':'subscribe','args':['allLiquidation.'+s for s in SYMBOLS], 'req_id':'liq'},
                    'okx':{'op':'subscribe','args':[{'channel':'liquidation-orders','instType':'SWAP'}]}}[source]
    attempt = 0
    while not stop_event.is_set():
        status = {'acknowledged':False, 'connected':False, 'errors':0}
        collector.update_source(source, **status)
        try:
            metadata = instruments if instruments is not None else (load_okx_instruments() if source == 'okx' else {})
            done = threading.Event()
            opened = threading.Event()
            opened_at = [0.0]
            def on_open(ws):
                status['connected'] = True
                collector.update_source(source, connected=True)
                opened_at[0] = time.monotonic(); opened.set()
                ws.send(json.dumps(subscription))
            def on_message(ws, message):
                if message == 'pong': return
                try:
                    data = json.loads(message)
                    if not isinstance(data, (dict, list)): raise ValueError('invalid message shape')
                    if isinstance(data, dict):
                        if data.get('event') == 'error' or data.get('success') is False or ('code' in data and str(data['code']) != '0'):
                            raise ValueError('subscription/server error: '+str(data.get('msg', data.get('ret_msg', data.get('code'))))[:180])
                        ack = ((source == 'binance' and data.get('id') == 1 and 'result' in data and data['result'] is None)
                               or (source == 'bybit' and data.get('op') == 'subscribe' and data.get('success') is True)
                               or (source == 'okx' and data.get('event') == 'subscribe' and data.get('arg') == subscription['args'][0]))
                        if ack:
                            status['acknowledged'] = True; collector.update_source(source, acknowledged=True); log.info('%s subscription ACK', source); return
                    if source == 'binance': events = normalize_binance(data)
                    elif source == 'bybit': events = normalize_bybit(data)
                    else: events = normalize_okx(data, metadata)
                    accepted = False
                    for event in events:
                        if collector.record(event):
                            accepted = True
                    if accepted:
                        last_event_ms = int(time.time()*1000)
                        status['last_accepted_event_ms'] = last_event_ms
                        collector.note_source_event(source, last_event_ms)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    status['errors'] += 1
                    collector.increment_source_error(source)
                    log.warning('%s message rejected: %s', source, str(exc)[:200])
            def on_error(ws, error):
                status['errors'] += 1; collector.increment_source_error(source); log.error('%s websocket error: %s', source, str(error)[:200])
            def on_close(ws, *args):
                status['connected'] = False; collector.update_source(source, connected=False); log.info('%s closed', source)
            ws = ws_factory(urls[source], on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            def watch():
                last_ping = time.monotonic()
                while not done.wait(.1):
                    if stop_event.is_set(): ws.close(); return
                    if opened.is_set() and not status['acknowledged'] and time.monotonic()-opened_at[0] >= ack_timeout:
                        status['errors'] += 1; collector.increment_source_error(source); log.error('%s subscription ACK timeout', source); ws.close(); return
                    if source == 'okx' and opened.is_set() and time.monotonic()-last_ping > 20:
                        try: ws.send('ping')
                        except Exception as exc: on_error(ws, exc); ws.close(); return
                        last_ping = time.monotonic()
            watcher = threading.Thread(target=watch, daemon=True); watcher.start()
            try: ws.run_forever(ping_interval=20, ping_timeout=10)
            finally:
                done.set(); ws.close(); watcher.join(timeout=1)
                collector.update_source(source, connected=False)
            attempt = 0 if status['acknowledged'] else min(attempt+1, 5)
        except Exception as exc:
            status['errors'] += 1; collector.increment_source_error(source); log.error('%s source failed: %s', source, str(exc)[:200])
            attempt = min(attempt+1, 5)
        if stop_event.wait(min(60, backoff * (2**attempt))): return


_default = None

def _collector():
    global _default
    if _default is None: _default = Collector()
    return _default

def record(event): return _collector().record(event)
def flush_once(**kwargs): return _collector().flush_once(**kwargs)
def write_health(**kwargs): return _collector().write_health(**kwargs)
def flush(stop_event=None): return _collector().flush(stop_event or threading.Event())
def binance_ws(stop_event=None): return run_source('binance', _collector(), stop_event or threading.Event())
def bybit_ws(stop_event=None): return run_source('bybit', _collector(), stop_event or threading.Event())
def okx_ws(stop_event=None): return run_source('okx', _collector(), stop_event or threading.Event())


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda *a: stop.set())
    collector = Collector()
    threads = [threading.Thread(target=run_source, args=(s,collector,stop), daemon=True) for s in ('binance','bybit','okx')]
    threads.append(threading.Thread(target=collector.flush, args=(stop,), daemon=True))
    for thread in threads: thread.start()
    try: collector.write_health()
    except OSError as exc: log.error('health write failed: %s', exc)
    while not stop.wait(1):
        if any(not t.is_alive() for t in threads):
            log.error('collector worker died; stopping'); stop.set()
        try: collector.write_health()
        except OSError as exc: log.error('health write failed: %s', exc)
    for thread in threads: thread.join(timeout=20)
    collector.flush_once()
    try: collector.write_health()
    except OSError as exc: log.error('health write failed: %s', exc)

if __name__ == '__main__': main()
