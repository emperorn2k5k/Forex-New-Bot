# -*- coding: utf-8 -*-
"""
forex_ai_bot_discord.py
Bot Discord: Teknikal (pandas) + Chart Candlestick (mplfinance) + Berita + Kalender Ekonomi + Keputusan AI (Gemini)
Dirancang untuk GitHub Actions: sekali jalan per jadwal (python forex_ai_bot_discord.py --once).
Memakai REST API Discord lewat requests, tanpa library discord.py, jadi ringan dan langsung berhenti setelah selesai.

Dua mode (dipilih otomatis):
  BOT     : DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID. Notifikasi + perintah berawalan tanda seru.
  WEBHOOK : DISCORD_WEBHOOK_URL saja. Hanya notifikasi, tanpa perintah.

V3.2: Fix bug chart candlestick (mplfinance + matplotlib Agg), lampiran gambar embed Discord (BOT & WEBHOOK),
      serta seluruh fitur V3.1 (state JSON, cooldown, pelacak posisi, WIB/WITA/WIT).

Library: yfinance, pandas, requests, google-genai, mplfinance, matplotlib
"""

# =====================================================================
# KONFIGURASI (EDIT DI SINI)
# Untuk GitHub Actions, isi token/key lewat Secrets, jangan ditulis di file ini.
# =====================================================================
DISCORD_BOT_TOKEN = "ISI_TOKEN_BOT_DISCORD"
DISCORD_CHANNEL_ID = "ISI_ID_CHANNEL"
DISCORD_WEBHOOK_URL = ""          # opsional: mode hanya-notifikasi tanpa bot
GEMINI_API_KEY = "ISI_API_KEY_GEMINI"
ADMIN_USER_ID = 0                 # 0 = orang pertama yang mengetik !start jadi admin
PREFIX = "!"
MODAL_AWAL_USD = 100.0
RISIKO_PER_TRADE_PERSEN = 1.0
INTERVAL_SCAN_MENIT = 15          # dipakai hanya jika dijalankan dengan --loop
WATCHLIST = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"]
ZONA_WAKTU_JAM = 7                # jam di pesan: 7 = WIB, 8 = WITA, 9 = WIT

# Pengaturan tambahan (opsional)
MIN_KEYAKINAN = 75                # sinyal ditahan jika skor < nilai ini
COOLDOWN_JAM = 4                  # jangan kirim ulang sinyal arah sama dalam X jam
GEMINI_MODEL = "gemini-2.5-flash"
ADX_MIN = 22                      # tren dianggap bertenaga jika ADX 1H >= nilai ini
RSI_AMAN_MIN = 32                 # RSI 15M harus di antara MIN dan MAX agar AI dipanggil
RSI_AMAN_MAX = 68
NEWS_BLOK_SEBELUM_MENIT = 45      # blokir sinyal X menit sebelum berita High Impact
NEWS_BLOK_SESUDAH_MENIT = 15      # ...dan X menit sesudah rilis
POSISI_KADALUARSA_JAM = 48        # sinyal yang tidak kena TP/SL dalam X jam dianggap kedaluwarsa
KIRIM_CHART = True                # lampirkan gambar candlestick pada hasil analisa / sinyal valid
# =====================================================================

import os
import re
import sys
import json
import math
import time
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd

try:
    import yfinance as yf
    from google import genai
    from google.genai import types as gtypes
except ImportError as e_imp:
    print("Library belum terinstal: %s" % e_imp)
    print("Pasang: yfinance pandas requests google-genai mplfinance matplotlib")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")
    import mplfinance as mpf
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ---------------------------------------------------------------------
# KONFIGURASI DARI ENVIRONMENT (GitHub Secrets). Jika kosong, nilai di atas dipakai.
# ---------------------------------------------------------------------
def _env(nama, default):
    return (os.environ.get(nama) or default).strip()


DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN)
DISCORD_CHANNEL_ID = _env("DISCORD_CHANNEL_ID", DISCORD_CHANNEL_ID)
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
GEMINI_API_KEY = _env("GEMINI_API_KEY", GEMINI_API_KEY)
try:
    if os.environ.get("ADMIN_USER_ID", "").strip():
        ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"].strip())
    if os.environ.get("MODAL_AWAL_USD", "").strip():
        MODAL_AWAL_USD = float(os.environ["MODAL_AWAL_USD"])
    if os.environ.get("RISIKO_PER_TRADE_PERSEN", "").strip():
        RISIKO_PER_TRADE_PERSEN = float(os.environ["RISIKO_PER_TRADE_PERSEN"])
    if os.environ.get("WATCHLIST", "").strip():
        WATCHLIST = [x.strip().upper() for x in os.environ["WATCHLIST"].split(",") if x.strip()]
    if os.environ.get("ZONA_WAKTU_JAM", "").strip():
        ZONA_WAKTU_JAM = int(os.environ["ZONA_WAKTU_JAM"])
except ValueError:
    print("PERINGATAN: ada nilai environment yang tidak valid, memakai konfigurasi di file.")


def tentukan_mode():
    if (re.fullmatch(r"\d{15,25}", DISCORD_CHANNEL_ID) and len(DISCORD_BOT_TOKEN) > 30
            and not DISCORD_BOT_TOKEN.startswith("ISI_")):
        return "bot"
    if DISCORD_WEBHOOK_URL.startswith(("https://discord.com/api/webhooks/",
                                       "https://discordapp.com/api/webhooks/")):
        return "webhook"
    return None


MODE = tentukan_mode()

ZONA = timezone(timedelta(hours=ZONA_WAKTU_JAM))
LABEL_ZONA = {7: "WIB", 8: "WITA", 9: "WIT"}.get(ZONA_WAKTU_JAM, "UTC%+d" % ZONA_WAKTU_JAM)


def waktu_lokal(ts=None):
    """Waktu dalam zona pilihan (server GitHub memakai UTC)."""
    if ts is None:
        return datetime.now(ZONA)
    return datetime.fromtimestamp(ts, ZONA)

# ---------------------------------------------------------------------
# DATA PAIR
# ---------------------------------------------------------------------
TICKER_MAP = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
}
ALIAS_PAIR = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "EMAS": "XAUUSD",
    "BTC": "BTCUSD", "BITCOIN": "BTCUSD",
    "EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY",
}
MATA_UANG = {
    "XAUUSD": ["USD"], "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"], "BTCUSD": ["USD"],
}
DESIMAL = {"XAUUSD": 2, "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "BTCUSD": 2}
PIP_SIZE = {"XAUUSD": 0.1, "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01, "BTCUSD": 1.0}

RSS_UMUM = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.investing.com/rss/news_1.rss",
    "https://www.fxstreet.com/rss/news",
]
RSS_BTC = ["https://www.coindesk.com/arc/outboundfeeds/rss/"]
KALENDER_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}

# ---------------------------------------------------------------------
# STATE GLOBAL
# ---------------------------------------------------------------------
STATE_LOCK = threading.RLock()
SCAN_AKTIF = True
LAST_SIGNAL = {}                  # {"XAUUSD": {"arah": "BUY", "waktu": 123.0, "entry": 1.0}}
POSISI = []                       # daftar sinyal terlacak (OPEN / CLOSED)
CACHE_BERITA = {}                 # {url: (waktu, [judul])}
KAL = {"waktu": 0.0, "gagal": 0.0, "data": []}
GEMINI_CLIENT = None
PESAN_TERAKHIR_ID = ""            # penanda pesan Discord terakhir yang sudah dibaca
SUDAH_SAPA = False
PERINGATAN = {}                   # {kunci: waktu} untuk membatasi pesan peringatan
SCAN_TERAKHIR = 0.0


def log(pesan):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), pesan))
    sys.stdout.flush()


def lokasi_folder():
    d = os.environ.get("BOT_STATE_DIR")
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def lokasi_state():
    return os.path.join(lokasi_folder(), "forex_bot_discord_state.json")


def rapikan_posisi():
    """Simpan semua posisi OPEN + 200 posisi CLOSED terakhir."""
    global POSISI
    with STATE_LOCK:
        terbuka = [p for p in POSISI if p.get("status") == "OPEN"]
        tutup = [p for p in POSISI if p.get("status") != "OPEN"]
        POSISI = tutup[-200:] + terbuka


def simpan_state():
    try:
        with STATE_LOCK:
            rapikan_posisi()
            batas_lama = time.time() - 4 * 86400
            for k in [k for k, v in PERINGATAN.items() if float(v) < batas_lama]:
                del PERINGATAN[k]
            data = {
                "admin_user_id": ADMIN_USER_ID,
                "modal": MODAL_AWAL_USD,
                "scan_aktif": SCAN_AKTIF,
                "last_signal": LAST_SIGNAL,
                "posisi": POSISI,
                "pesan_terakhir_id": PESAN_TERAKHIR_ID,
                "sudah_sapa": SUDAH_SAPA,
                "peringatan": PERINGATAN,
            }
            path = lokasi_state()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
    except Exception as e:
        log("Gagal menyimpan state: %s" % e)


def muat_state():
    global ADMIN_USER_ID, MODAL_AWAL_USD, SCAN_AKTIF, LAST_SIGNAL, POSISI
    global PESAN_TERAKHIR_ID, SUDAH_SAPA, PERINGATAN
    try:
        path = lokasi_state()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with STATE_LOCK:
            if ADMIN_USER_ID == 0 and data.get("admin_user_id"):
                ADMIN_USER_ID = int(data["admin_user_id"])
            if data.get("modal"):
                MODAL_AWAL_USD = float(data["modal"])
            if "scan_aktif" in data:
                SCAN_AKTIF = bool(data["scan_aktif"])
            if isinstance(data.get("last_signal"), dict):
                LAST_SIGNAL = data["last_signal"]
            if isinstance(data.get("posisi"), list):
                POSISI = [p for p in data["posisi"] if isinstance(p, dict) and "id" in p]
            PESAN_TERAKHIR_ID = str(data.get("pesan_terakhir_id") or "")
            SUDAH_SAPA = bool(data.get("sudah_sapa", False))
            if isinstance(data.get("peringatan"), dict):
                PERINGATAN = data["peringatan"]
        log("State dimuat dari %s" % path)
    except Exception as e:
        log("Gagal memuat state: %s" % e)


def boleh_peringatan(kunci, jam=6):
    """True jika peringatan dengan kunci ini belum dikirim dalam X jam terakhir."""
    with STATE_LOCK:
        if time.time() - float(PERINGATAN.get(kunci, 0)) > jam * 3600:
            PERINGATAN[kunci] = time.time()
            ok = True
        else:
            ok = False
    if ok:
        simpan_state()
    return ok


# ---------------------------------------------------------------------
# DISCORD (REST API + LAMPIRAN CHART)
# ---------------------------------------------------------------------
API = "https://discord.com/api/v10"
UA_DISCORD = "DiscordBot (https://github.com/forex-ai-bot, 1.0)"


def discord_req(metode, path, **kw):
    """Panggil REST API Discord dengan penanganan rate limit. Return JSON, {} jika kosong, None jika gagal."""
    headers = {"Authorization": "Bot %s" % DISCORD_BOT_TOKEN, "User-Agent": UA_DISCORD}
    for percobaan in range(3):
        try:
            r = requests.request(metode, API + path, headers=headers, timeout=25, **kw)
            if r.status_code == 429:
                try:
                    tunggu = float(r.json().get("retry_after", 2))
                except Exception:
                    tunggu = 2.0
                log("Discord rate limit, menunggu %.1f detik" % tunggu)
                time.sleep(min(tunggu, 15) + 0.5)
                continue
            if r.status_code >= 400:
                log("Discord API %s %s gagal (%d): %s" % (metode, path, r.status_code, r.text[:200]))
                return None
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()
        except Exception as e:
            log("Discord API error (%d): %s" % (percobaan + 1, e))
            time.sleep(2)
    return None


def pecah_teks(teks, batas=1900):
    """Pecah teks di batas baris agar tiap pesan tidak melebihi batas 2000 karakter Discord."""
    hasil = []
    cur = ""
    for baris in teks.split("\n"):
        while len(baris) > batas:
            if cur:
                hasil.append(cur)
                cur = ""
            hasil.append(baris[:batas])
            baris = baris[batas:]
        if cur and len(cur) + len(baris) + 1 > batas:
            hasil.append(cur)
            cur = baris
        else:
            cur = (cur + "\n" + baris) if cur else baris
    if cur:
        hasil.append(cur)
    return hasil or [" "]


def tujuan_aktif():
    return MODE in ("bot", "webhook")


WARNA_HIJAU = 0x2ECC71
WARNA_MERAH = 0xE74C3C
WARNA_KUNING = 0xF1C40F
WARNA_BIRU = 0x3498DB
WARNA_ABU = 0x95A5A6


def warna_arah(arah):
    return WARNA_HIJAU if arah == "BUY" else WARNA_MERAH


def warna_hasil(teks):
    """Warna kartu untuk pesan hasil pelacak sinyal (TP, SL, TP1, kedaluwarsa)."""
    if teks.startswith("\u2705 TP"):
        return WARNA_HIJAU
    if teks.startswith("\u274c SL"):
        return WARNA_MERAH
    if teks.startswith("\U0001f514"):
        return WARNA_BIRU
    if teks.startswith("\u231b"):
        return WARNA_ABU
    return None


def _kirim_bot(payload, chart_path=None):
    path = "/channels/%s/messages" % DISCORD_CHANNEL_ID
    if chart_path and os.path.exists(chart_path):
        fname = os.path.basename(chart_path)
        with open(chart_path, "rb") as f:
            files = {"files[0]": (fname, f, "image/png")}
            data = {"payload_json": json.dumps(payload)}
            return discord_req("POST", path, data=data, files=files)
    return discord_req("POST", path, json=payload)


def _kirim_webhook(payload, chart_path=None):
    for percobaan in range(2):
        try:
            if chart_path and os.path.exists(chart_path):
                fname = os.path.basename(chart_path)
                with open(chart_path, "rb") as f:
                    files = {"files[0]": (fname, f, "image/png")}
                    data = {"payload_json": json.dumps(payload)}
                    r = requests.post(DISCORD_WEBHOOK_URL, data=data, files=files, timeout=25)
            else:
                r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            if r.status_code == 429:
                try:
                    tunggu = float(r.json().get("retry_after", 2))
                except Exception:
                    tunggu = 2.0
                time.sleep(min(tunggu, 15) + 0.5)
                continue
            if r.status_code >= 400:
                log("Webhook gagal (%d): %s" % (r.status_code, r.text[:200]))
                return False
            return True
        except Exception as e:
            log("Webhook error (%d): %s" % (percobaan + 1, e))
            time.sleep(2)
    return False


def kirim(teks, warna=None, chart_path=None):
    """Kirim pesan. Jika warna diberikan, tampil sebagai kartu (embed) berwarna.
    Jika chart_path diberikan, gambar candlestick dilampirkan pada bagian pertama."""
    ok = True
    batas = 3900 if warna is not None else 1900
    potongan = pecah_teks(teks, batas)
    try:
        for idx, p in enumerate(potongan):
            lampiran = chart_path if (idx == 0 and chart_path and os.path.exists(chart_path)) else None
            fname = os.path.basename(lampiran) if lampiran else None
            try:
                if MODE == "bot":
                    if warna is not None:
                        embed = {"description": p, "color": warna}
                        if fname:
                            embed["image"] = {"url": "attachment://%s" % fname}
                        payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
                        if _kirim_bot(payload, lampiran) is None:
                            for q in pecah_teks(p, 1900):
                                if _kirim_bot({"content": q, "allowed_mentions": {"parse": []}}) is None:
                                    ok = False
                    else:
                        if _kirim_bot({"content": p, "allowed_mentions": {"parse": []}}, lampiran) is None:
                            ok = False
                elif MODE == "webhook":
                    if warna is not None:
                        embed = {"description": p, "color": warna}
                        if fname:
                            embed["image"] = {"url": "attachment://%s" % fname}
                        payload = {"embeds": [embed]}
                    else:
                        payload = {"content": p}
                    payload["allowed_mentions"] = {"parse": []}
                    payload["username"] = "Forex AI Bot"
                    if not _kirim_webhook(payload, lampiran):
                        ok = False
                else:
                    ok = False
            except Exception as e:
                log("Gagal kirim Discord: %s" % e)
                ok = False
            time.sleep(0.4)
    finally:
        if chart_path and os.path.exists(chart_path):
            try:
                os.remove(chart_path)
            except Exception:
                pass
    return ok


def ambil_pesan_baru():
    """Baca pesan channel setelah penanda terakhir. Return list (lama ke baru) atau None jika gagal."""
    global PESAN_TERAKHIR_ID
    path = "/channels/%s/messages" % DISCORD_CHANNEL_ID
    if not PESAN_TERAKHIR_ID:
        res = discord_req("GET", path, params={"limit": 10})
        if res is None:
            return None
        if not res:
            PESAN_TERAKHIR_ID = "0"
            simpan_state()
            return []
        res.sort(key=lambda m: int(m["id"]))
        PESAN_TERAKHIR_ID = str(res[-1]["id"])
        simpan_state()
        batas_ms = (time.time() - 1800) * 1000
        return [m for m in res if ((int(m["id"]) >> 22) + 1420070400000) >= batas_ms]
    hasil = []
    for _ in range(3):
        res = discord_req("GET", path, params={"after": PESAN_TERAKHIR_ID, "limit": 100})
        if res is None:
            if not hasil:
                return None
            break
        if not res:
            break
        res.sort(key=lambda m: int(m["id"]))
        hasil.extend(res)
        PESAN_TERAKHIR_ID = str(res[-1]["id"])
        if len(res) < 100:
            break
    simpan_state()
    return hasil


# ---------------------------------------------------------------------
# JAM PASAR (WEEKEND GUARD)
# ---------------------------------------------------------------------
def pasar_buka(symbol):
    """BTC buka 24/7. Forex & emas tutup Jumat ~21:00 UTC s/d Minggu ~22:00 UTC."""
    if symbol == "BTCUSD":
        return True
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    if wd == 5:
        return False
    if wd == 4 and now.hour >= 21:
        return False
    if wd == 6 and now.hour < 22:
        return False
    return True


# ---------------------------------------------------------------------
# DATA MARKET (yfinance)
# ---------------------------------------------------------------------
def bersihkan_df(df):
    """Ratakan MultiIndex yfinance terbaru & pastikan kolom OHLC bersih."""
    try:
        if df is None or len(df) == 0:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            lv0 = [str(x) for x in df.columns.get_level_values(0)]
            if "Close" in lv0:
                df.columns = df.columns.get_level_values(0)
            else:
                df.columns = df.columns.get_level_values(-1)
        df = df.loc[:, ~df.columns.duplicated()].copy()
        df.columns = [str(c).title() for c in df.columns]
        for k in ("Open", "High", "Low", "Close"):
            if k not in df.columns:
                return None
        df = df[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna()
        df = df[~df.index.duplicated(keep="last")]
        if len(df) == 0:
            return None
        return df
    except Exception as e:
        log("bersihkan_df error: %s" % e)
        return None


def ambil_data(symbol, period, interval, min_baris=60):
    ticker = TICKER_MAP[symbol]
    for percobaan in range(3):
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=False, threads=False)
            df = bersihkan_df(df)
            if df is not None and len(df) >= min_baris:
                return df
        except Exception as e:
            log("yf.download %s %s gagal (%d): %s" % (symbol, interval, percobaan + 1, e))
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
            df = bersihkan_df(df)
            if df is not None and len(df) >= min_baris:
                return df
        except Exception as e:
            log("yf.Ticker.history %s %s gagal (%d): %s" % (symbol, interval, percobaan + 1, e))
        time.sleep(3 * (percobaan + 1))
    return None


# ---------------------------------------------------------------------
# INDIKATOR TEKNIKAL (pure pandas): EMA20/50, RSI14, ATR14, ADX14
# ---------------------------------------------------------------------
def hitung_indikator(df):
    d = df.copy()
    d["EMA20"] = d["Close"].ewm(span=20, adjust=False).mean()
    d["EMA50"] = d["Close"].ewm(span=50, adjust=False).mean()

    delta = d["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    d["RSI"] = 100 - (100 / (1 + rs))
    d.loc[(avg_loss == 0) & (avg_gain > 0), "RSI"] = 100.0

    prev_close = d["Close"].shift(1)
    tr = pd.concat([
        d["High"] - d["Low"],
        (d["High"] - prev_close).abs(),
        (d["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["ATR"] = tr.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()

    # ADX 14 (Wilder)
    up = d["High"].diff()
    down = -d["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    pdi = 100 * plus_dm.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean() / d["ATR"]
    mdi = 100 * minus_dm.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean() / d["ATR"]
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, float("nan"))
    d["PDI"] = pdi
    d["MDI"] = mdi
    d["ADX"] = dx.ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()

    d = d.dropna(subset=["EMA20", "EMA50", "RSI", "ATR", "ADX"])
    return d


def ringkas(d):
    """Ringkasan angka indikator candle terakhir."""
    last = d.iloc[-1]
    ema20 = float(last["EMA20"])
    ema50 = float(last["EMA50"])
    rsi = float(last["RSI"])
    atr = float(last["ATR"])
    close = float(last["Close"])
    idx_lama = -6 if len(d) > 6 else 0
    slope = ema20 - float(d["EMA20"].iloc[idx_lama])

    if ema20 > ema50 and slope > 0:
        tren = "NAIK"
    elif ema20 < ema50 and slope < 0:
        tren = "TURUN"
    else:
        tren = "NETRAL"

    if rsi >= 70:
        rsi_status = "OVERBOUGHT"
    elif rsi <= 30:
        rsi_status = "OVERSOLD"
    else:
        rsi_status = "NORMAL"

    atr_rata = float(d["ATR"].tail(50).mean())
    rasio_atr = atr / atr_rata if atr_rata > 0 else 1.0
    gap_atr = abs(ema20 - ema50) / atr if atr > 0 else 0.0

    return {
        "close": close, "ema20": ema20, "ema50": ema50, "rsi": rsi,
        "rsi_status": rsi_status, "atr": atr, "tren": tren,
        "gap": ema20 - ema50, "gap_atr": gap_atr, "rasio_atr": rasio_atr,
        "adx": float(last["ADX"]), "pdi": float(last["PDI"]), "mdi": float(last["MDI"]),
        "high20": float(d["High"].tail(20).max()),
        "low20": float(d["Low"].tail(20).min()),
    }


def swing_terdekat(d, arah, entry, lookback=60):
    """Cari swing low (BUY) / swing high (SELL) fraktal 5 candle terdekat dari harga entry."""
    try:
        sub = d.tail(lookback + 4)
        lows = sub["Low"].tolist()
        highs = sub["High"].tolist()
        n = len(sub)
        for i in range(n - 3, 1, -1):
            if arah == "BUY":
                v = lows[i]
                if v < entry and v == min(lows[i - 2:i + 3]):
                    return float(v)
            else:
                v = highs[i]
                if v > entry and v == max(highs[i - 2:i + 3]):
                    return float(v)
    except Exception as e:
        log("swing_terdekat error: %s" % e)
    return None


# ---------------------------------------------------------------------
# RENDER CHART CANDLESTICK (mplfinance)
# ---------------------------------------------------------------------
def buat_chart(symbol, d15, h):
    """Render chart candlestick 15M + EMA20/50 + garis Entry/SL/TP1/TP (jika sinyal valid)."""
    if not KIRIM_CHART or not HAS_MPL or d15 is None or len(d15) < 20:
        return None
    try:
        sub = d15.tail(65).copy()
        if getattr(sub.index, "tz", None) is not None:
            sub.index = sub.index.tz_convert(ZONA).tz_localize(None)

        ap = [
            mpf.make_addplot(sub["EMA20"], color="#00D2FF", width=1.2),
            mpf.make_addplot(sub["EMA50"], color="#FF9F1C", width=1.2),
        ]

        hlines_val = []
        hlines_col = []
        hlines_sty = []
        if h.get("valid"):
            hlines_val = [h["entry"], h["sl"], h["tp1"], h["tp"]]
            hlines_col = ["#FFFFFF", "#E74C3C", "#3498DB", "#2ECC71"]
            hlines_sty = ["--", "-", ":", "-"]

        mc = mpf.make_marketcolors(
            up="#2ECC71", down="#E74C3C",
            edge="inherit", wick="inherit",
            ohlc="inherit",
        )
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mc,
            gridstyle=":",
            gridcolor="#2A2E39",
            facecolor="#131722",
            figcolor="#131722",
        )

        status_txt = ("%s (%d/100)" % (h["keputusan"], h["skor"])) if h.get("valid") else h.get("keputusan", "WAIT")
        judul = "%s 15M | %s | %s %s" % (
            symbol, status_txt, waktu_lokal().strftime("%d/%m %H:%M"), LABEL_ZONA
        )

        fd, tmp_path = tempfile.mkstemp(prefix="chart_%s_" % symbol, suffix=".png")
        os.close(fd)

        kw_plot = dict(
            type="candle",
            style=style,
            addplot=ap,
            title=judul,
            ylabel="Harga",
            volume=False,
            figsize=(9, 4.8),
            tight_layout=True,
            savefig=dict(fname=tmp_path, dpi=130, bbox_inches="tight"),
        )
        if hlines_val:
            kw_plot["hlines"] = dict(
                hlines=hlines_val,
                colors=hlines_col,
                linestyle=hlines_sty,
                linewidths=1.2,
            )

        mpf.plot(sub, **kw_plot)
        return tmp_path
    except Exception as e:
        log("buat_chart %s error: %s" % (symbol, e))
        return None


# ---------------------------------------------------------------------
# BERITA RSS
# ---------------------------------------------------------------------
def ambil_rss(url):
    now = time.time()
    c = CACHE_BERITA.get(url)
    if c and now - c[0] < 600:
        return c[1]
    judul = []
    try:
        r = requests.get(url, timeout=12, headers=UA)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            t = it.find("title")
            if t is not None and t.text:
                judul.append(t.text.strip())
            if len(judul) >= 15:
                break
    except Exception as e:
        log("RSS gagal (%s): %s" % (url[:50], e))
        if c:
            return c[1]
        return []
    CACHE_BERITA[url] = (now, judul)
    return judul


def ambil_berita(symbol):
    spesifik = []
    try:
        url_ticker = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US" % TICKER_MAP[symbol]
        spesifik += ambil_rss(url_ticker)
        if symbol == "BTCUSD":
            for u in RSS_BTC:
                spesifik += ambil_rss(u)
    except Exception as e:
        log("ambil_berita spesifik error: %s" % e)
    umum = []
    for u in RSS_UMUM:
        try:
            umum += ambil_rss(u)
        except Exception as e:
            log("ambil_berita umum error: %s" % e)
    hasil = []
    for j in spesifik[:5] + umum[:9]:
        if j not in hasil:
            hasil.append(j)
        if len(hasil) >= 12:
            break
    return hasil


# ---------------------------------------------------------------------
# KALENDER EKONOMI (High Impact)
# ---------------------------------------------------------------------
def ambil_kalender():
    now = time.time()
    if KAL["waktu"] and now - KAL["waktu"] < 3600:
        return KAL["data"]
    if now - KAL["gagal"] < 600:
        return KAL["data"]
    try:
        r = requests.get(KALENDER_URL, timeout=12, headers=UA)
        r.raise_for_status()
        raw = r.json()
        data = []
        for e in raw:
            if str(e.get("impact", "")).strip().lower() != "high":
                continue
            tgl = str(e.get("date", "")).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(tgl)
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            data.append({
                "judul": str(e.get("title", "?")),
                "mata_uang": str(e.get("country", "")).upper(),
                "ts": dt.timestamp(),
            })
        KAL["data"] = data
        KAL["waktu"] = now
        log("Kalender ekonomi dimuat: %d event High Impact" % len(data))
    except Exception as e:
        KAL["gagal"] = now
        log("Kalender ekonomi gagal: %s" % e)
    return KAL["data"]


def berita_berbahaya(symbol):
    """Return (event, selisih_menit) jika ada High Impact di jendela blokir, else (None, None)."""
    mata = MATA_UANG.get(symbol, ["USD"])
    now = time.time()
    for e in ambil_kalender():
        if e["mata_uang"] not in mata:
            continue
        m = (e["ts"] - now) / 60.0
        if -NEWS_BLOK_SESUDAH_MENIT <= m <= NEWS_BLOK_SEBELUM_MENIT:
            return e, m
    return None, None


def baris_kalender_prompt(symbol):
    mata = MATA_UANG.get(symbol, ["USD"])
    now = time.time()
    hasil = []
    for e in ambil_kalender():
        if e["mata_uang"] in mata:
            m = (e["ts"] - now) / 60.0
            if 0 <= m <= 360:
                hasil.append("- %s %s (dalam %d menit)" % (e["mata_uang"], e["judul"], int(m)))
    return hasil[:5]


def ket_menit(m):
    if m >= 0:
        return "dalam %d menit" % int(m)
    return "%d menit lalu" % int(abs(m))


def kirim_peringatan_berita():
    if not tujuan_aktif():
        return
    for symbol in WATCHLIST:
        if symbol not in TICKER_MAP or not pasar_buka(symbol):
            continue
        e, m = berita_berbahaya(symbol)
        if not e or m < 0:
            continue
        key = "news:%s|%d" % (e["judul"], int(e["ts"]))
        if not boleh_peringatan(key, 48):
            continue
        kirim("\u26a0\ufe0f PERINGATAN BERITA HIGH IMPACT\n%s: %s\nRilis %s.\n"
              "Pasar ditahan sementara: sinyal baru diblokir sampai %d menit setelah rilis."
              % (e["mata_uang"], e["judul"], ket_menit(m), NEWS_BLOK_SESUDAH_MENIT), WARNA_KUNING)


# ---------------------------------------------------------------------
# GEMINI AI
# ---------------------------------------------------------------------
def get_gemini():
    global GEMINI_CLIENT
    if GEMINI_CLIENT is None:
        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return GEMINI_CLIENT


def parse_json_ai(teks):
    if not teks:
        return None
    t = teks.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    a = t.find("{")
    b = t.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except Exception:
            return None
    return None


def blok_teknikal(nama, r, symbol):
    dg = DESIMAL[symbol]
    return (
        "%s: harga=%.*f, EMA20=%.*f, EMA50=%.*f, selisih EMA/ATR=%.2f, tren=%s, "
        "RSI14=%.1f (%s), ATR14=%.*f, rasio ATR=%.2f, ADX14=%.1f (+DI %.1f / -DI %.1f), "
        "high20=%.*f, low20=%.*f"
        % (nama, dg, r["close"], dg, r["ema20"], dg, r["ema50"], r["gap_atr"], r["tren"],
           r["rsi"], r["rsi_status"], dg, r["atr"], r["rasio_atr"], r["adx"], r["pdi"], r["mdi"],
           dg, r["high20"], dg, r["low20"])
    )


def tanya_gemini(symbol, r1h, r15, berita):
    if GEMINI_API_KEY.startswith("ISI_"):
        return None, "GEMINI_API_KEY belum diisi"
    daftar_berita = "\n".join("- " + b for b in berita) if berita else "- (tidak ada berita berhasil diambil)"
    kal = baris_kalender_prompt(symbol)
    daftar_kal = "\n".join(kal) if kal else "- (tidak ada berita High Impact dalam 6 jam ke depan)"
    prompt = (
        "Kamu adalah analis trading profesional (forex, emas, kripto).\n"
        "Pair: %s\n\n"
        "DATA TEKNIKAL:\n%s\n%s\n\n"
        "KALENDER EKONOMI HIGH IMPACT (6 jam ke depan):\n%s\n\n"
        "HEADLINE BERITA FINANSIAL TERBARU (bahasa Inggris):\n%s\n\n"
        "ATURAN:\n"
        "1. Utamakan tren 1H: BUY hanya jika tren NAIK, SELL hanya jika tren TURUN.\n"
        "2. ADX di atas 22 berarti tren bertenaga; bandingkan +DI dan -DI untuk arah dominan.\n"
        "3. Gunakan 15M untuk momentum entry. Hindari BUY saat RSI overbought dan SELL saat RSI oversold.\n"
        "4. Berita adalah FILTER: jika berita berisiko tinggi atau bertentangan dengan arah teknikal, "
        "pilih WAIT atau turunkan keyakinan. Berita High Impact yang dekat menurunkan keyakinan.\n"
        "5. Jika ragu, pilih WAIT.\n\n"
        "Balas HANYA dengan satu objek JSON valid, tanpa teks lain dan tanpa markdown:\n"
        '{"keputusan": "BUY atau SELL atau WAIT", "skor_keyakinan": angka 1-100, '
        '"alasan_singkat": "maksimal 2 kalimat bahasa Indonesia"}'
        % (symbol, blok_teknikal("TF 1H", r1h, symbol), blok_teknikal("TF 15M", r15, symbol),
           daftar_kal, daftar_berita)
    )
    error_terakhir = "tidak diketahui"
    for percobaan in range(2):
        try:
            client = get_gemini()
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            data = parse_json_ai(getattr(resp, "text", None))
            if not isinstance(data, dict):
                error_terakhir = "respons AI bukan JSON valid"
                time.sleep(3)
                continue
            keputusan = str(data.get("keputusan", "WAIT")).strip().upper()
            if keputusan not in ("BUY", "SELL", "WAIT"):
                keputusan = "WAIT"
            try:
                skor = int(float(data.get("skor_keyakinan", 0)))
            except Exception:
                skor = 0
            skor = max(0, min(100, skor))
            alasan = str(data.get("alasan_singkat", "-")).strip()[:300]
            return {"keputusan": keputusan, "skor": skor, "alasan": alasan}, None
        except Exception as e:
            error_terakhir = str(e)[:200]
            log("Gemini gagal (%d): %s" % (percobaan + 1, error_terakhir))
            time.sleep(4)
    return None, error_terakhir


# ---------------------------------------------------------------------
# MANAJEMEN RISIKO
# ---------------------------------------------------------------------
def nilai_per_unit_harga(symbol, harga):
    """Nilai USD untuk pergerakan 1.0 harga pada 1 lot standar."""
    if symbol == "XAUUSD":
        return 100.0
    if symbol in ("EURUSD", "GBPUSD"):
        return 100000.0
    if symbol == "USDJPY":
        return 100000.0 / harga if harga > 0 else 0.0
    if symbol == "BTCUSD":
        return 1.0
    return 1.0


def hitung_lot(symbol, entry, jarak_sl, modal):
    risiko_target = modal * (RISIKO_PER_TRADE_PERSEN / 100.0)
    nilai = nilai_per_unit_harga(symbol, entry)
    if jarak_sl <= 0 or nilai <= 0:
        return 0.01, 0.0, risiko_target
    lot_mentah = risiko_target / (jarak_sl * nilai)
    lot = math.floor(lot_mentah * 100) / 100.0
    lot = max(0.01, lot)
    risiko_aktual = lot * jarak_sl * nilai
    return lot, risiko_aktual, risiko_target


def hitung_level(arah, entry, atr, d1h):
    """SL di luar swing terdekat (jika jaraknya 1.0-2.5 ATR), selain itu 1.5 ATR. TP = 2R, TP1 = 1R."""
    jarak = 1.5 * atr
    sumber = "1.5x ATR"
    sw = swing_terdekat(d1h, arah, entry)
    if sw is not None:
        if arah == "BUY":
            dist = entry - (sw - 0.25 * atr)
        else:
            dist = (sw + 0.25 * atr) - entry
        if 1.0 * atr <= dist <= 2.5 * atr:
            jarak = dist
            sumber = "di luar swing %s" % ("low" if arah == "BUY" else "high")
    if arah == "BUY":
        sl, tp1, tp = entry - jarak, entry + jarak, entry + 2.0 * jarak
    else:
        sl, tp1, tp = entry + jarak, entry - jarak, entry - 2.0 * jarak
    return sl, tp1, tp, jarak, sumber


# ---------------------------------------------------------------------
# ANALISA LENGKAP
# ---------------------------------------------------------------------
def fmt(symbol, x):
    return "%.*f" % (DESIMAL.get(symbol, 5), x)


def fmt_jarak(symbol, jarak):
    if symbol in ("XAUUSD", "BTCUSD"):
        return "%.2f poin ($%.2f)" % (jarak, jarak)
    return "%s (%.1f pip)" % (fmt(symbol, jarak), jarak / PIP_SIZE.get(symbol, 0.0001))


def tahan(h, alasan):
    h["keputusan"] = "WAIT"
    h["alasan_tahan"] = alasan
    h["alasan"] = alasan
    return h


def analisa_pair(symbol, buat_gambar=False):
    h = {
        "symbol": symbol, "ok": False, "tutup": False, "error": None, "valid": False,
        "keputusan": "WAIT", "skor": 0, "alasan": "-", "alasan_tahan": None,
        "r1h": None, "r15": None, "berita_n": 0, "ai_error": None, "ai_dipanggil": False,
        "chart_path": None,
    }
    try:
        if not pasar_buka(symbol):
            h["tutup"] = True
            h["error"] = "Pasar %s sedang tutup (akhir pekan). Data akan basi, analisa dilewati." % symbol
            return h

        d1h = ambil_data(symbol, "10d", "1h")
        if d1h is None:
            h["error"] = "Gagal mengambil data 1H dari Yahoo Finance (cek koneksi internet)."
            return h
        d15 = ambil_data(symbol, "5d", "15m")
        if d15 is None:
            h["error"] = "Gagal mengambil data 15M dari Yahoo Finance (cek koneksi internet)."
            return h

        i1h = hitung_indikator(d1h)
        i15 = hitung_indikator(d15)
        if len(i1h) < 10 or len(i15) < 10:
            h["error"] = "Data candle tidak cukup untuk menghitung indikator (pasar mungkin tutup)."
            return h

        r1h = ringkas(i1h)
        r15 = ringkas(i15)
        h["r1h"] = r1h
        h["r15"] = r15
        h["ok"] = True

        # --- Pre-filter Python (hemat kuota Gemini) ---
        if r1h["gap_atr"] < 0.3:
            tahan(h, "EMA20 dan EMA50 (1H) terlalu rapat, pasar sideways.")
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if r1h["adx"] < ADX_MIN:
            tahan(h, "ADX 1H %.1f di bawah %d, tren belum bertenaga." % (r1h["adx"], ADX_MIN))
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if r15["rasio_atr"] < 0.6:
            tahan(h, "Volatilitas 15M sangat rendah, tidak ada momentum.")
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if r1h["tren"] == "NETRAL":
            tahan(h, "Arah tren 1H belum jelas (netral).")
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if r1h["tren"] != r15["tren"]:
            tahan(h, "Tren 1H (%s) dan 15M (%s) belum searah." % (r1h["tren"], r15["tren"]))
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if not (RSI_AMAN_MIN <= r15["rsi"] <= RSI_AMAN_MAX):
            tahan(h, "RSI 15M %.1f di area jenuh (aman %d-%d)." % (r15["rsi"], RSI_AMAN_MIN, RSI_AMAN_MAX))
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Blokir berita High Impact ---
        ev, mnt = berita_berbahaya(symbol)
        if ev:
            tahan(h, "Menjelang/sesudah berita High Impact: %s %s (%s). Pasar ditahan sementara."
                  % (ev["mata_uang"], ev["judul"], ket_menit(mnt)))
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Berita RSS + AI ---
        berita = ambil_berita(symbol)
        h["berita_n"] = len(berita)
        h["ai_dipanggil"] = True
        ai, err = tanya_gemini(symbol, r1h, r15, berita)
        if ai is None:
            h["ai_error"] = err
            h["alasan_tahan"] = "AI tidak tersedia: %s" % err
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        h["keputusan"] = ai["keputusan"]
        h["skor"] = ai["skor"]
        h["alasan"] = ai["alasan"]

        if h["keputusan"] == "BUY" and r1h["tren"] != "NAIK":
            h["alasan_tahan"] = "BUY bertentangan dengan tren 1H."
            h["keputusan"] = "WAIT"
        elif h["keputusan"] == "SELL" and r1h["tren"] != "TURUN":
            h["alasan_tahan"] = "SELL bertentangan dengan tren 1H."
            h["keputusan"] = "WAIT"

        if h["keputusan"] == "WAIT":
            if not h["alasan_tahan"]:
                h["alasan_tahan"] = "AI merekomendasikan WAIT."
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if h["skor"] < MIN_KEYAKINAN:
            h["alasan_tahan"] = "Keyakinan %d < batas %d." % (h["skor"], MIN_KEYAKINAN)
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Sinyal valid: Entry / SL / TP1 / TP / Lot ---
        entry = r15["close"]
        atr = r1h["atr"]
        sl, tp1, tp, jarak_sl, sumber = hitung_level(h["keputusan"], entry, atr, i1h)
        with STATE_LOCK:
            modal = MODAL_AWAL_USD
        lot, risiko_aktual, risiko_target = hitung_lot(symbol, entry, jarak_sl, modal)

        h["valid"] = True
        h["entry"] = entry
        h["sl"] = sl
        h["tp1"] = tp1
        h["tp"] = tp
        h["jarak"] = jarak_sl
        h["sumber_sl"] = sumber
        h["lot"] = lot
        h["risiko_aktual"] = risiko_aktual
        h["risiko_target"] = risiko_target
        h["modal"] = modal
        # Sinyal valid selalu buat chart jika KIRIM_CHART aktif
        h["chart_path"] = buat_chart(symbol, i15, h)
        return h
    except Exception as e:
        log("analisa_pair %s error: %s" % (symbol, e))
        h["ok"] = False
        h["error"] = "Terjadi kesalahan saat analisa: %s" % str(e)[:150]
        return h


def format_pesan(h):
    s = h["symbol"]
    if h.get("tutup"):
        return "\U0001f512 %s" % h["error"]
    if not h["ok"]:
        return "\u274c Analisa %s gagal.\n%s" % (s, h["error"])

    r1h = h["r1h"]
    r15 = h["r15"]
    waktu = waktu_lokal().strftime("%d/%m/%Y %H:%M") + " " + LABEL_ZONA
    b = []
    b.append("\U0001f4ca ANALISA %s  (%s)" % (s, waktu))
    b.append("Harga: %s" % fmt(s, r15["close"]))
    b.append("")
    b.append("TEKNIKAL 1H (tren utama)")
    b.append("Tren: %s | EMA20 %s | EMA50 %s" % (r1h["tren"], fmt(s, r1h["ema20"]), fmt(s, r1h["ema50"])))
    b.append("RSI: %.1f (%s) | ATR: %s | ADX: %.1f" % (r1h["rsi"], r1h["rsi_status"], fmt(s, r1h["atr"]), r1h["adx"]))
    b.append("")
    b.append("TEKNIKAL 15M (momentum)")
    b.append("Tren: %s | RSI: %.1f (%s)" % (r15["tren"], r15["rsi"], r15["rsi_status"]))
    b.append("")

    if not h["ai_dipanggil"]:
        b.append("\u23f3 SINYAL DITAHAN (WAIT)")
        b.append(h["alasan_tahan"] or h["alasan"])
        b.append("(AI tidak dipanggil, kuota hemat)")
    else:
        b.append("\U0001f916 AI GEMINI: %s | Keyakinan %d/100" % (h["keputusan"], h["skor"]))
        if h["ai_error"]:
            b.append("AI error: %s" % h["ai_error"])
        else:
            b.append("Alasan: %s" % h["alasan"])
            b.append("(berita dibaca: %d headline)" % h["berita_n"])
        b.append("")
        if h["valid"]:
            arah = h["keputusan"]
            j = h["jarak"]
            b.append("\U0001f3af SINYAL %s VALID" % arah)
            b.append("Entry : %s" % fmt(s, h["entry"]))
            b.append("SL    : %s" % fmt(s, h["sl"]))
            b.append("TP1   : %s  (1R, geser SL ke Break Even)" % fmt(s, h["tp1"]))
            b.append("TP    : %s  (RR 1:2)" % fmt(s, h["tp"]))
            b.append("Jarak SL: %s | Jarak TP: %s" % (fmt_jarak(s, j), fmt_jarak(s, 2 * j)))
            b.append("Dasar SL: %s" % h["sumber_sl"])
            b.append("Lot aman: %.2f" % h["lot"])
            b.append("Risiko jika SL kena: $%.2f (target $%.2f dari modal $%.2f)"
                     % (h["risiko_aktual"], h["risiko_target"], h["modal"]))
            if h["risiko_aktual"] > h["risiko_target"] * 1.5:
                b.append("\u26a0\ufe0f Lot minimum 0.01 membuat risiko lebih besar dari target. "
                         "Pertimbangkan akun cent / modal lebih besar, atau lewati sinyal ini.")
            if s == "XAUUSD":
                tanda = "-" if arah == "BUY" else "+"
                tanda_tp = "+" if arah == "BUY" else "-"
                b.append("")
                b.append("\u26a0\ufe0f Harga emas di sini dari Gold Futures (GC=F), bisa beda $5-$25 dari spot broker. "
                         "Jangan salin harga mentah. Ambil harga LIVE di broker, lalu pasang: "
                         "SL = live %s %.2f | TP1 = live %s %.2f | TP = live %s %.2f"
                         % (tanda, j, tanda_tp, j, tanda_tp, 2 * j))
        else:
            b.append("\u23f3 SINYAL DITAHAN: %s" % (h["alasan_tahan"] or "tidak memenuhi syarat."))

    b.append("")
    b.append("Bukan nasihat keuangan. Selalu pasang SL.")
    return "\n".join(b)


# ---------------------------------------------------------------------
# PELACAK SINYAL & PERFORMA
# ---------------------------------------------------------------------
def catat_sinyal(h):
    """Catat sinyal valid sebagai posisi terbuka. Return (id, baru)."""
    with STATE_LOCK:
        for p in POSISI:
            if p["symbol"] == h["symbol"] and p["arah"] == h["keputusan"] and p["status"] == "OPEN":
                return p["id"], False
        pid = str(int(time.time()))
        while any(p["id"] == pid for p in POSISI):
            pid = str(int(pid) + 1)
        POSISI.append({
            "id": pid, "symbol": h["symbol"], "arah": h["keputusan"],
            "entry": h["entry"], "sl": h["sl"], "tp1": h["tp1"], "tp": h["tp"],
            "waktu": time.time(), "status": "OPEN", "hasil": None,
            "tp1_kena": False, "diambil": False,
        })
        LAST_SIGNAL[h["symbol"]] = {"arah": h["keputusan"], "waktu": time.time(), "entry": h["entry"]}
    simpan_state()
    return pid, True


def tandai_diambil(pid):
    with STATE_LOCK:
        for p in POSISI:
            if p["id"] == pid:
                p["diambil"] = True
                break
        else:
            return False
    simpan_state()
    return True


def evaluasi_posisi(p, df):
    """Telusuri candle 15M sejak sinyal. SL dianggap kena lebih dulu jika SL & TP di candle sama."""
    hasil = {"hasil": None, "tp1": False}
    if df is None:
        return hasil
    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        t0 = pd.Timestamp(p["waktu"], unit="s", tz="UTC")
        sub = df[idx >= t0]
        for _, r in sub.iterrows():
            hi = float(r["High"])
            lo = float(r["Low"])
            if p["arah"] == "BUY":
                if lo <= p["sl"]:
                    hasil["hasil"] = "SL"
                    break
                if hi >= p["tp"]:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    break
                if hi >= p["tp1"]:
                    hasil["tp1"] = True
            else:
                if hi >= p["sl"]:
                    hasil["hasil"] = "SL"
                    break
                if lo <= p["tp"]:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    break
                if lo <= p["tp1"]:
                    hasil["tp1"] = True
    except Exception as e:
        log("evaluasi_posisi error: %s" % e)
    return hasil


def cek_posisi():
    if not tujuan_aktif():
        return
    with STATE_LOCK:
        terbuka = [dict(p) for p in POSISI if p["status"] == "OPEN"]
    if not terbuka:
        return
    per_symbol = {}
    for p in terbuka:
        per_symbol.setdefault(p["symbol"], []).append(p)

    for symbol, daftar in per_symbol.items():
        df = None
        if symbol in TICKER_MAP and pasar_buka(symbol):
            df = ambil_data(symbol, "5d", "15m", min_baris=10)
        for p in daftar:
            ev = evaluasi_posisi(p, df)
            pesan = None
            with STATE_LOCK:
                real = None
                for x in POSISI:
                    if x["id"] == p["id"]:
                        real = x
                        break
                if real is None or real["status"] != "OPEN":
                    continue
                tp1_baru = ev["tp1"] and not real.get("tp1_kena")
                if ev["tp1"]:
                    real["tp1_kena"] = True
                if ev["hasil"]:
                    real["status"] = "CLOSED"
                    real["hasil"] = ev["hasil"]
                    real["tutup"] = time.time()
                    if ev["hasil"] == "TP":
                        pesan = "\u2705 TP TERCAPAI (+2R)\n%s %s | Entry %s -> TP %s" % (
                            real["symbol"], real["arah"], fmt(symbol, real["entry"]), fmt(symbol, real["tp"]))
                    else:
                        pesan = "\u274c SL KENA (-1R)\n%s %s | Entry %s -> SL %s" % (
                            real["symbol"], real["arah"], fmt(symbol, real["entry"]), fmt(symbol, real["sl"]))
                        if real.get("tp1_kena"):
                            pesan += "\n(Sempat menyentuh TP1. Jika SL sudah digeser ke BE, hasilnya impas.)"
                elif time.time() - real["waktu"] > POSISI_KADALUARSA_JAM * 3600:
                    real["status"] = "CLOSED"
                    real["hasil"] = "EXPIRED"
                    real["tutup"] = time.time()
                    pesan = "\u231b Sinyal %s %s kedaluwarsa (tidak kena TP/SL dalam %d jam)." % (
                        real["symbol"], real["arah"], POSISI_KADALUARSA_JAM)
                elif tp1_baru:
                    pesan = ("\U0001f514 TP1 TERSENTUH\n%s %s | TP1 %s\n"
                             "Saran: geser SL ke Break Even (%s)." % (
                                 real["symbol"], real["arah"], fmt(symbol, real["tp1"]),
                                 fmt(symbol, real["entry"])))
            if pesan:
                simpan_state()
                kirim(pesan, warna_hasil(pesan))


def statistik(daftar):
    tp = sum(1 for p in daftar if p.get("hasil") == "TP")
    sl = sum(1 for p in daftar if p.get("hasil") == "SL")
    n = tp + sl
    wr = (tp * 100.0 / n) if n else 0.0
    r = tp * 2.0 - sl * 1.0
    return n, tp, sl, wr, r


def teks_performa():
    with STATE_LOCK:
        selesai = [p for p in POSISI if p.get("status") == "CLOSED" and p.get("hasil") in ("TP", "SL")]
        terbuka = [p for p in POSISI if p.get("status") == "OPEN"]
    b = ["\U0001f4c8 PERFORMA SINYAL BOT", ""]
    if not selesai:
        b.append("Belum ada sinyal yang selesai (kena TP/SL).")
    else:
        n, tp, sl, wr, r = statistik(selesai)
        b.append("Semua sinyal selesai: %d (TP %d | SL %d)" % (n, tp, sl))
        b.append("Win rate: %.1f%% | Total: %+.1fR" % (wr, r))
        sl_tp1 = sum(1 for p in selesai if p.get("hasil") == "SL" and p.get("tp1_kena"))
        if sl_tp1:
            b.append("SL yang sempat menyentuh TP1 dulu: %d" % sl_tp1)
        diambil = [p for p in selesai if p.get("diambil")]
        if diambil:
            n2, tp2, sl2, wr2, r2 = statistik(diambil)
            b.append("")
            b.append("Yang kamu tandai diambil: %d (TP %d | SL %d)" % (n2, tp2, sl2))
            b.append("Win rate: %.1f%% | Total: %+.1fR" % (wr2, r2))
        pairs = sorted(set(p["symbol"] for p in selesai))
        if pairs:
            b.append("")
            b.append("Per pair:")
            for pr in pairs:
                n3, tp3, sl3, wr3, r3 = statistik([p for p in selesai if p["symbol"] == pr])
                b.append("- %s: %d sinyal | WR %.0f%% | %+.1fR" % (pr, n3, wr3, r3))
    b.append("")
    if terbuka:
        b.append("Posisi terbuka (%d):" % len(terbuka))
        for p in terbuka:
            tambahan = " [TP1 kena]" if p.get("tp1_kena") else ""
            b.append("- %s %s | Entry %s | SL %s | TP %s%s" % (
                p["symbol"], p["arah"], fmt(p["symbol"], p["entry"]),
                fmt(p["symbol"], p["sl"]), fmt(p["symbol"], p["tp"]), tambahan))
    else:
        b.append("Tidak ada posisi terbuka.")
    b.append("")
    b.append("Catatan: dihitung dari harga Yahoo Finance (15M). Jika TP dan SL tersentuh di candle yang sama, "
             "dihitung SL. Hasil di broker bisa berbeda.")
    return "\n".join(b)


# ---------------------------------------------------------------------
# SCANNER (satu putaran per run)
# ---------------------------------------------------------------------
def petunjuk_ambil(pid):
    if MODE == "bot":
        return "\n\nSudah kamu ambil entry ini? Ketik %sambil %s" % (PREFIX, pid)
    return ""


def scan_sekali(paksa=False):
    """Pindai watchlist satu putaran. Return jumlah notifikasi terkirim."""
    global SCAN_TERAKHIR
    terkirim = 0
    dicoba = 0
    gagal_data = 0
    log("Mulai scan watchlist...")
    try:
        kirim_peringatan_berita()
    except Exception as e:
        log("peringatan berita error: %s" % e)
    for symbol in WATCHLIST:
        with STATE_LOCK:
            aktif = SCAN_AKTIF
        if not tujuan_aktif() or (not aktif and not paksa):
            break
        if symbol not in TICKER_MAP:
            continue
        if not pasar_buka(symbol):
            log("Scan %s dilewati: pasar tutup." % symbol)
            continue
        try:
            h = analisa_pair(symbol, buat_gambar=False)
            dicoba += 1
            if (not h["ok"]) and "Gagal mengambil data" in str(h.get("error")):
                gagal_data += 1
            if not h["ok"]:
                log("Scan %s: %s" % (symbol, h["error"]))
            elif not h["valid"]:
                log("Scan %s: tidak ada sinyal (%s)" % (symbol, h["alasan_tahan"] or h["keputusan"]))
            else:
                arah = h["keputusan"]
                boleh = True
                with STATE_LOCK:
                    prev = LAST_SIGNAL.get(symbol)
                    if prev and prev.get("arah") == arah and \
                            (time.time() - float(prev.get("waktu", 0))) < COOLDOWN_JAM * 3600:
                        boleh = False
                if not boleh:
                    log("Scan %s: sinyal %s sama, ditahan cooldown." % (symbol, arah))
                    if h.get("chart_path") and os.path.exists(h["chart_path"]):
                        os.remove(h["chart_path"])
                else:
                    pid, baru = catat_sinyal(h)
                    if baru:
                        kirim(
                            "\U0001f6a8 SINYAL OTOMATIS\n\n" + format_pesan(h) + petunjuk_ambil(pid),
                            warna_arah(arah),
                            chart_path=h.get("chart_path"),
                        )
                        terkirim += 1
                        log("Notifikasi %s %s terkirim." % (symbol, arah))
                    else:
                        log("Scan %s: posisi %s masih terbuka, tidak dikirim ulang." % (symbol, arah))
                        if h.get("chart_path") and os.path.exists(h["chart_path"]):
                            os.remove(h["chart_path"])
        except Exception as e:
            log("Scan %s error: %s" % (symbol, e))
        time.sleep(4)
    SCAN_TERAKHIR = time.time()
    if dicoba > 0 and gagal_data == dicoba and boleh_peringatan("data_gagal", 6):
        kirim("\u26a0\ufe0f Semua pair gagal diambil datanya dari Yahoo Finance. "
              "Kemungkinan koneksi bermasalah atau Yahoo membatasi IP (umum terjadi di server cloud). "
              "Bot akan mencoba lagi di jadwal berikutnya.", WARNA_KUNING)
    log("Scan selesai.")
    return terkirim


def atur_radar(nyala):
    global SCAN_AKTIF
    with STATE_LOCK:
        SCAN_AKTIF = nyala
    simpan_state()


# ---------------------------------------------------------------------
# AKSI & PERINTAH
# ---------------------------------------------------------------------
def normalisasi_pair(teks):
    pair = re.sub(r"[^A-Z]", "", str(teks).upper())
    return ALIAS_PAIR.get(pair, pair)


def jalankan_analisa(pair):
    kirim("\u23f3 Menganalisa %s (teknikal + chart + kalender + berita + AI)... tunggu sebentar." % pair)
    h = analisa_pair(pair, buat_gambar=True)
    teks = format_pesan(h)
    if h["ok"] and h["valid"]:
        pid, _ = catat_sinyal(h)
        teks += petunjuk_ambil(pid)
        warna = warna_arah(h["keputusan"])
    elif h["ok"] or h.get("tutup"):
        warna = WARNA_ABU
    else:
        warna = WARNA_KUNING
    kirim(teks, warna, chart_path=h.get("chart_path"))


def teks_status():
    with STATE_LOCK:
        m = MODAL_AWAL_USD
        aktif = SCAN_AKTIF
        sinyal = dict(LAST_SIGNAL)
        n_buka = sum(1 for p in POSISI if p.get("status") == "OPEN")
        admin = ADMIN_USER_ID
    b = [
        "\u2699\ufe0f STATUS",
        "Mode: %s | Admin ID: %s" % (MODE, admin if admin else "belum ada"),
        "Modal: $%.2f" % m,
        "Risiko/trade: %.1f%% (= $%.2f)" % (RISIKO_PER_TRADE_PERSEN, m * RISIKO_PER_TRADE_PERSEN / 100.0),
        "Radar: %s | Chart: %s" % ("AKTIF" if aktif else "MATI", "AKTIF" if (KIRIM_CHART and HAS_MPL) else "MATI"),
        "Watchlist: %s" % ", ".join(WATCHLIST),
        "Model AI: %s | Min keyakinan: %d" % (GEMINI_MODEL, MIN_KEYAKINAN),
        "Filter: ADX >= %d | RSI 15M %d-%d" % (ADX_MIN, RSI_AMAN_MIN, RSI_AMAN_MAX),
        "Blokir berita: -%d / +%d menit" % (NEWS_BLOK_SEBELUM_MENIT, NEWS_BLOK_SESUDAH_MENIT),
        "Posisi terlacak terbuka: %d" % n_buka,
    ]
    if sinyal:
        b.append("")
        b.append("Sinyal terakhir:")
        for k, v in sinyal.items():
            try:
                b.append("- %s %s (%s)" % (k, v["arah"], waktu_lokal(float(v["waktu"])).strftime("%d/%m %H:%M") + " " + LABEL_ZONA))
            except Exception:
                pass
    return "\n".join(b)


def teks_bantuan():
    return (
        "\U0001f916 FOREX AI BOT (Discord)\n\n"
        "Perintah:\n"
        "{p}analisa <PAIR>  - analisa lengkap + chart (contoh: {p}analisa XAUUSD)\n"
        "{p}scan on | off | now  - radar otomatis (now = pindai sekarang)\n"
        "{p}modal <USD>  - ubah modal (contoh: {p}modal 500)\n"
        "{p}performa  - statistik win rate sinyal\n"
        "{p}status  - pengaturan saat ini\n"
        "{p}ambil <ID>  - tandai sinyal yang kamu ambil\n"
        "{p}help  - panduan ini\n\n"
        "Pair: {pairs}\n"
        "Hanya sinyal berkeyakinan >= {mink} yang dikirim. Sinyal diblokir menjelang berita "
        "High Impact dan saat pasar tutup.\n"
        "Risiko per trade: {risk:.1f}% dari modal.\n\n"
        "Mode GitHub: perintah dibalas saat jadwal berikutnya berjalan, jadi bisa tertunda beberapa menit."
    ).format(p=PREFIX, pairs=", ".join(TICKER_MAP.keys()), mink=MIN_KEYAKINAN,
             risk=RISIKO_PER_TRADE_PERSEN)


def tangani_perintah(uid, isi):
    global ADMIN_USER_ID, MODAL_AWAL_USD
    bagian = isi.split()
    if not bagian:
        return
    perintah = bagian[0].lower()
    args = bagian[1:]

    if perintah in ("start", "help", "menu", "bantuan"):
        baru = False
        with STATE_LOCK:
            if ADMIN_USER_ID == 0:
                ADMIN_USER_ID = uid
                baru = True
            admin = ADMIN_USER_ID
        if baru:
            simpan_state()
            log("ADMIN_USER_ID disimpan: %s" % admin)
        if uid != admin:
            kirim("Bot ini privat.")
            return
        awal = "\u2705 Kamu (ID %s) tercatat sebagai admin bot.\n\n" % uid if baru else ""
        kirim(awal + teks_bantuan())
        return

    with STATE_LOCK:
        admin = ADMIN_USER_ID
    if admin == 0:
        kirim("Ketik %sstart dulu untuk mengaktifkan bot." % PREFIX)
        return
    if uid != admin:
        kirim("Bot ini privat.")
        return

    if perintah == "analisa":
        if not args:
            kirim("Format: %sanalisa <PAIR>\nContoh: %sanalisa XAUUSD\nPair: %s"
                  % (PREFIX, PREFIX, ", ".join(TICKER_MAP.keys())))
            return
        pair = normalisasi_pair(args[0])
        if pair not in TICKER_MAP:
            kirim("Pair '%s' tidak dikenal.\nPair tersedia: %s" % (args[0], ", ".join(TICKER_MAP.keys())))
            return
        jalankan_analisa(pair)

    elif perintah == "scan":
        sub = args[0].lower() if args else ""
        if sub in ("on", "off"):
            atur_radar(sub == "on")
            kirim("\u2705 Radar AKTIF." if sub == "on" else "\U0001f6d1 Radar dimatikan.")
        elif sub == "now":
            kirim("\u23f3 Memindai watchlist... bisa makan waktu 1-2 menit.")
            n = scan_sekali(paksa=True)
            kirim("\u2705 Scan selesai. Sinyal baru: %d" % n)
        else:
            with STATE_LOCK:
                status = "AKTIF" if SCAN_AKTIF else "MATI"
            kirim("Radar saat ini: %s\nGunakan %sscan on, %sscan off, atau %sscan now" % (status, PREFIX, PREFIX, PREFIX))

    elif perintah == "modal":
        if not args:
            with STATE_LOCK:
                m = MODAL_AWAL_USD
            kirim("Modal saat ini: $%.2f\nFormat: %smodal <JUMLAH_USD>  (contoh: %smodal 500)" % (m, PREFIX, PREFIX))
            return
        try:
            nilai = float(args[0].replace(",", "."))
        except ValueError:
            kirim("Jumlah tidak valid. Contoh: %smodal 500" % PREFIX)
            return
        if nilai <= 0 or nilai > 1000000000:
            kirim("Jumlah harus lebih dari 0.")
            return
        with STATE_LOCK:
            MODAL_AWAL_USD = nilai
        simpan_state()
        kirim("\u2705 Modal diubah menjadi $%.2f.\nRisiko per trade: $%.2f (%.1f%%)."
              % (nilai, nilai * RISIKO_PER_TRADE_PERSEN / 100.0, RISIKO_PER_TRADE_PERSEN))

    elif perintah == "performa":
        kirim(teks_performa())

    elif perintah == "status":
        kirim(teks_status())

    elif perintah == "ambil":
        if not args:
            kirim("Format: %sambil <ID sinyal>" % PREFIX)
            return
        ok = tandai_diambil(args[0])
        kirim("\u2705 Sinyal %s ditandai sudah kamu ambil." % args[0] if ok else "ID sinyal tidak ditemukan.")

    else:
        kirim("Perintah tidak dikenal. Ketik %shelp untuk panduan." % PREFIX)


def proses_perintah():
    """Baca pesan baru di channel dan jalankan perintah dari admin."""
    baseline = not PESAN_TERAKHIR_ID
    pesan = ambil_pesan_baru()
    if pesan is None:
        log("Gagal membaca channel. Cek token bot, ID channel, dan izin bot "
            "(View Channel, Read Message History, Send Messages, Attach Files).")
        return
    n = 0
    kosong = 0
    for m in pesan:
        try:
            penulis = m.get("author") or {}
            if penulis.get("bot"):
                continue
            teks = str(m.get("content") or "").strip()
            if not teks:
                if not m.get("attachments") and not m.get("embeds"):
                    kosong += 1
                continue
            if not teks.startswith(PREFIX):
                continue
            tangani_perintah(int(penulis["id"]), teks[len(PREFIX):])
            n += 1
        except Exception as e:
            log("Gagal memproses pesan: %s" % e)
    log("%d perintah diproses." % n)
    if baseline and n == 0:
        kirim("\u2705 Bot terhubung ke channel ini.\nKetik %sstart untuk mencatat dirimu sebagai admin "
              "dan melihat daftar perintah. Balasan datang saat jadwal berikutnya berjalan." % PREFIX)
    if kosong and n == 0 and boleh_peringatan("intent", 6):
        kirim("\u26a0\ufe0f Bot menerima pesan tanpa isi. Aktifkan Message Content Intent di Discord "
              "Developer Portal (menu Bot), lalu coba kirim perintah lagi.", WARNA_KUNING)


# ---------------------------------------------------------------------
# SATU PUTARAN RUN
# ---------------------------------------------------------------------
def jalankan_sekali():
    global SUDAH_SAPA
    muat_state()
    if MODE == "bot":
        proses_perintah()
    elif not SUDAH_SAPA:
        kirim("\u2705 Forex AI Bot aktif (mode webhook: hanya notifikasi). "
              "Pengaturan diubah lewat file konfigurasi di repo.")
        SUDAH_SAPA = True
    cek_posisi()
    with STATE_LOCK:
        aktif = SCAN_AKTIF
    if aktif and time.time() - SCAN_TERAKHIR > 120:
        scan_sekali()
    simpan_state()
    log("Run selesai.")


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
if __name__ == "__main__":
    if MODE is None:
        print("Konfigurasi Discord belum lengkap. Isi DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID, "
              "atau DISCORD_WEBHOOK_URL.")
        sys.exit(1)
    if GEMINI_API_KEY.startswith("ISI_"):
        print("PERINGATAN: GEMINI_API_KEY belum diisi. Analisa AI tidak akan berjalan.")
    log("Mode Discord: %s | Chart aktif: %s" % (MODE, KIRIM_CHART and HAS_MPL))

    if "--loop" in sys.argv:
        while True:
            try:
                jalankan_sekali()
            except Exception as e:
                log("Run error: %s" % e)
            time.sleep(max(1, INTERVAL_SCAN_MENIT) * 60)
    else:
        jalankan_sekali()
