import sqlite3
from datetime import datetime, timedelta

DB_PATH = "predictions.db"

def init_db():
    """Veritabanı tablolarını ve indeksleri ilklendirir."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                predicted_direction TEXT CHECK(predicted_direction IN ('BULLISH', 'BEARISH', 'NEUTRAL')) NOT NULL,
                entry_price REAL NOT NULL,
                target_price REAL,
                stop_loss REAL,
                timeframe_hours INTEGER NOT NULL,
                confidence_score REAL CHECK(confidence_score BETWEEN 0.0 AND 1.0),
                rationale TEXT,
                status TEXT CHECK(status IN ('PENDING', 'COMPLETED', 'EXPIRED')) DEFAULT 'PENDING',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                target_date DATETIME NOT NULL
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER UNIQUE NOT NULL,
                actual_price REAL NOT NULL,
                price_change_pct REAL NOT NULL,
                is_hit BOOLEAN NOT NULL,
                evaluated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (prediction_id) REFERENCES predictions(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
    finally:
        conn.close()

def save_prediction(symbol: str, direction: str, entry: float, target: float, stop: float, timeframe_hours: int, confidence: float, rationale: str) -> int:
    """Ajanın ürettiği yeni tahmini kaydeder."""
    target_date = datetime.utcnow() + timedelta(hours=timeframe_hours)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO predictions 
            (symbol, predicted_direction, entry_price, target_price, stop_loss, timeframe_hours, confidence_score, rationale, target_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, direction.upper(), entry, target, stop, timeframe_hours, confidence, rationale, target_date))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()

def check_and_update_outcomes(get_current_price_func):
    """Süresi dolmuş tahminleri güncel piyasa fiyatı ile karşılaştırıp doğrular."""
    now = datetime.utcnow()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, symbol, predicted_direction, entry_price, target_price 
            FROM predictions 
            WHERE status = 'PENDING' AND target_date <= ?
        """, (now,))
        pending_predictions = cursor.fetchall()

        for pred_id, symbol, direction, entry_price, target_price in pending_predictions:
            actual_price = get_current_price_func(symbol)
            if not actual_price:
                continue

            if direction == 'BULLISH':
                is_hit = actual_price >= target_price if target_price else actual_price > entry_price
            elif direction == 'BEARISH':
                is_hit = actual_price <= target_price if target_price else actual_price < entry_price
            else:
                is_hit = False

            price_change_pct = ((actual_price - entry_price) / entry_price) * 100

            cursor.execute("""
                INSERT INTO outcomes (prediction_id, actual_price, price_change_pct, is_hit)
                VALUES (?, ?, ?, ?)
            """, (pred_id, actual_price, price_change_pct, is_hit))

            cursor.execute("UPDATE predictions SET status = 'COMPLETED' WHERE id = ?", (pred_id,))
        conn.commit()
    finally:
        conn.close()

def get_performance_summary():
    """Tamamlanan tahminlerin genel ve coin bazlı başarı oranlarını getirir."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(p.id) AS total,
                COALESCE(SUM(CASE WHEN o.is_hit = 1 THEN 1 ELSE 0 END), 0) AS hits,
                COALESCE(ROUND(AVG(o.is_hit) * 100, 2), 0.0) AS win_rate,
                COALESCE(ROUND(AVG(o.price_change_pct), 2), 0.0) AS avg_change
            FROM predictions p
            JOIN outcomes o ON p.id = o.prediction_id
            WHERE p.status = 'COMPLETED'
        """)
        overall = cursor.fetchone()

        cursor.execute("""
            SELECT 
                p.symbol,
                COUNT(p.id) AS total,
                COALESCE(SUM(CASE WHEN o.is_hit = 1 THEN 1 ELSE 0 END), 0) AS hits,
                COALESCE(ROUND(AVG(o.is_hit) * 100, 2), 0.0) AS win_rate,
                COALESCE(ROUND(AVG(o.price_change_pct), 2), 0.0) AS avg_change
            FROM predictions p
            JOIN outcomes o ON p.id = o.prediction_id
            WHERE p.status = 'COMPLETED'
            GROUP BY p.symbol
            ORDER BY total DESC
        """)
        by_symbol = cursor.fetchall()
        return overall, by_symbol
    finally:
        conn.close()

def format_performance_report():
    """Telegram için performans raporu metnini hazırlar."""
    overall, by_symbol = get_performance_summary()
    total, hits, win_rate, avg_change = overall
    
    if not total or total == 0:
        return "📊 *Ajan Performans Raporu*\n\nHenüz tamamlanmış bir tahmin verisi bulunmuyor."
    
    msg = f"📊 *Ajan Tahmin Performans Raporu*\n\n"
    msg += f"🎯 *Genel Win-Rate:* %{win_rate:.2f}\n"
    msg += f"📈 *Başarılı / Toplam:* {hits}/{total}\n"
    msg += f"💰 *Ortalama Fiyat Değişimi:* %{avg_change:+.2f}\n\n"
    msg += "🔍 *Varlık Bazlı Detaylar:*\n"
    
    for symbol, s_total, s_hits, s_win_rate, s_change in by_symbol:
        msg += f"• *{symbol}*: %{s_win_rate:.1f} Win-Rate ({s_hits}/{s_total}) | Ort: %{s_change:+.2f}\n"
        
    return msg