import ccxt
import requests
import time
import os
import pandas as pd
from datetime import datetime, date

# ============================================================
# RADAR v3.0 — Final Production Version
# ============================================================

API_KEY        = os.environ.get('API_KEY')
SECRET_KEY     = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID        = os.environ.get('CHAT_ID')

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
STABLE_COINS     = {'USDC','BUSD','TUSD','USDP','FDUSD','DAI','FRAX'}

alerted_today = {}

# ============================================================
# FEAR & GREED
# ============================================================
def get_fear_greed():
    try:
        r = requests.get('https://api.alternative.me/fng/?limit=1', timeout=8)
        data = r.json()['data'][0]
        value = int(data['value'])
        label = data['value_classification']
        if value <= 25:   emoji = '😨'
        elif value <= 45: emoji = '😟'
        elif value <= 55: emoji = '😐'
        elif value <= 75: emoji = '😏'
        else:             emoji = '🤑'
        note = '← يعزز الإشارة' if value <= 45 else ('← كن حذراً' if value >= 76 else '')
        return {'value': value, 'label': label, 'emoji': emoji,
                'text': f"{emoji} الخوف والجشع: {value} [{label}] {note}"}
    except:
        return {'value': -1, 'label': 'N/A', 'emoji': '❓',
                'text': '❓ الخوف والجشع: غير متاح'}

# ============================================================
# BTC HEALTH FILTER
# ============================================================
def btc_is_healthy():
    try:
        ticker = binance.fetch_ticker('BTC/USDT')
        chg = ticker.get('percentage', 0) or 0
        if chg < -3.0:
            print(f"[BTC Filter] BTC at {chg:.2f}% — SLEEP MODE")
            return False
        return True
    except Exception as e:
        print(f"[BTC Filter Error] {e}")
        return True  # if error, don't block

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")

# ============================================================
# MARKET FILTER — Phase 1
# NEW: range -4% to +2%
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
            change_pct = ticker.get('percentage',  0) or 0

            # NEW range: -4% to +2%
            if volume_24h >= 5_000_000 and -4.0 <= change_pct <= 2.0:
                candidates.append({
                    'symbol':  symbol,
                    'volume':  volume_24h,
                    'change':  change_pct,
                    'price':   ticker.get('last', 0),
                    'negative': change_pct < 0
                })

        candidates.sort(key=lambda x: x['volume'], reverse=True)
        return candidates[:35]
    except Exception as e:
        print(f"[Market Filter Error] {e}")
        return []

# ============================================================
# INDICATORS
# ============================================================
def calc_rsi(closes, period=14):
    s     = pd.Series(closes)
    delta = s.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(com=period-1, adjust=False).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(com=period-1, adjust=False).mean()
    rs    = gain / loss
    return (100 - (100 / (1 + rs))).tolist()

def calc_macd(closes, fast=12, slow=26, signal=9):
    s           = pd.Series(closes)
    ema_fast    = s.ewm(span=fast,   adjust=False).mean()
    ema_slow    = s.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line.tolist(), signal_line.tolist(), histogram.tolist()

def find_swing_lows(data, lookback=2):
    swings = []
    for i in range(lookback, len(data) - lookback):
        if all(data[i] <= data[i-j] for j in range(1, lookback+1)) and \
           all(data[i] <= data[i+j] for j in range(1, lookback+1)):
            swings.append(i)
    return swings

def check_rsi_divergence(closes, rsi_vals):
    """
    Bullish divergence: price lower low + RSI higher low
    REQUIRES: first trough RSI < 35 (pre-oversold condition)
    """
    if len(closes) < 20 or len(rsi_vals) < 20:
        return False, False
    swings = find_swing_lows(closes, lookback=2)
    if len(swings) < 2:
        return False, False
    i1, i2      = swings[-2], swings[-1]
    price_lower = closes[i2] < closes[i1] * 1.005
    rsi_higher  = rsi_vals[i2] > rsi_vals[i1] + 1.5
    recent      = i2 >= len(closes) - 10
    # NEW: first trough must have had RSI < 35
    pre_oversold = rsi_vals[i1] < 35
    divergence   = price_lower and rsi_higher and recent
    return divergence, pre_oversold

def check_macd_cross(histogram, lookback=4):
    n = len(histogram)
    if n < lookback + 2:
        return False, 0
    for i in range(1, lookback + 1):
        idx = n - i
        if histogram[idx] >= 0 and histogram[idx-1] < 0:
            return True, i - 1
    return False, 0

def classify_rsi(val):
    if val < 30:   return 'oversold🔥', True
    if val <= 45:  return 'ideal✅',    True
    if val <= 55:  return 'ok🟡',       True
    return 'late⚠️', False

def calc_general_trend(closes, period=50):
    if len(closes) < period:
        return 'neutral'
    chg = (closes[-1] - closes[-period]) / closes[-period] * 100
    if chg >  8: return 'up'
    if chg < -8: return 'down'
    return 'neutral'

def calc_volume_ratio(volumes):
    if len(volumes) < 5:
        return 0.0
    prev   = sorted(volumes[-20:-1])
    median = prev[len(prev) // 2]
    return (volumes[-1] / median) if median > 0 else 0.0

# NEW: Structural SL from swing lows, max 2%
def calc_structural_sl(closes, lows, price):
    swing_lows = find_swing_lows(lows, lookback=2)
    # Find nearest swing low below price
    candidates = [lows[i] for i in swing_lows if lows[i] < price * 0.995]
    if candidates:
        structural_sl = max(candidates)  # nearest one below price
    else:
        structural_sl = price * 0.982   # fallback 1.8%

    sl_pct = abs((structural_sl - price) / price * 100)

    # MAX 2% rule
    if sl_pct > 2.0:
        return None, sl_pct  # signal rejected — SL too wide
    return structural_sl, sl_pct

def calc_tp(price, highs, closes):
    # Find nearest resistance (swing high above price)
    swing_highs = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
    res_candidates = [h for h in swing_highs if h > price * 1.005]
    if res_candidates:
        resistance = min(res_candidates)
    else:
        resistance = price * 1.025
    tp_dist = min(resistance - price, price * 0.035)
    tp = max(price + tp_dist, price * 1.02)
    return tp, resistance

# NEW: Bullish candle check (close > open by >= 0.3%)
def check_bullish_candle(klines):
    last = klines[-1]
    open_p  = float(last[1])
    close_p = float(last[4])
    if open_p == 0:
        return False, 0.0
    change = (close_p - open_p) / open_p * 100
    return change >= 0.3, round(change, 2)

def analyze(symbol, is_negative):
    try:
        ohlcv_4h = binance.fetch_ohlcv(symbol, '4h', limit=100)
        if len(ohlcv_4h) < 35:
            return None
        df4 = pd.DataFrame(ohlcv_4h, columns=['ts','open','high','low','close','volume'])
        closes_4h  = df4['close'].tolist()
        highs_4h   = df4['high'].tolist()
        lows_4h    = df4['low'].tolist()
        volumes_4h = df4['volume'].tolist()

        ohlcv_1h = binance.fetch_ohlcv(symbol, '1h', limit=60)
        df1 = pd.DataFrame(ohlcv_1h, columns=['ts','open','high','low','close','volume'])
        closes_1h = df1['close'].tolist()

        price = closes_4h[-1]

        # 4H Indicators
        rsi4_vals          = calc_rsi(closes_4h)
        ml4, sl4, hist4    = calc_macd(closes_4h)
        rsi4_last          = rsi4_vals[-1]
        rsi4_cls, rsi4_v   = classify_rsi(rsi4_last)
        div4, pre_oversold = check_rsi_divergence(closes_4h, rsi4_vals)
        cross4, cross4_age = check_macd_cross(hist4)
        trend4             = calc_general_trend(closes_4h)
        vol_ratio          = calc_volume_ratio(volumes_4h)
        bullish_candle, candle_chg = check_bullish_candle(ohlcv_4h)

        # 1H Indicators
        rsi1_vals       = calc_rsi(closes_1h)
        ml1, sl1, hist1 = calc_macd(closes_1h)
        rsi1_last       = rsi1_vals[-1]
        _, rsi1_v       = classify_rsi(rsi1_last)
        div1, _         = check_rsi_divergence(closes_1h, rsi1_vals)
        cross1, _       = check_macd_cross(hist1)
        conf_1h         = rsi1_v and (div1 or cross1)

        # Structural SL & TP
        sl, sl_pct = calc_structural_sl(closes_4h, lows_4h, price)
        if sl is None:
            return None  # SL too wide, reject
        tp, resistance = calc_tp(price, highs_4h, closes_4h)
        tp_pct = abs((tp - price) / price * 100)
        rr     = tp_pct / sl_pct if sl_pct > 0 else 0

        # NEW: For negative coins — extra requirements
        if is_negative:
            if vol_ratio < 2.0:
                return None  # negative coin needs volume spike >= 2x
            if not bullish_candle:
                return None  # negative coin needs green candle >= 0.3%

        # Core score (3 conditions)
        # Note: divergence only counts if pre_oversold
        div_valid  = div4 and pre_oversold
        core_score = sum([div_valid, cross4, rsi4_v])

        # Bonus
        bonus = sum([conf_1h, vol_ratio >= 1.5, trend4 == 'up', bullish_candle])

        # Volume classification
        if vol_ratio >= 2.5:   vol_label = '🐋🐋 دخول حيتان قوي'
        elif vol_ratio >= 1.5: vol_label = '🐋 دخول حيتان'
        else:                  vol_label = f'{vol_ratio}x عادي'

        return {
            'symbol':        symbol,
            'price':         price,
            'rsi4':          round(rsi4_last, 1),
            'rsi4_cls':      rsi4_cls,
            'rsi1':          round(rsi1_last, 1),
            'div4':          div_valid,
            'pre_oversold':  pre_oversold,
            'cross4':        cross4,
            'cross4_age':    cross4_age,
            'conf_1h':       conf_1h,
            'trend4':        trend4,
            'vol_ratio':     round(vol_ratio, 1),
            'vol_label':     vol_label,
            'bullish_candle':bullish_candle,
            'candle_chg':    candle_chg,
            'core_score':    core_score,
            'bonus':         bonus,
            'tp':            round(tp, 6),
            'sl':            round(sl, 6),
            'tp_pct':        round(tp_pct, 2),
            'sl_pct':        round(sl_pct, 2),
            'rr':            round(rr, 1),
            'resistance':    round(resistance, 6),
            'is_negative':   is_negative,
        }
    except Exception as e:
        print(f"[Analyze Error] {symbol}: {e}")
        return None

# ============================================================
# FORMAT MESSAGE
# ============================================================
def format_signal(data, signal_type, fg):
    trend_map = {'up':'📈 صاعد', 'down':'📉 هابط', 'neutral':'➡️ محايد'}
    icon  = '🟢' if signal_type == 'strong' else '🟡'
    label = 'إشارة قوية — يمكن الدخول' if signal_type == 'strong' else 'راقب — ادخل بحذر'

    # Negative coin warning
    neg_line = '⚡ _عملة في نطاق سالب — تأكد قبل الدخول_\n' if data['is_negative'] else ''

    msg = (
        f"{icon} *{label}*: `{data['symbol']}`\n"
        f"{neg_line}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 السعر: `{data['price']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 RSI 4H: `{data['rsi4']}` [{data['rsi4_cls']}]\n"
        f"📊 RSI 1H: `{data['rsi1']}`\n"
        f"📉 Divergence 4H: {'✅ نعم' if data['div4'] else '❌ لا'}"
        f"{' (RSI كان <35 🔥)' if data['pre_oversold'] else ''}\n"
        f"⚡ MACD Cross: {'✅ نعم (' + str(data['cross4_age']) + ' شمعات)' if data['cross4'] else '❌ لا'}\n"
        f"🔄 تأكيد 1H: {'✅ نعم' if data['conf_1h'] else '❌ لا'}\n"
        f"📈 الاتجاه العام: {trend_map.get(data['trend4'], data['trend4'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 الحجم: {data['vol_label']}\n"
        f"🕯️ شمعة خضراء: {'✅ +' + str(data['candle_chg']) + '%' if data['bullish_candle'] else '❌ لا'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{fg['text']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔴 مقاومة: `{data['resistance']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP: `{data['tp']}` (+{data['tp_pct']}%)\n"
        f"🛑 SL: `{data['sl']}` (-{data['sl_pct']}%) _[فني — لا تتجاوزه]_\n"
        f"⚖️ R:R: `1:{data['rr']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _للتحليل فقط — القرار لك_"
    )
    return msg

# ============================================================
# ANTI-DUPLICATE
# ============================================================
def already_alerted(symbol):
    return alerted_today.get(symbol) == str(date.today())

def mark_alerted(symbol):
    alerted_today[symbol] = str(date.today())

# ============================================================
# MAIN SCANNER
# ============================================================
def run_scanner(fg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning...")
    candidates = get_candidates()
    print(f"[Filter] {len(candidates)} candidates")

    strong_count = watch_count = 0

    for c in candidates:
        symbol      = c['symbol']
        is_negative = c['negative']

        if already_alerted(symbol):
            continue

        data = analyze(symbol, is_negative)
        if not data:
            continue

        core  = data['core_score']
        bonus = data['bonus']
        rr    = data['rr']
        trend = data['trend4']

        # STRONG: 3/3 core + not downtrend + R:R >= 1.5
        if core == 3 and trend != 'down' and rr >= 1.5:
            send_telegram(format_signal(data, 'strong', fg))
            mark_alerted(symbol)
            strong_count += 1
            print(f"  🟢 STRONG: {symbol} RSI:{data['rsi4']} RR:1:{rr}")

        # WATCH: 2/3 core + bonus >= 2 + R:R >= 1.3
        elif core == 2 and bonus >= 2 and rr >= 1.3:
            send_telegram(format_signal(data, 'watch', fg))
            mark_alerted(symbol)
            watch_count += 1
            print(f"  🟡 WATCH: {symbol} RSI:{data['rsi4']} RR:1:{rr}")

        time.sleep(0.3)

    total = strong_count + watch_count
    if total == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No signals.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {strong_count} strong + {watch_count} watch")

# ============================================================
# HEARTBEAT
# ============================================================
last_heartbeat_day = None

def send_heartbeat(fg):
    global last_heartbeat_day
    today = str(date.today())
    if last_heartbeat_day != today:
        send_telegram(
            f"💓 *رادار v3.0 — يعمل*\n"
            f"📅 {today}\n"
            f"🔄 يفحص كل 30 دقيقة\n"
            f"✅ Structural SL · BTC Filter · Volume Spike\n"
            f"{fg['text']}"
        )
        last_heartbeat_day = today

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    send_telegram(
        "🚀 *رادار v3.0 يعمل الآن*\n\n"
        "✅ Structural SL (max 2%)\n"
        "✅ BTC Filter (-3%)\n"
        "✅ RSI Pre-Oversold (<35)\n"
        "✅ نطاق -4% إلى +2%\n"
        "✅ Volume Spike شرطي للسالب\n"
        "✅ شمعة خضراء للسالب\n"
        "✅ Fear & Greed Index\n"
        "✅ Anti-duplicate · TP/SL فني"
    )
    while True:
        try:
            fg = get_fear_greed()
            send_heartbeat(fg)
            if btc_is_healthy():
                run_scanner(fg)
            else:
                send_telegram("😴 *وضع السبات* — BTC هابط أكثر من -3%\nسيستأنف الفحص في الدورة القادمة.")
        except Exception as e:
            print(f"[Main Error] {e}")
            time.sleep(60)
        time.sleep(1800)
