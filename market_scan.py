#!/usr/bin/env python3
"""market_scan.py — 加密市场情绪全聚合扫描 (免费数据源)

一条命令出完整报告:
  1. 四所合约情绪 (Coinalyze): OI变化 / 清算 / 多空比 / 资金费率
  2. Deribit 期权 gamma 墙: 按行权价的 call/put OI 分布 + max pain
  3. DefiLlama 稳定币: 总量 + 7日增减
  4. ETF 净流入: Farside 昨日数据 (反爬已处理)
  5. BTC 现货价格 + 24h

用法:
  python3 market_scan.py            # BTC 全景
  python3 market_scan.py UNIUSDT    # 单币种 (Coinalyze only)
  python3 market_scan.py --etf      # 只看 ETF
"""
import urllib.request, urllib.parse, urllib.error
import json, os, sys, time, re, hmac, hashlib, struct, base64
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

# CoinGlass 内部 API 解密 (coinglass-decrypt)
sys.path.insert(0, str(Path(__file__).parent))
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    from coinglass_decrypt import fetch_and_decrypt
    CG_AVAILABLE = True
except ImportError:
    CG_AVAILABLE = False

HOME = Path('/var/lib/upi-hermes')
KEY_FILE = HOME/'.hermes/secrets/market-apis.env'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

def _key(name):
    if not KEY_FILE.exists(): return None
    for line in KEY_FILE.read_text().splitlines():
        if line.startswith(name+'='):
            return line.split('=',1)[1].strip()
    return None

CY = _key('COINALYZE_KEY')
EX_NAME = {'A':'Binance','6':'Bybit','3':'OKX','4':'HTX','H':'Hyperliquid','S':'Aster'}

def get(url, headers=None, timeout=20):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout).read()

def cy_get(path):
    if not CY: return None
    return json.loads(get('https://api.coinalyze.net'+path, {'api-key':CY}, 25))

# ============ 1. Coinalyze 四所情绪 ============
_markets_cache = None
def find_perp(base, quote='USDT'):
    global _markets_cache
    if _markets_cache is None:
        _markets_cache = cy_get('/v1/future-markets') or []
    want = base.upper()+quote
    out = {}
    for m in _markets_cache:
        if m.get('symbol_on_exchange','').replace('-','')==want and m.get('is_perpetual') and m.get('margined')=='STABLE':
            out[m['exchange']] = m['symbol']
    return out

def hist(symbol, endpoint, hours=24):
    now = int(time.time()); frm = now - hours*3600
    d = cy_get(f'/v1/{endpoint}?symbols={symbol}&interval=4hour&from={frm}&to={now}')
    if d and isinstance(d,list):
        return d[0].get('history',[])
    return []

def scan_sentiment(base='BTC', quote='USDT', hours=24):
    perps = find_perp(base, quote)
    rows = []
    for ex in ['A','6','3','H']:
        sym = perps.get(ex)
        if not sym: continue
        try:
            oi  = hist(sym,'open-interest-history',hours)
            lq  = hist(sym,'liquidation-history',hours)
            lsr = hist(sym,'long-short-ratio-history',hours)
            fr  = hist(sym,'funding-rate-history',hours)
            oi_chg = (oi[-1]['c']/oi[0]['o']-1)*100 if len(oi)>=2 and oi[0]['o'] else None
            long_liq = sum(x.get('l',0) for x in lq)
            short_liq= sum(x.get('s',0) for x in lq)
            ls_now = (lsr[-1].get('l'), lsr[-1].get('s')) if lsr else None
            fr_now = fr[-1]['c'] if fr else None
            rows.append({'ex':EX_NAME.get(ex,ex),'oi_chg':oi_chg,'long_liq':long_liq,
                         'short_liq':short_liq,'ls':ls_now,'fr':fr_now})
            time.sleep(0.3)
        except Exception as e:
            rows.append({'ex':EX_NAME.get(ex,ex),'err':str(e)[:50]})
    return rows

# ============ 2. Deribit gamma 墙 ============
def deribit_walls():
    """返回 {spot, nearest_expiry, strikes:{strike:{call,put}}, max_pain, total_oi}"""
    d = json.loads(get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option', timeout=30))
    res = d.get('result',[])
    if not res: return None
    spot = res[0].get('estimated_delivery_price')
    # 找最近到期日 (OI 最大的)
    ex_oi = defaultdict(float)
    for o in res:
        ex_oi[o['instrument_name'].split('-')[1]] += o.get('open_interest') or 0
    expiry = max(ex_oi, key=ex_oi.get)
    # 按行权价聚合该到期日
    strikes = defaultdict(lambda:{'call':0.0,'put':0.0})
    for o in res:
        p = o['instrument_name'].split('-')
        if p[1]!=expiry: continue
        s = float(p[2]); cp = 'call' if p[3]=='C' else 'put'
        strikes[s][cp] += o.get('open_interest') or 0
    # max pain
    max_pain, min_v = None, None
    for c in strikes:
        v = sum(max(0,c-s)*v_['call'] + max(0,s-c)*v_['put'] for s,v_ in strikes.items())
        if min_v is None or v<min_v: max_pain,min_v = c,v
    return {'spot':spot,'expiry':expiry,'strikes':dict(strikes),'max_pain':max_pain,'total_oi':ex_oi[expiry]}

# ============ 3. DefiLlama 稳定币 ============
def stablecoin():
    for _ in range(2):
        try:
            rows = json.loads(get('https://stablecoins.llama.fi/stablecoincharts/all', timeout=40))
            if not isinstance(rows,list) or not rows: return None
            def tot(r): return (r.get('totalCirculating') or {}).get('peggedUSD')
            cur = tot(rows[-1])
            target = time.time() - 7*86400
            past = tot(min(rows, key=lambda r: abs(float(r.get('date') or 0)-target)))
            return {'total_b': cur/1e9, 'delta_7d_b': (cur-past)/1e9 if past else None}
        except Exception:
            time.sleep(1)
    return None

# ============ 4. Farside ETF ============
def etf_flow():
    """Farside BTC ETF 表 — 解析最近几日总净流入 (USD mn)"""
    try:
        html = get('https://farside.co.uk/btc/', {'Referer':'https://farside.co.uk/'}, 30).decode('utf-8','ignore')
    except Exception:
        return None
    # 找 Total 列的日数据
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
    out = []
    for r in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        cells = [re.sub(r'<[^>]+>|\s+',' ',c).strip() for c in cells]
        if len(cells) >= 2 and re.match(r'\d{1,2}\s+\w+', cells[0]):
            out.append(cells)
    return out[-7:]  # 表按日期升序, 最后7天是最新

# ============ 5. VPVR 成交密集区 (30d 成交量分布) ============
def vpvr(symbol='BTCUSDT', days=30, step=None, window_pct=0.08):
    """Volume Profile: 每价位成交量分布 — POC 只在现价 ±window_pct 内找"""
    try:
        ks = json.loads(get(f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit={days*24}', timeout=25))
        price = float(json.loads(get(f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}', timeout=10))['price'])
    except Exception:
        return None
    if step is None:
        step = 500 if price > 10000 else max(round(price*0.005,4), 0.001)
    vol_at = defaultdict(float)
    for k in ks:
        h,l,v = float(k[2]),float(k[3]),float(k[5])
        if h>l:
            lo_b,hi_b = int(l/step),int(h/step)
            for b in range(lo_b,hi_b+1):
                vol_at[b*step] += v/(hi_b-lo_b+1)
    # POC 只在现价附近窗口内找 — 30天前远在的历史巨量不误导
    lo,hi = price*(1-window_pct), price*(1+window_pct)
    local = {b:v for b,v in vol_at.items() if lo<=b<=hi}
    poc = max(local, key=local.get) if local else max(vol_at, key=vol_at.get)
    return {'price':price,'step':step,'vol':dict(vol_at),'poc':poc}

def render_vpvr(v, pct=0.06, topn=14):
    """渲染 ±pct 内的成交量带"""
    price,step,vol,poc = v['price'],v['step'],v['vol'],v['poc']
    lo,hi = price*(1-pct), price*(1+pct)
    band = {b:vv for b,vv in vol.items() if lo<=b<=hi}
    top = sorted(band.items(), key=lambda x:-x[1])[:topn]
    mx = top[0][1] if top else 1
    lines = []
    for b,vv in sorted(top, reverse=True):
        tag = 'POC' if b==poc else ('▲上方' if b>price else '▼下方')
        bar = '▮'*max(1,int(vv/mx*22))
        mark = ' ←现价' if abs(b-price)<step/2 else ''
        lines.append(f'  {b:>10,.2f} {vv:>8,.0f} {bar} {tag}{mark}')
    return lines

# ============ 6. 现货 ============
def spot_tickers(symbols=('BTCUSDT','ETHUSDT')):
    out = {}
    for s in symbols:
        try:
            d = json.loads(get(f'https://api.binance.com/api/v3/ticker/24hr?symbol={s}', timeout=10))
            out[s] = {'p':float(d['lastPrice']),'chg':float(d['priceChangePercent'])}
        except Exception: pass
    return out

def premium_trend(base='BTC', hours=24):
    """从 premium.db 读溢价历史,算 EMA20 和趋势
    返回 {'current':最新溢价, 'ema20':20期EMA, 'trend':'↑/↓/→', 'pctile':当前溢价在24h的分位}
    """
    db = Path('/var/lib/upi-hermes/.hermes/state/premium/premium.db')
    if not db.exists(): return None
    try:
        import sqlite3
        conn = sqlite3.connect(db)
        rows = conn.execute(
            'SELECT ts,pct FROM premium WHERE sym=? AND ts>? ORDER BY ts',
            (base, int(time.time())-hours*3600)).fetchall()
        conn.close()
        if not rows: return None
        vals = [r[1] for r in rows]
        # EMA20
        ema = vals[0]
        k = 2/(20+1)
        for v in vals[1:]: ema = v*k + ema*(1-k)
        cur = vals[-1]
        # 分位
        srt = sorted(vals)
        pctile = srt.index(min(srt, key=lambda x:abs(x-cur)))/len(srt)*100
        trend = '↑' if cur>ema else ('↓' if cur<ema else '→')
        return {'current':cur,'ema20':ema,'trend':trend,'pctile':pctile,'n':len(vals)}
    except Exception as e:
        return {'err':str(e)}

def spot_premium(base='BTC'):
    """Coinbase 现货 vs Binance 合约 溢价指数
    正 = Coinbase 高于 Binance = 美元资金/ETF 买现货 (结构性买盘)
    负 = Binance 高于 Coinbase = 合约炒作/亚洲资金主导 (易回撤)
    返回 {'coinbase':价, 'binance':价, 'premium':价差, 'premium_pct':%}
    """
    try:
        pair = f'{base}-USD'
        cb = json.loads(get(f'https://api.exchange.coinbase.com/products/{pair}/ticker', timeout=10))
        cb_price = float(cb['price'])
        bn = json.loads(get(f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={base}USDT', timeout=10))
        bn_price = float(bn['price'])
        diff = cb_price - bn_price
        return {'coinbase':cb_price, 'binance':bn_price,
                'premium':diff, 'premium_pct':diff/bn_price*100}
    except Exception as e:
        return {'err':str(e)}

def bucket_step(price):
    if price > 20000: return 500
    if price > 5000:  return 100
    if price > 500:   return 10
    if price > 50:    return 1
    if price > 1:     return 0.05
    return 0.005

def liq_map(symbol='BTCUSDT', window_pct=0.06):
    """读 liq_collector 累积的爆仓事件流,按价位分桶+分时间窗聚合"""
    f = Path('/var/lib/upi-hermes/.hermes/state/liq_map')/f'{symbol}.json'
    if not f.exists(): return None
    try:
        d = json.loads(f.read_text())
        events = d.get('events',[])
        if not events: return None
        spot = spot_tickers((symbol,)).get(symbol,{}).get('p')
        if not spot: return None
        lo,hi = spot*(1-window_pct), spot*(1+window_pct)
        now = int(time.time())
        step = bucket_step(spot)
        # 分时间窗聚合
        windows = {'1h':3600,'4h':4*3600,'24h':24*3600,'72h':72*3600}
        buckets = defaultdict(lambda: {w:{'long':0.0,'short':0.0,'n':0,'ex':set()} for w in windows})
        for e in events:
            if not (lo<=e['price']<=hi): continue
            b = round(e['price']/step)*step
            age = now - e['ts']
            for wname,wsec in windows.items():
                if age <= wsec:
                    cell = buckets[b][wname]
                    cell[e['side']] += e['qty']
                    cell['n'] += 1
                    cell['ex'].add(e['ex'])
        if not buckets: return None
        # 序列化 (set→list)
        ser = {b:{w:{'long':v['long'],'short':v['short'],'n':v['n'],'ex':sorted(v['ex'])} for w,v in ws.items()}
               for b,ws in sorted(buckets.items())}
        return {'spot':spot,'buckets':ser,'age':now-d['updated'],'total_events':len(events)}
    except Exception: return None

def print_liq_map(m, symbol):
    if not m:
        print('## 爆仓价位分布: 暂无数据 (collector 刚启动)')
        return
    print(f'## {symbol} 爆仓价位分布 (±6%内, {m["total_events"]}笔, 数据龄{m["age"]//60}min)')
    print(f'{"价位":>10} {"24h多爆":>10} {"24h空爆":>10} {"72h多爆":>10} {"72h空爆":>10} {"n":>4} 来源')
    mx = max((v['72h']['long']+v['72h']['short']) for _,v in m['buckets'].items()) or 1
    for b,ws in m['buckets'].items():
        v24, v72 = ws['24h'], ws['72h']
        tot72 = v72['long']+v72['short']
        if tot72<0.001: continue
        bar = '█'*int(tot72/mx*10)
        marker = ' ◀现价' if abs(b-m['spot'])/m['spot']<0.005 else ''
        ex_str = ','.join(e[:2] for e in v72['ex'])
        print(f'{b:>10.1f} {v24["long"]:>10.2f} {v24["short"]:>10.2f} {v72["long"]:>10.2f} {v72["short"]:>10.2f} {v72["n"]:>4} {ex_str} {bar}{marker}')

def _cg_totp(secret, t=None, step=30):
    """CoinGlass 内部 TOTP"""
    if t is None: t = int(time.time())
    counter = t // step
    key = base64.b32decode(secret)
    msg = struct.pack('>Q', counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0f
    return (struct.unpack('>I', h[offset:offset+4])[0] & 0x7fffffff) % 10**6

def _cg_data_param():
    """生成 CoinGlass heatmap 的加密 data 参数"""
    t = int(time.time())
    code = _cg_totp('I65VU7K5ZQL7WB4E', t)
    pt = f'{t},{code}'
    key = '1f68efd73f8d4921acc0dead41dd39bc'.encode()
    ct = AES.new(key, AES.MODE_ECB).encrypt(pad(pt.encode(), 16))
    return base64.b64encode(ct).decode()

# CoinGlass symbol 格式映射
CG_SYMBOL_FMT = {
    'Binance': '{c}USDT',
    'Bybit': '{c}USDT',
    'OKX': '{c}-USDT-SWAP',
    'Bitget': '{c}USDT_UMCBL',
    'Gate': '{c}_USDT',
}

def cg_fetch(path, params):
    """CoinGlass API 通用请求"""
    if not CG_AVAILABLE: return None
    params = dict(params)
    params['data'] = _cg_data_param()
    try:
        d = fetch_and_decrypt(f'https://capi.coinglass.com{path}', params)
        return d if isinstance(d,(dict,list)) else None
    except Exception:
        return None

def coinglass_heatmap(symbol='BTC', exchange='Binance', interval='5', limit=288):
    """CoinGlass 内部清算热图 API — 解密后返回结构化数据
    支持多币种/多交易所/多时间窗
    interval: 5,15,30,h2,h6,h12,h24,d1 (不同币种支持的 interval 不同)
    """
    fmt = CG_SYMBOL_FMT.get(exchange, '{c}USDT')
    sym = f'{exchange}_{fmt.format(c=symbol)}'
    d = cg_fetch('/api/index/v3/liqHeatMap',
        {'merge':'true','symbol':sym,'interval':str(interval),'limit':str(limit)})
    if not isinstance(d,dict) or 'liq' not in d: return None
    y_axis = d['y']
    liq = d['liq']
    prices = d.get('prices',[])
    spot = float(prices[-1][4]) if prices else 0
    by_price = defaultdict(float)
    for x_idx,y_idx,amt in liq:
        if y_idx < len(y_axis):
            by_price[y_axis[y_idx]] += float(amt)
    return {
        'spot':spot, 'y_axis':y_axis, 'by_price':dict(by_price),
        'range':(d.get('rangeLow'),d.get('rangeHigh')),
        'updateTime':d.get('updateTime'), 'instrument':d.get('instrument',{}).get('instrumentId'),
    }

def cg_home_stats():
    """CoinGlass 全市场统计"""
    return cg_fetch('/api/futures/home/statistics', {})

def cg_oi_change_rank(limit=10):
    """OI 变化排行 (全币种)"""
    return cg_fetch('/api/home/oi/changeRank',
        {'sort':'h4OiChangePercent','order':'desc','pageNum':'1','pageSize':str(limit),'ex':'all'})

def cg_coin_markets(limit=20):
    """全币种市场数据 (多空比/费率/爆仓/OI)"""
    return cg_fetch('/api/home/v2/coinMarkets',
        {'sort':'h4PriceChangePercent','order':'desc','pageNum':'1','pageSize':str(limit),'ex':'all'})

def cg_funding_chart(symbol='BTC'):
    """全所费率对比"""
    return cg_fetch('/api/fundingRate/chart', {'symbol':symbol})

def cg_liquidation_chart(symbol='BTC'):
    """180天爆仓历史 (按交易所)"""
    return cg_fetch('/api/futures/liquidation/chart', {'symbol':symbol})

def cg_liquidation_info():
    """各所爆仓统计"""
    return cg_fetch('/api/futures/liquidation/info', {})

def cg_etf_flow():
    """ETF 资金流历史"""
    return cg_fetch('/api/etf/flow', {})

def cg_ahr999():
    """AHR999 指数 (5711天历史)"""
    return cg_fetch('/api/index/ahr999', {})

def cg_fear_greed():
    """恐惧贪婪指数"""
    return cg_fetch('/api/index/fearGreed', {})

def print_cg_heatmap(h, symbol):
    if not h or 'err' in h:
        print(f'## CoinGlass 清算热图: {h.get("err","暂无数据") if h else "未启用"}')
        return
    spot = h['spot']
    by_price = h['by_price']
    # 上下分离
    above = sorted([(p,a) for p,a in by_price.items() if p>spot], key=lambda x:-x[1])
    below = sorted([(p,a) for p,a in by_price.items() if p<=spot], key=lambda x:-x[1])
    mx = max(by_price.values()) if by_price else 1
    print(f'## CoinGlass {symbol} 清算热图 (现价${spot:,.0f}, {len(by_price)}价位)')
    print(f'{"价位":>10} {"强度":>14} {"":>3} 位置')
    for p,a in above[:8]:
        bar = '█'*int(a/mx*12)
        print(f'  ${p:>9,.0f} {a:>14,.0f} {bar:<12} ↑阻力')
    print(f'  {"─"*40}')
    for p,a in below[:8]:
        bar = '█'*int(a/mx*12)
        marker = ' ◀现价' if abs(p-spot)/spot<0.005 else ''
        print(f'  ${p:>9,.0f} {a:>14,.0f} {bar:<12} ↓支撑{marker}')
    print()

# ============ 输出 ============
def fmt_b(n): return f'${n:.1f}B' if n else '-'

def collect(base='BTC'):
    """聚合全部数据为 dict — 供 --json 和其它脚本复用"""
    out = {'base':base,'ts':datetime.now(timezone.utc).isoformat()}
    out['spot'] = spot_tickers([f'{base}USDT','ETHUSDT'])
    if CY: out['sentiment'] = scan_sentiment(base)
    out['vpvr'] = vpvr(f'{base}USDT')
    out['liq_map'] = liq_map(f'{base}USDT')
    out['premium'] = spot_premium(base)
    out['premium_trend'] = premium_trend(base)
    out['cg_heatmap'] = coinglass_heatmap(base)
    if base=='BTC':
        out['deribit'] = deribit_walls()
        out['etf'] = etf_flow()
    out['stablecoin'] = stablecoin()
    return out

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    base = args[0].upper().replace('USDT','') if args else 'BTC'
    json_mode = '--json' in sys.argv

    if json_mode:
        print(json.dumps(collect(base), ensure_ascii=False, default=str))
        return

    print(f'# market_scan {base}/USDT — {datetime.now(timezone.utc).strftime("%m-%d %H:%M UTC")}\n')

    # 现货
    s = spot_tickers([f'{base}USDT','ETHUSDT'])
    for k,v in s.items():
        print(f'{k:<10} ${v["p"]:>11,.2f}  24h {v["chg"]:+.2f}%')
    print()

    # 情绪
    if CY:
        print('## 四所合约情绪 (24h)')
        print(f'{"所":<10}{"OIΔ":>8}{"多爆":>8}{"空爆":>8}{"账户多/空":>12}{"费率":>9}')
        rows = scan_sentiment(base)
        tl=ts=0
        for r in rows:
            if 'err' in r: print(f'{r["ex"]:<10}ERR'); continue
            ls = f'{r["ls"][0]:.0f}/{r["ls"][1]:.0f}' if r['ls'] else '-'
            print(f'{r["ex"]:<10}{r["oi_chg"] or 0:>+7.2f}%{r["long_liq"]:>7.1f}{r["short_liq"]:>7.1f}{ls:>12}{(r["fr"] or 0):>+8.4f}%')
            tl+=r['long_liq']; ts+=r['short_liq']
        print(f'合计: 多爆{tl:.0f} 空爆{ts:.0f} (空/多 {ts/max(tl,1):.1f}x)\n')
    else:
        print('(Coinalyze key 缺失, 跳过情绪表)\n')

    # Deribit 墙 (只 BTC)
    if base=='BTC':
        w = deribit_walls()
        if w:
            print(f'## Deribit {w["expiry"]} 期权墙 (spot est {w["spot"]:,.0f}, total OI {w["total_oi"]:,.0f} BTC, max pain {w["max_pain"]:,.0f})')
            spot_p = w['spot']
            # 显示 spot 上下最近的墙
            strikes = sorted(w['strikes'].items())
            show = [x for x in strikes if abs(x[0]-spot_p)/spot_p < 0.12]
            show.sort(key=lambda x: -(x[1]['call']+x[1]['put']))
            for s,v in show[:8]:
                net = v['call']-v['put']
                side = 'call' if net>0 else 'put'
                print(f'  {s:>9,.0f}  call{v["call"]:>7,.0f} put{v["put"]:>6,.0f} net{net:>+7,.0f} ({side})')
            print()

    # VPVR 成交密集区
    v = vpvr(f'{base}USDT')
    if v:
        print(f'## 30d 成交密集区 (VPVR, POC={v["poc"]:,.0f}, 现价{v["price"]:,.0f})')
        for line in render_vpvr(v):
            print(line)
        print()

    # 现货溢价 (Coinbase vs Binance)
    p = spot_premium(base)
    pt = premium_trend(base)
    if p and 'err' not in p:
        sig = '美元资金买现货(结构性买盘)' if p['premium']>0 else '合约主导(亚洲资金)'
        print(f'## 现货溢价 Coinbase-Binance: {p["premium"]:+.1f} ({p["premium_pct"]:+.3f}%) {sig}')
        print(f'  CB ${p["coinbase"]:,.0f} / BN ${p["binance"]:,.0f}')
        if pt and 'err' not in pt:
            print(f'  24h: 当前{pt["current"]:+.3f}% vs EMA20 {pt["ema20"]:+.3f}% {pt["trend"]} (分位{pt["pctile"]:.0f}%)')
        print()
    elif p and 'err' in p:
        print(f'## 现货溢价: ERR {p["err"]}\n')

    # 爆仓价位分布
    print_liq_map(liq_map(f'{base}USDT'), f'{base}USDT')
    print()

    # CoinGlass 清算热图
    print_cg_heatmap(coinglass_heatmap(base), base)

    # 稳定币
    st = stablecoin()
    if st:
        print(f'## 稳定币总供应 {fmt_b(st["total_b"])}  7日 {st["delta_7d_b"]:+.2f}B')
    print()

    # ETF (只 BTC)
    if base=='BTC':
        ef = etf_flow()
        if ef:
            print('## BTC ETF 净流入 USD mn (Farside, Total列)')
            for row in ef[-6:]:
                # row[0]=日期, row[-1]=Total
                print(f'  {row[0]:<14} {row[-1]:>10}')
        else:
            print('## ETF: 拉取失败 (Farside 反爬)')

if __name__=='__main__':
    main()
