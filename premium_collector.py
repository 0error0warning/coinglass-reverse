#!/usr/bin/env python3
"""Unadjusted Coinbase USD spot minus Binance USDT perpetual price history.

Not a same-currency spot premium or identified institutional/geographic flow.
Prices are sampled sequentially. No directories, files or network at import.
"""
import json
import logging
import math
import os
import sqlite3
import signal
import threading
import time
import urllib.request
from pathlib import Path

STATE_DIR=Path(os.environ.get('PREMIUM_STATE_DIR',str(Path.home()/'.local/state/coinglass/premium')))
DB=STATE_DIR/'premium.db'
log=logging.getLogger('premium')
SYMBOLS=[('BTC','BTC-USD','BTCUSDT'),('ETH','ETH-USD','ETHUSDT'),('SOL','SOL-USD','SOLUSDT')]
INTERVAL=60
KEEP_DAYS=30


def get(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=10) as response:
        return response.read()


def prune(conn):
    conn.execute('DELETE FROM premium WHERE ts < ?',(int(time.time())-KEEP_DAYS*86400,))


def init_db():
    DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB)
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS premium (sym TEXT, ts INTEGER, coinbase REAL, binance REAL, diff REAL, pct REAL, PRIMARY KEY(sym, ts))')
        prune(conn)
    return conn


def collect_once(conn):
    statuses=[]
    for sym,cb_pair,bn_sym in SYMBOLS:
        try:
            cb=float(json.loads(get(f'https://api.exchange.coinbase.com/products/{cb_pair}/ticker'))['price'])
            bn=float(json.loads(get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={bn_sym}'))['price'])
            if not all(math.isfinite(p) and p>0 for p in (cb,bn)):
                raise ValueError('prices must be positive finite numbers')
            with conn:
                conn.execute('INSERT OR REPLACE INTO premium (sym,ts,coinbase,binance,diff,pct) VALUES (?,?,?,?,?,?)',
                             (sym,int(time.time()),cb,bn,cb-bn,(cb-bn)/bn*100))
            statuses.append({'symbol':sym,'status':'ok'})
        except Exception as exc:
            log.warning('%s collection failed (%s)',sym,type(exc).__name__)
            statuses.append({'symbol':sym,'status':'error','error_type':type(exc).__name__})
    # Run even with no symbols or all upstream requests failing.
    with conn:
        prune(conn)
    return statuses


def main(stop_event=None):
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    owned_stop = stop_event is None
    if owned_stop:
        stop_event = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *a: stop_event.set())
    conn=init_db()
    try:
        while not stop_event.is_set():
            try:collect_once(conn)
            except Exception as exc:log.error('collection/retention failed (%s)',type(exc).__name__)
            if stop_event.wait(INTERVAL):break
    except KeyboardInterrupt:
        if owned_stop: stop_event.set()
        pass
    finally:conn.close()


if __name__=='__main__':
    main()
