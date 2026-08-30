import os
import sqlite3
import db_manager

# Test verilerinin canlı veritabanına yazılmaması için geçici veritabanı yolu
db_manager.DB_PATH = "test_predictions.db"

def mock_get_spot_price(symbol: str) -> float:
    """Borsadan anlık fiyat çekme işlevini taklit eden sahte fiyat fonksiyonu."""
    mock_prices = {
        "PENDLE": 6.50,  # Giriş: 5.20, Hedef: 6.10 -> Yükseliş gerçekleşti (BULLISH HIT)
        "NEAR": 3.80,    # Giriş: 4.50, Hedef: 4.00 -> Düşüş gerçekleşti (BEARISH HIT)
        "SUI": 1.10      # Giriş: 1.50, Hedef: 2.00 -> Yükseliş hedefine ulaşamadı (MISSED)
    }
    return mock_prices.get(symbol, 0.0)

def run_tests():
    print("--- 1. Veritabanı Tablo Oluşturma Testi ---")
    db_manager.init_db()
    print("✔ Tablolar ve indeksler başarıyla ilklendirildi.\n")

    print("--- 2. Tahmin Kaydetme Testi ---")
    # timeframe_hours=0 verilerek test esnasında vadesinin hemen dolması sağlanır
    pred1 = db_manager.save_prediction(
        symbol="PENDLE", direction="BULLISH", entry=5.20, target=6.10, 
        stop=4.80, timeframe_hours=0, confidence=0.85, rationale="TVL artışı ve RSI kırılımı."
    )
    pred2 = db_manager.save_prediction(
        symbol="NEAR", direction="BEARISH", entry=4.50, target=4.00, 
        stop=4.80, timeframe_hours=0, confidence=0.75, rationale="Direnç seviyesinden tepki."
    )
    pred3 = db_manager.save_prediction(
        symbol="SUI", direction="BULLISH", entry=1.50, target=2.00, 
        stop=1.30, timeframe_hours=0, confidence=0.60, rationale="Hacim artış beklentisi."
    )
    print(f"✔ 3 adet test tahmini kaydedildi (ID'ler: {pred1}, {pred2}, {pred3}).\n")

    print("--- 3. Fiyat Kontrolü ve Sonuç Güncelleme Testi ---")
    db_manager.check_and_update_outcomes(mock_get_spot_price)
    print("✔ Doğrulama fonksiyonu çalıştırıldı.\n")

    print("--- 4. Doğrulama Sonuçlarının Sorgulanması ---")
    conn = sqlite3.connect(db_manager.DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.symbol, p.predicted_direction, p.entry_price, p.target_price, 
                   o.actual_price, o.price_change_pct, o.is_hit
            FROM predictions p
            JOIN outcomes o ON p.id = o.prediction_id
        """)
        results = cursor.fetchall()
        
        for row in results:
            symbol, direction, entry, target, actual, change_pct, is_hit = row
            status_str = "BAŞARILI ✅" if is_hit else "BAŞARISIZ ❌"
            print(f"[{symbol}] Yön: {direction} | Giriş: ${entry} | Hedef: ${target} | Gerçekleşen: ${actual} | Değişim: %{change_pct:.2f} | Durum: {status_str}")
    finally:
        conn.close() # Bağlantı açıkça kapatılır

if __name__ == "__main__":
    try:
        run_tests()
    finally:
        if os.path.exists("test_predictions.db"):
            os.remove("test_predictions.db")
            print("\n✔ Test veritabanı dosyası başarıyla temizlendi.")