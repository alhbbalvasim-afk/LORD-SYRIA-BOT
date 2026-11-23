from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import math
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
TIMEFRAMES = (1, 2, 3, 5)
PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "USD/CHF": "CHF=X",
    "NZD/USD": "NZDUSD=X",
    "EUR/GBP": "EURGBP=X",
    "EUR/JPY": "EURJPY=X",
    "GBP/JPY": "GBPJPY=X",
    "EUR/CAD": "EURCAD=X",
    "GBP/CAD": "GBPCAD=X",
    "AUD/JPY": "AUDJPY=X",
    "EUR/CHF": "EURCHF=X",
    "CAD/JPY": "CADJPY=X",
    "GBP/CHF": "GBPCHF=X",
    "EUR/AUD": "EURAUD=X",
    "NZD/JPY": "NZDJPY=X",
}

"""Explicit closed-candle rules. Thresholds are engineering choices, not win rates."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class Decision:
    side: str
    setup: str
    reasons: tuple

def ema(values, period):
    out=[values[0]]
    k=2/(period+1)
    for v in values[1:]: out.append(k*v+(1-k)*out[-1])
    return out

def atr(bars):
    values=[max(bars[i][1]-bars[i][2],abs(bars[i][1]-bars[i-1][3]),abs(bars[i][2]-bars[i-1][3])) for i in range(1,len(bars))]
    return sum(values[-14:])/14

def pivots(bars, stop):
    # A pivot requires two subsequent closed candles, all before stop.
    highs=[];lows=[]
    for i in range(2,stop-2):
        neighbors=bars[i-2:i]+bars[i+1:i+3]
        if all(bars[i][1]>b[1] for b in neighbors): highs.append((i,bars[i][1]))
        if all(bars[i][2]<b[2] for b in neighbors): lows.append((i,bars[i][2]))
    return highs,lows

def confirmation(bar, direction, scale):
    o,h,l,c=bar; width=h-l; body=abs(c-o)
    if width<=0 or width>2.5*scale or body<.25*scale or body/width<.55:
        return False
    return (c>o and (h-c)/width<=.25) if direction==1 else (c<o and (c-l)/width<=.25)

def three_candles(bars, direction, scale):
    if len(bars)!=3 or not all(confirmation(b,direction,scale) for b in bars): return False
    for prev,cur in zip(bars,bars[1:]):
        # Opening inside previous real body; equal close is accepted for continuous markets.
        if not min(prev[0],prev[3])<=cur[0]<=max(prev[0],prev[3]):return False
        if direction*(cur[3]-prev[3])<=0:return False
    return True

def momentum(bars,direction,scale):
    closes=[b[3] for b in bars];fast=ema(closes,9)
    return direction*(closes[-1]-closes[-4])>=.2*scale and direction*(fast[-1]-fast[-3])>0

def room(bars,direction,scale):
    highs,lows=pivots(bars,len(bars)-1);price=bars[-1][3]
    obstacles=[v for _,v in (highs if direction==1 else lows) if direction*(v-price)>0]
    return not obstacles or min(abs(v-price) for v in obstacles)>=.75*scale

def liquidity_ob(bars,direction,scale):
    n=len(bars);last=bars[-1];tol=.15*scale
    if not confirmation(last,direction,scale):return None
    # Sweep -> displacement/BOS -> retest on the newest candle, in that order.
    for sweep in range(n-9,n-3):
        highs,lows=pivots(bars,sweep)
        victims=lows if direction==1 else highs
        opposing=highs if direction==1 else lows
        if not victims or not opposing:continue
        level=victims[-1][1];target=opposing[-1][1];b=bars[sweep]
        extreme=b[2] if direction==1 else b[1]
        if direction*(extreme-level)>=-.05*scale or direction*(b[3]-level)<=0:continue
        for br in range(sweep+1,min(sweep+5,n-1)):
            b=bars[br]
            if direction*(b[3]-target)<=.05*scale or not confirmation(b,direction,scale):continue
            # The level was still unbroken at the sweep; BOS cannot be historical.
            if direction*(bars[sweep][3]-target)>=0:continue
            opposite=[i for i in range(max(sweep-2,0),br) if direction*(bars[i][3]-bars[i][0])<0]
            if not opposite:continue
            ob=bars[opposite[-1]];low,high=ob[2],ob[1]
            if high-low>2*scale:continue
            between=bars[br+1:n-1]
            if any(direction*(x[3]-(low if direction==1 else high))<0 for x in between):continue
            if last[2]>high+tol or last[1]<low-tol:continue
            if direction*(last[3]-(high if direction==1 else low))<=0:continue
            if abs(last[3]-(high if direction==1 else low))>scale:continue
            return Decision('buy' if direction==1 else 'sell','سحب سيولة وإعادة اختبار أوردر بلوك',('سحب سيولة عند قاع مؤكد' if direction==1 else 'سحب سيولة عند قمة مؤكدة','كسر هيكل مع شمعة اندفاع','إعادة اختبار المنطقة وتأكيد إغلاق','زخم صاعد' if direction==1 else 'زخم هابط'))
    return None

def liquidity_sweep(bars,direction,scale):
    """A completed liquidity sweep is an independent setup: sweep and close back inside."""
    n=len(bars); tol=.05*scale
    for i in range(max(5,n-7),n):
        highs,lows=pivots(bars,i)
        levels=lows if direction==1 else highs
        if not levels:continue
        level=levels[-1][1];bar=bars[i]
        swept=(bar[2]<level-tol and bar[3]>level) if direction==1 else (bar[1]>level+tol and bar[3]<level)
        if swept:
            return Decision('buy' if direction==1 else 'sell','سحب سيولة مكتمل',('اختراق القاع ثم إغلاق فوقه' if direction==1 else 'اختراق القمة ثم إغلاق تحتها',))
    return None

def structure_break(bars,direction,scale):
    """A confirmed close through the latest confirmed swing is an independent setup."""
    if len(bars)<5:return None
    highs,lows=pivots(bars,len(bars)-1)
    levels=highs if direction==1 else lows
    if not levels:return None
    level=levels[-1][1];prev=bars[-2][3];last=bars[-1]
    crossed=(prev<=level and last[3]>level+.05*scale) if direction==1 else (prev>=level and last[3]<level-.05*scale)
    if crossed:
        return Decision('buy' if direction==1 else 'sell','كسر هيكل مؤكد',('إغلاق شمعة مكتملة بعد آخر قمة/قاع مؤكد',))
    return None

def level_retest(bars,direction,scale):
    """A previously broken swing level retested and held is an independent setup."""
    n=len(bars);tol=.2*scale;last=bars[-1]
    highs,lows=pivots(bars,n-1)
    levels=highs if direction==1 else lows
    for pivot_i,level in reversed(levels):
        for br in range(max(pivot_i+1,n-9),n-1):
            prev=bars[br-1][3];close=bars[br][3]
            broke=(prev<=level and close>level+.05*scale) if direction==1 else (prev>=level and close<level-.05*scale)
            if not broke:continue
            touched=(last[2]<=level+tol and last[3]>level) if direction==1 else (last[1]>=level-tol and last[3]<level)
            if touched:
                return Decision('buy' if direction==1 else 'sell','إعادة اختبار ناجحة',('كسر مستوى مؤكد ثم عودة السعر واختباره',))
    return None

def three_candle_setup(bars,direction,scale):
    if three_candles(bars[-3:],direction,scale):
        return Decision('buy' if direction==1 else 'sell','نموذج ثلاث شموع مكتمل',('ثلاث شموع مكتملة متتابعة في الاتجاه نفسه',))
    return None

def retest_three(bars,direction,scale):
    n=len(bars);start=n-3;tol=.2*scale
    if not three_candles(bars[-3:],direction,scale):return None
    closes=[b[3] for b in bars];fast=ema(closes,9)[-1];slow=ema(closes,21)[-1]
    if direction*(fast-slow)<=0:return None
    # Break a previously confirmed S/R level, then retest before pattern starts.
    for br in range(n-10,start-1):
        highs,lows=pivots(bars,br)
        levels=highs if direction==1 else lows
        if not levels:continue
        level=levels[-1][1]
        if direction*(bars[br-1][3]-level)>0 or direction*(bars[br][3]-level)<=.1*scale:continue
        for retest in range(br+1,start):
            b=bars[retest]
            if not b[2]-tol<=level<=b[1]+tol or direction*(b[3]-level)<0:continue
            if any(direction*(x[3]-level)<-tol for x in bars[retest:start]):continue
            if direction*(bars[-1][3]-level)>3*scale:continue
            return Decision('buy' if direction==1 else 'sell','إعادة اختبار وثلاثة جنود' if direction==1 else 'إعادة اختبار وثلاثة غربان',('كسر مقاومة وتحولها لدعم' if direction==1 else 'كسر دعم وتحوله لمقاومة','إعادة اختبار قبل النموذج','ثلاث شموع مؤكدة متتابعة','اتجاه وزخم صاعدان' if direction==1 else 'اتجاه وزخم هابطان'))
    return None

def evaluate(opens,highs,lows,closes):
    if len(opens)<30 or len({len(opens),len(highs),len(lows),len(closes)})!=1:
        return Decision('wait','',('لا توجد 30 شمعة مكتملة كافية',))
    bars=list(zip(opens,highs,lows,closes))
    if any(not all(math.isfinite(v) and v>0 for v in b) or not b[2]<=min(b[0],b[3])<=max(b[0],b[3])<=b[1] for b in bars):
        return Decision('wait','',('بيانات الشموع غير صالحة',))
    scale=atr(bars)
    if scale<=0:return Decision('wait','',('لا يوجد زخم سعري كافٍ',))
    candidates=[]
    # Each completed pattern can independently produce a signal; no global stack of
    # momentum + room + sweep + BOS + retest is required.
    for direction in (1,-1):
        for detector in (liquidity_sweep,structure_break,level_retest,three_candle_setup):
            result=detector(bars,direction,scale)
            if result:candidates.append(result)
    if not candidates:return Decision('wait','',('انتظار اكتمال سحب السيولة/كسر الهيكل/إعادة الاختبار أو نموذج الثلاث شموع مع الزخم',))
    if len({c.side for c in candidates})>1:return Decision('wait','',('تعارض الشروط؛ لا توجد صفقة',))
    return candidates[0]

def fetch_global_candles(pair, minutes):
    api_key = os.environ.get("TWELVE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("مفتاح بيانات السوق العالمي TWELVE_API_KEY غير مضبوط على الخادم.")
    interval = "5min" if minutes == 5 else "1min"
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": pair,
            "interval": interval,
            "outputsize": 300,
            "apikey": api_key,
            "format": "JSON",
            "timezone": "UTC",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") == "error":
        raise RuntimeError(payload.get("message", "تعذر جلب بيانات السوق العالمي."))
    values = payload.get("values") or []
    base_seconds = 300 if minutes == 5 else 60
    cutoff = int(datetime.now(timezone.utc).timestamp() // 60 * 60)
    raw = {}
    for row in values:
        try:
            stamp = int(datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            if stamp + base_seconds > cutoff:
                continue
            raw[stamp] = (
                float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    width = minutes * 60
    groups = {}
    for stamp, bar in raw.items():
        start = (stamp // width) * width
        groups.setdefault(start, {})[stamp] = bar
    candles = []
    for start in sorted(groups):
        stamps = range(start, start + width, base_seconds)
        parts = [groups[start].get(stamp) for stamp in stamps]
        if any(part is None for part in parts):
            continue
        candles.append((
            parts[0][0],
            max(part[1] for part in parts),
            min(part[2] for part in parts),
            parts[-1][3],
        ))
    if len(candles) < 30:
        raise RuntimeError("لا توجد 30 شمعة مكتملة كافية لهذا الزوج والمدة.")
    selected = candles[-30:]
    return tuple([bar[index] for bar in selected] for index in range(4))

COOLDOWN_UNTIL = 0.0
COOLDOWN_LOCK = threading.Lock()

HTML = r"""

<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090909">
<title>LORD.SYRIA | حاسبة التداول</title>
<style>
:root{--bg:#090909;--panel:#151515;--panel2:#1d1d1d;--gold:#d7b34b;--text:#f5f2e9;--muted:#aaa397;--green:#31c779;--red:#f06464;--line:#34312a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,Tahoma,sans-serif;padding:18px 12px 36px}
.wrap{max-width:760px;margin:auto}.brand{text-align:center;padding:14px 8px 22px}.brand h1{margin:0;color:var(--gold);font-size:30px;letter-spacing:2px}.brand p{margin:8px 0 0;color:var(--muted);font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0}
h2{font-size:17px;margin:0 0 14px;color:var(--gold)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input,select{width:100%;min-width:0;background:#0d0d0d;border:1px solid #454033;color:var(--text);border-radius:10px;padding:12px;font-size:16px;outline:none}
input:focus,select:focus{border-color:var(--gold)}
button{border:0;border-radius:10px;padding:13px 12px;font-size:15px;font-weight:bold;cursor:pointer;min-height:46px}
.gold{background:var(--gold);color:#17130a}.win{background:#123b29;color:#8df0b8;border:1px solid #26774e}.loss{background:#421d1d;color:#ffaaaa;border:1px solid #8b3b3b}.neutral{background:#292929;color:var(--text);border:1px solid #494949}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:13px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:13px;min-width:0}
.stat small{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}.stat strong{font-size:21px;overflow-wrap:anywhere}
.green{color:var(--green)}.red{color:var(--red)}.goldtxt{color:var(--gold)}
.preview{margin-top:12px;padding:12px;background:#101010;border-radius:10px;color:var(--muted);line-height:1.8;font-size:14px}
.periods{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}.periods button{flex:1 1 auto;min-width:70px;padding:9px;font-size:13px;min-height:40px}
.periods button.active{background:var(--gold);color:#17130a}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px;min-width:470px}th,td{padding:11px 8px;border-bottom:1px solid var(--line);text-align:center}th{color:var(--gold);font-weight:bold}td{color:#e9e5dc}
.empty{text-align:center;color:var(--muted);padding:22px 8px}
.footer{text-align:center;color:#817b70;font-size:11px;padding:15px 5px}
.note{font-size:12px;color:var(--muted);line-height:1.7;margin:10px 0 0}
@media(max-width:430px){body{padding:10px 9px 28px}.panel{padding:13px}.brand h1{font-size:26px}.stats{gap:8px}.stat{padding:11px}.stat strong{font-size:18px}}

/* Radar widget */
.radar-panel{border-color:#5b4920;background:linear-gradient(155deg,#17150f,#111)}
.radar-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.radar-badge{display:inline-flex;align-items:center;gap:7px;border:1px solid #31573e;border-radius:99px;padding:6px 10px;color:#8df0b8;font-size:12px}
.radar-dot{width:8px;height:8px;border-radius:50%;background:#31c779;box-shadow:0 0 10px #31c779}
.pair-window{height:170px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#806a2c #171717;border:1px solid var(--line);border-radius:12px;background:#0d0d0d;position:relative;touch-action:pan-y;overscroll-behavior:contain}
.pair-list{display:flex;flex-direction:column;gap:6px;padding:8px}
.pair-option{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;min-height:38px;background:#171717;color:var(--text);border:1px solid #2d2b25;border-radius:9px;padding:8px 11px;text-align:right;font-size:14px}
.pair-option.selected{border-color:var(--gold);background:#282315;color:#f5d778}
.pair-option .pair-name{direction:ltr;text-align:left;flex:1;font-weight:bold}
.pair-option .pair-dot{width:17px;height:17px;border:2px solid #777;border-radius:50%;flex:0 0 17px;position:relative}
.pair-option.selected .pair-dot{border-color:var(--gold)}
.pair-option.selected .pair-dot:after{content:"";position:absolute;inset:3px;border-radius:50%;background:var(--gold)}
.radar-controls{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:13px}
.radar-durations{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.radar-duration{min-height:43px;padding:8px 4px;background:#101010;color:var(--text);border:1px solid #454033;border-radius:9px;font-size:14px}
.radar-duration.active{background:var(--gold);color:#17130a;border-color:var(--gold)}
.radar-result{margin-top:12px;min-height:68px;display:flex;align-items:center;flex-direction:column;gap:6px;justify-content:center;text-align:center;border:1px solid var(--line);border-radius:12px;background:#0e0e0e;padding:12px;color:var(--muted);line-height:1.7}
.radar-result.busy{color:var(--gold)}.radar-result.no-signal{border-color:#6c5320;color:#f0d27a}.radar-result.signal-buy{border-color:var(--green);color:var(--green)}.radar-result.signal-sell{border-color:var(--red);color:var(--red)}
.radar-result strong{display:block;color:var(--text);font-size:18px}
.radar-generate{width:100%;margin-top:11px}
.radar-generate:disabled{opacity:.5;cursor:not-allowed}
.radar-note{margin-top:9px}
@media(max-width:430px){.radar-controls{grid-template-columns:1fr}.pair-window{height:155px}}

</style>
</head>
<body>
<div class="wrap">
  <header class="brand"><h1>LORD.SYRIA</h1><p>حاسبة التداول وإدارة رأس المال — Pocket Option</p></header>


  <section class="panel radar-panel" aria-labelledby="radarTitle">
    <div class="radar-top"><h2 id="radarTitle" style="margin:0">🤖 بوت LORD SYRIA</h2><span class="radar-badge"><i class="radar-dot"></i> جاهز للاختيار</span></div>
    <label>اختر الزوج — اضغط على الدائرة. تتحرك القائمة للأعلى ويمكنك لمسها لإيقافها مؤقتاً.</label>
    <div class="pair-window" id="pairWindow" aria-label="قائمة أزواج التداول"><div class="pair-list" id="pairList"></div></div>
    <div class="radar-controls">
      <div><label>مدة الإشارة</label><div class="radar-durations" id="radarDurations" role="group" aria-label="مدة الإشارة">
        <button type="button" class="radar-duration" data-minutes="1" aria-pressed="false">1 د</button><button type="button" class="radar-duration" data-minutes="2" aria-pressed="false">2 د</button><button type="button" class="radar-duration" data-minutes="3" aria-pressed="false">3 د</button><button type="button" class="radar-duration" data-minutes="5" aria-pressed="false">5 د</button>
      </div></div>
      <div style="display:flex;align-items:end"><button type="button" class="gold radar-generate" id="generateSignal" disabled>⚡ توليد صفقة</button></div>
    </div>
    <div class="radar-result" id="radarResult" aria-live="polite">اختر زوجاً ومدة لتفعيل زر توليد الصفقة.</div>
    <p class="note radar-note">أزواج الفوركس العالمية فقط. عند طلب الإشارة يُرسل الزوج والمدة إلى خادم LORD SYRIA لتحليل بيانات السوق. التنفيذ يدوي.</p>
  </section>

  <section class="panel">
    <h2>⚙️ إعدادات الحساب</h2>
    <div class="grid">
      <div><label for="initial">رأس المال الابتدائي ($)</label><input id="initial" type="number" min="0" step="0.01" value="100"></div>
      <div><label for="stake">قيمة الصفقة ($)</label><input id="stake" type="number" min="0.01" step="0.01" value="5"></div>
      <div><label for="payout">نسبة العائد (%)</label><input id="payout" type="number" min="0" max="1000" step="0.1" value="80"></div>
      <div><label for="period">عرض النتائج حسب المدة</label><select id="period"><option value="today">اليوم</option><option value="week">هذا الأسبوع</option><option value="month" selected>هذا الشهر</option><option value="3months">آخر 3 أشهر</option><option value="year">هذه السنة</option><option value="all">كل المدة</option></select></div>
    </div>
    <div class="preview" id="preview" aria-live="polite">أدخل قيمة الصفقة ونسبة العائد لعرض الحساب المتوقع.</div>
  </section>

  <section class="panel">
    <h2>🧮 تسجيل الصفقة</h2>
    <div class="grid">
      <div><label for="pair">الأصل / الزوج (اختياري)</label><input id="pair" type="text" placeholder="EUR/USD"></div>
      <div><label for="duration">مدة الصفقة</label><select id="duration"><option>5 ثوانٍ</option><option>15 ثانية</option><option>30 ثانية</option><option selected>1 دقيقة</option><option>2 دقيقة</option><option>3 دقائق</option><option>5 دقائق</option><option>10 دقائق</option><option>15 دقيقة</option><option>30 دقيقة</option><option>1 ساعة</option></select></div>
    </div>
    <div class="actions">
      <button class="win" id="addWin">🟢 تسجيل صفقة رابحة</button>
      <button class="loss" id="addLoss">🔴 تسجيل صفقة خاسرة</button>
    </div>
    <p class="note">يتم حساب الربح تلقائياً من قيمة الصفقة ونسبة العائد عند تسجيل الفوز. عند الخسارة تُخصم قيمة الصفقة كاملة. لا تنفّذ الحاسبة صفقات على المنصة.</p>
  </section>

  <section class="panel">
    <h2>📊 ملخص الأداء — <span id="periodName">هذا الشهر</span></h2>
    <div class="stats">
      <div class="stat"><small>الرصيد الحالي</small><strong id="balance" class="goldtxt">$100.00</strong></div>
      <div class="stat"><small>صافي الربح / الخسارة للمدة</small><strong id="net">$0.00</strong></div>
      <div class="stat"><small>الصفقات الرابحة</small><strong id="wins" class="green">0</strong></div>
      <div class="stat"><small>الصفقات الخاسرة</small><strong id="losses" class="red">0</strong></div>
      <div class="stat"><small>نسبة النجاح</small><strong id="rate">0%</strong></div>
      <div class="stat"><small>إجمالي الصفقات للمدة</small><strong id="count">0</strong></div>
    </div>
  </section>

  <section class="panel">
    <h2>🗓️ سجل الصفقات</h2>
    <div class="periods" aria-label="اختيار المدة">
      <button class="neutral" data-period="today">اليوم</button><button class="neutral" data-period="week">أسبوع</button><button class="neutral active" data-period="month">شهر</button><button class="neutral" data-period="3months">3 أشهر</button><button class="neutral" data-period="year">سنة</button><button class="neutral" data-period="all">الكل</button>
    </div>
    <div class="tablewrap"><table><thead><tr><th>التاريخ والوقت</th><th>الأصل</th><th>النتيجة</th><th>قيمة الصفقة</th><th>الصافي</th></tr></thead><tbody id="history"></tbody></table></div>
    <div id="empty" class="empty">لا توجد صفقات مسجلة لهذه المدة.</div>
    <div class="actions"><button class="neutral" id="export">⬇️ تصدير السجل CSV</button><button class="neutral" id="clear">مسح سجل الصفقات</button></div>
  </section>

  <section class="panel" id="growthCalculator">
    <h2>💰 حاسبة نمو رأس المال والأرباح اليومية</h2>
    <div class="grid">
      <div><label for="growthCapital">رأس المال ($)</label><input id="growthCapital" type="number" min="0.01" step="0.01" value="100"></div>
      <div><label for="dailyRate">نسبة الربح اليومية (%)</label><input id="dailyRate" type="number" min="0" max="100" step="0.1" value="2"></div>
      <div><label for="growthMode">طريقة الحساب</label><select id="growthMode"><option value="compound" selected>ربح مركب — إعادة استثمار الأرباح</option><option value="simple">ربح ثابت — دون إعادة استثمار</option></select></div>
      <div><label for="customDays">أو أدخل عدد الأيام</label><input id="customDays" type="number" min="1" max="3650" step="1" placeholder="مثلاً 45"></div>
    </div>
    <p class="note">اختر المدة ليظهر الربح المتوقع ورأس المال النهائي. الشهر محسوب على أساس 30 يوماً.</p>
    <div class="periods" id="growthPeriods" aria-label="اختيار مدة الحساب">
      <button type="button" class="neutral" data-days="7">أسبوع</button>
      <button type="button" class="neutral" data-days="15">15 يوماً</button>
      <button type="button" class="neutral active" data-days="30">شهر</button>
      <button type="button" class="neutral" data-days="60">شهران</button>
      <button type="button" class="neutral" data-days="90">3 أشهر</button>
      <button type="button" class="neutral" data-days="120">4 أشهر</button>
      <button type="button" class="neutral" data-days="180">6 أشهر</button>
      <button type="button" class="neutral" data-days="365">سنة</button>
    </div>
    <div class="stats">
      <div class="stat"><small>المدة المختارة</small><strong id="growthDuration" class="goldtxt">30 يوماً</strong></div>
      <div class="stat"><small>الربح المتوقع</small><strong id="growthProfit" class="green">$81.14</strong></div>
      <div class="stat"><small>رأس المال النهائي</small><strong id="growthFinal" class="goldtxt">$181.14</strong></div>
      <div class="stat"><small>نسبة النمو الإجمالية</small><strong id="growthPercent">81.14%</strong></div>
    </div>
    <div class="preview" id="growthMessage" aria-live="polite">الحساب تقديري ويفترض تحقيق النسبة اليومية كل يوم دون خسائر أو توقف.</div>
  </section>
  <div class="footer">LORD.SYRIA • أداة حساب ومتابعة فقط — التداول ينطوي على مخاطر</div>
</div>
<script>
(function(){
 const $=id=>document.getElementById(id);
 // Global forex only; signals are requested from the existing LORD SYRIA market-data service.
 const radarPairs=['EUR/USD','GBP/USD','USD/JPY','USD/CHF','AUD/USD','USD/CAD','NZD/USD','EUR/GBP','EUR/JPY','GBP/JPY','EUR/CHF','CAD/JPY','AUD/JPY','GBP/CHF','EUR/AUD','EUR/CAD','GBP/CAD','NZD/JPY'];
 let chosenRadarPair=null,chosenRadarMinutes=null,radarTimer=null;
 const pairList=$('pairList'),radarResult=$('radarResult'),generateSignal=$('generateSignal');
 function updateRadarReadiness(){generateSignal.disabled=!(chosenRadarPair&&chosenRadarMinutes)||Boolean(radarTimer);}
 radarPairs.forEach(pair=>{
  const b=document.createElement('button');b.type='button';b.className='pair-option';b.dataset.pair=pair;
  b.innerHTML='<span class="pair-name">'+pair+'</span><span class="pair-dot" aria-hidden="true"></span>';
  b.setAttribute('aria-pressed','false');
  b.addEventListener('click',()=>{
   chosenRadarPair=pair;
   pairList.querySelectorAll('.pair-option').forEach(x=>{const active=x.dataset.pair===pair;x.classList.toggle('selected',active);x.setAttribute('aria-pressed',active?'true':'false');});
   $('pair').value=pair;radarResult.className='radar-result';
   radarResult.textContent=chosenRadarMinutes?'تم اختيار '+pair+' · '+chosenRadarMinutes+' دقائق.':'تم اختيار '+pair+' — اختر مدة الإشارة.';
   updateRadarReadiness();
  });
  pairList.appendChild(b);
 });
 const pairWindow=$('pairWindow');let lastTickerFrame=0,resumeTickerAt=0;
 function movePairsUp(now){
  const delta=Math.min(80,now-lastTickerFrame||0);lastTickerFrame=now;
  if(now>resumeTickerAt&&pairWindow.scrollHeight>pairWindow.clientHeight){
   pairWindow.scrollTop+=delta*0.014;
   if(pairWindow.scrollTop>=pairWindow.scrollHeight-pairWindow.clientHeight-1)pairWindow.scrollTop=0;
  }
  requestAnimationFrame(movePairsUp);
 }
 const pauseTicker=()=>{resumeTickerAt=performance.now()+3200;};
 ['pointerdown','touchstart','wheel','focusin'].forEach(type=>pairWindow.addEventListener(type,pauseTicker,{passive:true}));
 requestAnimationFrame(movePairsUp);
 document.querySelectorAll('#radarDurations [data-minutes]').forEach(b=>b.addEventListener('click',()=>{
  chosenRadarMinutes=Number(b.dataset.minutes);
  document.querySelectorAll('#radarDurations [data-minutes]').forEach(x=>{const active=x===b;x.classList.toggle('active',active);x.setAttribute('aria-pressed',active?'true':'false');});
  $('duration').value=chosenRadarMinutes+' دقيقة';
  radarResult.className='radar-result';
  radarResult.textContent=chosenRadarPair?'تم اختيار '+chosenRadarPair+' · '+chosenRadarMinutes+' دقائق.':'تم اختيار '+chosenRadarMinutes+' دقائق — اختر الزوج.';
  updateRadarReadiness();
 }));
 let countdownInterval=null;
 generateSignal.addEventListener('click',async()=>{
  if(!chosenRadarPair||!chosenRadarMinutes||radarTimer)return;
  radarTimer=true;updateRadarReadiness();
  generateSignal.textContent='جارٍ التحليل...';
  radarResult.className='radar-result busy';
  radarResult.innerHTML='<strong>جارٍ تحليل '+escapeHtml(chosenRadarPair)+'</strong>انتظر ظهور النتيجة هنا.';
  try{
   const response=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pair:chosenRadarPair,minutes:chosenRadarMinutes})});
   const data=await response.json();
   if(!response.ok)throw new Error(data.error||'تعذر تحليل السوق الآن.');
   radarResult.className='radar-result '+(data.cls==='buy'?'signal-buy':data.cls==='sell'?'signal-sell':'no-signal');
   radarResult.innerHTML='<strong>'+escapeHtml(data.message)+'</strong><span>'+escapeHtml(data.pair)+' · '+escapeHtml(data.duration_label)+(data.entry_time?'<br>⏰ وقت الدخول: '+escapeHtml(data.entry_time):'')+(data.price?' · السعر: '+escapeHtml(data.price):'')+'</span>';
  }catch(e){
   radarResult.className='radar-result no-signal';
   radarResult.textContent=e.message||'تعذر الاتصال بخادم الإشارات.';
  }
  let seconds=60;
  generateSignal.textContent='إعادة الإشارة بعد '+seconds+' ث';
  countdownInterval=setInterval(()=>{
   seconds--;
   if(seconds<=0){clearInterval(countdownInterval);countdownInterval=null;radarTimer=false;generateSignal.textContent='⚡ توليد صفقة';updateRadarReadiness();return;}
   generateSignal.textContent='إعادة الإشارة بعد '+seconds+' ث';
  },1000);
 });
 updateRadarReadiness();

 const keys={initial:'lordSyriaInitial',trades:'lordSyriaTrades'};
 let trades=[];
 try{trades=JSON.parse(localStorage.getItem(keys.trades)||'[]');if(!Array.isArray(trades))trades=[];}catch(e){trades=[];}
 const savedInitial=localStorage.getItem(keys.initial);if(savedInitial!==null)$('initial').value=savedInitial;
 const names={today:'اليوم',week:'هذا الأسبوع',month:'هذا الشهر','3months':'آخر 3 أشهر',year:'هذه السنة',all:'كل المدة'};
 let selected='month';
 function money(n){return (n<0?'-$':'$')+Math.abs(n).toFixed(2);}
 function validInputs(){
  const stake=Number($('stake').value), payout=Number($('payout').value), initial=Number($('initial').value);
  return {stake,payout,initial,ok:Number.isFinite(stake)&&stake>0&&Number.isFinite(payout)&&payout>=0&&payout<=1000&&Number.isFinite(initial)&&initial>=0};
 }
 function updatePreview(){
  const v=validInputs();
  if(!v.ok){$('preview').textContent='تحقق من المدخلات: رأس المال لا يقل عن صفر، وقيمة الصفقة أكبر من صفر، والعائد بين 0 و1000%. ورأس المال الحالي يُحسب من رأس المال الابتدائي مضافاً إليه نتائج جميع الصفقات المسجلة.';return;}
  const profit=v.stake*v.payout/100;
  const breakeven=100/(1+v.payout/100);
  $('preview').innerHTML='عند الفوز: <b class="green">+'+money(profit)+'</b> &nbsp; | &nbsp; عند الخسارة: <b class="red">-'+money(v.stake)+'</b><br>نسبة الفوز اللازمة للتعادل تقريباً: <b class="goldtxt">'+breakeven.toFixed(1)+'%</b>';
 }
 function startOfWeek(d){const x=new Date(d);x.setHours(0,0,0,0);x.setDate(x.getDate()-((x.getDay()+6)%7));return x;}
 function inPeriod(iso,p){
  const d=new Date(iso),now=new Date();if(p==='all')return true;
  if(p==='today')return d.toDateString()===now.toDateString();
  if(p==='week')return d>=startOfWeek(now);
  if(p==='month')return d.getMonth()===now.getMonth()&&d.getFullYear()===now.getFullYear();
  if(p==='3months'){const from=new Date(now);from.setMonth(from.getMonth()-3);return d>=from;}
  if(p==='year')return d.getFullYear()===now.getFullYear();
  return true;
 }
 function render(){
  const v=validInputs();const initial=v.ok?v.initial:0;
  const allNet=trades.reduce((s,t)=>s+t.net,0);
  $('balance').textContent=money(initial+allNet);
  const list=trades.filter(t=>inPeriod(t.date,selected));
  const net=list.reduce((s,t)=>s+t.net,0),wins=list.filter(t=>t.result==='فوز').length,losses=list.length-wins;
  $('net').textContent=money(net);$('net').className=net>=0?'green':'red';
  $('wins').textContent=wins;$('losses').textContent=losses;$('count').textContent=list.length;
  $('rate').textContent=(list.length?wins/list.length*100:0).toFixed(1)+'%';
  $('periodName').textContent=names[selected];
  $('history').innerHTML=list.slice().reverse().map(t=>'<tr><td>'+new Date(t.date).toLocaleString('ar-EG')+'</td><td>'+escapeHtml(t.pair||'—')+'</td><td class="'+(t.result==='فوز'?'green':'red')+'">'+t.result+'</td><td>'+money(t.stake)+'</td><td class="'+(t.net>=0?'green':'red')+'">'+money(t.net)+'</td></tr>').join('');
  $('empty').style.display=list.length?'none':'block';
  document.querySelectorAll('[data-period]').forEach(b=>b.classList.toggle('active',b.dataset.period===selected));
  localStorage.setItem(keys.initial,String(initial));
 }
 function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
 function add(result){
  const v=validInputs();if(!v.ok){alert('يرجى إدخال رأس مال وقيمة صفقة ونسبة عائد صحيحة.');return;}
  const net=result==='فوز'?v.stake*v.payout/100:-v.stake;
  trades.push({date:new Date().toISOString(),pair:$('pair').value.trim(),duration:$('duration').value,stake:v.stake,payout:v.payout,result,net});
  localStorage.setItem(keys.trades,JSON.stringify(trades));render();
 }
 $('addWin').addEventListener('click',()=>add('فوز'));
 $('addLoss').addEventListener('click',()=>add('خسارة'));
 ['initial','stake','payout'].forEach(id=>$(id).addEventListener('input',()=>{updatePreview();render();}));
 $('period').addEventListener('change',e=>{selected=e.target.value;render();});
 document.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.period;$('period').value=selected;render();}));
 $('clear').addEventListener('click',()=>{if(confirm('هل أنت متأكد من مسح جميع الصفقات المسجلة؟ لا يمكن التراجع عن ذلك.')){trades=[];localStorage.removeItem(keys.trades);render();}});
 $('export').addEventListener('click',()=>{
  const rows=[['التاريخ','الأصل','النتيجة','قيمة الصفقة','نسبة العائد','الصافي'],...trades.map(t=>[new Date(t.date).toLocaleString('en-GB'),t.pair,t.result,t.stake,t.payout,t.net])];
  const csv='\ufeff'+rows.map(r=>r.map(x=>'"'+String(x??'').replace(/"/g,'""')+'"').join(',')).join('\r\n');
  const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download='LORD-SYRIA-trades.csv';a.click();URL.revokeObjectURL(url);
 });

 function formatDays(days){
  if(days===7)return 'أسبوع (7 أيام)';
  if(days===15)return '15 يوماً';
  if(days===30)return 'شهر (30 يوماً)';
  if(days===60)return 'شهران (60 يوماً)';
  if(days===90)return '3 أشهر (90 يوماً)';
  if(days===120)return '4 أشهر (120 يوماً)';
  if(days===180)return '6 أشهر (180 يوماً)';
  if(days===365)return 'سنة (365 يوماً)';
  return days+' يوماً';
 }
 function updateGrowth(days){
  const capital=Number($('growthCapital').value),rate=Number($('dailyRate').value),mode=$('growthMode').value;
  if(!Number.isFinite(capital)||capital<=0||!Number.isFinite(rate)||rate<0||rate>100||!Number.isInteger(days)||days<1||days>3650){
   $('growthMessage').textContent='يرجى إدخال رأس مال أكبر من صفر، ونسبة يومية بين 0 و100، ومدة بين يوم و3650 يوماً.';
   $('growthProfit').textContent='—';$('growthFinal').textContent='—';$('growthPercent').textContent='—';return;
  }
  const final=mode==='compound'?capital*Math.pow(1+rate/100,days):capital*(1+(rate/100)*days);
  const profit=final-capital, growth=capital?profit/capital*100:0;
  $('growthDuration').textContent=formatDays(days);
  $('growthProfit').textContent=money(profit);
  $('growthFinal').textContent=money(final);
  $('growthPercent').textContent=growth.toFixed(2)+'%';
  $('growthMessage').textContent=(mode==='compound'?'حساب مركب: تُضاف الأرباح إلى رأس المال يومياً ويُحسب ربح اليوم التالي على الرصيد الجديد. ':'حساب ثابت: تُحسب النسبة اليومية من رأس المال الأصلي فقط. ')+'النتيجة افتراضية ولا تضمن تحقيق هذه النسبة في التداول الحقيقي.';
  document.querySelectorAll('#growthPeriods [data-days]').forEach(b=>b.classList.toggle('active',Number(b.dataset.days)===days));
 }
 let selectedGrowthDays=30;
 document.querySelectorAll('#growthPeriods [data-days]').forEach(b=>b.addEventListener('click',()=>{
  selectedGrowthDays=Number(b.dataset.days);$('customDays').value='';updateGrowth(selectedGrowthDays);
 }));
 ['growthCapital','dailyRate','growthMode'].forEach(id=>$(id).addEventListener('input',()=>updateGrowth(selectedGrowthDays)));
 $('customDays').addEventListener('input',()=>{
  const days=Number($('customDays').value);
  if($('customDays').value.trim()!==''){selectedGrowthDays=days;updateGrowth(days);document.querySelectorAll('#growthPeriods [data-days]').forEach(b=>b.classList.remove('active'));}
 });
 if(!$('payout').value||$('payout').value==='ход')$('payout').value='80';
 updatePreview();render();updateGrowth(selectedGrowthDays);
})();
</script>
</body>
</html>

"""

@app.get("/")
def home():
    response = app.make_response(HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.post("/api/analyze")
def api_analyze():
    global COOLDOWN_UNTIL
    body = request.get_json(silent=True) or {}
    pair = body.get("pair")
    minutes = body.get("minutes", 1)
    if not isinstance(pair, str) or pair not in PAIRS:
        return jsonify(error="اختر زوجاً من أزواج الفوركس العالمية."), 400
    if type(minutes) is not int or minutes not in TIMEFRAMES:
        return jsonify(error="مدة الإشارة المتاحة: 1 أو 2 أو 3 أو 5 دقائق."), 400

    with COOLDOWN_LOCK:
        remaining = max(0, math.ceil(COOLDOWN_UNTIL - time.monotonic()))
        if remaining:
            return jsonify(error="انتظر انتهاء العداد قبل طلب إشارة جديدة.", cooldown=remaining), 429

    try:
        data = fetch_global_candles(pair, minutes)
        decision = evaluate(*data)
        side = decision.side
        if side == "buy":
            message = "🟢 CALL — صعود"
        elif side == "sell":
            message = "🔴 PUT — هبوط"
        else:
            message = "🟡 لا توجد إشارة مناسبة الآن"
        if decision.setup:
            message += " · " + decision.setup
        if decision.reasons:
            message += " · " + "؛ ".join(decision.reasons)
        price = f"{data[3][-1]:.5f}"
        entry_time = ""
        if side in ("buy", "sell"):
            now_epoch = int(time.time())
            next_entry = (now_epoch // (minutes * 60) + 1) * (minutes * 60)
            entry_time = datetime.fromtimestamp(next_entry, ZoneInfo("Europe/Istanbul")).strftime("%H:%M:%S")
            with COOLDOWN_LOCK:
                COOLDOWN_UNTIL = time.monotonic() + 60
        duration_label = {1: "دقيقة واحدة", 2: "دقيقتين", 3: "3 دقائق", 5: "5 دقائق"}[minutes]
        return jsonify(
            pair=pair,
            minutes=minutes,
            duration_label=duration_label,
            cls=side,
            message=message,
            price=price,
            entry_time=entry_time,
            cooldown=max(0, math.ceil(COOLDOWN_UNTIL - time.monotonic())),
        )
    except requests.RequestException:
        return jsonify(error="تعذر الاتصال بمصدر بيانات السوق العالمي الآن."), 502
    except Exception as error:
        return jsonify(error=str(error)), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
