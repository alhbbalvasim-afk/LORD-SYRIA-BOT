from flask import Flask, request, render_template_string
import os
import requests
from datetime import datetime, timedelta

app = Flask(__name__)

# Final unified version — use this file only.
TIMEFRAMES = (1,)

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
    try:
        data = fetch_candles(pair, minutes)
        if not data:
            return "wait", "🟡 لا توجد بيانات سوق كافية الآن", "", ""
        opens, highs, lows, closes = data
        e9 = ema(closes, 9)[-1]
        e21 = ema(closes, 21)[-1]
        r = rsi(closes, 14)
        price = closes[-1]
        momentum = closes[-1] - closes[-6]
        support = min(lows[-20:])
        resistance = max(highs[-20:])
        bullish = sum(1 for o, c in zip(opens[-3:], closes[-3:]) if c > o)
        bearish = sum(1 for o, c in zip(opens[-3:], closes[-3:]) if c < o)

        buy = sell = 0
        if e9 > e21: buy += 1
        elif e9 < e21: sell += 1
        if r is not None:
            if 52 <= r < 70: buy += 1
            elif 30 < r <= 48: sell += 1
        if momentum > 0: buy += 1
        elif momentum < 0: sell += 1
        if bullish >= 2: buy += 1
        elif bearish >= 2: sell += 1
        middle = (support + resistance) / 2
        if price > middle: buy += 1
        elif price < middle: sell += 1

        if buy >= 4 and buy > sell:
            return "buy", "🟢 CALL — صعود", f"Twelve Data — {pair} — {minutes}m", f"{price:.5f}"
        if sell >= 4 and sell > buy:
            return "sell", "🔴 PUT — هبوط", f"Twelve Data — {pair} — {minutes}m", f"{price:.5f}"
        return "wait", "🟡 لا توجد إشارة مناسبة الآن", f"Twelve Data — {pair} — {minutes}m", f"{price:.5f}"
    except Exception as e:
        return "wait", f"🟡 تعذر جلب بيانات السوق: {str(e)}", "", ""

def ticker_name(pair):
    return PAIRS.get(pair, "")

HTML = r"""
<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LORD SYRIA BOT — GLOBAL MARKET</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:Arial;color:#fff;background:radial-gradient(circle at top,#183027,#080b0a 55%,#020303);padding:20px 10px}
.c{max-width:540px;margin:auto;padding:20px;border:1px solid #34433d;border-radius:24px;background:#0b100e}
.logo{text-align:center;font-size:28px;font-weight:bold;color:#f4d35e}.sub{text-align:center;color:#9aa7a2;margin:7px 0 20px}
.notice{padding:12px;border:1px solid #f4d35e;border-radius:12px;color:#f4d35e;margin-bottom:16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.pair{position:relative}.pair input{position:absolute;opacity:0}
.pair label{display:block;padding:12px;text-align:center;border:1px solid #34413d;border-radius:10px;background:#17201d;cursor:pointer}
.pair input:checked+label{background:#00e676;color:#06100b}
button{width:100%;padding:16px;margin-top:18px;border:0;border-radius:12px;font-size:19px;font-weight:bold;background:#f4d35e;cursor:pointer}
.result{margin-top:20px;padding:20px;text-align:center;border:2px solid #f4d35e;border-radius:15px}.buy{border-color:#00e676}.sell{border-color:#ff5252}
.sig{font-size:28px;font-weight:bold}.buy .sig{color:#00e676}.sell .sig{color:#ff5252}.meta{margin-top:12px;color:#b8c2be}
</style></head><body><div class="c">
<div class="logo">⫷ LORD SYRIA BOT ⫸</div><div class="sub">Global Market Data — تحليل الدقيقة الواحدة</div>
<div class="notice">بيانات السوق العالمي — التحليل للإشارات فقط ولا ينفذ صفقات تلقائياً.</div>
<form method="post">
<div class="grid">
{% for p in pairs %}<div class="pair"><input type="radio" name="pair" id="p{{loop.index}}" value="{{p}}" {% if p==selected %}checked{% endif %}><label for="p{{loop.index}}">{{p}}</label></div>{% endfor %}
</div><button>⚡ تحليل السوق</button></form>
{% if message %}<div class="result {{cls}}"><div class="sig">{{message}}</div>
{% if symbol %}<div class="meta">المصدر: {{symbol}} | آخر سعر: {{price}}</div>{% endif %}
{% if entry_time %}<div class="meta" style="font-size:22px;color:#f4d35e;font-weight:bold;margin-top:16px">⏰ وقت الدخول: {{entry_time}}</div>
<div class="meta" style="font-size:18px">⏳ مدة الصفقة: 1 دقيقة</div>{% endif %}
</div>{% endif %}
</div></body></html>
"""

@app.route("/", methods=["GET", "POST"])
def home():
    selected = request.form.get("pair", "EUR/USD")
    minutes = 1
    message = cls = symbol = ""
    price = ""
    if selected not in PAIRS:
        selected = "EUR/USD"
    entry_time = ""
    if request.method == "POST":
        cls, message, symbol, price = analyze(selected, 1)
        now = datetime.now()
        entry = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        entry_time = entry.strftime("%H:%M")
    response = app.make_response(
        render_template_string(
            HTML,
            pairs=PAIRS.keys(),
            selected=selected,
            minutes=minutes,
            timeframes=TIMEFRAMES,
            message=message,
            cls=cls,
            symbol=symbol,
            price=price,
            entry_time=entry_time,
        )
    )
    # Prevent the browser from showing an old cached page after code changes.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

if __name__ == "__main__":
    print("LORD SYRIA GLOBAL MARKET BOT — 1 MINUTE ONLY")
    print("Open: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
