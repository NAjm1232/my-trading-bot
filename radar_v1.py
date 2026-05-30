import ccxt
import requests
import time
import os
import pandas as pd
from datetime import datetime, date

# ============================================================
# RADAR v3.1 — Smart Sleep + Fixed Notifications
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

# State tracking
alerted_today       = {}
sleep_mode_notified = False
btc_sleep_low       = None  # tracks BTC price when it entered sleep

# ============================================================
# FEAR & GREED
# ============================================================
def get_fear_greed():
    try:
        r    = requests.get('https://api.alternative.me/fng/?limit=1', timeout=8)
        data = r.json()['data'][0]
        val  = int(data['value'])
        lbl  = data['value_classification']
        if val <= 25:   emoji = '😨'
        elif val <= 45: emoji = '😟'
        elif val <= 55: emoji = '😐'
        elif val <= 75: emoji = '😏'
        else:           emoji = '🤑'
        note = '← يعزز الإشارة' if val <= 45 else ('← كن حذراً' if val >= 76 else '')
        return {'value': val, 'label': lbl, 'emoji': emoji,
                'text': f"{emoji} الخوف والجشع: {val} [{lbl}] {note}"}
    except:
        return {'value': -1, 'label': 'N/A', 'emoji': '❓',
                'text': '❓ الخوف والجشع: غير متاح'}

# ============================================================
# SMART BTC FILTER
# Returns: 'healthy' | 'light_sleep' | 'deep_sleep'
# ============================================================
def get_btc_status():
    global btc_sleep_low
    try:
        ticker  = binance.fetch_ticker('BTC/USDT')
        chg     = ticker.get('percentage', 0) or 0
        btc_price = ticker.get('last', 0) or 0

        if chg > -3.0:
            return 'healthy', chg, btc_price

        # BTC is down — track the low point
        if btc_sleep_low is None or btc_price < btc_sleep_low:
            btc_sleep_low = btc_price

        # Check if BTC recovered +1% from its low
        if btc_sleep_low and btc_price >= btc_sleep_low * 1.01:
            return 'recovering', chg, btc_price

        if chg <= -5.0:
            return 'deep_sleep', chg, btc_price
        return 'light_sleep', chg, btc_price

    except Exception as e:
        print(f"[BTC Status Error] {e}")
        return 'healthy', 0, 0

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
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
            change_pct = ticker.get('percentage',  0) or 0
            if volume_24h >= 5_000_000 and -4.0 <= change_pct <= 2.0:
                candidates.append({
                    'symbol':   symbol,
                    'volume':   volume_24h,
                    'change':   change_pct,
                    'price':    ticker.get('last', 0),
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
    if len(closes) < 20 or len(rsi_vals) < 20:
        return False, False
    swings = find_swing_lows(closes, lookback=2)
    if len(swings) < 2:
        return False, False
    i1, i2       = swings[-2], swings[-1]
    price_lower  = closes[i2] < closes[i1] * 1.005
    rsi_higher   = rsi_vals[i2] > rsi_vals[i1] + 1.5
    recent       = i2 >= len(closes) - 10
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

def calc_structural_sl(closes, lows, price):
    swing_lows = find_swing_lows(lows, lookback=2)
    candidates = [lows[i] for i in swing_lows if lows[i] < price * 0.995]
    structural_sl = max(candidates) if candidates else price * 0.982
    sl_pct = abs((structural_sl - price) / price * 100)
    if sl_pct > 2.0:
        return None, sl_pct
    return structural_sl, sl_pct

def calc_tp(price, highs):
    swing_highs = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
    res_candidates = [h for h in swing_highs if h > price * 1.005]
    resistance = min(res_candidates) if res_candidates else price * 1.025
    tp_dist    = min(resistance - price, price * 0.035)
    tp         = max(price + tp_dist, price * 1.02)
    return tp, resistance

def check_bullish_candle(klines):
    last    = klines[-1]
    open_p  = float(last[1])
    close_p = float(last[4])
    if open_p == 0:
        return False, 0.0
    chg = (close_p - open_p) / open_p * 100
    return chg >= 0.3, round(chg, 2)

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
        df1      = pd.DataFrame(ohlcv_1h, columns=['ts','open','high','low','close','volume'])
        closes_1h = df1['close'].tolist()

        price = closes_4h[-1]

        # 4H
        rsi4_vals          = calc_rsi(closes_4h)
        ml4, sl4, hist4    = calc_macd(closes_4h)
        rsi4_last          = rsi4_vals[-1]
        rsi4_cls, rsi4_v   = classify_rsi(rsi4_last)
        div4, pre_oversold = check_rsi_divergence(closes_4h, rsi4_vals)
        cross4, cross4_age = check_macd_cross(hist4)
        trend4             = calc_general_trend(closes_4h)
        vol_ratio          = calc_volume_ratio(volumes_4h)
        bullish_candle, candle_chg = check_bullish_candle(ohlcv_4h)

        # 1H
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
            return None
        tp, resistance = calc_tp(price, highs_4h)
        tp_pct = abs((tp - price) / price * 100)
        rr     = tp_pct / sl_pct if sl_pct > 0 else 0

        # Extra requirements for negative coins
        if is_negative:
            if vol_ratio < 2.0:
                return None
            if not bullish_candle:
                return None

        div_valid  = div4 and pre_oversold
        core_score = sum([div_valid, cross4, rsi4_v])
        bonus      = sum([conf_1h, vol_ratio >= 1.5, trend4 == 'up', bullish_candle])

        if vol_ratio >= 2.5:   vol_label = '🐋🐋 دخول حيتان قوي'
        elif vol_ratio >= 1.5: vol_label = f'🐋 دخول حيتان ({vol_ratio}x)'
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
    neg_line = '⚡ _عملة في نطاق سالب — تأكد قبل الدخول_\n' if data['is_negative'] else ''

    return (
        f"{icon} *{label}*: `{data['symbol']}`\n"
        f"{neg_line}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 السعر: `{data['price']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 RSI 4H: `{data['rsi4']}` [{data['rsi4_cls']}]\n"
        f"📊 RSI 1H: `{data['rsi1']}`\n"
        f"📉 Divergence: {'✅ نعم (RSI كان <35 🔥)' if data['div4'] else '❌ لا'}\n"
        f"⚡ MACD Cross: {'✅ نعم (' + str(data['cross4_age']) + ' شمعات)' if data['cross4'] else '❌ لا'}\n"
        f"🔄 تأكيد 1H: {'✅ نعم' if data['conf_1h'] else '❌ لا'}\n"
        f"📈 الاتجاه: {trend_map.get(data['trend4'], data['trend4'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 الحجم: {data['vol_label']}\n"
        f"🕯️ شمعة خضراء: {'✅ +' + str(data['candle_chg']) + '%' if data['bullish_candle'] else '❌ لا'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{fg['text']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔴 مقاومة: `{data['resistance']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP: `{data['tp']}` (+{data['tp_pct']}%)\n"
        f"🛑 SL: `{data['sl']}` (-{data['sl_pct']}%) _[فني]_\n"
        f"⚖️ R:R: `1:{data['rr']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _للتحليل فقط — القرار لك_"
    )

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
    candidates   = get_candidates()
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

        if core == 3 and trend != 'down' and rr >= 1.5:
            send_telegram(format_signal(data, 'strong', fg))
            mark_alerted(symbol)
            strong_count += 1
            print(f"  🟢 STRONG: {symbol}")

        elif core == 2 and bonus >= 2 and rr >= 1.3:
            send_telegram(format_signal(data, 'watch', fg))
            mark_alerted(symbol)
            watch_count += 1
            print(f"  🟡 WATCH: {symbol}")

        time.sleep(0.3)

    if strong_count + watch_count == 0:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] No signals.")

# ============================================================
# HEARTBEAT
# ============================================================
last_heartbeat_day = None

def send_heartbeat(fg):
    global last_heartbeat_day
    today = str(date.today())
    if last_heartbeat_day != today:
        send_telegram(
            f"💓 *رادار v3.1 — يعمل*\n"
            f"📅 {today}\n"
            f"🔄 كل 30 دقيقة\n"
            f"✅ Smart Sleep · Structural SL · BTC Filter\n"
            f"{fg['text']}"
        )
        last_heartbeat_day = today

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    send_telegram(
        "🚀 *رادار v3.1 يعمل الآن*\n\n"
        "✅ Structural SL (max 2%)\n"
        "✅ Smart BTC Sleep (خفيف/عميق)\n"
        "✅ صحيان تلقائي عند ارتداد BTC\n"
        "✅ RSI Pre-Oversold (<35)\n"
        "✅ نطاق -4% إلى +2%\n"
        "✅ Volume Spike شرطي\n"
        "✅ شمعة خضراء للسالب\n"
        "✅ Fear & Greed · Anti-duplicate"
    )

    while True:
        try:
            fg = get_fear_greed()
            send_heartbeat(fg)

            btc_status, btc_chg, btc_price = get_btc_status()

            if btc_status == 'healthy':
                # Reset sleep state
                if sleep_mode_notified:
                    send_telegram(
                        f"✅ *البوت استيقظ*\n"
                        f"BTC تعافى: {btc_chg:.2f}%\n"
                        f"استئناف الفحص الآن 🔍"
                    )
                    sleep_mode_notified = False
                    btc_sleep_low = None
                run_scanner(fg)

            elif btc_status == 'recovering':
                # BTC bounced +1% from low — wake up early
                if sleep_mode_notified:
                    send_telegram(
                        f"⚡ *استيقاظ مبكر*\n"
                        f"BTC ارتد من القاع +1%\n"
                        f"فحص فوري للفرص 🔍"
                    )
                    sleep_mode_notified = False
                    btc_sleep_low = None
                run_scanner(fg)

            elif btc_status == 'light_sleep':
                if not sleep_mode_notified:
                    send_telegram(
                        f"😴 *وضع السبات الخفيف*\n"
                        f"BTC: {btc_chg:.2f}%\n"
                        f"سأصحى تلقائياً عند ارتداد BTC"
                    )
                    sleep_mode_notified = True
                print(f"[Light Sleep] BTC {btc_chg:.2f}%")

            elif btc_status == 'deep_sleep':
                if not sleep_mode_notified:
                    send_telegram(
                        f"😴 *وضع السبات العميق*\n"
                        f"BTC: {btc_chg:.2f}% ⚠️\n"
                        f"السوق في هبوط حاد\n"
                        f"سأصحى عند تعافي BTC"
                    )
                    sleep_mode_notified = True
                print(f"[Deep Sleep] BTC {btc_chg:.2f}%")

        except Exception as e:
            print(f"[Main Error] {e}")
            time.sleep(60)

        time.sleep(1800)
