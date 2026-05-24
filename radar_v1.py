import ccxt
import requests
import time
import os
import pandas as pd
import pandas_ta as ta

# 1. جلب المتغيرات البيئية من السيرفر
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

# إعداد منصة بينانس مع توجيه الطلبات لخادم بديل لتفادي الحظر الجغرافي الأمريكي
binance = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'},
    'urls': {
        'api': {
            'public': 'https://api3.binance.com/api/v3',
            'private': 'https://api3.binance.com/api/v3',
        }
    }
})

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"خطأ في إرسال تليجرام: {e}")

def get_market_data_24h():
    """الفلترة الأولية: سيولة > 5M، وتغير بين +2% و +5%، واستبعاد العملات الممنوعة"""
    filtered_symbols = []
    try:
        tickers = binance.fetch_tickers()
        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue
            
            # استبعاد الرموز التابعة للرافعات المالية والميمز الشهيرة
            blacklist = ['UP/', 'DOWN/', 'BULL/', 'BEAR/', 'DOGE/', 'SHIB/', 'PEPE/', 'BONK/', 'FLOKI/', 'WIF/']
            if any(word in symbol for word in blacklist):
                continue
                
            volume_24h = ticker.get('quoteVolume', 0) # حجم التداول بالـ USDT
            percentage = ticker.get('percentage', 0)   # نسبة التغيير
            
            if volume_24h > 5000000 and (2.0 <= percentage <= 5.0):
                filtered_symbols.append(symbol)
    except Exception as e:
        print(f"خطأ في جلب بيانات 24 ساعة: {e}")
    return filtered_symbols

def analyze_indicators(symbol, timeframe='4h'):
    """حساب المؤشرات وفحص شروط الدخول (RSI Levels, MACD, Divergence)"""
    try:
        ohlcv = binance.fetch_ohlcv(symbol, timeframe, limit=100)
        if len(ohlcv) < 50:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # حساب المؤشرات الفنية باستخدام pandas_ta
        df['RSI'] = ta.rsi(df['close'], length=14)
        macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
        
        if macd_df is None or df['RSI'].isna().iloc[-1]:
            return None
            
        df = pd.concat([df, macd_df], axis=1)
        
        # تسمية أعمدة الماكد تلقائياً
        macd_col = 'MACD_12_26_9'
        signal_col = 'MACDs_12_26_9'
        
        # 1. فحص تقاطع الماكد الإيجابي (حديث: أقل من 3 شمعات)
        macd_cross = False
        for i in range(-1, -4, -1):
            if df[macd_col].iloc[i] > df[signal_col].iloc[i] and df[macd_col].iloc[i-1] <= df[signal_col].iloc[i-1]:
                macd_cross = True
                break

        # 2. فحص مستوى RSI الأخير
        last_rsi = df['RSI'].iloc[-1]
        rsi_status = "❌ لا تدخل"
        rsi_valid = False
        if last_rsi < 30:
            rsi_status = "🔥 تشبع بيعي (فرصة قوية)"
            rsi_valid = True
        elif 30 <= last_rsi <= 45:
            rsi_status = "✅ مثالي"
            rsi_valid = True
        elif 45 <= last_rsi <= 55:
            rsi_status = "🟡 مقبول"
            rsi_valid = True

        # 3. فحص الدايفرجنس
        divergence = False
        if df['close'].iloc[-1] < df['close'].iloc[-10] and df['RSI'].iloc[-1] > df['RSI'].iloc[-10]:
            divergence = True

        return {
            'rsi_val': round(last_rsi, 2),
            'rsi_status': rsi_status,
            'macd_cross': macd_cross,
            'divergence': divergence,
            'rsi_valid': rsi_valid,
            'last_price': df['close'].iloc[-1]
        }
    except Exception as e:
        print(f"خطأ في تحليل العملة {symbol}: {e}")
        return None

def run_scanner():
    print("🔄 بدء المسح عبر الخادم البديل وتطبيق الفلترة...")
    candidates = get_market_data_24h()
    print(f"🔍 العملات المطابقة للفلترة الأولية (سيولة وتغيير): {len(candidates)} عملة.")
    
    for symbol in candidates:
        analysis = analyze_indicators(symbol, timeframe='4h')
        if not analysis:
            continue
            
        conditions_met = 0
        if analysis['divergence']: conditions_met += 1
        if analysis['macd_cross']: conditions_met += 1
        if analysis['rsi_valid']: conditions_met += 1
        
        if conditions_met == 3:
            msg = (
                f"🚨 **إشارة رادار جديدة (تحقق الشروط 3/3)** 🚨\n\n"
                f"🪙 **العملة:** `{symbol}`\n"
                f"💰 **السعر الحالي:** `{analysis['last_price']}`\n"
                f"📈 **حالة الـ RSI:** {analysis['rsi_status']} ({analysis['rsi_val']})\n"
                f"📊 **تقاطع MACD:** ✅ إيجابي\n"
                f"📉 **الإنحراف (Divergence):** ✅ إيجابي متكون على 4H\n\n"
                f"🎯 هدف جني الأرباح (TP): +2% إلى +3%\n"
                f"🛑 وقف الخسارة (SL): -1% إلى -1.5%"
            )
            send_telegram_message(msg)
            
    print("✅ تم انتهاء المسح الحالي بنجاح.")

if __name__ == "__main__":
    send_telegram_message("🚀 تم تحديث نظام الرادار وتوجيه المسار لتجاوز القيود الجغرافية!")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"حدث خطأ في الحلقة الرئيسية: {e}")
        time.sleep(900)
