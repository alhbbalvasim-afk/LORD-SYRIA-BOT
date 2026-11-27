from flask import Flask, request, jsonify, send_from_directory
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# Final unified version — use this file only.
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

def ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))

def fetch_candles(pair, minutes=1):
    api_key = os.environ.get("TWELVE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TWELVE_API_KEY is not set")

    interval = "5min" if minutes == 5 else "1min"
    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": 300,
        "apikey": api_key,
        "format": "JSON",
    }

    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params=params,
        timeout=15
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("status") == "error":
        raise RuntimeError(payload.get("message", "Twelve Data error"))

    rows = payload.get("values") or []
    if len(rows) < 30:
        return None

    # Twelve Data returns newest first; analysis needs oldest -> newest.
    rows = list(reversed(rows))

    # Build true clock-aligned 2m/3m candles.
    if minutes in (2, 3):
        grouped = []
        current_key = None
        bucket = []

        def flush_bucket(items):
            if not items:
                return
            grouped.append({
                "open": items[0]["open"],
                "high": max(float(x["high"]) for x in items),
                "low": min(float(x["low"]) for x in items),
                "close": items[-1]["close"],
            })

        for row in rows:
            dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            bucket_minute = dt.minute - (dt.minute % minutes)
            key = (dt.year, dt.month, dt.day, dt.hour, bucket_minute)

            if current_key is None:
                current_key = key

            if key != current_key:
                flush_bucket(bucket)
                bucket = []
                current_key = key

            bucket.append(row)

        flush_bucket(bucket)
        rows = grouped

    if len(rows) < 30:
        return None

    opens = [float(x["open"]) for x in rows]
    highs = [float(x["high"]) for x in rows]
    lows = [float(x["low"]) for x in rows]
    closes = [float(x["close"]) for x in rows]
    return opens, highs, lows, closes

def analyze(pair, minutes=1):
    """Rule-based technical confluence; score is not a historical win probability."""
    try:
        data = fetch_candles(pair, minutes)
        if not data:
            return {"cls":"wait", "message":"لا توجد شموع كافية للتحليل", "symbol":pair,
                    "price":"—", "agreement":0, "tools":[]}
        opens, highs, lows, closes = data
        price = closes[-1]
        e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
        r = rsi(closes, 14)
        momentum = closes[-1] - closes[-6]
        # ATR-based distance gives support/resistance checks a volatility-aware scale.
        true_ranges = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
        atr = sum(true_ranges[-14:]) / max(1, len(true_ranges[-14:]))
        prior_high, prior_low = max(highs[-15:-2]), min(lows[-15:-2])
        support, resistance = min(lows[-20:-1]), max(highs[-20:-1])
        last_o,last_c,last_h,last_l=opens[-1],closes[-1],highs[-1],lows[-1]
        prev_o,prev_c=opens[-2],closes[-2]

        checks=[]; buy=sell=0
        def add(name, direction, detail, weight=1):
            nonlocal buy,sell
            checks.append({"name":name,"result":detail,"direction":direction})
            if direction=="buy": buy+=weight
            elif direction=="sell": sell+=weight

        add("اتجاه السوق وهيكله", "buy" if e9>e21 else "sell" if e9<e21 else "neutral",
            "EMA 9 أعلى من EMA 21" if e9>e21 else "EMA 9 أدنى من EMA 21" if e9<e21 else "المتوسطان متقاربان")
        add("كسر هيكل السعر", "buy" if last_c>prior_high else "sell" if last_c<prior_low else "neutral",
            "إغلاق فوق القمة القريبة" if last_c>prior_high else "إغلاق تحت القاع القريب" if last_c<prior_low else "لا يوجد كسر واضح", 2)
        add("سحب السيولة", "buy" if last_l<prior_low and last_c>prior_low else "sell" if last_h>prior_high and last_c<prior_high else "neutral",
            "سحب أسفل قاع ثم إغلاق فوقه" if last_l<prior_low and last_c>prior_low else "سحب أعلى قمة ثم إغلاق تحتها" if last_h>prior_high and last_c<prior_high else "لا يوجد سحب مؤكد", 2)
        near_support=abs(price-support)<=max(atr*.35, price*.00015)
        near_resistance=abs(price-resistance)<=max(atr*.35, price*.00015)
        add("الدعم والمقاومة", "buy" if near_support and last_c>last_o else "sell" if near_resistance and last_c<last_o else "neutral",
            "ارتداد قرب الدعم" if near_support and last_c>last_o else "رفض قرب المقاومة" if near_resistance and last_c<last_o else "لا يوجد ارتداد مؤكد")
        bull_engulf=last_c>last_o and prev_c<prev_o and last_c>=prev_o and last_o<=prev_c
        bear_engulf=last_c<last_o and prev_c>prev_o and last_o>=prev_c and last_c<=prev_o
        add("نموذج الشموع", "buy" if bull_engulf else "sell" if bear_engulf else "buy" if last_c>last_o and last_c-last_o>atr*.55 else "sell" if last_c<last_o and last_o-last_c>atr*.55 else "neutral",
            "ابتلاع شرائي" if bull_engulf else "ابتلاع بيعي" if bear_engulf else "شمعة صاعدة قوية" if last_c>last_o and last_c-last_o>atr*.55 else "شمعة هابطة قوية" if last_c<last_o and last_o-last_c>atr*.55 else "لا يوجد نموذج قوي")
        add("RSI والزخم", "buy" if r is not None and 52<=r<68 and momentum>0 else "sell" if r is not None and 32<r<=48 and momentum<0 else "neutral",
            f"RSI {r:.1f} والزخم صاعد" if r is not None and 52<=r<68 and momentum>0 else f"RSI {r:.1f} والزخم هابط" if r is not None and 32<r<=48 and momentum<0 else f"RSI {r:.1f} أو الزخم غير متوافق" if r is not None else "RSI غير متاح")

        total=sum(2 if x["name"] in ("كسر هيكل السعر","سحب السيولة") else 1 for x in checks)
        agreement=round(100*max(buy,sell)/total)
        # Require multiple independent confirmations and reject opposing structure.
        cls="buy" if buy>=5 and buy>sell and sell<=1 else "sell" if sell>=5 and sell>buy and buy<=1 else "wait"
        reasons=[x["name"]+": "+x["result"] for x in checks if x["direction"]==cls]
        message=("إشارة صعود؛ "+"، ".join(reasons[:3])) if cls=="buy" else ("إشارة هبوط؛ "+"، ".join(reasons[:3])) if cls=="sell" else "لا توجد إشارة: التأكيدات غير كافية أو متعارضة"
        return {"cls":cls,"message":message,"symbol":pair,"price":f"{price:.5f}",
                "agreement":agreement,"tools":checks,"buy_score":buy,"sell_score":sell}
    except Exception as e:
        return {"cls":"wait","message":f"تعذر جلب بيانات السوق: {str(e)}","symbol":pair,
                "price":"—","agreement":0,"tools":[]}

def ticker_name(pair):
    return PAIRS.get(pair, "")


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
.radar-result.busy{color:var(--gold)}.radar-result.no-signal{border-color:#6c5320;color:#f0d27a}
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
   radarResult.innerHTML='<strong>'+escapeHtml(data.message)+'</strong><span>'+escapeHtml(data.pair)+' · '+escapeHtml(data.duration_label)+'<br>⏰ وقت الدخول: '+escapeHtml(data.entry_time)+(data.price?' · السعر: '+escapeHtml(data.price):'')+'</span>';
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

@app.route("/", methods=["GET"])
def home():
    response = app.make_response(HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/analyzer", methods=["GET"])
def analyzer_page():
    return send_from_directory(app.root_path, "LORD-SYRIA-GLOBAL-QUANT-BOT.html")

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(silent=True) or {}
    pair = body.get("pair")
    minutes = body.get("minutes", 1)
    if not isinstance(pair, str) or pair not in PAIRS:
        return jsonify(error="اختر زوجاً عالمياً صحيحاً."), 400
    if type(minutes) is not int or minutes not in TIMEFRAMES:
        return jsonify(error="مدة الإشارة يجب أن تكون 1 أو 2 أو 3 أو 5 دقائق."), 400

    result = analyze(pair, minutes)
    local_now = datetime.now(ZoneInfo("Europe/Istanbul"))
    entry = local_now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
    duration_label = {1: "دقيقة واحدة", 2: "دقيقتين", 3: "3 دقائق", 5: "5 دقائق"}[minutes]
    return jsonify(
        pair=pair,
        minutes=minutes,
        duration_label=duration_label,
        **result,
        entry_time=entry.strftime("%H:%M"),
    )

if __name__ == "__main__":
    print("LORD SYRIA — حاسبة التداول والبوت العالمي")
    print("Open: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
