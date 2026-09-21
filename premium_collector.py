#!/usr/bin/env python3
"""premium_collector.py — Coinbase/Binance 现货溢价常驻收集

每分钟拉一次两所价格,计算溢价存 SQLite. 供 market_scan.py 读取做趋势分析.

数据表: premium(symbol, ts, coinbase, binance, diff, pct)
"""
import json, sqlite3, time, logging
from pathlib import Path
import urllib.request

STATE_DIR = Path('/var/lib/upi-hermes/.hermes/state/premium')
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB = STATE_DIR/'premium.db'
LOG = STATE_DIR/'collector.log'

from logging.handlers import RotatingFileHandler
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
    handlers=[RotatingFileHandler(LOG, maxBytes=500_000, backupCount=1), logging.StreamHandler()])
log = logging.getLogger('premium')

SYMBOLS = [('BTC','BTC-USD','BTCUSDT'), ('ETH','ETH-USD','ETHUSDT'), ('SOL','SOL-USD','SOLUSDT')]
INTERVAL = 60  # 秒
KEEP_DAYS = 30  # 保留30天

def get(url):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
    return urllib.request.urlopen(req, timeout=10).read()

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS premium
        (sym TEXT, ts INTEGER, coinbase REAL, binance REAL, diff REAL, pct REAL,
         PRIMARY KEY(sym, ts))''')
    conn.execute(f'DELETE FROM premium WHERE ts < {int(time.time())-KEEP_DAYS*86400}')
    conn.commit()
    return conn

def collect_once(conn):
    for sym, cb_pair, bn_sym in SYMBOLS:
        try:
            cb = json.loads(get(f'https://api.exchange.coinbase.com/products/{cb_pair}/ticker'))
            cb_p = float(cb['price'])
            bn = json.loads(get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={bn_sym}'))
            bn_p = float(bn['price'])
            diff = cb_p - bn_p
            conn.execute('INSERT OR REPLACE INTO premium VALUES (?,?,?,?,?,?)',
                (sym, int(time.time()), cb_p, bn_p, diff, diff/bn_p*100))
            conn.commit()
        except Exception as e:
            log.error(f'{sym}: {e}')

if __name__=='__main__':
    log.info('premium_collector start')
    conn = init_db()
    while True:
        collect_once(conn)
        time.sleep(INTERVAL)
