import sqlite3
from datetime import datetime, timedelta

DB_PATH = "predictions.db"

def init_db():
    """Veritabanı tablolarını ve indeksleri otomatik oluşturur."""
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