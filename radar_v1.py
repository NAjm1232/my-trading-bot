import ccxt
import requests
import time
import os
import pandas as pd
from datetime import datetime, date

# ============================================================
# RADAR v2.0 — Full trading scanner with improved indicators
# ============================================================

API_KEY       = os.environ.get('API_KEY')
SECRET_KEY    = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID       = os.environ.get('CHAT_ID')

binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# ============================================================
# BLACKLISTS
# ============================================================
MEME_COINS = {
    'TRUMP','DOGE','SHIB','PEPE','FLOKI','BONK','WIF','MEME',
    'NEIRO','BANANA','BABYDOGE','PNUT','GOAT','MOODENG','CHILLGUY',
    'ACT','PONKE','DOGS','LADYS','TURBO','BOME','SLERF','MYRO',
    'BRETT','POPCAT','SUNDOG','FWOG','CATI','HMSTR','MAJOR'
}
EXCLUDE_CONTAINS = ['UP/','DOWN/','BULL/','BEAR/','3L/','3S/']
STABLE_COINS = {'USDC','BUSD','TUSD','USDP','FDUSD','DAI','FRAX'}

# Anti-duplicate: track alerted coins per day
alerted_today = {}

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")

# ============================================================
# MARKET FILTER — Phase 1
# ============================================================
def get_candidates():
    candidates = []
    try:
        tickers = binance.fetch_tickers()
        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue
            base = symbol.replace('/USDT', '')
            if base in MEME_COINS or base in STABLE_COINS:
                continue
            if any(x in symbol for x in EXCLUDE_CONTAINS):
                continue

            volume_24h = ticker.get('quoteVolume', 0) or 0
            change_pct = ticker.get('percentage', 0) or 0

            if volume_24h >= 5_000_000 and 2.0 <= change_pct <= 5.0:
                candidates.append({
                    'symbol': symbol,
                    'volume': volume_24h,
                    'change': change_pct,
                    'price': ticker.get('last', 0)
                })

        # Sort by volume descending, take top 30
        candidates.sort(key=lambda x: x['volume'], reverse=True)
        return candidates[:30]

    except Exception as e:
        print(f"[Market Filter Error] {e}")
        return []

# ============================================================
# INDICATORS — Phase 2
# ============================================================
def calc_rsi(closes, period=14):
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0).ewm(com=period-1, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(com=period-1, adjust=False).mean()
    rs = gain / loss
    return (100 - (100 / (1 + rs))).tolist()

def calc_macd(closes, fast=12, slow=26, signal=9):
    s = pd.Series(closes)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line.tolist(), signal_line.tolist(), histogram.tolist()

def find_swing_lows(lows, lookback=3):
    """Find swing low indices — local minima"""
    swings = []
    for i in range(lookback, len(lows) - lookback):
        if all(lows[i] <= lows[i-j] for j in range(1, lookback+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, lookback+1)):
            swings.append(i)
    return swings

def check_rsi_divergence(closes, rsi_vals):
    """
    Bullish RSI Divergence:
    Price makes lower low but RSI makes higher low
    Uses swing points for accuracy
    """
    if len(closes) < 20 or len(rsi_vals) < 20:
        return False
    lows = closes  # use close prices for simplicity
    swings = find_swing_lows(lows, lookback=2)
    if len(swings) < 2:
        return False
    # Compare last two swing lows
    i1, i2 = swings[-2], swings[-1]
    price_lower = lows[i2] < lows[i1] * 1.005   # price lower low
    rsi_higher  = rsi_vals[i2] > rsi_vals[i1] + 1.5  # RSI higher low
    # Must be recent (last swing within last 10 candles)
    recent = i2 >= len(lows) - 10
    return price_lower and rsi_higher and recent

def check_macd_cross(histogram, lookback=4):
    """
    Bullish MACD cross: histogram was negative, turned positive
    Within last `lookback` candles
    """
    n = len(histogram)
    if n < lookback + 2:
        return False, 0
    for i in range(1, lookback + 1):
        idx = n - i
        if histogram[idx] >= 0 and histogram[idx-1] < 0:
            age = i - 1  # candles since cross
            return True, age
    return False, 0

def classify_rsi(val):
    if val < 30:   return 'oversold', True
    if val <= 45:  return 'ideal', True
    if val <= 55:  return 'ok', True
    return 'late', False

def calc_general_trend(closes, period=50):
    if len(closes) < period:
        return 'neutral'
    change = (closes[-1] - closes[-period]) / closes[-period] * 100
    if change > 8:   return 'up'
    if change < -8:  return 'down'
    return 'neutral'

def calc_volume_ratio(volumes):
    """Compare last candle volume to median of previous 19"""
    if len(volumes) < 5:
        return 0.0
    prev = sorted(volumes[-20:-1])
    median = prev[len(prev)//2]
    if median == 0:
        return 0.0
    return volumes[-1] / median

def calc_sr(highs, lows, closes):
    """Support/Resistance from swing points"""
    price = closes[-1]
    n = len(highs)
    swing_highs, swing_lows = [], []
    for i in range(2, n-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append(lows[i])
    res = min((h for h in swing_highs if h > price * 1.005), default=price * 1.025)
    sup = max((l for l in swing_lows if l < price * 0.995), default=price * 0.975)
    return res, sup

def analyze(symbol):
    try:
        # Fetch 4H candles
        ohlcv_4h = binance.fetch_ohlcv(symbol, '4h', limit=100)
        if len(ohlcv_4h) < 35:
            return None
        df4 = pd.DataFrame(ohlcv_4h, columns=['ts','open','high','low','close','volume'])

        closes_4h  = df4['close'].tolist()
        highs_4h   = df4['high'].tolist()
        lows_4h    = df4['low'].tolist()
        volumes_4h = df4['volume'].tolist()

        # Fetch 1H candles for confirmation
        ohlcv_1h = binance.fetch_ohlcv(symbol, '1h', limit=60)
        df1 = pd.DataFrame(ohlcv_1h, columns=['ts','open','high','low','close','volume'])
        closes_1h = df1['close'].tolist()

        # --- 4H Indicators ---
        rsi4_vals  = calc_rsi(closes_4h)
        ml4, sl4, hist4 = calc_macd(closes_4h)
        rsi4_last  = rsi4_vals[-1]
        rsi4_cls, rsi4_valid = classify_rsi(rsi4_last)
        div4       = check_rsi_divergence(closes_4h, rsi4_vals)
        cross4, cross4_age = check_macd_cross(hist4)
        trend4     = calc_general_trend(closes_4h)
        vol_ratio  = calc_volume_ratio(volumes_4h)
        res, sup   = calc_sr(highs_4h, lows_4h, closes_4h)

        # --- 1H Indicators (confirmation) ---
        rsi1_vals  = calc_rsi(closes_1h)
        ml1, sl1, hist1 = calc_macd(closes_1h)
        rsi1_last  = rsi1_vals[-1]
        _, rsi1_valid = classify_rsi(rsi1_last)
        div1       = check_rsi_divergence(closes_1h, rsi1_vals)
        cross1, _  = check_macd_cross(hist1)
        conf_1h    = rsi1_valid and (div1 or cross1)

        # --- CORE score (3 conditions) ---
        core_score = sum([div4, cross4, rsi4_valid])

        # --- BONUS score ---
        bonus = 0
        if conf_1h:    bonus += 1
        if vol_ratio >= 1.4: bonus += 1
        if trend4 == 'up': bonus += 1

        # --- TP / SL (realistic) ---
        price = closes_4h[-1]
        sl_dist = min(price - sup, price * 0.015)
        sl = max(price - sl_dist, price * 0.984)
        tp_dist = min(res - price, price * 0.035)
        tp_raw = price + tp_dist
        tp = max(tp_raw, price * 1.02)
        sl_pct = abs((sl - price) / price * 100)
        tp_pct = abs((tp - price) / price * 100)
        rr = tp_pct / sl_pct if sl_pct > 0 else 0

        return {
            'symbol':      symbol,
            'price':       price,
            'rsi4':        round(rsi4_last, 1),
            'rsi4_cls':    rsi4_cls,
            'rsi1':        round(rsi1_last, 1),
            'div4':        div4,
            'cross4':      cross4,
            'cross4_age':  cross4_age,
            'conf_1h':     conf_1h,
            'trend4':      trend4,
            'vol_ratio':   round(vol_ratio, 1),
            'core_score':  core_score,
            'bonus':       bonus,
            'tp':          round(tp, 6),
            'sl':          round(sl, 6),
            'tp_pct':      round(tp_pct, 2),
            'sl_pct':      round(sl_pct, 2),
            'rr':          round(rr, 1),
            'res':         round(res, 6),
            'sup':         round(sup, 6),
        }
    except Exception as e:
        print(f"[Analyze Error] {symbol}: {e}")
        return None

# ============================================================
# FORMAT TELEGRAM MESSAGE
# ============================================================
def format_signal(data, signal_type):
    rsi_emoji = {'oversold':'🔥','ideal':'✅','ok':'🟡','late':'⚠️'}
    trend_emoji = {'up':'📈','down':'📉','neutral':'➡️'}
    icon = '🟢' if signal_type == 'strong' else '🟡'
    label = 'إشارة قوية — يمكن الدخول' if signal_type == 'strong' else 'راقب هذه العملة'

    msg = (
        f"{icon} *{label}*: `{data['symbol']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 السعر: `{data['price']}`\n"
        f"📊 RSI 4H: `{data['rsi4']}` [{data['rsi4_cls']}] {rsi_emoji.get(data['rsi4_cls'],'')}\n"
        f"📊 RSI 1H: `{data['rsi1']}`\n"
        f"⚡ MACD Cross: {'نعم (' + str(data['cross4_age']) + ' شمعات)' if data['cross4'] else 'لا'}\n"
        f"📉 Divergence 4H: {'نعم ✅' if data['div4'] else 'لا'}\n"
        f"🔄 تأكيد 1H: {'نعم ✅' if data['conf_1h'] else 'لا'}\n"
        f"📈 الاتجاه العام: {data['trend4']} {trend_emoji.get(data['trend4'],'')}\n"
        f"📦 حجم الشمعة: {data['vol_ratio']}x\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔴 مقاومة: `{data['res']}`\n"
        f"🟢 دعم: `{data['sup']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP: `{data['tp']}` (+{data['tp_pct']}%)\n"
        f"🛑 SL: `{data['sl']}` (-{data['sl_pct']}%)\n"
        f"⚖️ R:R: `1:{data['rr']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _للتحليل فقط — القرار لك_"
    )
    return msg

# ============================================================
# ANTI-DUPLICATE
# ============================================================
def already_alerted(symbol):
    today = str(date.today())
    if alerted_today.get(symbol) == today:
        return True
    return False

def mark_alerted(symbol):
    alerted_today[symbol] = str(date.today())

# ============================================================
# MAIN SCANNER
# ============================================================
def run_scanner():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting scan...")
    candidates = get_candidates()
    print(f"[Filter] {len(candidates)} candidates after volume/change filter")

    strong_count = 0
    watch_count  = 0

    for c in candidates:
        symbol = c['symbol']
        if already_alerted(symbol):
            continue

        data = analyze(symbol)
        if not data:
            continue

        core  = data['core_score']
        bonus = data['bonus']
        rr    = data['rr']
        trend = data['trend4']

        # STRONG signal: 3/3 core + not downtrend + R:R >= 1.5
        if core == 3 and trend != 'down' and rr >= 1.5:
            msg = format_signal(data, 'strong')
            send_telegram(msg)
            mark_alerted(symbol)
            strong_count += 1
            print(f"  🟢 STRONG: {symbol} RSI:{data['rsi4']} RR:1:{rr}")

        # WATCH signal: 2/3 core + bonus >= 1 + R:R >= 1.3
        elif core == 2 and bonus >= 1 and rr >= 1.3:
            msg = format_signal(data, 'watch')
            send_telegram(msg)
            mark_alerted(symbol)
            watch_count += 1
            print(f"  🟡 WATCH: {symbol} RSI:{data['rsi4']} RR:1:{rr}")

        time.sleep(0.3)  # small delay between API calls

    total = strong_count + watch_count
    if total == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No signals found.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent {strong_count} strong + {watch_count} watch signals.")

# ============================================================
# HEARTBEAT — once daily at startup summary
# ============================================================
last_heartbeat_day = None

def send_heartbeat():
    global last_heartbeat_day
    today = str(date.today())
    if last_heartbeat_day != today:
        send_telegram(
            f"💓 *رادار التداول — يعمل بكفاءة*\n"
            f"📅 {today}\n"
            f"🔄 يفحص كل 30 دقيقة\n"
            f"⚡ الشروط: RSI + MACD + Divergence + 1H"
        )
        last_heartbeat_day = today

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    send_telegram("🚀 *رادار v2.0 يعمل الآن*\n✅ RSI محسّن\n✅ Divergence دقيق\n✅ إشارتين: قوية + راقب\n✅ Anti-duplicate\n✅ TP/SL تلقائي")
    while True:
        try:
            send_heartbeat()
            run_scanner()
        except Exception as e:
            print(f"[Main Error] {e}")
            time.sleep(60)
        time.sleep(1800)  # 30 minutes
