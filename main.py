import os
import re
import time
import json
import logging
import threading
import requests
import schedule
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

from db_manager import (
    init_db,
    save_prediction,
    check_and_update_outcomes,
    format_performance_report
)

# ---------------------------------------------------------
# SISTEM VE ORTAM AYARLARI
# ---------------------------------------------------------
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

TR_TZ = timezone(timedelta(hours=3))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def get_current_tr_time():
    """Anlık Türkiye saati ve tarihini string olarak döner."""
    return datetime.now(TR_TZ).strftime("%d.%m.%Y %H:%M:%S")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# DeepSeek LLM Yapılandırması
llm = LLM(
    model="deepseek/deepseek-chat",
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# ---------------------------------------------------------
# RAW (HAM) FONKSİYONLAR & API ÇAĞRILARI
# ---------------------------------------------------------

def get_tradeable_volume_coins(min_volume_usd: float = 1_000_000) -> list:
    """Binance üzerindeki 24s hacmi 1M$+ olan TÜM trade edilebilir coinleri getirir."""
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            valid_pairs = [
                d['symbol'].replace('USDT', '') for d in data 
                if d['symbol'].endswith('USDT') 
                and float(d.get('quoteVolume', 0)) >= min_volume_usd
                and not any(x in d['symbol'] for x in ['UP', 'DOWN', 'BEAR', 'BULL', 'FDUSD', 'USDC', 'TUSD', 'EUR'])
            ]
            return valid_pairs
    except Exception as e:
        logging.error(f"Dinamik hacim listesi çekilemedi: {e}")
    return ["BTC", "ETH", "SOL", "NEAR", "PENDLE", "SUI"]

def get_dexscreener_trending_tokens() -> str:
    """DexScreener ücretsiz API'si üzerinden on-chain en son trend/boost almış tokenları çeker."""
    now_str = get_current_tr_time()
    try:
        url = "https://api.dexscreener.com/token-boosts/latest/v1"
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                lines = [f"🔥 **DexScreener On-Chain Trend Tokenlar ({now_str}):**\n"]
                for item in data[:10]:
                    chain = item.get("chainId", "N/A").upper()
                    token_addr = item.get("tokenAddress", "")
                    short_addr = f"{token_addr[:4]}...{token_addr[-4:]}" if len(token_addr) > 8 else token_addr
                    description = item.get("description", "Profil Detayı Yok")
                    url_link = item.get("url", "https://dexscreener.com")
                    lines.append(f"• **[{chain}]** `{short_addr}`\n  └ {description[:60]}... [İncele]({url_link})")
                return "\n".join(lines)
    except Exception as e:
        logging.error(f"DexScreener verisi çekilemedi: {e}")
        
    return "⚠️ DexScreener on-chain verisi şu an çekilemiyor."

def fetch_crypto_price_and_info_raw(symbol: str) -> str:
    """CEX ve DEX (DexScreener) borsalarından canlı token verilerini dinamik çeker."""
    clean_sym = symbol.strip().upper().replace("USDT", "").replace("USD", "")
    now_str = get_current_tr_time()

    # 1. Binance
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={clean_sym}USDT"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            data = res.json()
            price = float(data['lastPrice'])
            change = float(data['priceChangePercent'])
            vol = float(data['quoteVolume']) / 1_000_000
            return f"[{clean_sym}] (Binance) Fiyat: ${price:,.4f} | 24s Değişim: %{change:+.2f} | Hacim: ${vol:,.1f}M (Veri Saati: {now_str} TRT)"
    except Exception:
        pass

    # 2. DexScreener Fallback
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={clean_sym}"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code == 200:
            pairs = res.json().get("pairs", [])
            if pairs:
                p = pairs[0]
                price = float(p.get("priceUsd", 0))
                change = float(p.get("priceChange", {}).get("h24", 0))
                vol = float(p.get("volume", {}).get("h24", 0)) / 1_000_000
                chain = p.get("chainId", "").upper()
                dex = p.get("dexId", "").upper()
                return f"[{clean_sym}] (DexScreener-{chain}/{dex}) Fiyat: ${price:,.6f} | 24s Değişim: %{change:+.2f} | Hacim: ${vol:,.2f}M ({now_str} TRT)"
    except Exception:
        pass

    return f"'{clean_sym}' için CEX veya DEX borsalarında canlı veri bulunamadı."

def fetch_wallet_balance_raw(target_addr: str = "") -> str:
    """Cüzdan bakiyesini sorgulayan temel fonksiyon."""
    address = target_addr.strip() if target_addr.strip() else WALLET_ADDRESS
    if not address:
        return "⚠️ Sorgulanacak cüzdan adresi bulunamadı (.env dosyasına WALLET_ADDRESS ekleyin)."

    now_str = get_current_tr_time()
    short_addr = f"{address[:6]}...{address[-4:]}"

    rpc_url = "https://cloudflare-eth.com"
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1
    }

    try:
        res = requests.post(rpc_url, json=payload, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            result_hex = res.json().get("result", "0x0")
            wei_balance = int(result_hex, 16)
            eth_balance = wei_balance / 10**18
            
            eth_price = 0.0
            try:
                p_res = requests.get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=ETHUSDT", headers=HEADERS, timeout=4)
                if p_res.status_code == 200:
                    eth_price = float(p_res.json()["result"]["list"][0]["lastPrice"])
            except Exception:
                pass
            
            usd_val = eth_balance * eth_price if eth_price > 0 else 0.0
            return f"💳 **Cüzdan Bakiyesi ({short_addr}):** {eth_balance:.4f} ETH (~${usd_val:,.2f} USD) | ({now_str} TRT)"
    except Exception as e:
        logging.error(f"Cüzdan sorgu hatası: {e}")

    return f"'{short_addr}' cüzdan bakiyesi çekilemedi."

def get_spot_price(symbol: str) -> float:
    """Binance kamuya açık REST API'si üzerinden anlık USDT fiyatını çeker."""
    try:
        formatted_symbol = f"{symbol.upper()}USDT"
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={formatted_symbol}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return float(response.json()["price"])
    except Exception as e:
        logging.error(f"[{symbol}] Fiyat çekme hatası: {e}")
    return None

def parse_and_save_predictions(text: str):
    """LLM yanıtı içerisindeki JSON tahmin bloğunu ayıklar ve SQLite'a kaydeder."""
    try:
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            json_str = match.group(1).strip()
            predictions_data = json.loads(json_str)
            for item in predictions_data:
                save_prediction(
                    symbol=item["symbol"],
                    direction=item["direction"],
                    entry=float(item.get("entry_price", 0.0)),
                    target=float(item.get("target_price", 0.0)),
                    stop=float(item.get("stop_loss", 0.0)),
                    timeframe_hours=int(item.get("timeframe_hours", 24)),
                    confidence=float(item.get("confidence_score", 0.8)),
                    rationale=item.get("rationale", "")
                )
            logging.info("✅ Tahminler SQLite veritabanına kaydedildi.")
    except Exception as e:
        logging.error(f"⚠️ Tahmin verisi ayrıştırma hatası: {e}")

# ---------------------------------------------------------
# CREWAI TOOL SARMALAYICILARI
# ---------------------------------------------------------

@tool("Web3 EVM Cüzdan Bakiye Sorgula")
def get_wallet_balance(wallet_address: str = "") -> str:
    """EVM cüzdan adresinin anlık ETH bakiyesini ve USD karşılığını sorgular."""
    return fetch_wallet_balance_raw(wallet_address)

@tool("Piyasadaki Trend ve Öne Çıkan Projeleri Keşfet")
def discover_market_projects() -> str:
    """Piyasadaki en yüksek işlem hacmine sahip aktif projeleri canlı tarar."""
    now_str = get_current_tr_time()
    try:
        url = "https://api.mexc.com/api/v3/ticker/24hr"
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            data = res.json()
            usdt_pairs = [d for d in data if d['symbol'].endswith('USDT') and not any(x in d['symbol'] for x in ['3S', '3L', '5S', '5L'])]
            sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)[:25]
            
            lines = [f"🌐 **Piyasa Keşif Havuzu ({now_str} TRT):**"]
            for p in sorted_pairs[:15]:
                sym = p['symbol'].replace('USDT', '')
                price = float(p['lastPrice'])
                change = float(p['priceChangePercent'])
                vol = float(p['quoteVolume']) / 1_000_000
                lines.append(f"• {sym}: ${price:,.4f} | 24s Değişim: %{change:+.2f} | Hacim: ${vol:,.1f}M")
            
            return "\n".join(lines)
    except Exception as e:
        logging.error(f"Tarama hatası: {e}")
    return "Piyasa taranamadı."

@tool("DexScreener On-Chain Trend Token Sorgula")
def get_dexscreener_info() -> str:
    """Merkeziyetsiz borsalardaki (DexScreener) en son trend ve boost almış on-chain tokenları getirir."""
    return get_dexscreener_trending_tokens()

@tool("Herhangi bir Projenin/Tokenin Canlı Verilerini Çek")
def get_crypto_price_and_info(symbol: str) -> str:
    """CEX ve DEX (DexScreener) borsalarından canlı token verilerini dinamik çeker."""
    return fetch_crypto_price_and_info_raw(symbol)

# ---------------------------------------------------------
# TELEGRAM BİLDİRİM FONKSİYONU
# ---------------------------------------------------------

def send_telegram_report(message: str):
    """Telegram grubuna/chatine rapor gönderir."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram token veya Chat ID bulunamadı.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    clean_msg = re.sub(r"```json\s*.*?```", "", message, flags=re.DOTALL).strip()
    
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": clean_msg, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        logging.info("Telegram bildirimi gönderildi.")
    except requests.exceptions.HTTPError:
        payload["parse_mode"] = None
        try:
            res = requests.post(url, json=payload, timeout=10)
            res.raise_for_status()
            logging.info("Telegram bildirimi düz metin olarak gönderildi.")
        except Exception as e:
            logging.error(f"Telegram gönderme hatası: {e}")
    except Exception as e:
        logging.error(f"Telegram gönderme hatası: {e}")

# ---------------------------------------------------------
# SPESİFİK TEKLİ COİN VE ALARM ANALİZ MOTORU
# ---------------------------------------------------------

def run_single_coin_analysis(symbol: str, trigger_reason: str = "Kullanıcı İsteği"):
    """Girilen herhangi bir coin için anlık teknik analiz ve tahmin üretir."""
    clean_symbol = symbol.strip().upper().replace("USDT", "")
    current_time_str = get_current_tr_time()
    logging.info(f"Dinamik Tekli Coin Analizi Başlatılıyor: {clean_symbol} ({trigger_reason})")

    check_and_update_outcomes(get_spot_price)
    price_info = fetch_crypto_price_and_info_raw(clean_symbol)

    analyst = Agent(
        role="Kripto Teknik ve Momentum Analisti",
        goal=f"{clean_symbol} coini için canlı teknik seviyeler ve tahmin üretmek.",
        backstory=f"Sen uzman bir kripto tüccarısın. KESİN Saat: {current_time_str}.",
        tools=[get_crypto_price_and_info, get_dexscreener_info],
        llm=llm,
        verbose=True
    )

    task = Task(
        description=f"""
        VARLIK: {clean_symbol}
        TETİKLEME NEDENİ: {trigger_reason}
        CANLI BORSA VERİSİ: {price_info}
        SAAT: {current_time_str} TRT

        GÖREV:
        1. Rapor başlığı: "⚡ **{clean_symbol} Anlık Analiz Raporu** ({trigger_reason})"
        2. Fiyat momentumu, potansiyel destek/direnç seviyelerini değerlendir.
        3. RAPORUN EN ALTINA veritabanı kaydı için şu JSON bloğunu EKSİKSİZ ekle:

        ```json
        [
          {{
            "symbol": "{clean_symbol}",
            "direction": "BULLISH",
            "entry_price": 0.0,
            "target_price": 0.0,
            "stop_loss": 0.0,
            "timeframe_hours": 24,
            "confidence_score": 0.85,
            "rationale": "{trigger_reason} uyarısı üzerine üretilen teknik analiz."
          }}
        ]
        ```
        """,
        expected_output=f"{clean_symbol} için Türkçe teknik rapor ve JSON tahmin bloğu.",
        agent=analyst
    )

    crew = Crew(agents=[analyst], tasks=[task], process=Process.sequential)
    try:
        result = crew.kickoff()
        output_text = str(result)
        parse_and_save_predictions(output_text)
        send_telegram_report(output_text)
    except Exception as e:
        logging.error(f"Tekli coin analiz hatası: {e}")

def check_single_symbol_spike(symbol: str):
    """Tek bir coin için 15m mumundaki %5+ değişimi kontrol eder."""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=15m&limit=2"
        res = requests.get(url, timeout=4)
        if res.status_code == 200:
            data = res.json()
            if len(data) >= 2:
                current_candle = data[-1]
                open_price = float(current_candle[1])
                close_price = float(current_candle[4])
                pct_change = ((close_price - open_price) / open_price) * 100

                if abs(pct_change) >= 5.0:
                    direction_symbol = "🚀 YÜKSELİŞ" if pct_change > 0 else "📉 DÜŞÜŞ"
                    alert_msg = (
                        f"🚨 *ANİ HACİM/FİYAT PATLAMASI ALARMI!*\n\n"
                        f"🔥 *{symbol}* son 15 dakikada *%{pct_change:+.2f}* {direction_symbol} gösterdi!\n"
                        f"💰 Anlık Fiyat: ${close_price}\n\n"
                        f"🤖 *DeepSeek anlık analiz başlatılıyor...*"
                    )
                    send_telegram_report(alert_msg)
                    run_single_coin_analysis(symbol, trigger_reason=f"15m %{pct_change:+.2f} Spike")
    except Exception as e:
        logging.error(f"Spike kontrol hatası [{symbol}]: {e}")

def check_volume_price_spikes():
    """Hacmi 1M$+ olan TÜM altcoinleri (yaklaşık 180-220 adet) 1.5 saniyede paralel tarlar."""
    logging.info("⚡ Hacmi 1M$+ olan tüm altcoinler taranıyor...")
    dynamic_watchlist = get_tradeable_volume_coins(min_volume_usd=1_000_000)
    
    with ThreadPoolExecutor(max_workers=15) as executor:
        executor.map(check_single_symbol_spike, dynamic_watchlist)

# ---------------------------------------------------------
# GENEL OTONOM PORTFÖY RAPORU
# ---------------------------------------------------------

def run_agent_job():
    current_time_str = get_current_tr_time()
    logging.info(f"Otonom AI Ajan Genel Analiz Sürecini Başlatıyor ({current_time_str} TRT)")

    check_and_update_outcomes(get_spot_price)

    crypto_analyst = Agent(
        role="Otonom Portföy ve Piyasa Analisti",
        goal="Cüzdan bakiyesini kontrol etmek, piyasayı tarayıp fırsatları keşfetmek ve tahmin üretmek.",
        backstory=f"Sen portföy bilincine sahip otonom bir finansal ajansın. KESİN Saat: {current_time_str}.",
        tools=[get_wallet_balance, discover_market_projects, get_crypto_price_and_info, get_dexscreener_info],
        llm=llm,
        verbose=True
    )

    analysis_task = Task(
        description=f"""
        ŞU ANKİ KESİN TARİH VE SAAT: {current_time_str} TRT.

        GÖREV ADIMLARI:
        1. 'get_wallet_balance' ile cüzdandaki likiditeyi kontrol et.
        2. 'discover_market_projects' ve 'get_dexscreener_info' ile piyasayı ve on-chain trendleri tara.
        3. Taramadan 2 veya 3 projeyi KENDİN seçip 'get_crypto_price_and_info' ile canlı verilerini al.
        4. Raporun EN ÜSTÜNE şu başlığı ekle:
           "📊 **Otonom Portföy & Piyasa Raporu** ({current_time_str})"
        5. Cüzdan bakiyesi ve seçtiğin projeler için Türkçe Telegram raporu oluştur.
        6. RAPORUN EN ALTINA veritabanı için tahminleri AYNEN şu JSON formatında yaz:

        ```json
        [
          {{
            "symbol": "COIN_ADI",
            "direction": "BULLISH",
            "entry_price": 5.20,
            "target_price": 6.10,
            "stop_loss": 4.80,
            "timeframe_hours": 24,
            "confidence_score": 0.85,
            "rationale": "Gerekçe açıklaması."
          }}
        ]
        ```
        """,
        expected_output="Cüzdan bakiyesi, canlı piyasa analizi ve en altta JSON tahmin bloğu içeren rapor.",
        agent=crypto_analyst
    )

    crew = Crew(agents=[crypto_analyst], tasks=[analysis_task], process=Process.sequential)

    try:
        result = crew.kickoff()
        output_text = str(result)
        parse_and_save_predictions(output_text)
        send_telegram_report(output_text)
    except Exception as e:
        error_msg = f"❌ Ajan çalışma hatası: {e}"
        logging.error(error_msg)
        send_telegram_report(error_msg)

# ---------------------------------------------------------
# TELEGRAM BOT HANDLER'LARI VE BUTONLAR
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutunda butonlu kontrol panelini görüntüler."""
    keyboard = [
        [InlineKeyboardButton("📊 Performans", callback_data="btn_performance"), InlineKeyboardButton("💳 Bakiye", callback_data="btn_balance")],
        [InlineKeyboardButton("🔥 DexScreener On-Chain Trendler", callback_data="btn_dex")],
        [InlineKeyboardButton("🚀 Genel Analiz Başlat", callback_data="btn_run_agent")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        "🤖 *Kripto Araştırma Ajanı Kontrol Paneli*\n\n"
        "İstediğiniz **HERHANGİ BİR COIN** için anlık analiz başlatabilirsiniz:\n"
        "• `/analiz PEPE` -> PEPE için canlı analiz\n"
        "• `/analiz ARB` -> ARB için canlı analiz\n\n"
        "Diğer Komutlar:\n"
        "• `/bakiye` -> Cüzdan bakiyesini sorgular\n"
        "• `/performans` -> Tahmin başarı oranlarını gösterir\n"
        "• `/dex` -> DexScreener on-chain trend tokenları listeler"
    )
    
    if update.message:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")

async def performans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/performans komutu ile win-rate raporunu gönderir."""
    check_and_update_outcomes(get_spot_price)
    report_text = format_performance_report()
    await update.message.reply_text(report_text, parse_mode="Markdown")

async def bakiye_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bakiye komutu ile anlık cüzdan bakiyesini görüntüler."""
    balance_info = fetch_wallet_balance_raw()
    await update.message.reply_text(balance_info, parse_mode="Markdown")

async def dex_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dex komutu ile DexScreener on-chain trend tokenlarını listeler."""
    dex_info = get_dexscreener_trending_tokens()
    await update.message.reply_text(dex_info, parse_mode="Markdown")

async def analiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yazılan herhangi bir coin için tamamen dinamik anlık analiz başlatır."""
    if not context.args:
        await update.message.reply_text("⚠️ Lütfen analiz etmek istediğiniz coin sembolünü yazın.\nÖrnek: `/analiz PEPE` veya `/analiz AVAX`", parse_mode="Markdown")
        return

    symbol = context.args[0].upper()
    await update.message.reply_text(f"⏳ *{symbol}* için borsa verileri çekiliyor ve DeepSeek analizi başlatılıyor...", parse_mode="Markdown")
    threading.Thread(target=run_single_coin_analysis, args=(symbol, "Kullanıcı İstek Komutu"), daemon=True).start()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buton tıklamalarını işleyen yönlendirici (CallbackQueryHandler)."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_performance":
        check_and_update_outcomes(get_spot_price)
        report_text = format_performance_report()
        await query.message.reply_text(report_text, parse_mode="Markdown")

    elif query.data == "btn_balance":
        balance_info = fetch_wallet_balance_raw()
        await query.message.reply_text(balance_info, parse_mode="Markdown")

    elif query.data == "btn_dex":
        dex_info = get_dexscreener_trending_tokens()
        await query.message.reply_text(dex_info, parse_mode="Markdown")

    elif query.data == "btn_run_agent":
        await query.message.reply_text("⏳ *Genel otonom analiz başlatıldı...*", parse_mode="Markdown")
        threading.Thread(target=run_agent_job, daemon=True).start()

# ---------------------------------------------------------
# ZAMANLANMIŞ ARKA PLAN DÖNGÜSÜ
# ---------------------------------------------------------

def run_schedule_loop():
    """Arka planda periyodik raporları ve ani alarm taramasını yürütür."""
    schedule.every(4).hours.do(run_agent_job)
    schedule.every(15).minutes.do(check_volume_price_spikes)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ---------------------------------------------------------
# ANA ÇALIŞTIRMA NOKTASI (MAIN)
# ---------------------------------------------------------

def main():
    init_db()

    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN .env dosyasında bulunamadı!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Telegram Komut Dinleyicileri
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("performans", performans))
    app.add_handler(CommandHandler("bakiye", bakiye_command))
    app.add_handler(CommandHandler("dex", dex_command))
    app.add_handler(CommandHandler("analiz", analiz_command))
    
    # Buton Dinleyicisi
    app.add_handler(CallbackQueryHandler(button_callback))

    # Arka Plan Zamanlayıcı Thread'i
    threading.Thread(target=run_schedule_loop, daemon=True).start()

    logging.info("🚀 Kripto Bot Servisi (DexScreener & Hacim Filtreli Spike) Dinlemede...")
    app.run_polling()

if __name__ == "__main__":
    main()