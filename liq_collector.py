#!/usr/bin/env python3
"""liq_collector.py — 爆仓流常驻收集器

订阅:
  - Binance fapi WS  !forceOrder@arr  (全市场爆仓推送)
  - Bybit v5 WS      liquidation      (BTCUSDT/ETHUSDT 等)
  - OKX v5 WS        liquidation-orders (币本位+USDT 永续)

输出: ~/.hermes/state/liq_map/{symbol}.json
  {"events": [{ts, ex, price, qty, side}],  # side: 'long'=多爆 'short'=空爆
   "updated": ts}
每桶在读取端按价格分桶,收集端只存原始事件,72h 滑动窗口.

运行:
  python3 liq_collector.py            # 前台
  systemd unit: liq-collector.service
"""
import json, os, sys, time, threading, logging
from pathlib import Path
from collections import defaultdict
from logging.handlers import RotatingFileHandler

try:
    import websocket  # websocket-client
except ImportError:
    print('need websocket-client: uv pip install --python ~/.venvs/liq/bin/python websocket-client')
    sys.exit(1)

STATE_DIR = Path('/var/lib/upi-hermes/.hermes/state/liq_map')
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG = STATE_DIR/'collector.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=2),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('liq')

# 内存事件缓存 — {symbol: [events]}
_store = defaultdict(list)
_lock = threading.Lock()
SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','UNIUSDT','ZECUSDT','TAOUSDT','SUIUSDT','HYPEUSDT']
MAX_EVENTS_PER_SYM = 50000  # 防爆内存

def record(symbol, exchange, price, qty, side):
    """side: 'BUY'=空单被爆(强平买入) / 'SELL'=多单被爆(强平卖出)"""
    ev = {'ts': int(time.time()), 'ex': exchange, 'price': price, 'qty': qty,
          'side': 'short' if side in ('BUY','Buy') else 'long'}
    with _lock:
        _store[symbol].append(ev)
        if len(_store[symbol]) > MAX_EVENTS_PER_SYM:
            _store[symbol] = _store[symbol][-MAX_EVENTS_PER_SYM//2:]

def flush():
    """每 60s 把内存事件追加到磁盘,合并并按 72h 滑动窗口裁剪"""
    while True:
        time.sleep(60)
        wrote = []
        with _lock:
            cutoff = int(time.time()) - 72*3600
            for sym, evs in _store.items():
                if not evs: continue
                f = STATE_DIR/f'{sym}.json'
                old_evs = []
                if f.exists():
                    try: old_evs = json.loads(f.read_text()).get('events',[])
                    except Exception: pass
                merged = old_evs + evs
                merged = [e for e in merged if e['ts'] > cutoff]
                tmp = f.with_suffix('.tmp')
                tmp.write_text(json.dumps({'events':merged,'updated':int(time.time())}))
                tmp.replace(f)
                wrote.append(f'{sym}(+{len(evs)})')
                _store[sym] = []
        if wrote:
            log.info(f'flushed: {" ".join(wrote)}')

# ---- Binance ----
def binance_ws():
    url = 'wss://fstream.binance.com/ws/!forceOrder@arr'
    def on_msg(ws,msg):
        try:
            d = json.loads(msg)
            o = d.get('o',{})
            sym = o.get('s'); side = o.get('S')
            price = float(o.get('p',0)); qty = float(o.get('q',0))
            if sym and price and qty:
                record(sym, 'binance', price, qty, side)
        except Exception: pass
    def on_err(ws,e): log.error(f'binance ws err {e}')
    def on_close(ws,*a):
        log.info('binance ws closed, reconnect in 5s'); time.sleep(5); binance_ws()
    ws = websocket.WebSocketApp(url, on_message=on_msg, on_error=on_err, on_close=on_close)
    ws.run_forever(ping_interval=20)

# ---- Bybit ----
def bybit_ws():
    url = 'wss://stream.bybit.com/v5/public/linear'
    def on_open(ws):
        for s in SYMBOLS:
            ws.send(json.dumps({'op':'subscribe','args':[f'liquidation.{s}']}))
        log.info('bybit subscribed')
    def on_msg(ws,msg):
        try:
            d = json.loads(msg)
            if d.get('topic','').startswith('liquidation.'):
                data = d.get('data',{})
                sym = data.get('symbol'); side = data.get('side')
                price = float(data.get('price',0)); qty = float(data.get('size',0))
                if sym and price and qty:
                    record(sym, 'bybit', price, qty, side)
        except Exception: pass
    def on_err(ws,e): log.error(f'bybit ws err {e}')
    def on_close(ws,*a):
        log.info('bybit ws closed, reconnect in 5s'); time.sleep(5); bybit_ws()
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg, on_error=on_err, on_close=on_close)
    ws.run_forever(ping_interval=20)

# ---- OKX ----
# OKX 用 instFamily 订阅,比如 BTC-USDT 会覆盖 BTC-USDT-SWAP
def okx_ws():
    url = 'wss://ws.okx.com:8443/ws/v5/public'
    families = ['BTC-USDT','ETH-USDT','SOL-USDT','UNI-USDT','ZEC-USDT','TAO-USDT','SUI-USDT','HYPE-USDT']
    def on_open(ws):
        args = [{'channel':'liquidation-orders','instFamily':f} for f in families]
        ws.send(json.dumps({'op':'subscribe','args':args}))
        log.info('okx subscribed')
    def on_msg(ws,msg):
        try:
            d = json.loads(msg)
            if d.get('arg',{}).get('channel')=='liquidation-orders':
                for data in d.get('data',[]):
                    inst = data.get('instId','')  # e.g. BTC-USDT-SWAP
                    sym = inst.replace('-SWAP','').replace('-','')
                    for detail in data.get('details',[]):
                        price = float(detail.get('bkPx',0) or 0)
                        qty = float(detail.get('sz',0) or 0)
                        side = detail.get('posSide','')  # 'long' or 'short'
                        if sym and price and qty:
                            # OKX posSide 直接给多/空,反向映射回 BUY/SELL 语义
                            record(sym, 'okx', price, qty, 'BUY' if side=='short' else 'SELL')
        except Exception: pass
    def on_err(ws,e): log.error(f'okx ws err {e}')
    def on_close(ws,*a):
        log.info('okx ws closed, reconnect in 5s'); time.sleep(5); okx_ws()
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg, on_error=on_err, on_close=on_close)
    ws.run_forever(ping_interval=25, ping_payload='ping')

if __name__=='__main__':
    log.info('liq_collector start')
    threading.Thread(target=flush, daemon=True).start()
    threading.Thread(target=bybit_ws, daemon=True).start()
    threading.Thread(target=okx_ws, daemon=True).start()
    binance_ws()
