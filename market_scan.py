#!/usr/bin/env python3
"""Read-only market scanner. Units, source status and sampling limits are explicit.

Examples: python market_scan.py BTC --json
          python market_scan.py --etf
          python market_scan.py BTC --source cg_heatmap --model 2 --window 48h
"""
import argparse
import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from liquidation_events import valid_event

try:
    from coinglass_client import (
        _cg_totp, _cg_data_param, cg_fetch, coinglass_heatmap,
        cg_home_stats, cg_oi_change_rank, cg_coin_markets, cg_funding_chart,
        cg_liquidation_chart, cg_liquidation_info, cg_etf_flow, cg_ahr999,
        cg_fear_greed, cg_open_interest_chart, cg_option_chart,
        cg_option_max_pain, cg_exchange_balance, cg_spot_markets, cg_rsi,
        cg_whale_vs_retail, cg_depth_delta, cg_hyperliquid_liq_map,
        cg_option_net_premium, cg_option_expiry, resolve_pair_instrument,
        annualize_funding_percent,
    )
    CG_AVAILABLE = True
except ImportError:
    CG_AVAILABLE = False
    def coinglass_heatmap(*args, **kwargs):
        raise RuntimeError('CoinGlass dependencies are unavailable; install requirements.txt')

UA = 'Mozilla/5.0 (compatible; CoinGlassReverse/2)'
# No implicit reads of a particular user's credentials or import-time I/O.
CY = os.environ.get('COINALYZE_KEY')
EX_NAME = {'A':'Binance','6':'Bybit','3':'OKX','4':'HTX','H':'Hyperliquid','S':'Aster'}
_markets_cache = None


def get(url, headers=None, timeout=20):
    h = {'User-Agent': UA, 'Accept': '*/*'}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as response:
        return response.read()


def cy_get(path):
    key = CY
    explicit = os.environ.get('MARKET_API_KEY_FILE')
    if not key and explicit:
        for line in Path(explicit).read_text().splitlines():
            if line.startswith('COINALYZE_KEY='):
                key = line.split('=',1)[1].strip()
                break
    if not key:
        raise RuntimeError('COINALYZE_KEY is not configured')
    return json.loads(get('https://api.coinalyze.net'+path, {'api_key':key}, 25))


def find_perp(base, quote='USDT'):
    global _markets_cache
    if _markets_cache is None:
        _markets_cache = cy_get('/v1/future-markets')
    if not isinstance(_markets_cache,list):
        raise ValueError('Coinalyze markets response is not a list')
    want = base.upper()+quote
    return {m['exchange']:m['symbol'] for m in _markets_cache
            if m.get('symbol_on_exchange','').replace('-','')==want
            and m.get('is_perpetual') and m.get('margined')=='STABLE'}


def hist(symbol, endpoint, hours=24):
    if not isinstance(hours,(int,float)) or not 0 < hours <= 24*365:
        raise ValueError('hours must be positive and <= 8760')
    now = int(time.time())
    params={'symbols':symbol,'interval':'4hour','from':now-int(hours*3600),'to':now}
    if endpoint in ('liquidation-history','open-interest-history'):
        params['convert_to_usd']='true'
    d=cy_get('/v1/'+endpoint+'?'+urllib.parse.urlencode(params))
    if not isinstance(d,list):
        raise ValueError('Coinalyze history response is not a list')
    return d[0].get('history',[]) if d else []


def scan_sentiment(base='BTC', quote='USDT', hours=24):
    rows=[]
    for ex,sym in find_perp(base,quote).items():
        if ex not in ('A','6','3','H'):
            continue
        try:
            oi=hist(sym,'open-interest-history',hours)
            lq=hist(sym,'liquidation-history',hours)
            lsr=hist(sym,'long-short-ratio-history',hours)
            fr=hist(sym,'funding-rate-history',hours)
            rows.append({'ex':EX_NAME.get(ex,ex),
                         'oi_chg':(oi[-1]['c']/oi[0]['o']-1)*100 if len(oi)>=2 and oi[0].get('o') else None,
                         'long_liq':sum(x['l'] for x in lq) if lq and all(x.get('l') is not None for x in lq) else None,
                         'short_liq':sum(x['s'] for x in lq) if lq and all(x.get('s') is not None for x in lq) else None,
                         'ls':(lsr[-1].get('l'),lsr[-1].get('s')) if lsr else None,
                         'lsr':lsr[-1].get('r') if lsr else None,
                         'fr':fr[-1].get('c') if fr else None,
                         'liquidation_unit':'USD','oi_change_unit':'percent','funding_unit':'percent',
                         'ls_unit':'percent_of_accounts','lsr_unit':'upstream_long_short_accounts_ratio',
                         'coverage':'selected stable-margined perpetuals; upstream history may be incomplete'})
        except Exception as exc:
            rows.append({'ex':EX_NAME.get(ex,ex),'status':'error','error_type':type(exc).__name__})
    return rows


def deribit_walls(currency='BTC', expiry_policy='max_oi', expiry=None, now=None):
    """OI distribution, NOT gamma/dealer exposure. Select a documented expiry policy."""
    if currency not in ('BTC','ETH') or expiry_policy not in ('max_oi','nearest'):
        raise ValueError('unsupported currency or expiry policy')
    now=now or datetime.now(timezone.utc)
    data=json.loads(get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?'+urllib.parse.urlencode({'currency':currency,'kind':'option'}),timeout=30))
    if 'error' in data:
        raise ValueError('Deribit business error')
    instruments=[];totals=defaultdict(float);dates={}
    for item in data.get('result',[]):
        parts=item.get('instrument_name','').split('-')
        if len(parts)!=4 or parts[0]!=currency or parts[3] not in ('C','P'):
            continue
        try:
            dt=datetime.strptime(parts[1],'%d%b%y').replace(hour=8,tzinfo=timezone.utc)
            strike=float(parts[2]); oi=float(item.get('open_interest') or 0)
        except (ValueError,TypeError):
            continue
        if dt<=now or not all(math.isfinite(x) and x>=0 for x in (strike,oi)):
            continue
        instruments.append((parts,item,strike,oi)); totals[parts[1]]+=oi;dates[parts[1]]=dt
    if expiry is not None and expiry not in totals:
        raise ValueError('requested expiry has no available unexpired instruments')
    if not totals:
        return None
    selected=expiry or (min(dates,key=lambda key:dates[key]) if expiry_policy=='nearest' else max(totals,key=lambda key:totals[key]))
    strikes=defaultdict(lambda:{'call':0.,'put':0.});reference=None
    for parts,item,strike,oi in instruments:
        if parts[1]==selected:
            strikes[strike]['call' if parts[3]=='C' else 'put']+=oi
            reference=item.get('estimated_delivery_price',reference)
    pain=min(strikes,key=lambda price:sum(max(0,price-s)*v['call']+max(0,s-price)*v['put'] for s,v in strikes.items()))
    return {'spot':reference,'reference_price':reference,
            'reference_price_kind':'deribit_estimated_delivery_price',
            'expiry':selected,'expiry_policy':'explicit' if expiry else expiry_policy,
            'strikes':dict(strikes),'max_pain':pain,'total_oi':totals[selected],'unit':currency,
            'metric':'open_interest_distribution','max_pain_method':'simplified terminal intrinsic-value objective; not a forecast'}


def stablecoin():
    rows=json.loads(get('https://stablecoins.llama.fi/stablecoincharts/all',timeout=30))
    if not isinstance(rows,list) or not rows:
        return None
    target=time.time()-7*86400
    cur=(rows[-1].get('totalCirculating') or {}).get('peggedUSD')
    past=(min(rows,key=lambda r:abs(float(r.get('date') or 0)-target)).get('totalCirculating') or {}).get('peggedUSD')
    if cur is None:
        return None
    return {'total_b':cur/1e9,'delta_7d_b':(cur-past)/1e9 if past is not None else None,'unit':'billion_USD'}


def etf_flow():
    """Farside HTML table strings (USD millions); NOT CoinGlass changeUsd units."""
    html=get('https://farside.co.uk/btc/',{'Referer':'https://farside.co.uk/'},30).decode('utf-8','ignore')
    out=[]
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>',html,re.S):
        cells=[re.sub(r'<[^>]+>|\s+',' ',c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>',row,re.S)]
        if len(cells)>=2 and re.match(r'\d{1,2}\s+\w+',cells[0]):
            out.append(cells)
    return out[-7:]


def vpvr(symbol='BTCUSDT', days=30, step=None, window_pct=.08):
    """Approximate OHLCV volume allocation, not executed volume-at-price."""
    if not isinstance(days,int) or isinstance(days,bool) or not 1<=days<=41:
        raise ValueError('days must be 1..41 (Binance maximum 1000 hourly bars)')
    if not 0<window_pct<=1 or (step is not None and (not math.isfinite(step) or step<=0)):
        raise ValueError('invalid VPVR step/window')
    params=urllib.parse.urlencode({'symbol':symbol,'interval':'1h','limit':days*24})
    candles=json.loads(get('https://api.binance.com/api/v3/klines?'+params,timeout=25))
    price=float(json.loads(get('https://api.binance.com/api/v3/ticker/price?'+urllib.parse.urlencode({'symbol':symbol}),timeout=10))['price'])
    if not math.isfinite(price) or price<=0:
        raise ValueError('invalid reference price')
    if step is None:
        step=500 if price>10000 else max(price*.005,1e-12)
    volume=defaultdict(float)
    for k in candles:
        high,low,qty=float(k[2]),float(k[3]),float(k[5])
        if not all(math.isfinite(x) for x in (high,low,qty)) or low<0 or high<low or qty<0:
            continue
        left,right=int(low/step),int(high/step)
        if right-left>100000:
            raise ValueError('step too small for candle range')
        for b in range(left,right+1):
            volume[b*step]+=qty/(right-left+1)
    if not volume:
        return None
    local={b:v for b,v in volume.items() if price*(1-window_pct)<=b<=price*(1+window_pct)}
    poc=max(local or volume,key=lambda key:(local or volume)[key])
    return {'price':price,'step':step,'vol':dict(volume),'poc':poc,'unit':'base_asset','method':'uniform OHLC range approximation'}


def render_vpvr(v, pct=.06, topn=14):
    price,step,volume,poc=v['price'],v['step'],v['vol'],v['poc']
    top=sorted(((b,q) for b,q in volume.items() if price*(1-pct)<=b<=price*(1+pct)),key=lambda x:-x[1])[:topn]
    maximum=max((q for _,q in top),default=0) or 1
    return [f'  {format_price(b):>14} {q:>12,.2f} {"▮"*int(q/maximum*22)} {"POC" if b==poc else ""}' for b,q in sorted(top,reverse=True)]


def spot_tickers(symbols=('BTCUSDT','ETHUSDT')):
    out={}
    for symbol in dict.fromkeys(symbols):
        try:
            data=json.loads(get('https://api.binance.com/api/v3/ticker/24hr?'+urllib.parse.urlencode({'symbol':symbol}),timeout=10))
            out[symbol]={'p':float(data['lastPrice']),'chg':float(data['priceChangePercent'])}
        except Exception as exc:
            out[symbol]={'status':'error','error_type':type(exc).__name__}
    return out


def premium_trend(base='BTC', hours=24):
    db=Path(os.environ.get('PREMIUM_STATE_DIR',str(Path.home()/'.local/state/coinglass/premium')))/'premium.db'
    if not db.exists():
        return None
    with sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro',uri=True) as conn:
        rows=conn.execute('SELECT ts,pct FROM premium WHERE sym=? AND ts>? ORDER BY ts',(base,int(time.time())-hours*3600)).fetchall()
    if not rows:
        return None
    values=[r[1] for r in rows];ema=values[0];k=2/21
    for value in values[1:]:
        ema=value*k+ema*(1-k)
    current=values[-1]
    return {'current':current,'ema20':ema,'trend':'↑' if current>ema else ('↓' if current<ema else '→'),
            'pctile':sum(v<current for v in values)/len(values)*100,'n':len(values),'unit':'percent',
            'metric':'usd_spot_minus_usdt_perpetual','fx_adjusted':False,'as_of_ms':rows[-1][0]*1000}


def spot_premium(base='BTC'):
    """Unadjusted USD spot minus USDT perpetual; no inferred flow/geography."""
    cb=float(json.loads(get('https://api.exchange.coinbase.com/products/'+urllib.parse.quote(base+'-USD',safe='')+'/ticker',timeout=10))['price'])
    bn=float(json.loads(get('https://fapi.binance.com/fapi/v1/ticker/price?'+urllib.parse.urlencode({'symbol':base+'USDT'}),timeout=10))['price'])
    if not all(math.isfinite(x) and x>0 for x in (cb,bn)):
        raise ValueError('invalid premium prices')
    return {'coinbase':cb,'binance':bn,'premium':cb-bn,'premium_pct':(cb-bn)/bn*100,
            'metric':'usd_spot_minus_usdt_perpetual','fx_adjusted':False,'synchronous_quotes':False,
            'unit':'USD_price_minus_USDT_price','pct_unit':'percent'}


def bucket_step(price):
    if price>20000:return 500
    if price>5000:return 100
    if price>500:return 10
    if price>50:return 1
    if price>1:return .05
    return max(price*.005,1e-12)


def liq_map(symbol='BTCUSDT', window_pct=.06):
    """Aggregate typed additive observations only; never convert old untyped events."""
    if not re.fullmatch(r'[A-Za-z0-9_]+',symbol):
        raise ValueError('invalid symbol')
    root=Path(os.environ.get('LIQ_STATE_DIR',str(Path.home()/'.local/state/coinglass/liq_map')))
    path=root/f'{symbol}.json'
    if not path.exists():return None
    data=json.loads(path.read_text())
    if data.get('schema_version')!=2:
        return {'status':'legacy_untyped','error':'Legacy quantities/times cannot safely be aggregated; preserve files separately.'}
    events=data.get('events',[])
    reference=spot_tickers((symbol,)).get(symbol,{}).get('p')
    if not reference:return None
    now_ms=int(time.time()*1000);step=bucket_step(reference)
    windows={'1h':3600,'4h':14400,'24h':86400,'72h':259200}
    buckets=defaultdict(lambda:{w:{'long':0.,'short':0.,'n':0,'ex':set()} for w in windows})
    excluded=0;seen=set();accepted=0;unit=None
    for event in events:
        try:
            price=float(event['price']);qty=float(event['base_qty']);stamp=float(event['exchange_ts_ms'])
            side=event['liquidated_side'];base=event['base_asset'];eid=event['event_id']
            valid=(valid_event(event) and event.get('symbol')==symbol and side in ('long','short')
                   and all(math.isfinite(x) and x>0 for x in (price,qty,stamp))
                   and event['coverage']['additive'] is True and event.get('quantity_kind')!='cumulative_filled_snapshot'
                   and 0<=now_ms-stamp<=windows['72h']*1000 and eid not in seen and bool(base)
                   and isinstance(base,str) and event.get('quote_asset') in ('USDT','USDC','USD')
                   and base+event['quote_asset']==symbol and (unit is None or base==unit))
            if not valid:excluded+=1;continue
            seen.add(eid);unit=base
            if not reference*(1-window_pct)<=price<=reference*(1+window_pct):continue
            accepted+=1;bucket=round(price/step)*step
            for name,seconds in windows.items():
                if now_ms-stamp<=seconds*1000:
                    cell=buckets[bucket][name];cell[side]+=qty;cell['n']+=1;cell['ex'].add(event['ex'])
        except (KeyError,TypeError,ValueError):
            excluded+=1
    serialized={b:{w:{**v,'ex':sorted(v['ex'])} for w,v in ws.items()} for b,ws in sorted(buckets.items())}
    updated=data.get('updated_ms')
    return {'status':'ok' if accepted else 'no_data','spot':reference,'buckets':serialized,'unit':unit,
            'age':max(0,(now_ms-updated)/1000) if isinstance(updated,(int,float)) else None,
            'total_events':len(events),'accepted_events':accepted,'excluded_events':excluded,
            'coverage':'typed additive observations only; sampled feeds are not a full liquidation ledger; Binance cumulative snapshots excluded',
            'price_semantics':'exchange-specific liquidation transfer / bankruptcy prices, not necessarily executions'}


def format_price(value):
    if value is None:return 'N/A'
    return f'{value:,.2f}' if abs(value)>=1 else f'{value:.10g}'


def format_percent(value):
    return 'N/A' if value is None else f'{value:+.4f}%'


def print_liq_map(result, symbol):
    if not result or not result.get('buckets'):
        print(f'## {symbol} liquidation observations: {result.get("status","no_data") if result else "no_data"}')
        return
    print(f'## {symbol} sampled liquidation observations ({result["unit"]}); excluded={result["excluded_events"]}')
    for price,windows in result['buckets'].items():
        print(f'{format_price(price):>14} 24h long={windows["24h"]["long"]:.6g} short={windows["24h"]["short"]:.6g}')


def print_cg_heatmap(result, symbol):
    if not result or result.get('status') in ('error','no_data') or 'err' in result:
        print(f'## CoinGlass {symbol}: no usable heatmap')
        return
    reference=result.get('reference_contract_price',result.get('spot'))
    profile=result.get('by_price',{});maximum=max(profile.values(),default=0) or 1
    print(f'## CoinGlass {symbol}: contract candle close {format_price(reference)}, latest-slice intensity (unit unverified)')
    for price,intensity in sorted(profile.items(),key=lambda x:-x[1])[:16]:
        if math.isfinite(intensity) and intensity>=0:
            print(f'  {format_price(price):>14} {intensity:>14,.2f} {"█"*int(intensity/maximum*12)}')


def _spot_status(data):
    if not data:
        return 'no_data'
    usable = 0
    errors = 0
    no_data = 0
    for row in data.values():
        if isinstance(row, dict) and row.get('status') == 'error':
            errors += 1
        elif isinstance(row, dict) and row.get('status') == 'no_data':
            no_data += 1
        elif isinstance(row, dict) and row.get('p') is not None:
            usable += 1
        else:
            no_data += 1
    if usable and not errors and not no_data:
        return 'ok'
    if usable:
        return 'partial'
    if errors:
        return 'error'
    return 'no_data'


def _sentiment_status(data):
    if not data:
        return 'no_data'
    usable = 0
    errors = 0
    no_data = 0
    fields = ('oi_chg', 'long_liq', 'short_liq', 'ls', 'fr')
    for row in data:
        if isinstance(row, dict) and row.get('status') == 'error':
            errors += 1
        elif isinstance(row, dict) and row.get('status') == 'no_data':
            no_data += 1
        elif isinstance(row, dict) and any(row.get(key) is not None for key in fields):
            usable += 1
        else:
            no_data += 1
    if usable and not errors and not no_data:
        return 'ok'
    if usable:
        return 'partial'
    if errors:
        return 'error'
    return 'no_data'


def _aggregate_source_status(name, data):
    if isinstance(data, dict) and data.get('status') in ('ok', 'partial', 'error', 'legacy_untyped', 'no_data', 'not_configured'):
        return data['status']
    if name == 'spot' and isinstance(data, dict):
        return _spot_status(data)
    if name == 'sentiment' and isinstance(data, list):
        return _sentiment_status(data)
    return 'no_data' if data is None or data == [] or data == {} else 'ok'


def _source(call, name=None):
    stamp=datetime.now(timezone.utc).isoformat()
    try:
        data=call()
        return {'status':_aggregate_source_status(name, data),'data':data,'as_of':stamp}
    except Exception as exc:
        # Exception messages may embed signed query strings: return typed safe diagnostics.
        error={'type':type(exc).__name__}
        for key in ('category','code'):
            if hasattr(exc,key):error[key]=getattr(exc,key)
        return {'status':'error','error':error,'as_of':stamp}


SOURCE_NAMES=('spot','sentiment','vpvr','liq_map','premium','premium_trend','cg_heatmap','deribit','etf','stablecoin')
CG_ADAPTER_SOURCE_NAMES=('cg_whale_vs_retail','cg_depth_delta','cg_hyperliquid_liq_map','cg_option_net_premium','cg_option_expiry')
DEFAULT_SOURCE_NAMES=SOURCE_NAMES
ALL_SOURCE_NAMES=SOURCE_NAMES+CG_ADAPTER_SOURCE_NAMES


def _adapter_exchange(options, default):
    return (options or {}).get('exchange') or default


def _depth_instrument(base, options):
    options=options or {}
    if options.get('depth_instrument'):
        return options['depth_instrument']
    instrument, _ = resolve_pair_instrument(base, _adapter_exchange(options, 'Binance'),
                                            quote=options.get('quote', 'USDT'),
                                            original_symbol=options.get('original_symbol'))
    return instrument


def collect(base='BTC', sources=None, heatmap_options=None, adapter_options=None):
    base=base.upper()
    if not re.fullmatch(r'[A-Z0-9]+',base):raise ValueError('invalid base asset')
    selected=list(sources) if sources is not None else list(DEFAULT_SOURCE_NAMES)
    if any(name not in ALL_SOURCE_NAMES for name in selected):raise ValueError('unknown source')
    adapter_options=adapter_options or {}
    calls={'spot':lambda:spot_tickers((base+'USDT','ETHUSDT')),
           'sentiment':lambda:scan_sentiment(base),'vpvr':lambda:vpvr(base+'USDT'),
           'liq_map':lambda:liq_map(base+'USDT'),'premium':lambda:spot_premium(base),
           'premium_trend':lambda:premium_trend(base),
           'cg_heatmap':lambda:coinglass_heatmap(base,**(heatmap_options or {})),
           'deribit':lambda:deribit_walls(currency=base) if base in ('BTC','ETH') else None,
           'etf':lambda:etf_flow() if base=='BTC' else None,'stablecoin':stablecoin,
           'cg_whale_vs_retail':lambda:cg_whale_vs_retail(base,
               interval=adapter_options.get('interval', '1d'), limit=adapter_options.get('limit', 1000)),
           'cg_depth_delta':lambda:cg_depth_delta(_depth_instrument(base, adapter_options),
               depth=adapter_options.get('depth', 1), interval=adapter_options.get('interval', '15m'),
               limit=adapter_options.get('limit', 300)),
           'cg_hyperliquid_liq_map':lambda:cg_hyperliquid_liq_map(base),
           'cg_option_net_premium':lambda:cg_option_net_premium(base,
               exchange=_adapter_exchange(adapter_options, 'Deribit'), window=adapter_options.get('window', '30d')),
           'cg_option_expiry':lambda:cg_option_expiry(base,
               exchange=_adapter_exchange(adapter_options, 'Deribit'),
               subtype=adapter_options.get('subtype', 'ALL'), currency=adapter_options.get('currency', 'USD'))}
    out={'schema_version':2,'base':base,'ts':datetime.now(timezone.utc).isoformat()}
    for name in selected:
        if name=='sentiment' and not CY and not os.environ.get('MARKET_API_KEY_FILE'):
            out[name]={'status':'not_configured','data':None,'as_of':out['ts']}
        else:
            out[name]=_source(calls[name], name)
    return out


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base',nargs='?',default='BTC')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--etf',action='store_true',help='Farside BTC ETF only, values are USD millions')
    parser.add_argument('--source',action='append',choices=ALL_SOURCE_NAMES)
    parser.add_argument('--model',choices=('1','2','3','legacy'),default='1')
    parser.add_argument('--scope',choices=('pair','aggregate'),default='pair')
    parser.add_argument('--window')
    parser.add_argument('--exchange')
    parser.add_argument('--quote',default='USDT')
    parser.add_argument('--original-symbol')
    parser.add_argument('--depth-instrument')
    parser.add_argument('--depth',type=int)
    parser.add_argument('--subtype')
    parser.add_argument('--currency')
    parser.add_argument('--interval')
    parser.add_argument('--limit',type=int)
    args=parser.parse_args(argv)
    if args.etf and args.source:parser.error('--etf cannot be combined with --source')
    if args.window and (args.interval is not None or args.limit is not None):parser.error('--window conflicts with --interval/--limit')
    base=args.base.upper()
    if base.endswith('USDT'):base=base[:-4]
    options={'model':args.model if args.model=='legacy' else int(args.model),'scope':args.scope,'quote':args.quote}
    if args.exchange is not None:options['exchange']=args.exchange
    for key in ('window','original_symbol','interval','limit'):
        value=getattr(args,key)
        if value is not None:options[key]=value
    adapter_options={'quote':args.quote}
    for key in ('exchange','original_symbol','depth_instrument','depth','subtype','currency','interval','limit','window'):
        value=getattr(args,key)
        if value is not None:adapter_options[key]=value
    out=collect('BTC' if args.etf else base,sources=['etf'] if args.etf else args.source,
                heatmap_options=options,adapter_options=adapter_options)
    if args.json:
        print(json.dumps(out,ensure_ascii=False,default=str,allow_nan=False))
    else:
        print(f'# {out["base"]} market scan — {out["ts"]}')
        for name in (n for n in out if n in ALL_SOURCE_NAMES):
            item=out[name];data=item.get('data')
            if item['status']!='ok' and not (item['status']=='partial' and name in ('spot','sentiment')):
                print(f'## {name}: {item["status"]} {item.get("error",{})}');continue
            if name=='cg_heatmap':print_cg_heatmap(data,base)
            elif name=='liq_map':print_liq_map(data,base+'USDT')
            elif name=='sentiment':
                print(f'## selected perpetual sentiment ({item["status"]}); liquidation unit USD')
                for row in data:
                    if row.get('status') == 'error':
                        print(f'{row.get("ex","unknown")}: error {row.get("error_type","Error")}')
                    elif row.get('status') == 'no_data':
                        print(f'{row.get("ex","unknown")}: no_data')
                    else:
                        print(f'{row.get("ex","unknown")}: OI {format_percent(row.get("oi_chg"))}, funding {format_percent(row.get("fr"))}, long {row.get("long_liq")}, short {row.get("short_liq")}')
            elif name=='spot':
                print(f'## spot ({item["status"]})\n'+json.dumps(data,ensure_ascii=False,default=str))
            else:print(f'## {name}\n'+json.dumps(data,ensure_ascii=False,default=str))
    return 1 if any(out[n]['status'] in ('error','partial') for n in out if n in ALL_SOURCE_NAMES) else 0


if __name__=='__main__':
    raise SystemExit(main())
