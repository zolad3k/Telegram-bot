#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, hmac, hashlib, logging, urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
import requests
git add main.py
git commit -m "Remove exchangeInfo to fix 451 error"
git push
# ===== Logi =====
logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("scanner")

# ===== Endpoints z rotacją (omijamy 451) =====
DEFAULT_BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api-gcp.binance.com",
    "https://api4.binance.com",
]
# Możesz podać własną listę (po przecinku) w secrets: BINANCE_API_BASES
BASES = [b.strip() for b in os.environ.get("BINANCE_API_BASES","").split(",") if b.strip()] or DEFAULT_BASES
TIMEOUT = (8, 15)
MAX_RETRIES = 3
sess = requests.Session()
sess.headers.update({"User-Agent":"tg-alerts/1.1"})

def http_get(path: str, params: Dict=None, signed: bool=False) -> Optional[dict]:
    """GET z rotacją baz. Na 451/429/5xx próbuje kolejny mirror."""
    params = params or {}
    api_key = os.environ.get("BINANCE_API_KEY")
    secret  = os.environ.get("BINANCE_API_SECRET")

    for base in BASES:
        for attempt in range(MAX_RETRIES):
            try:
                url = f"{base}{path}"
                if signed and api_key and secret:
                    params_local = dict(params)
                    params_local["timestamp"] = int(time.time()*1000)
                    q = urllib.parse.urlencode(params_local, doseq=True)
                    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
                    url = f"{url}?{q}&signature={sig}"
                    r = sess.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=TIMEOUT)
                else:
                    r = sess.get(url, params=params, timeout=TIMEOUT)

                # Blokady / rate limits
                if r.status_code in (451, 403):
                    log.warning(f"{path} -> {r.status_code} na {base}, próbuję inny mirror…")
                    break  # kolejna baza
                if r.status_code == 429:
                    log.warning("429 (rate limit) – śpię 2s i ponawiam")
                    time.sleep(2); continue
                if r.status_code >= 500:
                    log.warning(f"{path} -> {r.status_code} na {base}, ponawiam")
                    time.sleep(1); continue

                r.raise_for_status()
                return r.json()
            except Exception as e:
                log.warning(f"GET {path} na {base} próba {attempt+1}/{MAX_RETRIES} nieudana: {e}")
                time.sleep(1)
        # spróbuj następny base
    log.error(f"GET {path} nieudane na wszystkich bazach: {BASES}")
    return None

# ===== Binance helpers (bez exchangeInfo!) =====
def get_usdt_symbols_top_n(n:int) -> List[str]:
    """Wybiera TOP-N par USDT po 24h wolumenie z /ticker/24hr (lekki endpoint)."""
    t = http_get("/api/v3/ticker/24hr")
    if not t: return []
    def fnum(x):
        try: return float(x)
        except: return 0.0
    pairs = [
        row for row in t
        if isinstance(row, dict)
        and row.get("symbol","").endswith("USDT")
        and not row.get("symbol","").startswith("USD")
        and row.get("symbol") not in ("BUSDUSDT","USDCUSDT","TUSDUSDT","USDTUSDC")
    ]
    pairs.sort(key=lambda r: fnum(r.get("quoteVolume",0)), reverse=True)
    out = [r["symbol"] for r in pairs[:max(1,n)]]
    log.info(f"Wybrane TOP {len(out)} par USDT po wolumenie (bez exchangeInfo).")
    return out

def get_klines(symbol: str, interval: str, limit:int=300):
    return http_get("/api/v3/klines", params={"symbol":symbol,"interval":interval,"limit":limit}) or []

# ===== Indykatory =====
def ema(vals: List[float], span:int) -> List[float]:
    if not vals: return []
    k = 2/(span+1)
    out=[vals[0]]
    for v in vals[1:]: out.append(v*k + out[-1]*(1-k))
    return out

def rsi(vals: List[float], period:int=14) -> List[float]:
    if len(vals) <= period: return []
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        d=vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    out=[None]*period
    for i in range(period, len(gains)):
        ag=(ag*(period-1)+gains[i])/period
        al=(al*(period-1)+losses[i])/period
        rs=float('inf') if al==0 else ag/al
        out.append(100 - 100/(1+rs))
    return out

def macd(vals: List[float], fast=12, slow=26, signal=9):
    if len(vals) < slow+signal+5: return [],[],[]
    ef=ema(vals,fast); es=ema(vals,slow)
    L=min(len(ef),len(es)); ef=ef[-L:]; es=es[-L:]
    line=[a-b for a,b in zip(ef,es)]
    sig=ema(line,signal)
    L2=min(len(line),len(sig)); line=line[-L2:]; sig=sig[-L2:]
    hist=[m-s for m,s in zip(line,sig)]
    return line, sig, hist

def signals(symbol:str, closes:List[float], volumes:List[float], mode:str)->List[str]:
    out=[]
    if len(closes)<60: return out
    r = rsi(closes,14); ml,sl,h = macd(closes)
    if not r or not h: return out

    r_now=r[-1]; r_prev=r[-2]
    ml_now=ml[-1]; ml_prev=ml[-2]
    sl_now=sl[-1]; sl_prev=sl[-2]
    vol=volumes[-1]; sma20=sum(volumes[-20:])/20 if len(volumes)>=20 else sum(volumes)/len(volumes)
    vol_spike = vol > 1.5*sma20
    momentum = (closes[-1]-closes[-4])/closes[-4]*100 if closes[-4] else 0

    macd_bull = ml_prev<=sl_prev and ml_now>sl_now
    macd_bear = ml_prev>=sl_prev and ml_now<sl_now
    rsi_rebound = r_prev<50<=r_now

    if mode=="conservative":
        if macd_bull and rsi_rebound and vol_spike and momentum>1.0:
            out.append(f"✅ {symbol}: MACD↑, RSI>50, wolumen↑, mom {momentum:.2f}%")
    else:
        if macd_bull or rsi_rebound or (vol_spike and momentum>0.7):
            out.append(f"⚡ {symbol}: wczesny sygnał (MACD/RSI/Vol), mom {momentum:.2f}%")

    if macd_bear or (not vol_spike and momentum<-1.0 and r_now<r_prev):
        out.append(f"⚠️ {symbol}: możliwe osłabienie")

    if r_now>=70: out.append(f"🧭 {symbol}: RSI {r_now:.1f} (overbought)")
    if r_now<=30: out.append(f"🧭 {symbol}: RSI {r_now:.1f} (oversold)")
    return list(dict.fromkeys(out))

# ===== Telegram =====
def tg_send(text:str)->bool:
    tok=os.environ.get("TELEGRAM_BOT_TOKEN"); chat=os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        log.error("Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"); return False
    url=f"https://api.telegram.org/bot{tok}/sendMessage"
    payload={"chat_id":chat,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    for i in range(MAX_RETRIES):
        try:
            r=sess.post(url,json=payload,timeout=TIMEOUT)
            if r.status_code==429:
                wait=int(r.headers.get("Retry-After","2")); time.sleep(wait); continue
            r.raise_for_status(); return True
        except Exception as e:
            log.warning(f"Telegram próba {i+1}: {e}"); time.sleep(1)
    log.error("Telegram: nie udało się wysłać."); return False

# ===== Główne =====
def scan():
    interval=os.environ.get("INTERVAL","1h")
    mode=os.environ.get("ALERT_MODE","aggressive").lower()
    top_n=int(os.environ.get("TOP_N","40"))
    syms_env=[s.strip().upper() for s in os.environ.get("SYMBOLS","").split(",") if s.strip()]
    symbols = syms_env or get_usdt_symbols_top_n(top_n)
    if not symbols:
        log.error("Brak symboli do skanowania."); return

    log.info(f"Skanuję {len(symbols)} par, interwał {interval}, tryb {mode}")
    alerts=[]; checked=0
    for sym in symbols:
        k = get_klines(sym, interval, 300)
        if not k or len(k)<30: continue
        closes=[float(x[4]) for x in k]
        volumes=[float(x[5]) for x in k]
        alerts.extend(signals(sym, closes, volumes, mode))
        checked+=1
        time.sleep(0.12)

    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if alerts:
        header=f"📈 Binance scan ({interval}, {mode}) — {checked} par\n{ts}\n\n"
        msg=header
        for line in alerts:
            if len(msg)+len(line)+1>3800:
                tg_send(msg); msg=header
            msg+=line+"\n"
        if msg.strip(): tg_send(msg)
        log.info(f"Wysłano {len(alerts)} alertów.")
    else:
        log.info(f"Brak nowych sygnałów ({checked} par).")

def main():
    try: scan()
    except Exception as e:
        log.exception(f"Nieoczekiwany błąd: {e}")

if __name__=="__main__":
    main()
