# -*- coding: utf-8 -*-
"""
forex_ai_bot_discord.py
Bot Discord: Teknikal (pandas) + Chart Candlestick (mplfinance) + Berita + Kalender Ekonomi + Keputusan AI (Gemini 3.8 Flash)
Dirancang untuk GitHub Actions: sekali jalan per jadwal (python forex_ai_bot_discord.py --once).
Memakai REST API Discord lewat requests, tanpa library discord.py, jadi ringan dan langsung berhenti setelah selesai.

Dua mode (dipilih otomatis):
  BOT     : DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID. Notifikasi + perintah berawalan tanda seru.
  WEBHOOK : DISCORD_WEBHOOK_URL saja. Hanya notifikasi, tanpa perintah.

V4.3 (Python 3.11+ Type-Safe & Bugfix Release):
  - Fix evaluasi_posisi(): candle entry aktif hanya dicek via harga Close (mencegah false SL/TP dari wick sebelum sinyal)
  - Fix konfirmasi_struktur_15m(): validasi body & momentum memakai candle 15M yang sudah tutup (iloc[-2]), bukan candle baru buka
  - Fix analisa_pair(): tambah filter umur data 15M (cek_data_basi) untuk mencegah analisa saat CME break / hari libur pasar
  - Fix buat_chart(): lokalisasi index waktu ke UTC terlebih dahulu sebelum konversi ke zona waktu lokal (WIB/WITA/WIT)
  - Type annotations lengkap Python 3.11+, pathlib.Path, dan penanganan error terstruktur

Library: yfinance, pandas, requests, google-genai, mplfinance, matplotlib
"""

from __future__ import annotations

# =====================================================================
# KONFIGURASI (EDIT DI SINI)
# Untuk GitHub Actions, isi token/key lewat Secrets, jangan ditulis di file ini.
# =====================================================================
DISCORD_BOT_TOKEN: str = "ISI_TOKEN_BOT_DISCORD"
DISCORD_CHANNEL_ID: str = "ISI_ID_CHANNEL"
DISCORD_WEBHOOK_URL: str = ""          # opsional: mode hanya-notifikasi tanpa bot
GEMINI_API_KEY: str = "ISI_API_KEY_GEMINI"
ADMIN_USER_ID: int = 0                 # 0 = orang pertama yang mengetik !start jadi admin
PREFIX: str = "!"
MODAL_AWAL_USD: float = 100.0
RISIKO_PER_TRADE_PERSEN: float = 1.0
INTERVAL_SCAN_MENIT: int = 5           # dipakai hanya jika dijalankan dengan --loop
WATCHLIST: list[str] = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"]
ZONA_WAKTU_JAM: int = 7                # jam di pesan: 7 = WIB, 8 = WITA, 9 = WIT

# Pengaturan tambahan (opsional)
MIN_KEYAKINAN: int = 75                # sinyal ditahan jika skor < nilai ini
COOLDOWN_JAM: int = 4                  # jangan kirim ulang sinyal arah sama dalam X jam
GEMINI_MODEL: str = "gemini-3.8-flash"
ADX_MIN: int = 22                      # tren dianggap bertenaga jika ADX 1H >= nilai ini
RSI_AMAN_MIN: int = 32                 # RSI 15M harus di antara MIN dan MAX agar AI dipanggil
RSI_AMAN_MAX: int = 68
NEWS_BLOK_SEBELUM_MENIT: int = 45      # blokir sinyal X menit sebelum berita High Impact
NEWS_BLOK_SESUDAH_MENIT: int = 15      # ...dan X menit sesudah rilis
POSISI_KADALUARSA_JAM: int = 48        # sinyal yang tidak kena TP/SL dalam X jam dianggap kedaluwarsa
MAX_UMUR_DATA_MENIT: int = 45          # lewati analisa jika candle 15M terakhir lebih tua dari X menit
KIRIM_CHART: bool = True               # lampirkan gambar candlestick pada hasil analisa / sinyal valid
# =====================================================================

import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    import yfinance as yf
    from google import genai
    from google.genai import types as gtypes
except ImportError as e_imp:
    print(f"Library belum terinstal: {e_imp}")
    print("Pasang: yfinance pandas requests google-genai mplfinance matplotlib")
    sys.exit(1)

MPL_ERROR: str | None = None
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    import mplfinance as mpf

    HAS_MPL = True
except Exception as e_mpl:
    HAS_MPL = False
    MPL_ERROR = str(e_mpl)


# ---------------------------------------------------------------------
# KONFIGURASI DARI ENVIRONMENT (GitHub Secrets). Jika kosong, nilai di atas dipakai.
# ---------------------------------------------------------------------
def _env(nama: str, default: str) -> str:
    """Ambil nilai environment variable dengan fallback nilai default."""
    return (os.environ.get(nama) or default).strip()


DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN)
DISCORD_CHANNEL_ID = _env("DISCORD_CHANNEL_ID", DISCORD_CHANNEL_ID)
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
GEMINI_API_KEY = _env("GEMINI_API_KEY", GEMINI_API_KEY)

ENV_MODAL_RAW: str = os.environ.get("MODAL_AWAL_USD", "").strip()
ENV_WATCHLIST_RAW: str = os.environ.get("WATCHLIST", "").strip()

try:
    if os.environ.get("ADMIN_USER_ID", "").strip():
        ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"].strip())
    if ENV_MODAL_RAW:
        MODAL_AWAL_USD = float(ENV_MODAL_RAW)
    if os.environ.get("RISIKO_PER_TRADE_PERSEN", "").strip():
        RISIKO_PER_TRADE_PERSEN = float(os.environ["RISIKO_PER_TRADE_PERSEN"])
    if ENV_WATCHLIST_RAW:
        WATCHLIST = [x.strip().upper() for x in ENV_WATCHLIST_RAW.split(",") if x.strip()]
    if os.environ.get("ZONA_WAKTU_JAM", "").strip():
        ZONA_WAKTU_JAM = int(os.environ["ZONA_WAKTU_JAM"])
except ValueError:
    print("PERINGATAN: ada nilai environment yang tidak valid, memakai konfigurasi di file.")


def tentukan_mode() -> str | None:
    """Tentukan mode operasi Discord ('bot' atau 'webhook') dari kredensial yang tersedia."""
    if (
        re.fullmatch(r"\d{15,25}", DISCORD_CHANNEL_ID)
        and len(DISCORD_BOT_TOKEN) > 30
        and not DISCORD_BOT_TOKEN.startswith("ISI_")
    ):
        return "bot"
    if DISCORD_WEBHOOK_URL.startswith((
        "https://discord.com/api/webhooks/",
        "https://discordapp.com/api/webhooks/",
    )):
        return "webhook"
    return None


MODE: str | None = tentukan_mode()

ZONA: timezone = timezone(timedelta(hours=ZONA_WAKTU_JAM))
LABEL_ZONA: str = {7: "WIB", 8: "WITA", 9: "WIT"}.get(ZONA_WAKTU_JAM, f"UTC{ZONA_WAKTU_JAM:+d}")


def waktu_lokal(ts: float | None = None) -> datetime:
    """Konversi epoch timestamp (atau waktu saat ini) ke objek datetime zona lokal."""
    if ts is None:
        return datetime.now(ZONA)
    return datetime.fromtimestamp(ts, ZONA)


# ---------------------------------------------------------------------
# DATA PAIR & DATACLASSES
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class LevelTrade:
    """Struktur level harga entry, SL, TP1, TP, dan sumber perhitungan SL."""

    sl: float
    tp1: float
    tp: float
    jarak: float
    sumber: str


TICKER_MAP: dict[str, str] = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
}
ALIAS_PAIR: dict[str, str] = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "EMAS": "XAUUSD",
    "BTC": "BTCUSD",
    "BITCOIN": "BTCUSD",
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "JPY": "USDJPY",
}
MATA_UANG: dict[str, list[str]] = {
    "XAUUSD": ["USD"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "BTCUSD": ["USD"],
}
DESIMAL: dict[str, int] = {"XAUUSD": 2, "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "BTCUSD": 2}
PIP_SIZE: dict[str, float] = {"XAUUSD": 0.1, "EURUSD": 0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01, "BTCUSD": 1.0}

RSS_UMUM: list[str] = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.investing.com/rss/news_1.rss",
    "https://www.fxstreet.com/rss/news",
]
RSS_BTC: list[str] = ["https://www.coindesk.com/arc/outboundfeeds/rss/"]
KALENDER_URL: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA: dict[str, str] = {"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"}

# ---------------------------------------------------------------------
# STATE GLOBAL
# ---------------------------------------------------------------------
STATE_LOCK = threading.RLock()
SCAN_AKTIF: bool = True
LAST_SIGNAL: dict[str, dict[str, Any]] = {}
POSISI: list[dict[str, Any]] = []
CACHE_BERITA: dict[str, tuple[float, list[str]]] = {}
KAL: dict[str, Any] = {"waktu": 0.0, "gagal": 0.0, "data": []}
GEMINI_CLIENT: Any = None
PESAN_TERAKHIR_ID: str = ""
SUDAH_SAPA: bool = False
PERINGATAN: dict[str, float] = {}
REKAP_MINGGUAN_TERAKHIR: str = ""
SCAN_TERAKHIR: float = 0.0


def log(pesan: str) -> None:
    """Cetak pesan log ber-timestamp ke stdout."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {pesan}")
    sys.stdout.flush()


def lokasi_folder() -> Path:
    """Tentukan direktori penyimpanan file state bot."""
    d = os.environ.get("BOT_STATE_DIR")
    if d:
        folder = Path(d)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return folder
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd()


def lokasi_state() -> Path:
    """Kembalikan Path lengkap menuju file JSON state bot."""
    return lokasi_folder() / "forex_bot_discord_state.json"


def rapikan_posisi() -> None:
    """Simpan semua posisi OPEN ditambah maksimal 200 posisi CLOSED terakhir."""
    global POSISI
    with STATE_LOCK:
        terbuka = [p for p in POSISI if p.get("status") == "OPEN"]
        tutup = [p for p in POSISI if p.get("status") != "OPEN"]
        POSISI = tutup[-200:] + terbuka


def simpan_state() -> None:
    """Simpan state bot secara atomik menggunakan file sementara."""
    try:
        with STATE_LOCK:
            rapikan_posisi()
            batas_lama = time.time() - 4 * 86400
            for k in [k for k, v in PERINGATAN.items() if float(v) < batas_lama]:
                del PERINGATAN[k]
            data: dict[str, Any] = {
                "admin_user_id": ADMIN_USER_ID,
                "modal": MODAL_AWAL_USD,
                "env_modal_raw": ENV_MODAL_RAW,
                "scan_aktif": SCAN_AKTIF,
                "watchlist": list(WATCHLIST),
                "env_watchlist_raw": ENV_WATCHLIST_RAW,
                "last_signal": LAST_SIGNAL,
                "posisi": POSISI,
                "pesan_terakhir_id": PESAN_TERAKHIR_ID,
                "sudah_sapa": SUDAH_SAPA,
                "peringatan": PERINGATAN,
                "rekap_mingguan_terakhir": REKAP_MINGGUAN_TERAKHIR,
            }
            path = lokasi_state()
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f)
            tmp.replace(path)
    except Exception as e:
        log(f"Gagal menyimpan state: {e}")


def muat_state() -> None:
    """Muat state dari disk tanpa menimpa GitHub Secrets yang baru diperbarui."""
    global ADMIN_USER_ID, MODAL_AWAL_USD, SCAN_AKTIF, WATCHLIST, LAST_SIGNAL, POSISI
    global PESAN_TERAKHIR_ID, SUDAH_SAPA, PERINGATAN, REKAP_MINGGUAN_TERAKHIR
    try:
        path = lokasi_state()
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        with STATE_LOCK:
            if ADMIN_USER_ID == 0 and data.get("admin_user_id"):
                ADMIN_USER_ID = int(data["admin_user_id"])

            secret_modal_berubah = bool(ENV_MODAL_RAW) and (ENV_MODAL_RAW != str(data.get("env_modal_raw") or ""))
            if not secret_modal_berubah and data.get("modal"):
                MODAL_AWAL_USD = float(data["modal"])

            if "scan_aktif" in data:
                SCAN_AKTIF = bool(data["scan_aktif"])

            secret_wl_berubah = bool(ENV_WATCHLIST_RAW) and (ENV_WATCHLIST_RAW != str(data.get("env_watchlist_raw") or ""))
            if not secret_wl_berubah and isinstance(data.get("watchlist"), list):
                wl_valid = [
                    str(x).strip().upper()
                    for x in data["watchlist"]
                    if str(x).strip().upper() in TICKER_MAP
                ]
                if wl_valid:
                    WATCHLIST = wl_valid

            if isinstance(data.get("last_signal"), dict):
                LAST_SIGNAL = data["last_signal"]
            if isinstance(data.get("posisi"), list):
                POSISI = [p for p in data["posisi"] if isinstance(p, dict) and "id" in p]
            PESAN_TERAKHIR_ID = str(data.get("pesan_terakhir_id") or "")
            SUDAH_SAPA = bool(data.get("sudah_sapa", False))
            if isinstance(data.get("peringatan"), dict):
                PERINGATAN = data["peringatan"]
            REKAP_MINGGUAN_TERAKHIR = str(data.get("rekap_mingguan_terakhir") or "")
        log(f"State dimuat dari {path}")
    except Exception as e:
        log(f"Gagal memuat state: {e}")


def boleh_peringatan(kunci: str, jam: float = 6) -> bool:
    """Kembalikan True jika peringatan dengan kunci ini belum dikirim dalam X jam terakhir."""
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
# DISCORD (REST API + LAMPIRAN CHART + FALLBACK AMAN)
# ---------------------------------------------------------------------
API: str = "https://discord.com/api/v10"
UA_DISCORD: str = "DiscordBot (https://github.com/forex-ai-bot, 1.0)"


def discord_req(metode: str, path: str, **kw: Any) -> Any:
    """Panggil REST API Discord dengan penanganan rate limit."""
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "User-Agent": UA_DISCORD}
    for percobaan in range(3):
        try:
            r = requests.request(metode, API + path, headers=headers, timeout=25, **kw)
            if r.status_code == 429:
                try:
                    tunggu = float(r.json().get("retry_after", 2))
                except Exception:
                    tunggu = 2.0
                log(f"Discord rate limit, menunggu {tunggu:.1f} detik")
                time.sleep(min(tunggu, 15) + 0.5)
                continue
            if r.status_code >= 400:
                log(f"Discord API {metode} {path} gagal ({r.status_code}): {r.text[:200]}")
                return None
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()
        except Exception as e:
            log(f"Discord API error ({percobaan + 1}): {e}")
            time.sleep(2)
    return None


def pecah_teks(teks: str, batas: int = 1900) -> list[str]:
    """Pecah teks di batas baris agar tidak melebihi batas karakter Discord."""
    hasil: list[str] = []
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
            cur = f"{cur}\n{baris}" if cur else baris
    if cur:
        hasil.append(cur)
    return hasil or [" "]


def tujuan_aktif() -> bool:
    """Cek apakah mode Discord ('bot' atau 'webhook') aktif."""
    return MODE in ("bot", "webhook")


WARNA_HIJAU: int = 0x2ECC71
WARNA_MERAH: int = 0xE74C3C
WARNA_KUNING: int = 0xF1C40F
WARNA_BIRU: int = 0x3498DB
WARNA_ABU: int = 0x95A5A6


def warna_arah(arah: str) -> int:
    """Kembalikan kode warna embed berdasarkan arah sinyal BUY/SELL."""
    return WARNA_HIJAU if arah == "BUY" else WARNA_MERAH


def warna_hasil(teks: str) -> int | None:
    """Tentukan warna kartu embed untuk pesan hasil pelacakan posisi."""
    if teks.startswith("\u2705 TP"):
        return WARNA_HIJAU
    if teks.startswith("\u274c SL"):
        return WARNA_MERAH
    if teks.startswith("\U0001f514"):
        return WARNA_BIRU
    if teks.startswith("\u231b"):
        return WARNA_ABU
    return None


def _kirim_bot(payload: dict[str, Any], chart_path: Path | None = None) -> Any:
    path = f"/channels/{DISCORD_CHANNEL_ID}/messages"
    if chart_path and chart_path.exists():
        fname = chart_path.name
        payload_kirim = dict(payload)
        payload_kirim["attachments"] = [{"id": 0, "filename": fname}]
        with chart_path.open("rb") as f:
            files = {"files[0]": (fname, f, "image/png")}
            data = {"payload_json": json.dumps(payload_kirim)}
            return discord_req("POST", path, data=data, files=files)
    return discord_req("POST", path, json=payload)


def _kirim_webhook(payload: dict[str, Any], chart_path: Path | None = None) -> bool:
    for percobaan in range(2):
        try:
            if chart_path and chart_path.exists():
                fname = chart_path.name
                payload_kirim = dict(payload)
                payload_kirim["attachments"] = [{"id": 0, "filename": fname}]
                with chart_path.open("rb") as f:
                    files = {"files[0]": (fname, f, "image/png")}
                    data = {"payload_json": json.dumps(payload_kirim)}
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
                log(f"Webhook gagal ({r.status_code}): {r.text[:200]}")
                return False
            return True
        except Exception as e:
            log(f"Webhook error ({percobaan + 1}): {e}")
            time.sleep(2)
    return False


def kirim(teks: str, warna: int | None = None, chart_path: str | Path | None = None) -> bool:
    """Kirim pesan ke Discord dengan fallback bertahap dan pembersihan file chart otomatis."""
    ok = True
    batas = 3900 if warna is not None else 1900
    potongan = pecah_teks(teks, batas)
    chart_file: Path | None = Path(chart_path) if chart_path else None
    try:
        for idx, p in enumerate(potongan):
            lampiran = chart_file if (idx == 0 and chart_file and chart_file.exists()) else None
            fname = lampiran.name if lampiran else None
            try:
                if MODE == "bot":
                    terkirim_bagian = False
                    if warna is not None:
                        embed: dict[str, Any] = {"description": p, "color": warna}
                        if fname:
                            embed["image"] = {"url": f"attachment://{fname}"}
                        payload: dict[str, Any] = {"embeds": [embed], "allowed_mentions": {"parse": []}}
                        if _kirim_bot(payload, lampiran) is not None:
                            terkirim_bagian = True
                        elif lampiran is not None:
                            embed_tanpa_img = {"description": p, "color": warna}
                            if _kirim_bot({"embeds": [embed_tanpa_img], "allowed_mentions": {"parse": []}}) is not None:
                                terkirim_bagian = True
                        if not terkirim_bagian:
                            semua_polos = True
                            for q in pecah_teks(p, 1900):
                                if _kirim_bot({"content": q, "allowed_mentions": {"parse": []}}) is None:
                                    semua_polos = False
                            terkirim_bagian = semua_polos
                    else:
                        if _kirim_bot({"content": p, "allowed_mentions": {"parse": []}}, lampiran) is not None:
                            terkirim_bagian = True
                        elif lampiran is not None:
                            if _kirim_bot({"content": p, "allowed_mentions": {"parse": []}}) is not None:
                                terkirim_bagian = True
                    if not terkirim_bagian:
                        ok = False

                elif MODE == "webhook":
                    terkirim_bagian = False
                    base_wh: dict[str, Any] = {"allowed_mentions": {"parse": []}, "username": "Forex AI Bot"}
                    if warna is not None:
                        embed = {"description": p, "color": warna}
                        if fname:
                            embed["image"] = {"url": f"attachment://{fname}"}
                        payload = dict(base_wh, embeds=[embed])
                        if _kirim_webhook(payload, lampiran):
                            terkirim_bagian = True
                        elif lampiran is not None:
                            payload_no_img = dict(base_wh, embeds=[{"description": p, "color": warna}])
                            if _kirim_webhook(payload_no_img):
                                terkirim_bagian = True
                        if not terkirim_bagian:
                            semua_polos = True
                            for q in pecah_teks(p, 1900):
                                if not _kirim_webhook(dict(base_wh, content=q)):
                                    semua_polos = False
                            terkirim_bagian = semua_polos
                    else:
                        if _kirim_webhook(dict(base_wh, content=p), lampiran):
                            terkirim_bagian = True
                        elif lampiran is not None:
                            if _kirim_webhook(dict(base_wh, content=p)):
                                terkirim_bagian = True
                    if not terkirim_bagian:
                        ok = False
                else:
                    ok = False
            except Exception as e:
                log(f"Gagal kirim Discord: {e}")
                ok = False
            time.sleep(0.4)
    finally:
        if chart_file and chart_file.exists():
            try:
                chart_file.unlink()
            except OSError:
                pass
    return ok


def ambil_pesan_baru() -> list[dict[str, Any]] | None:
    """Baca pesan baru di channel Discord sejak ID pesan terakhir."""
    global PESAN_TERAKHIR_ID
    path = f"/channels/{DISCORD_CHANNEL_ID}/messages"
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
    hasil: list[dict[str, Any]] = []
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
# JAM PASAR & VALIDASI KESEGARAN DATA
# ---------------------------------------------------------------------
def pasar_buka(symbol: str) -> bool:
    """Cek apakah sesi reguler pasar sedang buka (BTC 24/7; Forex/Emas tutup akhir pekan)."""
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


def cek_data_basi(df: pd.DataFrame, max_umur_menit: int = MAX_UMUR_DATA_MENIT) -> tuple[bool, int]:
    """Periksa apakah candle terakhir terlalu tua (menandakan jeda CME harian atau libur pasar).

    Args:
        df: DataFrame OHLC dengan index waktu candle.
        max_umur_menit: Batas maksimal usia candle terakhir dalam menit.

    Returns:
        Tuple (is_stale, umur_menit).
    """
    if len(df) == 0:
        return True, 999999
    last_ts = pd.Timestamp(df.index[-1])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    else:
        last_ts = last_ts.tz_convert("UTC")
    now_utc = pd.Timestamp.now(tz="UTC")
    umur_menit = max(0, int((now_utc - last_ts).total_seconds() // 60))
    return umur_menit > max_umur_menit, umur_menit


# ---------------------------------------------------------------------
# DATA MARKET (yfinance)
# ---------------------------------------------------------------------
def bersihkan_df(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Ratakan kolom MultiIndex yfinance dan bersihkan nilai NaN/duplikat OHLC."""
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
        log(f"bersihkan_df error: {e}")
        return None


def ambil_data(symbol: str, period: str, interval: str, min_baris: int = 60) -> pd.DataFrame | None:
    """Unduh data historis OHLC dari Yahoo Finance dengan mekanisme retry."""
    ticker = TICKER_MAP[symbol]
    for percobaan in range(3):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
                threads=False,
            )
            df = bersihkan_df(df)
            if df is not None and len(df) >= min_baris:
                return df
        except Exception as e:
            log(f"yf.download {symbol} {interval} gagal ({percobaan + 1}): {e}")
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
            df = bersihkan_df(df)
            if df is not None and len(df) >= min_baris:
                return df
        except Exception as e:
            log(f"yf.Ticker.history {symbol} {interval} gagal ({percobaan + 1}): {e}")
        time.sleep(3 * (percobaan + 1))
    return None


# ---------------------------------------------------------------------
# INDIKATOR TEKNIKAL (pure pandas): EMA20/50, RSI14, ATR14, ADX14
# ---------------------------------------------------------------------
def hitung_indikator(df: pd.DataFrame) -> pd.DataFrame:
    """Hitung indikator EMA20, EMA50, RSI14, ATR14, +DI, -DI, dan ADX14."""
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
    tr = pd.concat(
        [
            d["High"] - d["Low"],
            (d["High"] - prev_close).abs(),
            (d["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
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


def ringkas(d: pd.DataFrame) -> dict[str, Any]:
    """Ringkas metrik indikator teknikal pada baris terbaru."""
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
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "rsi_status": rsi_status,
        "atr": atr,
        "tren": tren,
        "gap": ema20 - ema50,
        "gap_atr": gap_atr,
        "rasio_atr": rasio_atr,
        "adx": float(last["ADX"]),
        "pdi": float(last["PDI"]),
        "mdi": float(last["MDI"]),
        "high20": float(d["High"].tail(20).max()),
        "low20": float(d["Low"].tail(20).min()),
    }


def swing_terdekat(d: pd.DataFrame, arah: str, entry: float, lookback: int = 60) -> float | None:
    """Cari swing low (BUY) atau swing high (SELL) fraktal 5 candle terdekat dari entry."""
    try:
        sub = d.tail(lookback + 4)
        lows = sub["Low"].tolist()
        highs = sub["High"].tolist()
        n = len(sub)
        for i in range(n - 3, 1, -1):
            if arah == "BUY":
                v = lows[i]
                if v < entry and v == min(lows[i - 2 : i + 3]):
                    return float(v)
            else:
                v = highs[i]
                if v > entry and v == max(highs[i - 2 : i + 3]):
                    return float(v)
    except Exception as e:
        log(f"swing_terdekat error: {e}")
    return None


def konfirmasi_struktur_15m(i15: pd.DataFrame, r1h: dict[str, Any], r15: dict[str, Any]) -> tuple[bool, str]:
    """Validasi struktur EMA 15M, dominasi +DI/-DI 1H, serta momentum candle 15M yang sudah tutup.

    Menggunakan candle iloc[-2] (candle terakhir yang sudah close) untuk evaluasi
    range dan penutupan body agar hasil tidak fluktuatif saat candle baru terbuka.
    """
    if len(i15) < 4:
        return False, "Data candle 15M kurang untuk konfirmasi struktur."

    tren = r1h["tren"]
    close = r15["close"]
    ema20 = r15["ema20"]
    ema50 = r15["ema50"]
    atr15 = r15["atr"]

    c_closed = i15.iloc[-2]
    c_prev = i15.iloc[-3]
    hi0, lo0, op0, cl0 = (
        float(c_closed["High"]),
        float(c_closed["Low"]),
        float(c_closed["Open"]),
        float(c_closed["Close"]),
    )
    hi1, lo1 = float(c_prev["High"]), float(c_prev["Low"])
    rng = hi0 - lo0
    if rng <= 0:
        return False, "Candle 15M terakhir yang tutup doji/datar tanpa momentum."

    if tren == "NAIK":
        if r1h["pdi"] <= r1h["mdi"]:
            return False, f"+DI 1H ({r1h['pdi']:.1f}) belum dominan di atas -DI ({r1h['mdi']:.1f})."
        if not (close > ema20 and ema20 > ema50):
            return False, "Struktur 15M belum bullish rapi (harga harus di atas EMA20 > EMA50)."
        if atr15 > 0 and (close - ema20) > 2.2 * atr15:
            return False, "Harga 15M sudah terlalu jauh di atas EMA20 (rawan koreksi)."
        pos_close = (cl0 - lo0) / rng
        bull_candle = (cl0 >= op0 and pos_close >= 0.45) or (pos_close >= 0.65)
        if not bull_candle or cl0 <= lo1 or close < lo0:
            return False, "Momentum candle 15M tertutup belum mengonfirmasi dorongan naik."
        return True, "Struktur 15M bullish terkonfirmasi (harga > EMA20 > EMA50, candle konfirmasi tutup kuat)."

    if tren == "TURUN":
        if r1h["mdi"] <= r1h["pdi"]:
            return False, f"-DI 1H ({r1h['mdi']:.1f}) belum dominan di atas +DI ({r1h['pdi']:.1f})."
        if not (close < ema20 and ema20 < ema50):
            return False, "Struktur 15M belum bearish rapi (harga harus di bawah EMA20 < EMA50)."
        if atr15 > 0 and (ema20 - close) > 2.2 * atr15:
            return False, "Harga 15M sudah terlalu jauh di bawah EMA20 (rawan pantulan)."
        pos_close = (hi0 - cl0) / rng
        bear_candle = (cl0 <= op0 and pos_close >= 0.45) or (pos_close >= 0.65)
        if not bear_candle or cl0 >= hi1 or close > hi0:
            return False, "Momentum candle 15M tertutup belum mengonfirmasi tekanan turun."
        return True, "Struktur 15M bearish terkonfirmasi (harga < EMA20 < EMA50, candle konfirmasi tutup kuat)."

    return False, "Arah tren belum terkonfirmasi."


# ---------------------------------------------------------------------
# RENDER CHART CANDLESTICK (mplfinance)
# ---------------------------------------------------------------------
def buat_chart(symbol: str, d15: pd.DataFrame | None, h: dict[str, Any]) -> str | None:
    """Render chart candlestick 15M (65 candle terakhir) dalam zona waktu lokal."""
    if not KIRIM_CHART or not HAS_MPL or d15 is None or len(d15) < 20:
        return None
    fig = None
    try:
        sub = d15.tail(65).copy()
        idx = pd.to_datetime(sub.index)
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        sub.index = idx.tz_convert(ZONA).tz_localize(None)

        ap = [
            mpf.make_addplot(sub["EMA20"], color="#00D2FF", width=1.3),
            mpf.make_addplot(sub["EMA50"], color="#FF9F1C", width=1.3),
        ]

        mc = mpf.make_marketcolors(
            up="#2ECC71",
            down="#E74C3C",
            edge="inherit",
            wick="inherit",
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

        status_txt = f"{h['keputusan']} ({h['skor']}/100)" if h.get("valid") else str(h.get("keputusan", "WAIT"))
        judul = f"{symbol} 15M | {status_txt} | {waktu_lokal().strftime('%d/%m %H:%M')} {LABEL_ZONA}"

        fig, axlist = mpf.plot(
            sub,
            type="candle",
            style=style,
            addplot=ap,
            title=judul,
            ylabel="Harga",
            volume=False,
            figsize=(9.5, 5.0),
            tight_layout=True,
            returnfig=True,
        )
        ax = axlist[0]

        handles = [
            Line2D([0], [0], color="#00D2FF", lw=1.5, label=f"EMA20 ({fmt(symbol, float(sub['EMA20'].iloc[-1]))})"),
            Line2D([0], [0], color="#FF9F1C", lw=1.5, label=f"EMA50 ({fmt(symbol, float(sub['EMA50'].iloc[-1]))})"),
        ]

        if h.get("valid"):
            levels = [
                ("Entry", float(h["entry"]), "#FFFFFF", "--"),
                ("SL", float(h["sl"]), "#E74C3C", "-"),
                ("TP1 (1R)", float(h["tp1"]), "#3498DB", ":"),
                ("TP (2R)", float(h["tp"]), "#2ECC71", "-"),
            ]
            for lbl, val, col, sty in levels:
                if math.isfinite(val):
                    ax.axhline(val, color=col, linestyle=sty, linewidth=1.25, alpha=0.92)
                    handles.append(Line2D([0], [0], color=col, lw=1.4, ls=sty, label=f"{lbl}: {fmt(symbol, val)}"))

        ax.legend(
            handles=handles,
            loc="upper left",
            fontsize=8,
            facecolor="#1E222D",
            edgecolor="#363A45",
            labelcolor="white",
            framealpha=0.88,
        )

        fd, tmp_path = tempfile.mkstemp(prefix=f"chart_{symbol}_", suffix=".png")
        os.close(fd)
        fig.savefig(tmp_path, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
        return tmp_path
    except Exception as e:
        log(f"buat_chart {symbol} error: {e}")
        return None
    finally:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass


# ---------------------------------------------------------------------
# BERITA RSS
# ---------------------------------------------------------------------
def ambil_rss(url: str) -> list[str]:
    """Ambil maksimal 15 headline dari feed RSS dengan cache 10 menit."""
    now = time.time()
    c = CACHE_BERITA.get(url)
    if c and now - c[0] < 600:
        return c[1]
    judul: list[str] = []
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
        log(f"RSS gagal ({url[:50]}): {e}")
        if c:
            return c[1]
        return []
    CACHE_BERITA[url] = (now, judul)
    return judul


def ambil_berita(symbol: str) -> list[str]:
    """Gabungkan headline spesifik pair dan headline ekonomi umum."""
    spesifik: list[str] = []
    try:
        url_ticker = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={TICKER_MAP[symbol]}&region=US&lang=en-US"
        spesifik += ambil_rss(url_ticker)
        if symbol == "BTCUSD":
            for u in RSS_BTC:
                spesifik += ambil_rss(u)
    except Exception as e:
        log(f"ambil_berita spesifik error: {e}")
    umum: list[str] = []
    for u in RSS_UMUM:
        try:
            umum += ambil_rss(u)
        except Exception as e:
            log(f"ambil_berita umum error: {e}")
    hasil: list[str] = []
    for j in spesifik[:5] + umum[:9]:
        if j not in hasil:
            hasil.append(j)
        if len(hasil) >= 12:
            break
    return hasil


# ---------------------------------------------------------------------
# KALENDER EKONOMI (High Impact)
# ---------------------------------------------------------------------
def ambil_kalender() -> list[dict[str, Any]]:
    """Ambil jadwal berita ekonomi High Impact minggu ini."""
    now = time.time()
    if KAL["waktu"] and now - KAL["waktu"] < 3600:
        return KAL["data"]
    if now - KAL["gagal"] < 600:
        return KAL["data"]
    try:
        r = requests.get(KALENDER_URL, timeout=12, headers=UA)
        r.raise_for_status()
        raw = r.json()
        data: list[dict[str, Any]] = []
        for e in raw:
            if str(e.get("impact", "")).strip().lower() != "high":
                continue
            tgl = str(e.get("date", "")).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(tgl)
            except ValueError:
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
        log(f"Kalender ekonomi dimuat: {len(data)} event High Impact")
    except Exception as e:
        KAL["gagal"] = now
        log(f"Kalender ekonomi gagal: {e}")
    return KAL["data"]


def berita_berbahaya(symbol: str) -> tuple[dict[str, Any] | None, float | None]:
    """Cek apakah ada rilis berita High Impact dalam jendela blokir."""
    mata = MATA_UANG.get(symbol, ["USD"])
    now = time.time()
    for e in ambil_kalender():
        if e["mata_uang"] not in mata:
            continue
        m = (e["ts"] - now) / 60.0
        if -NEWS_BLOK_SESUDAH_MENIT <= m <= NEWS_BLOK_SEBELUM_MENIT:
            return e, m
    return None, None


def baris_kalender_prompt(symbol: str) -> list[str]:
    """Format daftar berita High Impact 6 jam ke depan untuk prompt AI."""
    mata = MATA_UANG.get(symbol, ["USD"])
    now = time.time()
    hasil: list[str] = []
    for e in ambil_kalender():
        if e["mata_uang"] in mata:
            m = (e["ts"] - now) / 60.0
            if 0 <= m <= 360:
                hasil.append(f"- {e['mata_uang']} {e['judul']} (dalam {int(m)} menit)")
    return hasil[:5]


def ket_menit(m: float) -> str:
    """Format selisih waktu dalam menit ke bahasa Indonesia."""
    if m >= 0:
        return f"dalam {int(m)} menit"
    return f"{int(abs(m))} menit lalu"


def kirim_peringatan_berita() -> None:
    """Kirim notifikasi peringatan dini menjelang rilis berita High Impact."""
    if not tujuan_aktif():
        return
    with STATE_LOCK:
        wl = list(WATCHLIST)
    for symbol in wl:
        if symbol not in TICKER_MAP or not pasar_buka(symbol):
            continue
        e, m = berita_berbahaya(symbol)
        if not e or m is None or m < 0:
            continue
        key = f"news:{e['judul']}|{int(e['ts'])}"
        if not boleh_peringatan(key, 48):
            continue
        kirim(
            f"\u26a0\ufe0f PERINGATAN BERITA HIGH IMPACT\n{e['mata_uang']}: {e['judul']}\nRilis {ket_menit(m)}.\n"
            f"Pasar ditahan sementara: sinyal baru diblokir sampai {NEWS_BLOK_SESUDAH_MENIT} menit setelah rilis.",
            WARNA_KUNING,
        )


# ---------------------------------------------------------------------
# GEMINI AI
# ---------------------------------------------------------------------
def get_gemini() -> Any:
    """Inisialisasi singleton client Gemini."""
    global GEMINI_CLIENT
    if GEMINI_CLIENT is None:
        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return GEMINI_CLIENT


def parse_json_ai(teks: str | None) -> dict[str, Any] | None:
    """Ekstrak dan parse objek JSON dari output teks Gemini."""
    if not teks:
        return None
    t = teks.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        parsed = json.loads(t)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    a = t.find("{")
    b = t.rfind("}")
    if a != -1 and b > a:
        try:
            parsed = json.loads(t[a : b + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def blok_teknikal(nama: str, r: dict[str, Any], symbol: str) -> str:
    """Susun ringkasan angka teknikal terformat untuk prompt AI."""
    dg = DESIMAL[symbol]
    return (
        f"{nama}: harga={r['close']:.{dg}f}, EMA20={r['ema20']:.{dg}f}, EMA50={r['ema50']:.{dg}f}, "
        f"selisih EMA/ATR={r['gap_atr']:.2f}, tren={r['tren']}, RSI14={r['rsi']:.1f} ({r['rsi_status']}), "
        f"ATR14={r['atr']:.{dg}f}, rasio ATR={r['rasio_atr']:.2f}, ADX14={r['adx']:.1f} "
        f"(+DI {r['pdi']:.1f} / -DI {r['mdi']:.1f}), high20={r['high20']:.{dg}f}, low20={r['low20']:.{dg}f}"
    )


def tanya_gemini(
    symbol: str,
    r1h: dict[str, Any],
    r15: dict[str, Any],
    berita: list[str],
    info_struktur: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Kirim konteks teknikal + berita + kalender ke Gemini AI dan validasi respons JSON."""
    if GEMINI_API_KEY.startswith("ISI_"):
        return None, "GEMINI_API_KEY belum diisi"
    daftar_berita = "\n".join(f"- {b}" for b in berita) if berita else "- (tidak ada berita berhasil diambil)"
    kal = baris_kalender_prompt(symbol)
    daftar_kal = "\n".join(kal) if kal else "- (tidak ada berita High Impact dalam 6 jam ke depan)"
    prompt = (
        "Kamu adalah analis trading profesional (forex, emas, kripto).\n"
        f"Pair: {symbol}\n\n"
        f"DATA TEKNIKAL:\n{blok_teknikal('TF 1H', r1h, symbol)}\n{blok_teknikal('TF 15M', r15, symbol)}\n"
        f"Konfirmasi Struktur 15M: {info_struktur or '-'}\n\n"
        f"KALENDER EKONOMI HIGH IMPACT (6 jam ke depan):\n{daftar_kal}\n\n"
        f"HEADLINE BERITA FINANSIAL TERBARU (bahasa Inggris):\n{daftar_berita}\n\n"
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
            except (TypeError, ValueError):
                skor = 0
            skor = max(0, min(100, skor))
            alasan = str(data.get("alasan_singkat", "-")).strip()[:300]
            return {"keputusan": keputusan, "skor": skor, "alasan": alasan}, None
        except Exception as e:
            error_terakhir = str(e)[:200]
            log(f"Gemini gagal ({percobaan + 1}): {error_terakhir}")
            time.sleep(4)
    return None, error_terakhir


# ---------------------------------------------------------------------
# MANAJEMEN RISIKO
# ---------------------------------------------------------------------
def nilai_per_unit_harga(symbol: str, harga: float) -> float:
    """Hitung nilai USD untuk pergerakan 1.0 harga pada 1.0 lot standar."""
    if symbol == "XAUUSD":
        return 100.0
    if symbol in ("EURUSD", "GBPUSD"):
        return 100000.0
    if symbol == "USDJPY":
        return 100000.0 / harga if harga > 0 else 0.0
    if symbol == "BTCUSD":
        return 1.0
    return 1.0


def hitung_lot(symbol: str, entry: float, jarak_sl: float, modal: float) -> tuple[float, float, float]:
    """Hitung ukuran lot aman berdasarkan persentase risiko modal."""
    risiko_target = modal * (RISIKO_PER_TRADE_PERSEN / 100.0)
    nilai = nilai_per_unit_harga(symbol, entry)
    if jarak_sl <= 0 or nilai <= 0:
        return 0.01, 0.0, risiko_target
    lot_mentah = risiko_target / (jarak_sl * nilai)
    lot = max(0.01, math.floor(lot_mentah * 100) / 100.0)
    risiko_aktual = lot * jarak_sl * nilai
    return lot, risiko_aktual, risiko_target


def hitung_level(arah: str, entry: float, atr: float, d1h: pd.DataFrame) -> LevelTrade:
    """Tentukan level SL (swing terdekat atau 1.5x ATR), TP1 (1R), dan TP (2R)."""
    jarak = 1.5 * atr
    sumber = "1.5x ATR"
    sw = swing_terdekat(d1h, arah, entry)
    if sw is not None:
        dist = entry - (sw - 0.25 * atr) if arah == "BUY" else (sw + 0.25 * atr) - entry
        if 1.0 * atr <= dist <= 2.5 * atr:
            jarak = dist
            sumber = f"di luar swing {'low' if arah == 'BUY' else 'high'}"
    if arah == "BUY":
        sl, tp1, tp = entry - jarak, entry + jarak, entry + 2.0 * jarak
    else:
        sl, tp1, tp = entry + jarak, entry - jarak, entry - 2.0 * jarak
    return LevelTrade(sl=sl, tp1=tp1, tp=tp, jarak=jarak, sumber=sumber)


# ---------------------------------------------------------------------
# ANALISA LENGKAP
# ---------------------------------------------------------------------
def fmt(symbol: str, x: float) -> str:
    """Format angka harga sesuai jumlah desimal pair."""
    return f"{x:.{DESIMAL.get(symbol, 5)}f}"


def fmt_jarak(symbol: str, jarak: float) -> str:
    """Format jarak harga ke satuan poin/USD atau pip."""
    if symbol in ("XAUUSD", "BTCUSD"):
        return f"{jarak:.2f} poin (${jarak:.2f})"
    return f"{fmt(symbol, jarak)} ({jarak / PIP_SIZE.get(symbol, 0.0001):.1f} pip)"


def tahan(h: dict[str, Any], alasan: str) -> dict[str, Any]:
    """Set status hasil analisa menjadi WAIT beserta alasannya."""
    h["keputusan"] = "WAIT"
    h["alasan_tahan"] = alasan
    h["alasan"] = alasan
    return h


def analisa_pair(symbol: str, buat_gambar: bool = False) -> dict[str, Any]:
    """Jalankan pipeline analisa teknikal, kesegaran data, filter berita, dan AI untuk 1 pair."""
    h: dict[str, Any] = {
        "symbol": symbol,
        "ok": False,
        "tutup": False,
        "error": None,
        "valid": False,
        "keputusan": "WAIT",
        "skor": 0,
        "alasan": "-",
        "alasan_tahan": None,
        "r1h": None,
        "r15": None,
        "berita_n": 0,
        "ai_error": None,
        "ai_dipanggil": False,
        "chart_path": None,
    }
    try:
        if not pasar_buka(symbol):
            h["tutup"] = True
            h["error"] = f"Pasar {symbol} sedang tutup (akhir pekan). Data akan basi, analisa dilewati."
            return h

        d1h = ambil_data(symbol, "10d", "1h")
        if d1h is None:
            h["error"] = "Gagal mengambil data 1H dari Yahoo Finance (cek koneksi internet)."
            return h
        d15 = ambil_data(symbol, "5d", "15m")
        if d15 is None:
            h["error"] = "Gagal mengambil data 15M dari Yahoo Finance (cek koneksi internet)."
            return h

        basi, umur_menit = cek_data_basi(d15, MAX_UMUR_DATA_MENIT)
        if basi:
            h["tutup"] = True
            h["error"] = (
                f"Data 15M {symbol} basi (candle terakhir {umur_menit} menit lalu; "
                "pasar kemungkinan jeda CME/libur). Analisa dilewati."
            )
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
            tahan(h, f"ADX 1H {r1h['adx']:.1f} di bawah {ADX_MIN}, tren belum bertenaga.")
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
            tahan(h, f"Tren 1H ({r1h['tren']}) dan 15M ({r15['tren']}) belum searah.")
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h
        if not (RSI_AMAN_MIN <= r15["rsi"] <= RSI_AMAN_MAX):
            tahan(h, f"RSI 15M {r15['rsi']:.1f} di area jenuh (aman {RSI_AMAN_MIN}-{RSI_AMAN_MAX}).")
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Validasi Struktur & Momentum Candle 15M Tertutup ---
        ok_struktur, info_struktur = konfirmasi_struktur_15m(i15, r1h, r15)
        if not ok_struktur:
            tahan(h, info_struktur)
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Blokir berita High Impact ---
        ev, mnt = berita_berbahaya(symbol)
        if ev and mnt is not None:
            tahan(
                h,
                f"Menjelang/sesudah berita High Impact: {ev['mata_uang']} {ev['judul']} "
                f"({ket_menit(mnt)}). Pasar ditahan sementara.",
            )
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Berita RSS + AI ---
        berita = ambil_berita(symbol)
        h["berita_n"] = len(berita)
        h["ai_dipanggil"] = True
        ai, err = tanya_gemini(symbol, r1h, r15, berita, info_struktur=info_struktur)
        if ai is None:
            h["ai_error"] = err
            h["alasan_tahan"] = f"AI tidak tersedia: {err}"
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
            h["alasan_tahan"] = f"Keyakinan {h['skor']} < batas {MIN_KEYAKINAN}."
            if buat_gambar:
                h["chart_path"] = buat_chart(symbol, i15, h)
            return h

        # --- Sinyal valid: Entry / SL / TP1 / TP / Lot ---
        entry = r15["close"]
        atr = r1h["atr"]
        lvl = hitung_level(h["keputusan"], entry, atr, i1h)
        with STATE_LOCK:
            modal = MODAL_AWAL_USD
        lot, risiko_aktual, risiko_target = hitung_lot(symbol, entry, lvl.jarak, modal)

        h["valid"] = True
        h["entry"] = entry
        h["sl"] = lvl.sl
        h["tp1"] = lvl.tp1
        h["tp"] = lvl.tp
        h["jarak"] = lvl.jarak
        h["sumber_sl"] = lvl.sumber
        h["lot"] = lot
        h["risiko_aktual"] = risiko_aktual
        h["risiko_target"] = risiko_target
        h["modal"] = modal
        h["chart_path"] = buat_chart(symbol, i15, h)
        return h
    except Exception as e:
        log(f"analisa_pair {symbol} error: {e}")
        h["ok"] = False
        h["error"] = f"Terjadi kesalahan saat analisa: {str(e)[:150]}"
        return h


def format_pesan(h: dict[str, Any]) -> str:
    """Format hasil analisa menjadi pesan teks terstruktur untuk Discord."""
    s = h["symbol"]
    if h.get("tutup"):
        return f"\U0001f512 {h['error']}"
    if not h["ok"]:
        return f"\u274c Analisa {s} gagal.\n{h['error']}"

    r1h = h["r1h"]
    r15 = h["r15"]
    waktu = f"{waktu_lokal().strftime('%d/%m/%Y %H:%M')} {LABEL_ZONA}"
    b: list[str] = [
        f"\U0001f4ca ANALISA {s}  ({waktu})",
        f"Harga: {fmt(s, r15['close'])}",
        "",
        "TEKNIKAL 1H (tren utama)",
        f"Tren: {r1h['tren']} | EMA20 {fmt(s, r1h['ema20'])} | EMA50 {fmt(s, r1h['ema50'])}",
        f"RSI: {r1h['rsi']:.1f} ({r1h['rsi_status']}) | ATR: {fmt(s, r1h['atr'])} | ADX: {r1h['adx']:.1f}",
        "",
        "TEKNIKAL 15M (momentum)",
        f"Tren: {r15['tren']} | RSI: {r15['rsi']:.1f} ({r15['rsi_status']})",
        "",
    ]

    if not h["ai_dipanggil"]:
        b.append("\u23f3 SINYAL DITAHAN (WAIT)")
        b.append(h["alasan_tahan"] or h["alasan"])
        b.append("(AI tidak dipanggil, kuota hemat)")
    else:
        b.append(f"\U0001f916 AI GEMINI 3.8: {h['keputusan']} | Keyakinan {h['skor']}/100")
        if h["ai_error"]:
            b.append(f"AI error: {h['ai_error']}")
        else:
            b.append(f"Alasan: {h['alasan']}")
            b.append(f"(berita dibaca: {h['berita_n']} headline)")
        b.append("")
        if h["valid"]:
            arah = h["keputusan"]
            j = h["jarak"]
            b.append(f"\U0001f3af SINYAL {arah} VALID")
            b.append(f"Entry : {fmt(s, h['entry'])}")
            b.append(f"SL    : {fmt(s, h['sl'])}")
            b.append(f"TP1   : {fmt(s, h['tp1'])}  (1R, geser SL ke Break Even)")
            b.append(f"TP    : {fmt(s, h['tp'])}  (RR 1:2)")
            b.append(f"Jarak SL: {fmt_jarak(s, j)} | Jarak TP: {fmt_jarak(s, 2 * j)}")
            b.append(f"Dasar SL: {h['sumber_sl']}")
            b.append(f"Lot aman: {h['lot']:.2f}")
            b.append(
                f"Risiko jika SL kena: ${h['risiko_aktual']:.2f} "
                f"(target ${h['risiko_target']:.2f} dari modal ${h['modal']:.2f})"
            )
            if h["risiko_aktual"] > h["risiko_target"] * 1.5:
                b.append(
                    "\u26a0\ufe0f Lot minimum 0.01 membuat risiko lebih besar dari target. "
                    "Pertimbangkan akun cent / modal lebih besar, atau lewati sinyal ini."
                )
            if s == "XAUUSD":
                tanda = "-" if arah == "BUY" else "+"
                tanda_tp = "+" if arah == "BUY" else "-"
                b.append("")
                b.append(
                    "\u26a0\ufe0f Harga emas di sini dari Gold Futures (GC=F), bisa beda $5-$25 dari spot broker. "
                    "Jangan salin harga mentah. Ambil harga LIVE di broker, lalu pasang: "
                    f"SL = live {tanda} {j:.2f} | TP1 = live {tanda_tp} {j:.2f} | TP = live {tanda_tp} {2 * j:.2f}"
                )
        else:
            b.append(f"\u23f3 SINYAL DITAHAN: {h['alasan_tahan'] or 'tidak memenuhi syarat.'}")

    b.append("")
    b.append("Bukan nasihat keuangan. Selalu pasang SL.")
    return "\n".join(b)


# ---------------------------------------------------------------------
# PELACAK SINYAL & PERFORMA
# ---------------------------------------------------------------------
def catat_sinyal(h: dict[str, Any]) -> tuple[str, bool]:
    """Catat sinyal valid sebagai posisi terbuka baru."""
    with STATE_LOCK:
        for p in POSISI:
            if p["symbol"] == h["symbol"] and p["arah"] == h["keputusan"] and p["status"] == "OPEN":
                return p["id"], False
        pid = str(int(time.time()))
        while any(p["id"] == pid for p in POSISI):
            pid = str(int(pid) + 1)
        POSISI.append({
            "id": pid,
            "symbol": h["symbol"],
            "arah": h["keputusan"],
            "entry": h["entry"],
            "sl": h["sl"],
            "tp1": h["tp1"],
            "tp": h["tp"],
            "waktu": time.time(),
            "status": "OPEN",
            "hasil": None,
            "tp1_kena": False,
            "diambil": False,
        })
        LAST_SIGNAL[h["symbol"]] = {"arah": h["keputusan"], "waktu": time.time(), "entry": h["entry"]}
    simpan_state()
    return pid, True


def tandai_diambil(pid: str) -> bool:
    """Tandai posisi dengan ID tertentu sebagai sudah diambil oleh trader."""
    with STATE_LOCK:
        for p in POSISI:
            if p["id"] == pid:
                p["diambil"] = True
                break
        else:
            return False
    simpan_state()
    return True


def evaluasi_posisi(p: dict[str, Any], df: pd.DataFrame | None) -> dict[str, Any]:
    """Evaluasi apakah posisi terbuka sudah menyentuh TP1, TP, atau SL.

    Aturan akurasi:
    1. Untuk candle aktif tempat sinyal muncul (t0 - 15m <= idx < t0), hanya harga
       Close candle tersebut yang diperiksa agar wick sebelum sinyal tidak memicu
       false SL/TP.
    2. Untuk semua candle yang membuka pada atau setelah t0 (idx >= t0), rentang penuh
       High & Low diperiksa (SL diprioritaskan jika kedua level tersentuh di candle sama).
    """
    hasil: dict[str, Any] = {"hasil": None, "tp1": False}
    if df is None or len(df) == 0:
        return hasil
    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        t0 = pd.Timestamp(p["waktu"], unit="s", tz="UTC")
        is_buy = p["arah"] == "BUY"
        sl = float(p["sl"])
        tp1 = float(p["tp1"])
        tp = float(p["tp"])

        # 1. Periksa candle aktif tempat sinyal muncul hanya lewat harga Close
        candle_entry = df[(idx >= (t0 - pd.Timedelta(minutes=15))) & (idx < t0)]
        for _, r in candle_entry.iterrows():
            cl = float(r["Close"])
            if is_buy:
                if cl <= sl:
                    hasil["hasil"] = "SL"
                    return hasil
                if cl >= tp:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    return hasil
                if cl >= tp1:
                    hasil["tp1"] = True
            else:
                if cl >= sl:
                    hasil["hasil"] = "SL"
                    return hasil
                if cl <= tp:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    return hasil
                if cl <= tp1:
                    hasil["tp1"] = True

        # 2. Periksa candle-candle penuh yang membuka setelah waktu sinyal (idx >= t0)
        sub = df[idx >= t0]
        for _, r in sub.iterrows():
            hi = float(r["High"])
            lo = float(r["Low"])
            if is_buy:
                if lo <= sl:
                    hasil["hasil"] = "SL"
                    break
                if hi >= tp:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    break
                if hi >= tp1:
                    hasil["tp1"] = True
            else:
                if hi >= sl:
                    hasil["hasil"] = "SL"
                    break
                if lo <= tp:
                    hasil["hasil"] = "TP"
                    hasil["tp1"] = True
                    break
                if lo <= tp1:
                    hasil["tp1"] = True
    except Exception as e:
        log(f"evaluasi_posisi error: {e}")
    return hasil


def cek_posisi() -> None:
    """Periksa semua posisi OPEN dan kirim notifikasi saat TP1, TP, SL, atau kedaluwarsa."""
    if not tujuan_aktif():
        return
    with STATE_LOCK:
        terbuka = [dict(p) for p in POSISI if p["status"] == "OPEN"]
    if not terbuka:
        return
    per_symbol: dict[str, list[dict[str, Any]]] = {}
    for p in terbuka:
        per_symbol.setdefault(p["symbol"], []).append(p)

    for symbol, daftar in per_symbol.items():
        df = None
        if symbol in TICKER_MAP and pasar_buka(symbol):
            df = ambil_data(symbol, "5d", "15m", min_baris=10)
        for p in daftar:
            ev = evaluasi_posisi(p, df)
            pesan: str | None = None
            with STATE_LOCK:
                real = next((x for x in POSISI if x["id"] == p["id"]), None)
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
                        pesan = (
                            f"\u2705 TP TERCAPAI (+2R)\nID: {real['id']} | {real['symbol']} {real['arah']}\n"
                            f"Entry {fmt(symbol, real['entry'])} -> TP {fmt(symbol, real['tp'])}"
                        )
                    else:
                        pesan = (
                            f"\u274c SL KENA (-1R)\nID: {real['id']} | {real['symbol']} {real['arah']}\n"
                            f"Entry {fmt(symbol, real['entry'])} -> SL {fmt(symbol, real['sl'])}"
                        )
                        if real.get("tp1_kena"):
                            pesan += (
                                "\n(Catatan: posisi sempat menyentuh TP1. "
                                f"Jika SL sudah digeser ke Break Even {fmt(symbol, real['entry'])}, "
                                "hasil aktual Anda impas / 0R.)"
                            )
                elif time.time() - real["waktu"] > POSISI_KADALUARSA_JAM * 3600:
                    real["status"] = "CLOSED"
                    real["hasil"] = "EXPIRED"
                    real["tutup"] = time.time()
                    pesan = (
                        f"\u231b Sinyal ID {real['id']} ({real['symbol']} {real['arah']}) kedaluwarsa "
                        f"(tidak kena TP/SL dalam {POSISI_KADALUARSA_JAM} jam)."
                    )
                elif tp1_baru:
                    be_harga = fmt(symbol, real["entry"])
                    tp1_harga = fmt(symbol, real["tp1"])
                    tp_harga = fmt(symbol, real["tp"])
                    sl_awal = fmt(symbol, real["sl"])
                    pesan = (
                        "\U0001f514 TP1 TERSENTUH (+1R) — AMANKAN POSISI!\n"
                        f"ID: {real['id']} | {real['symbol']} {real['arah']}\n"
                        f"Entry   : {be_harga}\n"
                        f"TP1     : {tp1_harga} (Tercapai)\n"
                        f"TP Akhir: {tp_harga} (Target +2R)\n"
                        f"SL Awal : {sl_awal}\n\n"
                        "\U0001f6e1\ufe0f PENGINGAT MANAJEMEN POSISI:\n"
                        f"Segera geser SL Anda ke titik Break Even (BE) di harga {be_harga} "
                        "agar transaksi menjadi bebas risiko (Free Trade)."
                    )
            if pesan:
                simpan_state()
                kirim(pesan, warna_hasil(pesan))


def statistik(daftar: list[dict[str, Any]]) -> tuple[int, int, int, float, float]:
    """Hitung total sinyal selesai, TP, SL, win rate (%), dan net R."""
    tp = sum(1 for p in daftar if p.get("hasil") == "TP")
    sl = sum(1 for p in daftar if p.get("hasil") == "SL")
    n = tp + sl
    wr = (tp * 100.0 / n) if n else 0.0
    r = tp * 2.0 - sl * 1.0
    return n, tp, sl, wr, r


def teks_performa() -> str:
    """Susun laporan statistik performa seluruh sinyal historis."""
    with STATE_LOCK:
        selesai = [p for p in POSISI if p.get("status") == "CLOSED" and p.get("hasil") in ("TP", "SL")]
        terbuka = [p for p in POSISI if p.get("status") == "OPEN"]
    b = ["\U0001f4c8 PERFORMA SINYAL BOT", ""]
    if not selesai:
        b.append("Belum ada sinyal yang selesai (kena TP/SL).")
    else:
        n, tp, sl, wr, r = statistik(selesai)
        b.append(f"Semua sinyal selesai: {n} (TP {tp} | SL {sl})")
        b.append(f"Win rate: {wr:.1f}% | Total: {r:+.1f}R")
        sl_tp1 = sum(1 for p in selesai if p.get("hasil") == "SL" and p.get("tp1_kena"))
        if sl_tp1:
            b.append(f"SL yang sempat menyentuh TP1 dulu: {sl_tp1}")
        diambil = [p for p in selesai if p.get("diambil")]
        if diambil:
            n2, tp2, sl2, wr2, r2 = statistik(diambil)
            b.append("")
            b.append(f"Yang kamu tandai diambil: {n2} (TP {tp2} | SL {sl2})")
            b.append(f"Win rate: {wr2:.1f}% | Total: {r2:+.1f}R")
        pairs = sorted({p["symbol"] for p in selesai})
        if pairs:
            b.append("")
            b.append("Per pair:")
            for pr in pairs:
                n3, _, _, wr3, r3 = statistik([p for p in selesai if p["symbol"] == pr])
                b.append(f"- {pr}: {n3} sinyal | WR {wr3:.0f}% | {r3:+.1f}R")
    b.append("")
    if terbuka:
        b.append(f"Posisi terbuka ({len(terbuka)}):")
        for p in terbuka:
            tambahan = " [TP1 kena]" if p.get("tp1_kena") else ""
            b.append(
                f"- ID {p['id']} | {p['symbol']} {p['arah']} | Entry {fmt(p['symbol'], p['entry'])} "
                f"| SL {fmt(p['symbol'], p['sl'])} | TP {fmt(p['symbol'], p['tp'])}{tambahan}"
            )
    else:
        b.append("Tidak ada posisi terbuka.")
    b.append("")
    b.append(
        "Catatan: dihitung dari harga Yahoo Finance (15M). Jika TP dan SL tersentuh di candle yang sama, "
        "dihitung SL. Hasil di broker bisa berbeda."
    )
    return "\n".join(b)


def cek_rekap_mingguan() -> None:
    """Kirim ringkasan performa mingguan otomatis setiap Sabtu pagi waktu lokal."""
    global REKAP_MINGGUAN_TERAKHIR
    if not tujuan_aktif():
        return
    now_lokal = waktu_lokal()
    if now_lokal.weekday() != 5 or not (6 <= now_lokal.hour < 12):
        return
    minggu_ini = now_lokal.strftime("%G-W%V")
    with STATE_LOCK:
        if REKAP_MINGGUAN_TERAKHIR == minggu_ini:
            return
        batas_7hari = time.time() - 7 * 86400
        selesai_minggu = [
            p
            for p in POSISI
            if p.get("status") == "CLOSED"
            and p.get("hasil") in ("TP", "SL")
            and float(p.get("tutup", p.get("waktu", 0))) >= batas_7hari
        ]
        expired_minggu = sum(
            1
            for p in POSISI
            if p.get("status") == "CLOSED"
            and p.get("hasil") == "EXPIRED"
            and float(p.get("tutup", p.get("waktu", 0))) >= batas_7hari
        )
        terbuka = [p for p in POSISI if p.get("status") == "OPEN"]

    tgl_str = f"{now_lokal.strftime('%d/%m/%Y %H:%M')} {LABEL_ZONA}"
    b = [
        f"\U0001f4ca REKAP PERFORMA MINGGUAN ({minggu_ini})",
        f"Waktu laporan: {tgl_str}",
        "",
    ]

    if not selesai_minggu:
        b.append("Jumlah sinyal selesai (TP/SL) minggu ini: 0")
        b.append("Win rate: - | Total R: +0.0R")
        b.append("Pair terbaik: belum ada data transaksi selesai minggu ini.")
        warna = WARNA_ABU
    else:
        n, tp, sl, wr, total_r = statistik(selesai_minggu)
        b.append(f"Jumlah sinyal selesai: {n} (TP {tp} | SL {sl})")
        b.append(f"Win rate minggu ini: {wr:.1f}%")
        b.append(f"Total R minggu ini: {total_r:+.1f}R")

        per_pair: dict[str, list[dict[str, Any]]] = {}
        for p in selesai_minggu:
            per_pair.setdefault(p["symbol"], []).append(p)

        best_pair = "-"
        best_stat = (-999999.0, -1.0, 0)
        rincian_pair: list[str] = []
        for pr in sorted(per_pair.keys()):
            np_cnt, _, _, wrp, rp = statistik(per_pair[pr])
            rincian_pair.append(f"- {pr}: {np_cnt} sinyal | WR {wrp:.0f}% | {rp:+.1f}R")
            skor_rank = (rp, wrp, np_cnt)
            if skor_rank > best_stat:
                best_stat = skor_rank
                best_pair = f"{pr} ({rp:+.1f}R, WR {wrp:.0f}% dari {np_cnt} sinyal)"

        b.append(f"Pair terbaik: {best_pair}")
        b.append("")
        b.append("Rincian per pair minggu ini:")
        b.extend(rincian_pair)
        warna = WARNA_HIJAU if total_r > 0 else (WARNA_MERAH if total_r < 0 else WARNA_BIRU)

    if expired_minggu:
        b.append("")
        b.append(f"Sinyal kedaluwarsa minggu ini: {expired_minggu}")
    if terbuka:
        b.append(f"Posisi masih terbuka saat ini: {len(terbuka)}")

    b.append("")
    b.append("Selamat berakhir pekan! Evaluasi jurnal trading dan tetap disiplin dengan manajemen risiko.")

    if kirim("\n".join(b), warna):
        with STATE_LOCK:
            REKAP_MINGGUAN_TERAKHIR = minggu_ini
        simpan_state()
        log(f"Rekap mingguan {minggu_ini} terkirim.")


# ---------------------------------------------------------------------
# PANTAU POSISI LIVE (!posisi)
# ---------------------------------------------------------------------
def teks_posisi_live() -> str:
    """Ambil harga terkini dan tampilkan floating P/L serta jarak ke TP1, TP, dan SL."""
    with STATE_LOCK:
        terbuka = [dict(p) for p in POSISI if p.get("status") == "OPEN"]

    if not terbuka:
        return "\U0001f4ed Tidak ada posisi yang sedang OPEN saat ini."

    symbols = sorted({p["symbol"] for p in terbuka})
    harga_live: dict[str, float | None] = {}
    for sym in symbols:
        if sym in TICKER_MAP:
            df = ambil_data(sym, "5d", "15m", min_baris=5)
            harga_live[sym] = float(df["Close"].iloc[-1]) if (df is not None and len(df) > 0) else None
        else:
            harga_live[sym] = None

    b = [
        f"\U0001f4cc POSISI TERBUKA ({len(terbuka)}) — {waktu_lokal().strftime('%d/%m/%Y %H:%M')} {LABEL_ZONA}",
        "",
    ]

    for p in terbuka:
        sym = p["symbol"]
        arah = p["arah"]
        entry = float(p["entry"])
        sl = float(p["sl"])
        tp1 = float(p["tp1"])
        tp = float(p["tp"])
        live = harga_live.get(sym)
        tag_ambil = "Diambil" if p.get("diambil") else "Pantau"
        tag_tp1 = " | TP1 SUDAH KENA (SL -> BE)" if p.get("tp1_kena") else ""
        b.append(f"\u25aa\ufe0f ID {p['id']} | {sym} {arah} [{tag_ambil}]{tag_tp1}")
        b.append(f"   Entry: {fmt(sym, entry)} | SL: {fmt(sym, sl)} | TP1: {fmt(sym, tp1)} | TP: {fmt(sym, tp)}")

        if live is None:
            b.append("   Harga Live: gagal diambil dari Yahoo Finance")
        else:
            jarak_r = abs(entry - sl)
            if arah == "BUY":
                floating_diff = live - entry
                dist_tp1 = tp1 - live
                dist_tp = tp - live
                dist_sl = live - sl
            else:
                floating_diff = entry - live
                dist_tp1 = live - tp1
                dist_tp = live - tp
                dist_sl = sl - live

            floating_r = (floating_diff / jarak_r) if jarak_r > 0 else 0.0
            status_pl = f"{floating_r:+.2f}R ({fmt_jarak(sym, abs(floating_diff))})"
            b.append(f"   Harga Live: {fmt(sym, live)} | Floating: {status_pl}")

            ket_tp1 = "Tercapai" if (p.get("tp1_kena") or dist_tp1 <= 0) else fmt_jarak(sym, dist_tp1)
            ket_tp = "Tercapai" if dist_tp <= 0 else fmt_jarak(sym, dist_tp)
            ket_sl = "Tersentuh" if dist_sl <= 0 else fmt_jarak(sym, dist_sl)
            b.append(f"   Jarak -> TP1: {ket_tp1} | TP: {ket_tp} | SL: {ket_sl}")
        b.append("")

    return "\n".join(b).strip()


# ---------------------------------------------------------------------
# SCANNER (satu putaran per run)
# ---------------------------------------------------------------------
def petunjuk_ambil(pid: str) -> str:
    """Kembalikan instruksi perintah !ambil jika bot berjalan dalam mode bot."""
    if MODE == "bot":
        return f"\n\nSudah kamu ambil entry ini? Ketik {PREFIX}ambil {pid}"
    return ""


def scan_sekali(paksa: bool = False) -> int:
    """Pindai seluruh watchlist satu kali dan kirim sinyal valid."""
    global SCAN_TERAKHIR
    terkirim = 0
    dicoba = 0
    gagal_data = 0
    log("Mulai scan watchlist...")
    try:
        kirim_peringatan_berita()
    except Exception as e:
        log(f"peringatan berita error: {e}")
    with STATE_LOCK:
        daftar_wl = list(WATCHLIST)
    for symbol in daftar_wl:
        with STATE_LOCK:
            aktif = SCAN_AKTIF
        if not tujuan_aktif() or (not aktif and not paksa):
            break
        if symbol not in TICKER_MAP:
            continue
        if not pasar_buka(symbol):
            log(f"Scan {symbol} dilewati: pasar tutup.")
            continue
        try:
            h = analisa_pair(symbol, buat_gambar=True)
            dicoba += 1
            if (not h["ok"]) and "Gagal mengambil data" in str(h.get("error")):
                gagal_data += 1
            if not h["ok"]:
                log(f"Scan {symbol}: {h['error']}")
            elif not h["valid"]:
                log(f"Scan {symbol}: tidak ada sinyal ({h['alasan_tahan'] or h['keputusan']})")
            else:
                arah = h["keputusan"]
                boleh = True
                with STATE_LOCK:
                    prev = LAST_SIGNAL.get(symbol)
                    if prev and prev.get("arah") == arah and (
                        (time.time() - float(prev.get("waktu", 0))) < COOLDOWN_JAM * 3600
                    ):
                        boleh = False
                if not boleh:
                    log(f"Scan {symbol}: sinyal {arah} sama, ditahan cooldown.")
                    if h.get("chart_path") and Path(h["chart_path"]).exists():
                        Path(h["chart_path"]).unlink(missing_ok=True)
                else:
                    pid, baru = catat_sinyal(h)
                    if baru:
                        kirim(
                            f"\U0001f6a8 SINYAL OTOMATIS\n\n{format_pesan(h)}{petunjuk_ambil(pid)}",
                            warna_arah(arah),
                            chart_path=h.get("chart_path"),
                        )
                        terkirim += 1
                        log(f"Notifikasi {symbol} {arah} terkirim.")
                    else:
                        log(f"Scan {symbol}: posisi {arah} masih terbuka, tidak dikirim ulang.")
                        if h.get("chart_path") and Path(h["chart_path"]).exists():
                            Path(h["chart_path"]).unlink(missing_ok=True)
        except Exception as e:
            log(f"Scan {symbol} error: {e}")
        time.sleep(4)
    SCAN_TERAKHIR = time.time()
    if dicoba > 0 and gagal_data == dicoba and boleh_peringatan("data_gagal", 6):
        kirim(
            "\u26a0\ufe0f Semua pair gagal diambil datanya dari Yahoo Finance. "
            "Kemungkinan koneksi bermasalah atau Yahoo membatasi IP (umum terjadi di server cloud). "
            "Bot akan mencoba lagi di jadwal berikutnya.",
            WARNA_KUNING,
        )
    log("Scan selesai.")
    return terkirim


def atur_radar(nyala: bool) -> None:
    """Aktifkan atau matikan pemindaian otomatis watchlist."""
    global SCAN_AKTIF
    with STATE_LOCK:
        SCAN_AKTIF = nyala
    simpan_state()


# ---------------------------------------------------------------------
# AKSI & PERINTAH
# ---------------------------------------------------------------------
def normalisasi_pair(teks: str) -> str:
    """Normalisasi input nama pair atau alias menjadi simbol baku."""
    pair = re.sub(r"[^A-Z]", "", str(teks).upper())
    return ALIAS_PAIR.get(pair, pair)


def jalankan_analisa(pair: str) -> None:
    """Jalankan analisa manual untuk satu pair beserta pengiriman chart."""
    kirim(f"\u23f3 Menganalisa {pair} (teknikal + chart + kalender + berita + AI)... tunggu sebentar.")
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


def teks_status() -> str:
    """Susun ringkasan status konfigurasi dan posisi aktif bot."""
    with STATE_LOCK:
        m = MODAL_AWAL_USD
        aktif = SCAN_AKTIF
        wl = list(WATCHLIST)
        sinyal = dict(LAST_SIGNAL)
        n_buka = sum(1 for p in POSISI if p.get("status") == "OPEN")
        admin = ADMIN_USER_ID
    b = [
        "\u2699\ufe0f STATUS",
        f"Mode: {MODE} | Admin ID: {admin if admin else 'belum ada'}",
        f"Modal: ${m:.2f}",
        f"Risiko/trade: {RISIKO_PER_TRADE_PERSEN:.1f}% (= ${m * RISIKO_PER_TRADE_PERSEN / 100.0:.2f})",
        f"Radar: {'AKTIF' if aktif else 'MATI'} | Chart: {'AKTIF' if (KIRIM_CHART and HAS_MPL) else 'MATI'}",
        f"Watchlist: {', '.join(wl)}",
        f"Model AI: {GEMINI_MODEL} | Min keyakinan: {MIN_KEYAKINAN}",
        f"Filter: ADX >= {ADX_MIN} | RSI 15M {RSI_AMAN_MIN}-{RSI_AMAN_MAX}",
        f"Blokir berita: -{NEWS_BLOK_SEBELUM_MENIT} / +{NEWS_BLOK_SESUDAH_MENIT} menit",
        f"Posisi terlacak terbuka: {n_buka}",
    ]
    if sinyal:
        b.append("")
        b.append("Sinyal terakhir:")
        for k, v in sinyal.items():
            try:
                waktu_str = f"{waktu_lokal(float(v['waktu'])).strftime('%d/%m %H:%M')} {LABEL_ZONA}"
                b.append(f"- {k} {v['arah']} ({waktu_str})")
            except (KeyError, TypeError, ValueError):
                pass
    return "\n".join(b)


def teks_bantuan() -> str:
    """Susun panduan daftar perintah bot Discord."""
    return (
        "\U0001f916 FOREX AI BOT (Discord)\n\n"
        "Perintah:\n"
        f"{PREFIX}analisa <PAIR>  - analisa lengkap + chart (contoh: {PREFIX}analisa XAUUSD)\n"
        f"{PREFIX}posisi  - lihat posisi OPEN, harga live, dan jarak ke TP1/TP/SL\n"
        f"{PREFIX}watchlist [add|del <PAIR>]  - lihat atau atur daftar pantauan pair\n"
        f"{PREFIX}scan on | off | now  - radar otomatis (now = pindai sekarang)\n"
        f"{PREFIX}modal <USD>  - ubah modal (contoh: {PREFIX}modal 500)\n"
        f"{PREFIX}performa  - statistik win rate sinyal\n"
        f"{PREFIX}status  - pengaturan saat ini\n"
        f"{PREFIX}ambil <ID>  - tandai sinyal yang kamu ambil\n"
        f"{PREFIX}help  - panduan ini\n\n"
        f"Pair didukung: {', '.join(TICKER_MAP.keys())}\n"
        f"Hanya sinyal berkeyakinan >= {MIN_KEYAKINAN} yang dikirim. Sinyal diblokir menjelang berita "
        "High Impact dan saat pasar tutup.\n"
        f"Risiko per trade: {RISIKO_PER_TRADE_PERSEN:.1f}% dari modal.\n\n"
        "Mode GitHub: perintah dibalas saat jadwal berikutnya berjalan, jadi bisa tertunda beberapa menit."
    )


def tangani_perintah(uid: int, isi: str) -> None:
    """Proses dan eksekusi perintah chat dari admin bot."""
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
            log(f"ADMIN_USER_ID disimpan: {admin}")
        if uid != admin:
            kirim("Bot ini privat.")
            return
        awal = f"\u2705 Kamu (ID {uid}) tercatat sebagai admin bot.\n\n" if baru else ""
        kirim(awal + teks_bantuan())
        return

    with STATE_LOCK:
        admin = ADMIN_USER_ID
    if admin == 0:
        kirim(f"Ketik {PREFIX}start dulu untuk mengaktifkan bot.")
        return
    if uid != admin:
        kirim("Bot ini privat.")
        return

    if perintah == "analisa":
        if not args:
            kirim(
                f"Format: {PREFIX}analisa <PAIR>\nContoh: {PREFIX}analisa XAUUSD\n"
                f"Pair: {', '.join(TICKER_MAP.keys())}"
            )
            return
        pair = normalisasi_pair(args[0])
        if pair not in TICKER_MAP:
            kirim(f"Pair '{args[0]}' tidak dikenal.\nPair tersedia: {', '.join(TICKER_MAP.keys())}")
            return
        jalankan_analisa(pair)

    elif perintah == "posisi":
        kirim(teks_posisi_live(), WARNA_BIRU)

    elif perintah == "watchlist":
        sub = args[0].lower() if args else "list"
        if sub in ("list", "lihat"):
            with STATE_LOCK:
                wl = list(WATCHLIST)
            baris_wl = [f"- {sym} ({'Buka' if pasar_buka(sym) else 'Tutup'})" for sym in wl]
            kirim(
                f"\U0001f4cb WATCHLIST AKTIF ({len(wl)} pair)\n"
                f"{chr(10).join(baris_wl) if baris_wl else '(kosong)'}\n\n"
                f"Pair didukung: {', '.join(TICKER_MAP.keys())}\n"
                f"Tambah: {PREFIX}watchlist add <PAIR>\n"
                f"Hapus : {PREFIX}watchlist del <PAIR>",
                WARNA_BIRU,
            )
        elif sub == "add":
            if len(args) < 2:
                kirim(f"Format: {PREFIX}watchlist add <PAIR>\nPair didukung: {', '.join(TICKER_MAP.keys())}")
                return
            pair = normalisasi_pair(args[1])
            if pair not in TICKER_MAP:
                kirim(f"Pair '{args[1]}' tidak didukung.\nPair tersedia: {', '.join(TICKER_MAP.keys())}")
                return
            with STATE_LOCK:
                if pair in WATCHLIST:
                    kirim(f"\u2139\ufe0f {pair} sudah ada di watchlist: {', '.join(WATCHLIST)}")
                    return
                WATCHLIST.append(pair)
                wl_baru = list(WATCHLIST)
            simpan_state()
            kirim(f"\u2705 {pair} ditambahkan ke watchlist.\nWatchlist aktif: {', '.join(wl_baru)}", WARNA_HIJAU)
        elif sub in ("del", "remove", "hapus"):
            if len(args) < 2:
                kirim(f"Format: {PREFIX}watchlist del <PAIR>")
                return
            pair = normalisasi_pair(args[1])
            with STATE_LOCK:
                if pair not in WATCHLIST:
                    kirim(f"\u2139\ufe0f {pair} tidak ada di watchlist.\nWatchlist aktif: {', '.join(WATCHLIST)}")
                    return
                if len(WATCHLIST) <= 1:
                    kirim("\u26a0\ufe0f Watchlist minimal harus memiliki 1 pair aktif.")
                    return
                WATCHLIST.remove(pair)
                wl_baru = list(WATCHLIST)
            simpan_state()
            kirim(f"\u2705 {pair} dihapus dari watchlist.\nWatchlist aktif: {', '.join(wl_baru)}", WARNA_HIJAU)
        else:
            kirim(
                f"Sub-perintah tidak dikenal.\nGunakan: {PREFIX}watchlist, "
                f"{PREFIX}watchlist add <PAIR>, atau {PREFIX}watchlist del <PAIR>"
            )

    elif perintah == "scan":
        sub = args[0].lower() if args else ""
        if sub in ("on", "off"):
            atur_radar(sub == "on")
            kirim("\u2705 Radar AKTIF." if sub == "on" else "\U0001f6d1 Radar dimatikan.")
        elif sub == "now":
            kirim("\u23f3 Memindai watchlist... bisa makan waktu 1-2 menit.")
            n = scan_sekali(paksa=True)
            kirim(f"\u2705 Scan selesai. Sinyal baru: {n}")
        else:
            with STATE_LOCK:
                status = "AKTIF" if SCAN_AKTIF else "MATI"
            kirim(f"Radar saat ini: {status}\nGunakan {PREFIX}scan on, {PREFIX}scan off, atau {PREFIX}scan now")

    elif perintah == "modal":
        if not args:
            with STATE_LOCK:
                m = MODAL_AWAL_USD
            kirim(f"Modal saat ini: ${m:.2f}\nFormat: {PREFIX}modal <JUMLAH_USD>  (contoh: {PREFIX}modal 500)")
            return
        try:
            nilai = float(args[0].replace(",", "."))
        except ValueError:
            kirim(f"Jumlah tidak valid. Contoh: {PREFIX}modal 500")
            return
        if nilai <= 0 or nilai > 1_000_000_000:
            kirim("Jumlah harus lebih dari 0.")
            return
        with STATE_LOCK:
            MODAL_AWAL_USD = nilai
        simpan_state()
        kirim(
            f"\u2705 Modal diubah menjadi ${nilai:.2f}.\n"
            f"Risiko per trade: ${nilai * RISIKO_PER_TRADE_PERSEN / 100.0:.2f} ({RISIKO_PER_TRADE_PERSEN:.1f}%)."
        )

    elif perintah == "performa":
        kirim(teks_performa())

    elif perintah == "status":
        kirim(teks_status())

    elif perintah == "ambil":
        if not args:
            kirim(f"Format: {PREFIX}ambil <ID sinyal>")
            return
        ok = tandai_diambil(args[0])
        kirim(f"\u2705 Sinyal {args[0]} ditandai sudah kamu ambil." if ok else "ID sinyal tidak ditemukan.")

    else:
        kirim(f"Perintah tidak dikenal. Ketik {PREFIX}help untuk panduan.")


def proses_perintah() -> None:
    """Baca pesan baru di channel dan proses setiap perintah admin."""
    baseline = not PESAN_TERAKHIR_ID
    pesan = ambil_pesan_baru()
    if pesan is None:
        log(
            "Gagal membaca channel. Cek token bot, ID channel, dan izin bot "
            "(View Channel, Read Message History, Send Messages, Attach Files)."
        )
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
            tangani_perintah(int(penulis["id"]), teks[len(PREFIX) :])
            n += 1
        except Exception as e:
            log(f"Gagal memproses pesan: {e}")
    log(f"{n} perintah diproses.")
    if baseline and n == 0:
        kirim(
            f"\u2705 Bot terhubung ke channel ini.\nKetik {PREFIX}start untuk mencatat dirimu sebagai admin "
            "dan melihat daftar perintah. Balasan datang saat jadwal berikutnya berjalan."
        )
    if kosong and n == 0 and boleh_peringatan("intent", 6):
        kirim(
            "\u26a0\ufe0f Bot menerima pesan tanpa isi. Aktifkan Message Content Intent di Discord "
            "Developer Portal (menu Bot), lalu coba kirim perintah lagi.",
            WARNA_KUNING,
        )


# ---------------------------------------------------------------------
# SATU PUTARAN RUN
# ---------------------------------------------------------------------
def jalankan_sekali() -> None:
    """Jalankan satu siklus penuh: muat state, proses perintah, cek posisi, rekap, dan scan."""
    global SUDAH_SAPA
    muat_state()
    if MODE == "bot":
        proses_perintah()
    elif not SUDAH_SAPA:
        kirim(
            "\u2705 Forex AI Bot aktif (mode webhook: hanya notifikasi). "
            "Pengaturan diubah lewat file konfigurasi di repo."
        )
        SUDAH_SAPA = True
    cek_posisi()
    cek_rekap_mingguan()
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
        print(
            "Konfigurasi Discord belum lengkap. Isi DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID, "
            "atau DISCORD_WEBHOOK_URL."
        )
        sys.exit(1)
    if GEMINI_API_KEY.startswith("ISI_"):
        print("PERINGATAN: GEMINI_API_KEY belum diisi. Analisa AI tidak akan berjalan.")
    if not HAS_MPL:
        log(f"PERINGATAN: Gagal import matplotlib/mplfinance, chart dinonaktifkan. Error asli: {MPL_ERROR}")
    log(f"Mode Discord: {MODE} | Model: {GEMINI_MODEL} | Chart aktif: {KIRIM_CHART and HAS_MPL}")

    if "--loop" in sys.argv:
        while True:
            try:
                jalankan_sekali()
            except Exception as e:
                log(f"Run error: {e}")
            time.sleep(max(1, INTERVAL_SCAN_MENIT) * 60)
    else:
        jalankan_sekali()
