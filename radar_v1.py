import ccxt
import requests
import time
import os
import pandas as pd

# 1. إعداد المتغيرات
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"خطأ في إرسال تليجرام: {e}")

def get_market_data_24h():
    filtered_symbols = []
    try:
        tickers = binance.fetch_tickers()
        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'): continue
            blacklist = ['UP/', 'DOWN/', 'BULL/', 'BEAR/', 'DOGE/', 'SHIB/', 'PEPE/', 'BONK/', 'FLOKI/', 'WIF/']
            if any(word in symbol for word in blacklist): continue
                
            volume_24h = ticker.get('quoteVolume', 0)
            percentage = ticker.get('percentage', 0)
            
            if volume_24h > 5000000 and (2.0 <= percentage <= 5.0):
                filtered_symbols.append(symbol)
    except Exception as e:
        print(f"خطأ في جلب بيانات السوق: {e}")
    return filtered_symbols

def analyze_indicators(symbol, timeframe='4h'):
    try:
        ohlcv = binance.fetch_ohlcv(symbol, timeframe, limit=50)
        if len(ohlcv) < 50: return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # حساب المؤشرات يدوياً
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        last_rsi = df['RSI'].iloc[-1]
        divergence = (df['close'].iloc[-1] < df['close'].iloc[-10]) and (df['RSI'].iloc[-1] > df['RSI'].iloc[-10])
        macd_cross = (df['MACD'].iloc[-1] > df['Signal'].iloc[-1]) and (df['MACD'].iloc[-2] <= df['Signal'].iloc[-2])

        return {
            'rsi_val': round(last_rsi, 2),
            'macd_cross': macd_cross,
            'divergence': divergence,
            'rsi_valid': last_rsi < 55,
            'last_price': df['close'].iloc[-1]
        }
    except Exception:
        return None

def run_scanner():
    # إشعار بداية الفحص
    send_telegram_message("🔍 جاري فحص السوق الآن للبحث عن فرص جديدة...")
    
    candidates = get_market_data_24h()
    found_count = 0
    for symbol in candidates:
        analysis = analyze_indicators(symbol)
        if analysis and analysis['divergence'] and analysis['macd_cross'] and analysis['rsi_valid']:
            msg = (f"🚨 **إشارة رادار:** `{symbol}`\n💰 السعر: `{analysis['last_price']}`\n📈 RSI: {analysis['rsi_val']}\n📊 تقاطع MACD: نعم\n📉 دايفرجنس: نعم")
            send_telegram_message(msg)
            found_count += 1
            
    if found_count == 0:
        print("تم المسح: لا توجد فرص تطابق الشروط حالياً.")

if __name__ == "__main__":
    send_telegram_message("🚀 الرادار يعمل الآن بذكاء الاصطناعي!")
    while True:
        run_scanner()
        time.sleep(900)
