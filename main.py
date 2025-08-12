#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scan Binance & Alert TG
- Pobiera listę par (domyślnie TOP_N najwiekszych po wolumenie USDT)
- Liczy RSI i MACD z klines
- Wysyła alerty na Telegram wg wybranego trybu (aggressive/conservative)

ENV:
  TELEGRAM_BOT_TOKEN (required)
  TELEGRAM_CHAT_ID   (required)
  SYMBOLS            (optional, comma-separated e.g. BTCUSDT,ETHUSDT)
  TOP_N              (optional, default 40)
  INTERVAL           (optional, 1m/5m/15m/1h/4h/1d; default 1h)
  ALERT_MODE         (optional, aggressive|conservative; default aggressive)
"""

import os
import time
import math
import json
import hmac
import hashlib
import logging
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timezone
import urllib.parse
import random

import requests

# ------------ Konfiguracja logowania ------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("scanner")

BINANCE_API = "https://api.binance.com"
TIMEOUT = (8, 15)  # (connect, read)
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]

# ------------ Helpers HTTP ------------
sess = requests.Session()
sess.headers.update({"User-Agent": "tg-alerts/1.0"})

def http_get(url: str, params: Dict = None, signed: bool = False) -> Optional[dict]:
    params = params or {}
    for attempt in range(MAX_RETRIES):
        try:
            if signed:
                api_key = os.environ.get("BINANCE_API_KEY")
                secret = os.environ.get("BINANCE_API_SECRET")
                if not api_key or not secret:
                    log.warning("Brak BINANCE_API_KEY/SECRET -> używam niesygnowanych endpointów")
                    signed = False
                else:
                    ts = int(time.time() * 1000)
                    params["timestamp"] = ts
                    q = urllib.parse.urlencode(params, doseq=True)
                    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
                    q = f"{q}&signature={sig}"
                    headers = {"X-MBX-APIKEY": api_key}
                    resp = sess.get(f"{url}?{q}", headers=headers, timeout=TIMEOUT)
                    if resp.status_code == 429:
                        log.warning("Rate limit 429, śpię 2s")
                        time.sleep(2)
                        continue
                    resp.raise_for_status()
                    return resp.json()

            # zwykły GET
            resp = sess.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 429:
                log.warning("Rate limit 429, śpię 2s")
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)]
            log.warning(f"GET {url} próba {attempt+1}/{MAX_RETRIES} nieudana: {e} -> czekam {wait}s")
            time.sleep(wait)
    log.error(f"GET {url} nie powiodło się po {MAX_RETRIES} próbach")
    return None

# ------------ Dane z Binance ------------
def get_usdt_symbols_top_n(n: int) -> List[str]:
    """Zwraca TOP N par USDT po wolumenie (24h)."""
    tickers = http_get(f"{BINANCE_API}/api/v3/ticker/24hr")
    if not tickers:
        return []
    filtered = [
        t for t in tickers
        if t.get("symbol","").endswith("USDT")
        and t.get("symbol") not in ("BUSDUSDT","USDCUSDT","TUSDUSDT","USDTUSDC")
        and not t.get("symbol","").startswith("USD")
    ]
    def safe_float(x):
        try: return float(x)
        except: return 0.0
    filtered.sort(key=lambda t: safe_float(t.get("quoteVolume", 0.0)), reverse=True)
    syms = [t["symbol"] for t in filtered[:max(1, n)]]
    log.info(f"Wybrane TOP {len(syms)} par USDT po wolumenie")
    return syms

def get_klines(symbol: str, interval: str, limit: int = 300) -> List[List]:
    data = http_get(f"{BINANCE_API}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return data or []

# ------------ Indykatory ------------
def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [None]*(period)  # align
    # Wilder
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
        if avg_loss == 0:
            rs = float('inf')
        else:
            rs = avg_gain / avg_loss
        r = 100 - (100 / (1 + rs))
        rsis.append(r)
    return rsis

def ema(values: List[float], span: int) -> List[float]:
    if not values:
        return []
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def macd(values: List[float], fast=12, slow=26, signal=9) -> Tuple[List[float], List[float], List[float]]:
    if len(values) < slow + signal + 5:
        return [], [], []
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    # wyrównaj długości
    length = min(len(ema_fast), len(ema_slow))
    ema_fast = ema_fast[-length:]
    ema_slow = ema_slow[-length:]
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    length2 = min(len(macd_line), len(signal_line))
    macd_line = macd_line[-length2:]
    signal_line = signal_line[-length2:]
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist

# ------------ Reguły alertów ------------
def generate_signals(symbol: str, closes: List[float], volumes: List[float], mode: str) -> List[str]:
    out = []
    if len(closes) < 60:
        return out

    r = rsi(closes, 14)
    macd_line, signal_line, hist = macd(closes)
    if not r or not hist:
        return out

    last_close = closes[-1]
    r_now = r[-1]
    r_prev = r[-2] if len(r) > 1 else r[-1]
    macd_now = macd_line[-1]
    macd_prev = macd_line[-2] if len(macd_line) > 1 else macd_line[-1]
    signal_now = signal_line[-1]
    signal_prev = signal_line[-2] if len(signal_line) > 1 else signal_line[-1]
    hist_now = hist[-1]
    hist_prev = hist[-2] if len(hist) > 1 else hist[-1]

    # wolumen (prosty spike vs SMA20)
    vol = volumes[-1]
    sma20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes)/len(volumes)
    vol_spike = vol > 1.5 * sma20

    # momentum % vs 3 świeczki
    momentum = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] != 0 else 0

    # sygnały
    macd_bull_cross = macd_prev <= signal_prev and macd_now > signal_now
    macd_bear_cross = macd_prev >= signal_prev and macd_now < signal_now
    rsi_rebound = r_prev < 50 <= r_now  # powyżej 50
    rsi_overbought = r_now >= 70
    rsi_oversold = r_now <= 30

    if mode == "conservative":
        if macd_bull_cross and rsi_rebound and vol_spike and momentum > 1.0:
            out.append(f"✅ {symbol}: MACD bullish cross, RSI↑>50, wolumen↑, mom {momentum:.2f}%")
    else:  # aggressive
        if (macd_bull_cross or rsi_rebound or (vol_spike and momentum > 0.7)):
            out.append(f"⚡ {symbol}: wczesny sygnał (MACD/RSI/Volume), mom {momentum:.2f}%")
    # ostrzeżenia spadkowe
    if macd_bear_cross or (not vol_spike and momentum < -1.0 and r_now < r_prev):
        out.append(f"⚠️ {symbol}: możliwe osłabienie (MACD/RSI/mom)")

    # ekstremy
    if rsi_overbought:
        out.append(f"🧭 {symbol}: RSI {r_now:.1f} (overbought)")
    if rsi_oversold:
        out.append(f"🧭 {symbol}: RSI {r_now:.1f} (oversold)")

    # unikalne
    # redukuj duplikaty
    uniq = []
    seen = set()
    for s in out:
        key = s.split(":")[0]
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

# ------------ Telegram ------------
def tg_send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID – nie mogę wysłać powiadomień.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = sess.post(url, json=payload, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                log.warning(f"Telegram 429 – śpię {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)]
            log.warning(f"Telegram send próba {attempt+1}/{MAX_RETRIES} nieudana: {e} -> czekam {wait}s")
            time.sleep(wait)
    log.error("Nie udało się wysłać wiadomości Telegram po kilku próbach.")
    return False

# ------------ Główny przepływ ------------
def scan():
    interval = os.environ.get("INTERVAL", "1h")
    mode = os.environ.get("ALERT_MODE", "aggressive").lower()
    top_n = int(os.environ.get("TOP_N", "40"))

    # Ustal symbole
    env_syms = os.environ.get("SYMBOLS", "").strip()
    if env_syms:
        symbols = [s.strip().upper() for s in env_syms.split(",") if s.strip()]
    else:
        symbols = get_usdt_symbols_top_n(top_n)

    if not symbols:
        log.error("Brak symboli do skanowania.")
        return

    log.info(f"Skanuję {len(symbols)} par, interwał {interval}, tryb {mode}")

    alerts: List[str] = []
    checked = 0

    for sym in symbols:
        # pobierz dane świec
        kl = get_klines(sym, interval, limit=300)
        if not kl or len(kl) < 30:
            log.debug(f"{sym}: za mało danych ({len(kl) if kl else 0})")
            continue

        closes = [float(k[4]) for k in kl]
        volumes = [float(k[5]) for k in kl]
        sigs = generate_signals(sym, closes, volumes, mode)
        alerts.extend(sigs)
        checked += 1

        # prosty throttling
        time.sleep(0.12)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if alerts:
        header = f"📈 Binance scan ({interval}, {mode}) — {checked} par\n{ts}\n\n"
        # porcjuj w bloki żeby nie przekroczyć limitu Telegram
        chunk = header
        for line in alerts:
            if len(chunk) + len(line) + 1 > 3800:
                tg_send_message(chunk)
                chunk = header
            chunk += line + "\n"
        if chunk.strip():
            tg_send_message(chunk)
        log.info(f"Wysłano {len(alerts)} alertów dla {checked} par.")
    else:
        log.info(f"Brak nowych sygnałów ({checked} par).")
        # opcjonalnie wyślij cichy ping raz na jakiś czas (wyłączone domyślnie)
        # tg_send_message(f"🤖 {ts}: brak nowych sygnałów ({checked} par, {interval}, {mode}).")

def main():
    try:
        scan()
    except Exception as e:
        # Nie zrywaj workflowa – zaloguj i zakończ 0, żeby cron leciał dalej
        log.exception(f"Nieoczekiwany błąd: {e}")

if __name__ == "__main__":
    main()
