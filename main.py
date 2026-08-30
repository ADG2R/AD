import os
import time
import requests
import schedule
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool
# main.py dosyasının en üstüne ekleyin
from db_manager import init_db, save_prediction, check_and_update_outcomes

# Bot başladığında veritabanını ilklendirin
init_db()

# 1. DeepSeek çıktısından gelen verileri kaydetme örneği:
# DeepSeek'ten dönen JSON veya ayrıştırılmış veriyi veritabanına yazın:
prediction_id = save_prediction(
    symbol="PENDLE",
    direction="BULLISH",
    entry=5.20,
    target=6.10,
    stop=4.80,
    timeframe_hours=24,
    confidence=0.85,
    rationale="TVL artışı ve RSI pozitif uyumsuzluk."
)

# 2. Periyodik olarak süresi dolan tahminleri doğrulama örneği:
# (get_spot_price sizin borsadan anlık fiyat çeken fonksiyonunuz olmalıdır)
check_and_update_outcomes(get_spot_price)
# ---------------------------------------------------------
# DISK DOLMASINI VE VERITABANI KILITLENMESINI ENGELLEYEN AYAR
# ---------------------------------------------------------
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"

# .env ortam değişkenlerini yükle
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

# Türkiye Saat Dilimi (UTC+3)
TR_TZ = timezone(timedelta(hours=3))

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
# DİNAMİK CANLI BİLGİ VE WEB3 CÜZDAN ARAÇLARI (TOOLS)
# ---------------------------------------------------------

@tool("Web3 EVM Cüzdan Bakiye Sorgula")
def get_wallet_balance(wallet_address: str = "") -> str:
    """
    EVM cüzdan adresinin anlık ETH bakiyesini ve USD karşılığını sorgular.
    Parametre verilmezse .env dosyasında tanımlı WALLET_ADDRESS değerini kullanır.
    """
    target_address = wallet_address.strip() if wallet_address.strip() else WALLET_ADDRESS
    if not target_address:
        return "⚠️ Sorgulanacak cüzdan adresi bulunamadı (.env dosyasına WALLET_ADDRESS ekleyin)."

    now_str = get_current_tr_time()
    short_addr = f"{target_address[:6]}...{target_address[-4:]}"
    print(f"🔍 [DEBUG - {now_str}] '{short_addr}' cüzdan bakiyesi sorgulanıyor...")

    rpc_url = "https://cloudflare-eth.com"
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [target_address, "latest"],
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
            msg = f"💳 **Cüzdan Bakiyesi ({short_addr}):** {eth_balance:.4f} ETH (~${usd_val:,.2f} USD) | (Saati: {now_str} TRT)"
            print(f"✅ [DEBUG] Cüzdan Yanıtı: {msg}")
            return msg
    except Exception as e:
        print(f"❌ [DEBUG] Cüzdan sorgu hatası: {e}")

    return f"'{short_addr}' cüzdan bakiyesi çekilemedi."


@tool("Piyasadaki Trend ve Öne Çıkan Projeleri Keşfet")
def discover_market_projects() -> str:
    """Piyasadaki en yüksek işlem hacmine sahip aktif projeleri canlı tarar."""
    now_str = get_current_tr_time()
    print(f"🔍 [DEBUG - {now_str}] Piyasa projeleri taranıyor...")
    
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
            
            res_str = "\n".join(lines)
            print(f"✅ [DEBUG] Piyasa taraması başarılı ({len(sorted_pairs[:15])} proje bulundu).")
            return res_str
    except Exception as e:
        print(f"❌ [DEBUG] Tarama hatası: {e}")

    return "Piyasa taranamadı."


@tool("Herhangi bir Projenin/Tokenin Canlı Verilerini Çek")
def get_crypto_price_and_info(symbol: str) -> str:
    """Ajanın keşfettiği HERHANGİ bir kripto projesinin canlı borsa verilerini çekmesini sağlar."""
    clean_sym = symbol.strip().upper().replace("USDT", "").replace("USD", "")
    now_str = get_current_tr_time()
    print(f"🔍 [DEBUG - {now_str}] '{clean_sym}' için borsa sorgusu atılıyor...")

    # 1. Bybit V5
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={clean_sym}USDT"
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            tickers = res.json().get("result", {}).get("list", [])
            if tickers:
                t = tickers[0]
                price = float(t['lastPrice'])
                change = float(t['price24hPcnt']) * 100
                vol = float(t['turnover24h']) / 1_000_000
                msg = f"[{clean_sym}] Fiyat: ${price:,.4f} | 24s Değişim: %{change:+.2f} | Hacim: ${vol:,.1f}M (Veri Saati: {now_str} TRT)"
                print(f"✅ [DEBUG] Bybit Yanıtı: {msg}")
                return msg
    except Exception:
        pass

    # 2. MEXC Yedek
    try:
        url = f"https://api.mexc.com/api/v3/ticker/24hr?symbol={clean_sym}USDT"
        res = requests.get(url, headers=HEADERS, timeout=6)
        if res.status_code == 200 and "lastPrice" in res.json():
            data = res.json()
            price = float(data['lastPrice'])
            change = float(data['priceChangePercent'])
            vol = float(data['quoteVolume']) / 1_000_000
            msg = f"[{clean_sym}] (MEXC) Fiyat: ${price:,.4f} | 24s Değişim: %{change:+.2f} | Hacim: ${vol:,.1f}M (Veri Saati: {now_str} TRT)"
            print(f"✅ [DEBUG] MEXC Yanıtı: {msg}")
            return msg
    except Exception:
        pass

    return f"'{clean_sym}' projesi için borsa verisi bulunamadı."

# ---------------------------------------------------------
# TELEGRAM BİLDİRİM FONKSİYONU
# ---------------------------------------------------------

def send_telegram_report(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ [DEBUG] Telegram token veya Chat ID bulunamadı.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        print("✅ [DEBUG] Telegram bildirimi başarıyla gönderildi.")
    except Exception as e:
        print(f"❌ [DEBUG] Telegram gönderme hatası: {e}")

# ---------------------------------------------------------
# CREWAI AJAN VE GÖREV TANIMLARI
# ---------------------------------------------------------

def run_agent_job():
    current_time_str = get_current_tr_time()
    print(f"\n🚀 [DEBUG] Otonom AI Ajan Analiz Sürecini Başlatıyor ({current_time_str} TRT)")

    crypto_analyst = Agent(
        role="Otonom Portföy ve Piyasa Analisti",
        goal="Cüzdan bakiyesini kontrol etmek, piyasayı tarayıp fırsatları bağımsız keşfetmek ve bakiyeye uygun rasyonel analiz sunmak.",
        backstory=f"""Sen portföy bilincine sahip otonom bir finansal ajansın.
        Şu anki KESİN Türkiye Saati: {current_time_str}.
        Her analizin başında mutlaka 'get_wallet_balance' aracını kullanarak cüzdan bakiyesini ve USD değerini kontrol edersin.
        Ardından 'discover_market_projects' ve 'get_crypto_price_and_info' araçlarıyla piyasa fırsatlarını incelersin.
        Asla geçmiş hafızandaki uydurma verileri veya tarihleri kullanmazsın.""",
        tools=[get_wallet_balance, discover_market_projects, get_crypto_price_and_info],
        llm=llm,
        verbose=True
    )

    analysis_task = Task(
        description=f"""
        ŞU ANKİ KESİN TARİH VE SAAT: {current_time_str} TRT.

        GÖREV ADIMLARI:
        1. 'get_wallet_balance' aracını çalıştırarak cüzdandaki likiditeyi kontrol et.
        2. 'discover_market_projects' aracını çağırarak piyasadaki trendleri tara.
        3. Taramadan 2 veya 3 projeyi KENDİN seçip 'get_crypto_price_and_info' ile canlı verilerini al.
        4. Raporun EN ÜSTÜNE şu başlığı ekle:
           "📊 **Otonom Portföy & Piyasa Raporu** ({current_time_str})"
        5. Cüzdan bakiyeni de rapora dahil ederek, seçtiğin projeler için Türkçe Telegram raporu oluştur.
        """,
        expected_output="Cüzdan bakiyesi ve canlı piyasa keşfine dayalı Türkçe Telegram kripto raporu.",
        agent=crypto_analyst
    )

    crew = Crew(
        agents=[crypto_analyst],
        tasks=[analysis_task],
        process=Process.sequential
    )

    try:
        result = crew.kickoff()
        send_telegram_report(str(result))
    except Exception as e:
        error_msg = f"❌ Ajan çalışma hatası: {e}"
        print(error_msg)
        send_telegram_report(error_msg)

if __name__ == "__main__":
    run_agent_job()
    schedule.every(4).hours.do(run_agent_job)
    print("🚀 Kripto Bot Servisi 7/24 Aktif Konumda Çalışıyor...")
    while True:
        schedule.run_pending()
        time.sleep(30)