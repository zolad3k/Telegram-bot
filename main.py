import os, time, argparse, requests
from datetime import datetime

BINANCE = "https://api.binance.com"  # spot

# ======= PARAMS from env (z sensownymi defaultami) =======
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")
CANDLE_INTERVAL     = os.getenv("CANDLE_INTERVAL", "4h")       # 15m/1h/4h/1d
MIN_24H_QUOTE       = float(os.getenv("MIN_24H_QUOTE", "1500000"))  # min USDC obrotu 24h
BREAKOUT_LOOKBACK   = int(os.getenv("BREAKOUT_LOOKBACK", "20"))
VOLUME_SPIKE_MULT   = float(os.getenv("VOLUME_SPIKE_MULT", "1.8"))
SCAN_EVERY_SEC      = int(os.getenv("SCAN_EVERY_SEC", "300"))  # tylko gdy nie one-shot

# ======= Telegram =======
def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Brak TG_BOT_TOKEN / TG_CHAT_ID — pomijam wysyłkę.")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("Telegram error:", e)

# ======= Binance helpers =======
def get_usdc_pairs():
    """Zwróć listę symboli z kwotą USDC (TRADING)."""
    r = requests.get(BINANCE + "/api/v3/exchangeInfo", timeout=30)
    r.raise_for_status()
    info = r.json()
    symbols = []
    for s in info["symbols"]:
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDC":
            # odfiltrowujemy dźwignie/kontrakty – na spot ich nie ma, ale nazwy bywały podobne
            symbols.append(s["symbol"])
    return symbols

def get_24h_map():
    """Mapa symbol -> (quoteVolume, lastPrice)."""
    r = requests.get(BINANCE + "/api/v3/ticker/24hr", timeout=30)
    r.raise_for_status()
    data = r.json()
    out = {}
    for d in data:
        try:
            out[d["symbol"]] = (float(d["quoteVolume"]), float(d["lastPrice"]))
        except:
            pass
    return out

def klines(symbol, interval, limit):
    p = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE + "/api/v3/klines", params=p, timeout=30)
    r.raise_for_status()
    return r.json()

# ======= Skan jednej pary =======
def check_symbol(symbol):
    """Zwraca tekst alertu jeśli mamy breakout + spike wolumenu, albo None."""
    # bierzemy LOOKBACK + 1 świec (ostatnia = świeca bieżąca)
    data = klines(symbol, CANDLE_INTERVAL, BREAKOUT_LOOKBACK + 1)
    if len(data) < BREAKOUT_LOOKBACK + 1:
        return None

    # zamkniecia i wolumeny (vol = [5])
    closes = [float(x[4]) for x in data]           # close = index 4
    vols   = [float(x[5]) for x in data]           # volume = index 5
    last_close = closes[-1]
    prev_high   = max(float(x[2]) for x in data[:-1])  # high = index 2 (bez bieżącej)
    avg_prev_vol = sum(vols[:-1]) / max(1, len(vols)-1)
    last_vol     = vols[-1]

    breakout = last_close > prev_high
    spike    = last_vol > avg_prev_vol * VOLUME_SPIKE_MULT

    if breakout and spike:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        return (f"🚀 <b>BREAKOUT + VOLUME</b>\n"
                f"• {symbol}\n"
                f"• Interwał: {CANDLE_INTERVAL}\n"
                f"• Close: {last_close:.6f} > PrevHigh: {prev_high:.6f}\n"
                f"• Vol: {last_vol:.2f} > avg: {avg_prev_vol:.2f} x{VOLUME_SPIKE_MULT}\n"
                f"• {ts}")
    return None

# ======= Cały skan =======
def scan_once():
    try:
        usdc = set(get_usdc_pairs())
        m24  = get_24h_map()
    except Exception as e:
        print("Błąd pobierania listy symboli:", e)
        tg_send(f"❗ Błąd skanera: {e}")
        return

    # filtr po obrocie
    watch = [s for s in usdc if s in m24 and m24[s][0] >= MIN_24H_QUOTE]
    print(f"Do sprawdzenia: {len(watch)} par (filtr 24hQuote >= {MIN_24H_QUOTE})")

    hits = []
    for sym in watch:
        try:
            alert = check_symbol(sym)
            if alert:
                hits.append(alert)
        except Exception as e:
            print(sym, "ERR", e)

    if hits:
        for a in hits:
            print(a, "\n")
            tg_send(a)
    else:
        print("Brak sygnałów w tym przebiegu.")

# ======= CLI =======
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--one-shot", action="store_true", help="Wykonaj pojedynczy skan i zakończ.")
    args = ap.parse_args()

    if args.one_shot:
        scan_once()
    else:
        print("Start ciągłego skanera (Actions nie używa tego trybu).")
        while True:
            scan_once()
            time.sleep(SCAN_EVERY_SEC)
