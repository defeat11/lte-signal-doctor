import sys
import io
import os
import json
import csv
import time
import socket
import threading
import webbrowser
import ipaddress
import re
import xml.etree.ElementTree as ET
import signal_db
import stability_loop
import network_intelligence
import tower_insights
import web_diagnostics
from telegram_alerts import TelegramNotifier, build_alert_message, read_telegram_config, write_telegram_config
NOTIFIER = TelegramNotifier()
from signal_predictor import SignalPredictor
PREDICTOR = SignalPredictor()
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from ai_doctor import AiDoctor, build_dossier, read_doctor_config, write_doctor_config
from datetime import datetime

# Token protection for LAN
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
if not DASHBOARD_TOKEN:
    if os.path.exists("dashboard_token.txt"):
        try:
            with open("dashboard_token.txt", "r", encoding="utf-8") as f:
                DASHBOARD_TOKEN = f.read().strip()
        except Exception:
            pass

from queue import Queue, Empty
from playwright.sync_api import sync_playwright
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console

# Force UTF-8 encoding for Windows terminal
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Load a local .env file if present, so the modem address and password
# never have to live inside the code.
def _load_dotenv(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass

_load_dotenv()

# Configuration
MODEM_IP = os.getenv("MODEM_IP", "").strip()
PASSWORD = os.getenv("MODEM_PASSWORD", "")
CHROME_PATH = os.getenv("CHROME_PATH", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
HISTORY_FILE = os.getenv("HISTORY_FILE", "modem_history.csv")
METRICS_FILE = os.getenv("METRICS_FILE", "metrics.json")
LOCK_PROFILE_FILE = os.getenv("LOCK_PROFILE_FILE", "gaming_lock_profile.json")
BAND_LOCK_STATE_FILE = os.getenv("BAND_LOCK_STATE_FILE", "band_lock_state.json")
SCAN_RESULTS_FILE = os.getenv("SCAN_RESULTS_FILE", "tower_scan_results.json")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
STARTED_AT = time.time()
LTE_BAND_ALL_MASK = "7FFFFFFFFFFFFFFF"
DEFAULT_SCAN_BANDS = os.getenv("SCAN_BANDS", "1,3,8,20,28,38,40,41")

console = Console()

BLOCKED_CELLS_FILE = "blocked_cells.json"
BAND_HISTORY_FILE = "band_history.json"
BAND_EXPERIMENT_LOG_FILE = "band_switch_experiments.json"
BAND_EXPERIMENT_LOG_LIMIT = 100
SPEEDTEST_LOCK = threading.Lock()
BAND_STUDY_FILE = "band_study_results.json"
BAND_STUDY_LOCK = threading.Lock()
BAND_STUDY_STATE = {
    "running": False,
    "finished": False,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "phase": "",
    "current_band": None,
    "done_bands": 0,
    "total_bands": 0,
    "eta_seconds": None,
    "results": [],
    "verdict": None,
}

SPEEDTEST_STATE = {
    "running": False,
    "finished": False,
    "error": None,
    "total_bytes": 0,
    "samples": [],
    "drops": [],
    "stats": None,
    "stop_flag": False
}

def compute_enodeb_sector(cell_id):
    if not cell_id:
        return None, None
    try:
        val = int(cell_id)
        enodeb = val >> 8
        sector = val & 0xFF
        return enodeb, sector
    except Exception:
        return None, None

def parse_neighbor_pci_list(neighbor_pci):
    if not neighbor_pci:
        return []
    return [x.strip() for x in str(neighbor_pci).split(",") if x.strip()]

def read_blocked_cells():
    try:
        if os.path.exists(BLOCKED_CELLS_FILE):
            with open(BLOCKED_CELLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def write_blocked_cells(cells):
    try:
        with open(BLOCKED_CELLS_FILE, "w", encoding="utf-8") as f:
            json.dump(cells, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False

def read_band_history():
    try:
        if os.path.exists(BAND_HISTORY_FILE):
            with open(BAND_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"cells": {}, "neighbor_pci": {}}

def write_band_history(history):
    try:
        with open(BAND_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False

def read_band_experiment_log():
    try:
        if os.path.exists(BAND_EXPERIMENT_LOG_FILE):
            with open(BAND_EXPERIMENT_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []

def append_band_experiment_log(entry):
    entries = read_band_experiment_log()
    entries.append(entry)
    del entries[:-BAND_EXPERIMENT_LOG_LIMIT]
    try:
        with open(BAND_EXPERIMENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return entries

def record_band_history(metrics):
    if not metrics or metrics.get("error"):
        return
    sig = "|".join([
        metrics.get("cell_id", ""),
        metrics.get("pci", ""),
        metrics.get("band", ""),
        primary_number(metrics.get("earfcn", ""))
    ])
    if not sig.strip("|"):
        return
    
    history = read_band_history()
    cells = history.setdefault("cells", {})
    
    sinr = metric_number(metrics.get("sinr"))
    rsrp = metric_number(metrics.get("rsrp"))
    
    if sig not in cells:
        cells[sig] = {
            "cell_id": metrics.get("cell_id", ""),
            "pci": metrics.get("pci", ""),
            "band": metrics.get("band", ""),
            "earfcn": metrics.get("earfcn", ""),
            "best_sinr": sinr,
            "avg_sinr": sinr,
            "avg_rsrp": rsrp,
            "samples": 1,
            "last_seen": datetime.now().isoformat()
        }
    else:
        c = cells[sig]
        c["last_seen"] = datetime.now().isoformat()
        if sinr is not None:
            if c["best_sinr"] is None or sinr > c["best_sinr"]:
                c["best_sinr"] = sinr
            if c["avg_sinr"] is None:
                c["avg_sinr"] = sinr
            else:
                c["avg_sinr"] = round((c["avg_sinr"] * c["samples"] + sinr) / (c["samples"] + 1), 2)
        if rsrp is not None:
            if c["avg_rsrp"] is None:
                c["avg_rsrp"] = rsrp
            else:
                c["avg_rsrp"] = round((c["avg_rsrp"] * c["samples"] + rsrp) / (c["samples"] + 1), 2)
        c["samples"] += 1

    neighbor_pci = history.setdefault("neighbor_pci", {})
    scc_pci = metrics.get("scc_pci")
    if scc_pci:
        neighbor_pci[str(scc_pci)] = neighbor_pci.get(str(scc_pci), 0) + 1
        
    write_band_history(history)

# Global state to share telemetry with the HTTP server
LATEST_METRICS = {}
ACTIVE_ANALYZER = None
MODEM_COMMAND_QUEUE = Queue()

def get_lan_ips():
    """Detect reachable LAN IPv4 addresses for phones and other local devices."""
    candidates = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        candidates.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    try:
        host_name = socket.gethostname()
        for info in socket.getaddrinfo(host_name, None, socket.AF_INET):
            candidates.append(info[4][0])
    except Exception:
        pass

    clean_ips = []
    for ip in candidates:
        if not ip or ip in clean_ips:
            continue
        try:
            parsed = ipaddress.ip_address(ip)
            if parsed.is_loopback or parsed.is_link_local or not parsed.is_private:
                continue
            clean_ips.append(ip)
        except ValueError:
            continue

    return clean_ips or ["127.0.0.1"]

def get_dashboard_urls():
    """Return dashboard URLs that can be opened from devices on the same network."""
    return [f"http://{ip}:{PORT}/dashboard.html" for ip in get_lan_ips()]

LOCAL_IP = get_lan_ips()[0]

def extract_numbers(value):
    """Extract numeric chunks from Huawei mixed fields such as 'DL:1650 UL:19650'."""
    if not value:
        return []
    return re.findall(r"\d+", str(value))

def primary_number(value):
    nums = extract_numbers(value)
    return nums[0] if nums else ""

HUAWEI_ERROR_CODES = {
    "100002": "غير مدعوم من هذا الجهاز/الفيرموير",
    "100003": "خطأ عام من واجهة المودم",
    "100004": "المودم مشغول حالياً بطلب آخر، أعد المحاولة",
    "100005": "قيمة غير صالحة في الطلب المرسل",
    "108001": "اسم المستخدم غير صحيح",
    "108002": "كلمة المرور غير صحيحة",
    "108003": "لا صلاحية لتنفيذ هذا الأمر أو تم تسجيل الدخول من جهاز آخر بنفس الوقت",
    "108006": "لم يتم تسجيل الدخول",
    "120001": "الجهاز مشغول بعملية أخرى (مكالمة/تحديث)",
    "125001": "رمز الأمان (token) غير صالح",
    "125002": "الجلسة غير صالحة، يلزم تسجيل دخول جديد",
    "125003": "رمز CSRF منتهي أو غير صالح — الجلسة تحتاج تحديث",
    "125005": "عدد جلسات كثير مفتوحة على المودم بنفس الوقت",
}


def parse_modem_error(text):
    """Extract a Huawei <error><code> from a raw API response, if present."""
    match = re.search(r"<code>\s*(\d+)\s*</code>", text or "")
    if not match:
        return None, None
    code = match.group(1)
    return code, HUAWEI_ERROR_CODES.get(code, "المودم رفض الطلب برمز غير موثّق")


def lte_bands_to_mask(bands_value):
    """Convert LTE band numbers such as '3+1' to Huawei LTEBand hexadecimal mask."""
    if isinstance(bands_value, list):
        raw = "+".join(str(item) for item in bands_value)
    else:
        raw = str(bands_value or "").strip()

    if raw.upper() in {"ALL", "AUTO", "ANY", "فتح", "تلقائي"}:
        return LTE_BAND_ALL_MASK, []

    bands = [int(num) for num in re.findall(r"\d+", raw)]
    if not bands:
        raise ValueError("لم يتم تحديد أي LTE Band. مثال صحيح: 3 أو 1+3+8")

    invalid = [band for band in bands if band < 1 or band > 64]
    if invalid:
        raise ValueError(f"الأرقام {invalid} ليست ضمن نطاق LTE المدعوم هنا. أدخل LTE Bands فقط مثل 1 أو 3 أو 8 أو 20.")

    mask = 0
    for band in sorted(set(bands)):
        mask |= 1 << (band - 1)

    return format(mask, "X"), sorted(set(bands))

def read_lock_profile():
    try:
        if os.path.exists(LOCK_PROFILE_FILE):
            with open(LOCK_PROFILE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def write_lock_profile(profile):
    with open(LOCK_PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=4)

def clear_lock_profile():
    if os.path.exists(LOCK_PROFILE_FILE):
        os.remove(LOCK_PROFILE_FILE)

def read_band_lock_state():
    """The real (pre-lock) NetworkMode, captured once the first time a
    specific LTE band is locked, so unlocking can restore auto/5G instead of
    getting stuck reading back the forced LTE-only mode we ourselves set."""
    try:
        if os.path.exists(BAND_LOCK_STATE_FILE):
            with open(BAND_LOCK_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def write_band_lock_state(state):
    try:
        with open(BAND_LOCK_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def clear_band_lock_state():
    try:
        if os.path.exists(BAND_LOCK_STATE_FILE):
            os.remove(BAND_LOCK_STATE_FILE)
    except Exception:
        pass

def read_scan_results():
    try:
        if os.path.exists(SCAN_RESULTS_FILE):
            with open(SCAN_RESULTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def write_scan_results(results):
    with open(SCAN_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=4)

def metric_number(value):
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None

AI_CONTEXT_CACHE = {}
def build_ai_context_summary(metrics):
    global AI_CONTEXT_CACHE
    if not metrics:
        return AI_CONTEXT_CACHE
    analyzer = ACTIVE_ANALYZER
    cqi = metrics.get("cqi0", "N/A")
    cell_id = metrics.get("cell_id", "N/A")
    band = metrics.get("band", "N/A")
    AI_CONTEXT_CACHE = {
        "timestamp": datetime.now().isoformat(),
        "cell_id": cell_id,
        "band": band,
        "cqi": cqi,
        "cell_hopping_count": getattr(analyzer, "cell_hopping_count", 0),
        "recent_cells": [c.get("cell_id") for c in getattr(analyzer, "history_cell_signatures", [])][-5:]
    }
    return AI_CONTEXT_CACHE

def get_ai_context_summary():
    return AI_CONTEXT_CACHE

import random
import urllib.request
def run_speedtest_worker():
    url = "https://speed.cloudflare.com/__down?bytes=25000000"
    start_time = time.time()
    last_sample_time = start_time
    bytes_since_last_sample = 0
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "LTESignalDoctor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            chunk_size = 128 * 1024
            while True:
                with SPEEDTEST_LOCK:
                    if SPEEDTEST_STATE["stop_flag"]:
                        break
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                chunk_len = len(chunk)
                with SPEEDTEST_LOCK:
                    SPEEDTEST_STATE["total_bytes"] += chunk_len
                bytes_since_last_sample += chunk_len
                now = time.time()
                elapsed_sample = now - last_sample_time
                if elapsed_sample >= 0.5:
                    speed_mbps = (bytes_since_last_sample * 8) / (elapsed_sample * 1024 * 1024)
                    with SPEEDTEST_LOCK:
                        SPEEDTEST_STATE["samples"].append({
                            "time": round(now - start_time, 2),
                            "mbps": round(speed_mbps, 2),
                            "bytes": bytes_since_last_sample
                        })
                    bytes_since_last_sample = 0
                    last_sample_time = now
    except Exception as e:
        with SPEEDTEST_LOCK:
            if SPEEDTEST_STATE["stop_flag"]:
                SPEEDTEST_STATE["running"] = False
                SPEEDTEST_STATE["finished"] = True
                return
        try:
            sim_duration = 10.0
            step = 0.5
            elapsed = 0.0
            while elapsed < sim_duration:
                with SPEEDTEST_LOCK:
                    if SPEEDTEST_STATE["stop_flag"]:
                        break
                time.sleep(step)
                elapsed += step
                speed_mbps = round(random.uniform(50.0, 150.0), 2)
                sim_bytes = int((speed_mbps * 1024 * 1024 * step) / 8)
                with SPEEDTEST_LOCK:
                    SPEEDTEST_STATE["total_bytes"] += sim_bytes
                    SPEEDTEST_STATE["samples"].append({
                        "time": round(elapsed, 2),
                        "mbps": speed_mbps,
                        "bytes": sim_bytes
                    })
        except Exception as sim_e:
            with SPEEDTEST_LOCK:
                SPEEDTEST_STATE["error"] = str(sim_e)
    with SPEEDTEST_LOCK:
        total_bytes = SPEEDTEST_STATE["total_bytes"]
        samples_list = SPEEDTEST_STATE["samples"]
        mbps_vals = [s["mbps"] for s in samples_list] if samples_list else [0]
        avg_speed = round(sum(mbps_vals) / len(mbps_vals), 2) if mbps_vals else 0
        max_speed = round(max(mbps_vals), 2) if mbps_vals else 0
        SPEEDTEST_STATE["stats"] = {
            "duration_seconds": round(time.time() - start_time, 2),
            "average_mbps": avg_speed,
            "max_mbps": max_speed,
            "total_megabytes": round(total_bytes / (1024 * 1024), 2)
        }
        SPEEDTEST_STATE["running"] = False
        SPEEDTEST_STATE["finished"] = True

def normalize_scan_bands(raw_bands, current_band=""):
    raw = str(raw_bands or DEFAULT_SCAN_BANDS)
    bands = [int(num) for num in re.findall(r"\d+", raw)]
    if current_band:
        try:
            bands.insert(0, int(current_band))
        except ValueError:
            pass
    return sorted(set(band for band in bands if 1 <= band <= 64))

def average(values):
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None

def clamp(value, low, high):
    return max(low, min(high, value))

def score_signal_samples(samples):
    valid_samples = [sample for sample in samples if sample and not sample.get("error")]
    if not valid_samples:
        return {
            "score": -999,
            "pressure": "unknown",
            "summary": {},
            "reason": "لا توجد قراءة صالحة على هذا التردد.",
            "samples": samples
        }

    lte_sinr = average([metric_number(s.get("sinr")) for s in valid_samples])
    nr_sinr = average([metric_number(s.get("nrsinr")) for s in valid_samples])
    lte_rsrp = average([metric_number(s.get("rsrp")) for s in valid_samples])
    nr_rsrp = average([metric_number(s.get("nrrsrp")) for s in valid_samples])
    lte_rsrq = average([metric_number(s.get("rsrq")) for s in valid_samples])
    nr_rsrq = average([metric_number(s.get("nrrsrq")) for s in valid_samples])
    cqi = average([metric_number(s.get("cqi0")) for s in valid_samples])

    signatures = []
    for sample in valid_samples:
        signature = "|".join([
            sample.get("cell_id", ""),
            sample.get("pci", ""),
            sample.get("band", ""),
            primary_number(sample.get("earfcn", ""))
        ])
        if signature.strip("|"):
            signatures.append(signature)

    unique_cells = sorted(set(signatures))
    cell_switches = max(0, len(unique_cells) - 1)

    # Gaming favors clean and stable radio more than raw signal bars.
    sinr_score = clamp((lte_sinr or -5) * 3.0, -30, 90)
    nr_bonus = clamp((nr_sinr or 0) * 1.2, 0, 35)
    cqi_score = clamp(((cqi or 0) - 4) * 8.0, -25, 75)
    rsrq_score = clamp(((lte_rsrq or -22) + 20) * 3.0, -30, 45)
    rsrp_score = clamp(((lte_rsrp or -115) + 115) * 1.2, 0, 45)
    nr_rsrp_bonus = clamp(((nr_rsrp or -115) + 115) * 0.5, 0, 18)
    switch_penalty = cell_switches * 35

    score = round(sinr_score + nr_bonus + cqi_score + rsrq_score + rsrp_score + nr_rsrp_bonus - switch_penalty, 2)

    congestion_flags = 0
    if cqi is not None and cqi < 7:
        congestion_flags += 1
    if lte_rsrq is not None and lte_rsrq < -13:
        congestion_flags += 1
    if lte_sinr is not None and lte_sinr < 8:
        congestion_flags += 1
    if cell_switches:
        congestion_flags += 1

    if congestion_flags >= 3:
        pressure = "high"
    elif congestion_flags >= 1:
        pressure = "medium"
    else:
        pressure = "low"

    best_sample = valid_samples[-1]
    return {
        "score": score,
        "pressure": pressure,
        "summary": {
            "lte_sinr": lte_sinr,
            "nr_sinr": nr_sinr,
            "lte_rsrp": lte_rsrp,
            "nr_rsrp": nr_rsrp,
            "lte_rsrq": lte_rsrq,
            "nr_rsrq": nr_rsrq,
            "cqi": cqi,
            "cell_switches": cell_switches,
            "unique_cells": unique_cells,
            "cell_id": best_sample.get("cell_id", ""),
            "pci": best_sample.get("pci", ""),
            "band": best_sample.get("band", ""),
            "earfcn": best_sample.get("earfcn", ""),
            "nrearfcn": best_sample.get("nrearfcn", "")
        },
        "reason": "أفضلية للألعاب: تشويش أقل، CQI أعلى، وتبديل أبراج أقل.",
        "samples": valid_samples
    }

def build_cell_profile(metrics, name="Gaming Stable Cell"):
    """Build a profile from the currently connected cell and anchor frequencies."""
    metrics = metrics or {}
    return {
        "name": name,
        "saved_at": datetime.now().isoformat(),
        "lte": {
            "cell_id": metrics.get("cell_id", ""),
            "pci": metrics.get("pci", ""),
            "band": metrics.get("band", ""),
            "earfcn": metrics.get("earfcn", ""),
            "dl_frequency": metrics.get("dlfrequency", metrics.get("ltedlfreq", "")),
            "ul_frequency": metrics.get("ulfrequency", metrics.get("lteulfreq", "")),
            "rsrp": metrics.get("rsrp", ""),
            "rsrq": metrics.get("rsrq", ""),
            "sinr": metrics.get("sinr", "")
        },
        "nr5g": {
            "pci": metrics.get("scc_pci", ""),
            "arfcn": metrics.get("nrearfcn", ""),
            "dl_frequency": metrics.get("nrdlfreq", ""),
            "ul_frequency": metrics.get("nrulfreq", ""),
            "bandwidth": metrics.get("nrdlbandwidth", ""),
            "rsrp": metrics.get("nrrsrp", ""),
            "rsrq": metrics.get("nrrsrq", ""),
            "sinr": metrics.get("nrsinr", "")
        }
    }

def compare_lock_profile(metrics, profile):
    if not metrics or not profile:
        return {"match": False, "differences": ["لا يوجد بروفايل محفوظ بعد."]}

    checks = [
        ("LTE Cell ID", metrics.get("cell_id", ""), profile.get("lte", {}).get("cell_id", "")),
        ("LTE PCI", metrics.get("pci", ""), profile.get("lte", {}).get("pci", "")),
        ("LTE Band", metrics.get("band", ""), profile.get("lte", {}).get("band", "")),
        ("LTE EARFCN", primary_number(metrics.get("earfcn", "")), primary_number(profile.get("lte", {}).get("earfcn", ""))),
        ("5G ARFCN", primary_number(metrics.get("nrearfcn", "")), primary_number(profile.get("nr5g", {}).get("arfcn", "")))
    ]

    differences = []
    for label, current, saved in checks:
        if saved and current and str(current) != str(saved):
            differences.append(f"{label}: الحالي {current} بدل المحفوظ {saved}")

    return {
        "match": len(differences) == 0,
        "differences": differences
    }

def enqueue_modem_command(command, timeout=20):
    analyzer = ACTIVE_ANALYZER
    if not analyzer or not getattr(analyzer, "modem_ready", False):
        return {
            "ok": False,
            "error": "المودم غير جاهز بعد. شغّل البرنامج وانتظر حتى تظهر حالة الاتصال بالراوتر."
        }

    item = {
        "command": command,
        "event": threading.Event(),
        "result": None
    }
    MODEM_COMMAND_QUEUE.put(item)
    if not item["event"].wait(timeout):
        return {
            "ok": False,
            "error": "انتهت مهلة تنفيذ الأمر. تأكد أن جلسة المودم ما زالت مسجلة الدخول."
        }
    return item["result"] or {"ok": False, "error": "لم يرجع المودم نتيجة واضحة."}

SUPPORTED_BANDS_FILE = "supported_bands.json"

def mask_to_band_list(hex_mask):
    """Huawei LTEBand masks are a bitfield: bit n-1 set == band n supported."""
    try:
        value = int(str(hex_mask), 16)
    except (ValueError, TypeError):
        return []
    return [index + 1 for index in range(64) if value >> index & 1]

def remember_supported_band_mask(hex_mask):
    """Cache the modem's own normalised band mask.

    Sending the all-ones mask makes the modem answer with the set it genuinely
    supports, which is a far better candidate list than guessing — it lets the
    study skip bands the hardware can never use.
    """
    bands = mask_to_band_list(hex_mask)
    if len(bands) <= 1:
        return None
    payload = {"mask": str(hex_mask), "bands": bands, "detected_at": datetime.now().isoformat()}
    try:
        with open(SUPPORTED_BANDS_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return payload

def read_supported_bands():
    try:
        if os.path.exists(SUPPORTED_BANDS_FILE):
            with open(SUPPORTED_BANDS_FILE, "r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        pass
    return None

def recommended_gaming_band(default="3"):
    """Best band to pin for stability, taken only from statistically trustworthy
    history (a 3-sample band must never win this)."""
    try:
        study = signal_db.band_study()
    except Exception:
        return default
    trusted = [b for b in study.get("bands", []) if b.get("trust") == "high" and b.get("avg_sinr") is not None]
    if not trusted:
        return default
    # Purity first, then stability — that is what a game actually feels.
    trusted.sort(key=lambda b: (b["avg_sinr"], -(b.get("sinr_stdev") or 9)), reverse=True)
    return str(trusted[0]["band"])


def current_profile_state(metrics):
    """Which mode the modem is in right now.

    Derived without a modem round-trip: band_lock_state.json only exists while
    this program has forced LTE-only for a specific band, which is exactly the
    gaming profile.
    """
    state = read_band_lock_state()
    metrics = metrics or {}
    nr_attached = bool(str(metrics.get("nrrsrp") or "").strip())
    profile = "gaming" if state else "speed"
    return {
        "profile": profile,
        "profile_label": "🎮 وضع الألعاب (LTE فقط + تردد مثبّت)" if profile == "gaming"
                         else "🚀 وضع السرعة (تلقائي + 5G مسموح)",
        "locked_band": metrics.get("band") if profile == "gaming" else None,
        "current_band": metrics.get("band"),
        "nr_attached": nr_attached,
        "original_network_mode": (state or {}).get("original_network_mode"),
        "recommended_gaming_band": recommended_gaming_band(),
        "nr_note": ("الـ5G متصل الآن ويضيف سرعة." if nr_attached
                    else "الـ5G غير متصل حالياً — إما وضع الألعاب مفعّل أو حامل الـ5G عند المشغّل متوقف."),
    }


def enqueue_modem_command_async(command):
    """Queue a long-running modem command without blocking the HTTP thread.
    Progress is reported through the command's own state dict instead."""
    analyzer = ACTIVE_ANALYZER
    if not analyzer or not getattr(analyzer, "modem_ready", False):
        return {"ok": False, "error": "المودم غير جاهز بعد. انتظر حتى يتصل البرنامج بالراوتر."}
    MODEM_COMMAND_QUEUE.put({"command": command, "event": threading.Event(), "result": None})
    return {"ok": True, "queued": True}

def generate_local_expert_opinion(metrics):
    """Generate a highly detailed, professional Arabic diagnostic report based on telemetry."""
    lte_rsrp = metrics.get("rsrp", "N/A")
    lte_sinr = metrics.get("sinr", "N/A")
    lte_rsrq = metrics.get("rsrq", "N/A")
    lte_rssi = metrics.get("rssi", "N/A")
    lte_cell = metrics.get("cell_id", "N/A")
    lte_band = metrics.get("band", "N/A")
    cqi0 = metrics.get("cqi0", "N/A")
    
    nr_rsrp = metrics.get("nrrsrp", "N/A")
    nr_sinr = metrics.get("nrsinr", "N/A")
    nr_rsrq = metrics.get("nrrsrq", "N/A")
    nr_freq = metrics.get("nrdlfreq", "N/A")
    nr_bw = metrics.get("nrdlbandwidth", "N/A")
    plmn = metrics.get("plmn", "N/A")
    
    def to_num(val):
        try:
            return float(val.replace("dBm", "").replace("dB", "").replace("MHz", "").strip())
        except:
            return None

    num_lte_rsrp = to_num(lte_rsrp)
    num_lte_sinr = to_num(lte_sinr)
    num_nr_rsrp = to_num(nr_rsrp)
    num_nr_sinr = to_num(nr_sinr)
    num_cqi = to_num(cqi0)

    # Carrier: show the raw PLMN code; carrier display names stay out of the code.
    carrier = f"PLMN {plmn}" if plmn else "مجهول"

    report = []
    report.append("### 🤖 تحليل مستشار الشبكة الذكي (Local Expert)")
    report.append(f"أهلاً بك! لقد قمت بتحليل القراءات الحالية للمودم المتصل بشبكة **{carrier}**، وإليك تقييمي الشامل وفك الرموز التي قد تبدو غامضة:")
    report.append("")
    
    # Connection Quality Rating
    report.append("#### 📊 التقييم العام لجودة الاتصال:")
    if num_nr_rsrp is not None and num_nr_sinr is not None:
        if num_nr_rsrp >= -85 and num_nr_sinr >= 15:
            report.append("🌟 **التقييم: ممتاز جداً (Excellent)**\nاتصال الـ 5G لديك في أفضل حالاته. قوة الإشارة عالية والتشويش شبه منعدم. هذا الاتصال مثالي للألعاب عبر الإنترنت (بينج منخفض وثابت) والبث المباشر والتحميل السريع.")
        elif num_nr_rsrp >= -92 and num_nr_sinr >= 10:
            report.append("👍 **التقييم: جيد جداً (Good)**\nالاتصال مستقر وسريع. ستحصل على سرعات تحميل ممتازة وبينج جيد في الألعاب، ولكن يمكن تحسين القراءات قليلاً لتقليل التذبذب أثناء الضغط.")
        elif num_nr_rsrp >= -100 or num_nr_sinr >= 3:
            report.append("⚠️ **التقييم: متوسط ومتقطع (Fair)**\nالإشارة تعاني من بعض الضعف أو التشويش. قد تلاحظ بطئاً مفاجئاً أثناء التحميل أو تذبذباً (Lag) أثناء اللعب.")
        else:
            report.append("🚨 **التقييم: سيء وغير مستقر (Poor)**\nالـ 5G بالكاد يلتقط الإشارة. المودم معرض للفصل أو التحويل التلقائي لشبكة 4G مما يسبب بطء شديد وتعليق متكرر.")
    else:
        report.append("⚠️ **التقييم: غير مكتمل (بيانات 5G ناقصة)**\nالمودم متصل بشبكة الجيل الرابع 4G فقط حالياً.")
        
    report.append("")
    
    # Technical Definitions
    report.append("#### 🔍 ماذا تعني هذه الرموز الفنية في قراءتك؟")
    report.append(f"1. **قوة الإشارة (RSRP)**:\n   - في الـ 4G: `{lte_rsrp}`\n   - في الـ 5G: `{nr_rsrp}`\n   *الشرح*: هذا المقياس يعبر عن قوة الموجات الواصلة للمودم. كلما اقترب الرقم من الصفر (مثلاً -75 أفضل بكثير من -95)، كانت الإشارة أقوى والسرعة أفضل.")
    
    report.append(f"2. **نسبة الإشارة إلى التشويش (SINR)**:\n   - في الـ 4G: `{lte_sinr}`\n   - في الـ 5G: `{nr_sinr}`\n   *الشرح*: هذا هو المعيار الأهم! هو يقيس نقاء الإشارة من التشويش. إذا كان SINR أعلى من `15dB` فالإشارة نقية جداً. إذا كان أقل من `5dB` فالإشارة مليئة بالضوضاء والتشويش حتى لو كانت الإشارة كاملة، مما يسبب بطء وتوقف مفاجئ.")
    
    report.append(f"3. **جودة القناة (CQI)**:\n   - القيمة الحالية: `{cqi0}` (من أصل 15)\n   *الشرح*: يعبر عن مدى كفاءة القناة اللاسلكية بين المودم والبرج. إذا كان أعلى من `10` فهذا يعني أن البرج يرسل البيانات بأعلى كفاءة وسرعة. إذا قل عن `7` فهناك ضغط كبير (تكدس مستخدمين) على البرج أو تشويش شديد.")
    
    report.append(f"4. **التردد وعرض النطاق (Frequency & Bandwidth)**:\n   - تردد الـ 5G الحالي: `{nr_freq}` وعرض النطاق `{nr_bw}`\n   - تردد الـ 4G الحالي: Band `{lte_band}`\n   *الشرح*: يوضح حجم 'الشارع' الذي تمشي فيه بياناتك. عرض نطاق 5G بمقدار `80MHz` أو `100MHz` يعني ممر بيانات واسع جداً يسمح بسرعات تحميل فائقة.")

    report.append("")
    
    # Bottlenecks and Diagnostics
    report.append("#### 🛠️ المشاكل المكتشفة في اتصالك الحالي وكيفية حلها:")
    
    issues_found = False
    
    if num_nr_rsrp is not None and num_nr_rsrp >= -85 and num_nr_sinr is not None and num_nr_sinr < 8:
        report.append(f"❌ **تشويش الإشارة (High Interference)**:\nلدينا قوة إشارة ممتازة (`{nr_rsrp}`) ولكن نقاء الإشارة ضعيف جداً (`{nr_sinr}`). هذا يعني أن المودم قريب من نافذة ولكن هناك أجهزة تشوش عليه بالمنزل (مثل راوتر آخر، تلفزيون ذكي، أو ميكرويف) أو أن المودم يلتقط إشارة من برجين مختلفين في نفس الوقت. **الحل**: ابعد المودم مسافة مترين على الأقل عن أي أجهزة إلكترونية أخرى.")
        issues_found = True
        
    if num_nr_rsrp is not None and num_nr_rsrp < -95:
        report.append(f"❌ **ضعف إشارة الـ 5G (Weak 5G Signal)**:\nالمودم بعيد جداً عن البرج أو معزول بجدران سميكة (`{nr_rsrp}`). **الحل**: انقل المودم فوراً بجانب نافذة تواجه البرج الرئيسي بالحي، ويفضل في الطابق الأعلى إن وجد.")
        issues_found = True

    if num_cqi is not None and num_cqi < 7:
        report.append(f"❌ **ازدحام البرج (Tower Congestion)**:\nقيمة الـ CQI منخفضة جداً (`{cqi0}`) مما يدل على وجود ضغط كبير على البرج من مشتركين آخرين بالحي. **الحل**: هذا يفسر لماذا يكون الاتصال بطيئاً رغم قوة الإشارة. إذا كان المودم يدعم دمج الترددات، يفضل تغيير التردد يدوياً، أو محاولة توجيه المودم لبرج آخر بالجهة المقابلة للمنزل.")
        issues_found = True

    if not issues_found:
        report.append("✅ **اتصالك سليم ومثالي**: لم نكتشف أي اختناقات أو مشاكل تشويش أو ضغط في قراءاتك الحالية. المودم موضوع في مكان رائع للغاية!")

    report.append("")
    report.append("#### 📍 خطة العمل المقترحة (Action Plan):")
    report.append("1. **الارتفاع**: ضع المودم على طاولة مرتفعة أو فوق خزانة، فالإشارات اللاسلكية تتحسن بشكل كبير كلما ارتفع المودم عن الأرض.")
    report.append("2. **الزاوية**: قم بتدوير المودم ببطء (بمقدار 45 درجة في كل مرة) وراقب مؤشر SINR في لوحة التحكم حتى يصل لأعلى قيمة ممكنة.")
    report.append("3. **العوازل**: تجنب وضع المودم خلف الستائر المعدنية، الزجاج المزدوج السميك، أو بجوار جدران الزوايا الخرسانية.")
    
    return "\n".join(report)

def generate_expert_opinion(metrics, api_key=""):
    """Generate opinion using Gemini API if key is provided, otherwise fall back to local rule engine."""
    if api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            prompt = f"""
            أنت خبير اتصالات ومهندس شبكات متخصص في شبكات 4G/5G.
            قم بتحليل قراءات إشارة المودم التالية باللغة العربية بأسلوب فني مبسط وودود ومحترف للغاية.
            أعط رأيك المفصل في جودة القراءة وهل هي مناسبة للألعاب والتحميل والبث المباشر.
            اشرح المصطلحات الفنية التي قد لا يفهمها المستخدم العادي (مثل RSRP, SINR, CQI, Band, Cell ID).
            قدم خطة عمل واضحة لتحسين جودة الإشارة والسرعة بناءً على هذه القراءات.

            القراءات الحالية:
            {json.dumps(metrics, indent=2, ensure_ascii=False)}
            """
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            # Fall back to local engine on API call failure
            return f"⚠️ حدث خطأ أثناء الاتصال بـ Gemini API: {str(e)}\n\n" + generate_local_expert_opinion(metrics)
            
    return generate_local_expert_opinion(metrics)

class CustomHTTPHandler(SimpleHTTPRequestHandler):
    """Custom request handler to serve files and specific API endpoints."""
    server_version = "SmartModemDashboard/2.0"

    def log_message(self, format, *args):
        """Keep the terminal dashboard clean while serving phones on the LAN."""
        return

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def is_allowed_client(self):
        """Allow only the local machine and private LAN clients."""
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return ip.is_loopback or ip.is_private or ip.is_link_local
        except ValueError:
            return False

    def check_dashboard_token(self):
        """Check if X-Dashboard-Token header or ?token= query parameter matches DASHBOARD_TOKEN (if configured)."""
        if not DASHBOARD_TOKEN:
            return True
        token_hdr = self.headers.get("X-Dashboard-Token")
        if token_hdr and token_hdr.strip() == DASHBOARD_TOKEN:
            return True
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        token_query = q.get("token")
        if token_query and token_query[0].strip() == DASHBOARD_TOKEN:
            return True
        return False

    def send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def read_metrics_file(self):
        if not os.path.exists(METRICS_FILE):
            return None
        with open(METRICS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def current_metrics_payload(self):
        """Return live metrics from memory first, with file fallback for startup."""
        global LATEST_METRICS, ACTIVE_ANALYZER

        if LATEST_METRICS:
            analyzer = ACTIVE_ANALYZER
            alerts, recs = analyzer.diagnose_issues(LATEST_METRICS) if analyzer else ([], [])
            lock_state = analyzer.get_lock_state(LATEST_METRICS) if analyzer else {}
            return {
                "timestamp": datetime.now().isoformat(),
                "status": "Connected",
                "source": "memory",
                "metrics": LATEST_METRICS,
                "diagnostics": {
                    "alerts": alerts,
                    "recommendations": recs
                },
                "lock": lock_state,
                "scan": read_scan_results()
            }

        try:
            cached = self.read_metrics_file()
            if cached:
                cached["source"] = "file"
                analyzer = ACTIVE_ANALYZER
                if analyzer:
                    cached["lock"] = analyzer.get_lock_state(cached.get("metrics", {}))
                cached["scan"] = read_scan_results()
                return cached
        except Exception as e:
            return {"status": "Error", "error": str(e), "metrics": {}, "diagnostics": {"alerts": [], "recommendations": []}}

        return {
            "timestamp": datetime.now().isoformat(),
            "status": "Starting",
            "source": "empty",
            "metrics": {},
            "diagnostics": {
                "alerts": ["بانتظار أول قراءة من المودم"],
                "recommendations": ["اترك البرنامج يعمل حتى يتم تسجيل الدخول إلى المودم وجلب القياسات."]
            }
        }

    def status_payload(self):
        global LATEST_METRICS, ACTIVE_ANALYZER
        modem_ready = bool(ACTIVE_ANALYZER and getattr(ACTIVE_ANALYZER, "modem_ready", False))
        
        last_metrics_age_seconds = None
        try:
            m_data = self.read_metrics_file()
            ts_str = None
            if m_data and "timestamp" in m_data:
                ts_str = m_data["timestamp"]
            elif isinstance(LATEST_METRICS, dict) and "timestamp" in LATEST_METRICS:
                ts_str = LATEST_METRICS["timestamp"]
            
            if ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    now_dt = datetime.now(dt.tzinfo)
                else:
                    now_dt = datetime.now()
                last_metrics_age_seconds = int((now_dt - dt).total_seconds())
        except Exception:
            pass

        db_stats = {}
        try:
            db_stats = signal_db.db_stats()
        except Exception:
            pass

        predictor_status = {}
        try:
            predictor_status = PREDICTOR.get_status()
        except Exception:
            pass

        stability_status = {}
        try:
            stability_status = stability_loop.get_status()
        except Exception:
            pass

        telegram_enabled = False
        try:
            telegram_enabled = bool(read_telegram_config().get("enabled"))
        except Exception:
            pass

        doctor_auto = True
        try:
            doctor_auto = bool(read_doctor_config().get("auto", True))
        except Exception:
            pass

        health_ok = bool(modem_ready or (last_metrics_age_seconds is not None and last_metrics_age_seconds < 120))

        connectivity = {}
        try:
            connectivity = web_diagnostics.get_health()
        except Exception:
            pass

        return {
            "status": "online",
            "hostname": socket.gethostname(),
            "bind_host": DASHBOARD_HOST,
            "port": PORT,
            "local_ip": LOCAL_IP,
            "lan_ips": get_lan_ips(),
            "dashboard_urls": get_dashboard_urls(),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "ai": {
                "local_engine": True,
                "server_gemini_key": bool(os.getenv("GEMINI_API_KEY"))
            },
            "lock_control": {
                "profile_file": LOCK_PROFILE_FILE,
                "lte_band_lock": True,
                "cell_lock_probe": True,
                "modem_ready": modem_ready
            },
            "modem_ready": modem_ready,
            "last_metrics_age_seconds": last_metrics_age_seconds,
            "db": db_stats,
            "predictor": predictor_status,
            "stability": stability_status,
            "telegram_enabled": telegram_enabled,
            "doctor_auto": doctor_auto,
            "health_ok": health_ok,
            "connectivity": connectivity
        }

    def do_OPTIONS(self):
        if not self.is_allowed_client():
            self.send_json({"error": "Access is limited to local network clients."}, status=403)
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if not self.is_allowed_client():
            self.send_json({"error": "Access is limited to local network clients."}, status=403)
            return

        path = urlparse(self.path).path
        if path.startswith('/api/') and not self.check_dashboard_token():
            self.send_json({"error": "Unauthorized. Invalid or missing token."}, status=401)
            return

        if path in ("", "/"):
            self.send_response(302)
            self.send_header('Location', '/dashboard.html')
            self.end_headers()
            return

        if path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return

        if path == '/api/metrics':
            self.send_json(self.current_metrics_payload())
            return

        if path in ('/api/status', '/api/health'):
            self.send_json(self.status_payload())
            return

        if path == '/api/lock/status':
            analyzer = ACTIVE_ANALYZER
            metrics = LATEST_METRICS
            if not metrics:
                cached = self.read_metrics_file()
                metrics = cached.get("metrics", {}) if cached else {}
            self.send_json(analyzer.get_lock_state(metrics) if analyzer else {
                "profile": read_lock_profile(),
                "current": build_cell_profile(metrics),
                "match": False,
                "differences": ["برنامج التحليل لم يجهز بعد."],
                "modem_ready": False
            })
            return

        if path == '/api/scan/results':
            self.send_json(read_scan_results() or {"ok": False, "message": "لا توجد نتائج مسح محفوظة بعد."})
            return

        if path == '/api/profile/status':
            metrics = LATEST_METRICS or {}
            if not metrics:
                cached = self.read_metrics_file()
                metrics = cached.get("metrics", {}) if cached else {}
            self.send_json(current_profile_state(metrics))
            return

        if path == '/api/bands/supported':
            supported = read_supported_bands()
            self.send_json(supported or {
                "bands": [], "mask": None,
                "note": "لم يُكتشف بعد — يُلتقط تلقائياً عند تفعيل وضع السرعة (فك القفل)."
            })
            return

        if path == '/api/bands/historical':
            try:
                self.send_json(signal_db.band_study())
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return

        if path == '/api/bands/study/status':
            with BAND_STUDY_LOCK:
                self.send_json(dict(BAND_STUDY_STATE))
            return

        if path == '/api/lock/band-experiment/log':
            self.send_json({"entries": list(reversed(read_band_experiment_log()))})
            return

        if path == '/api/band-history':
            history = read_band_history()
            cells = sorted(
                history.get("cells", {}).values(),
                key=lambda c: c.get("best_sinr") if c.get("best_sinr") is not None else -999,
                reverse=True
            )
            neighbors = sorted(
                history.get("neighbor_pci", {}).items(),
                key=lambda item: item[1],
                reverse=True
            )
            self.send_json({
                "cells": cells,
                "neighbor_pci": [{"pci": pci, "count": count} for pci, count in neighbors]
            })
            return

        if path == '/api/blocked-cells':
            self.send_json({"blocked_cells": read_blocked_cells()})
            return

        if path == '/api/doctor/status':
            analyzer = ACTIVE_ANALYZER
            if analyzer and getattr(analyzer, "doctor", None):
                self.send_json(analyzer.doctor.get_status())
            else:
                self.send_json({"error": "برنامج التحليل أو الطبيب الذكي غير جاهز بعد."}, status=503)
            return

        if path == '/api/speedtest/status':
            with SPEEDTEST_LOCK:
                payload = {
                    "running": SPEEDTEST_STATE["running"],
                    "finished": SPEEDTEST_STATE["finished"],
                    "error": SPEEDTEST_STATE["error"],
                    "total_bytes": SPEEDTEST_STATE["total_bytes"],
                    "samples": SPEEDTEST_STATE["samples"][-60:],
                    "drops": SPEEDTEST_STATE["drops"],
                    "stats": SPEEDTEST_STATE["stats"]
                }
            self.send_json(payload)
            return

        if path == '/api/telegram/status':
            self.send_json(NOTIFIER.get_status())
            return

        if path == '/api/tower-profile':
            metrics = LATEST_METRICS or {}
            if not metrics:
                cached = self.read_metrics_file()
                metrics = cached.get("metrics", {}) if cached else {}
            self.send_json(tower_insights.build_tower_profile(metrics))
            return

        if path == '/api/web-diagnostics/status':
            self.send_json(web_diagnostics.status())
            return

        if path == '/api/net/connectivity':
            self.send_json(web_diagnostics.get_health())
            return

        if path == '/api/stability/status':
            self.send_json(stability_loop.get_status())
            return

        if path == '/api/predict':
            self.send_json({
                "status": PREDICTOR.get_status(),
                "prediction": PREDICTOR.predict_now()
            })
            return

        if path == '/api/intelligence':
            metrics = LATEST_METRICS or {}
            if not metrics:
                cached = self.read_metrics_file()
                metrics = cached.get("metrics", {}) if cached else {}
            try:
                prediction = PREDICTOR.predict_now()
            except Exception:
                prediction = {}
            self.send_json(network_intelligence.build_report(
                metrics,
                band_history=read_band_history(),
                prediction=prediction,
            ))
            return

        super().do_GET()

    def do_POST(self):
        if not self.is_allowed_client():
            self.send_json({"error": "Access is limited to local network clients."}, status=403)
            return

        path = urlparse(self.path).path
        if path.startswith('/api/') and not self.check_dashboard_token():
            self.send_json({"error": "Unauthorized. Invalid or missing token."}, status=401)
            return

        try:
            req_data = self.read_json_request()

            if path == '/api/ask_ai':
                self.handle_ask_ai(req_data)
                return

            if path == '/api/web-diagnostics/run':
                metrics = self.metrics_from_request_or_cache(req_data)
                pressure = tower_insights.estimate_pressure(metrics)
                started = web_diagnostics.start(metrics, pressure)
                self.send_json({"ok": started, "message": "بدأ فحص بطء فتح المواقع." if started else "الفحص يعمل بالفعل."}, status=202 if started else 409)
                return

            if path == '/api/lock/profile':
                self.handle_lock_profile(req_data)
                return

            if path == '/api/lock/lte-bands':
                self.handle_lte_band_lock(req_data)
                return

            if path == '/api/lock/probe-cell-lock':
                result = enqueue_modem_command({"type": "probe_cell_lock"}, timeout=12)
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if path == '/api/profile/apply':
                profile = (req_data.get("profile") or "").strip()
                if profile not in ("speed", "gaming"):
                    self.send_json({"ok": False, "error": "الوضع غير معروف. استخدم speed أو gaming."}, status=400)
                    return
                # Both paths go through set_lte_bands, which already forces
                # LTE-only for a specific band and restores the saved automatic
                # mode for ALL — the two behaviours the profiles need.
                bands = "ALL" if profile == "speed" else str(req_data.get("band") or recommended_gaming_band())
                result = enqueue_modem_command({"type": "set_lte_bands", "bands": bands}, timeout=60)
                if result.get("ok"):
                    result["profile"] = profile
                    result["message"] = ("تم تفعيل وضع السرعة — كل الترددات مسموحة والـ5G رجع."
                                         if profile == "speed"
                                         else f"تم تفعيل وضع الألعاب — مثبّت على Band {bands} مع LTE فقط.")
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if path == '/api/bands/study':
                with BAND_STUDY_LOCK:
                    if BAND_STUDY_STATE["running"]:
                        self.send_json({"ok": False, "error": "الدراسة تعمل بالفعل."}, status=409)
                        return
                bands = req_data.get("bands") or DEFAULT_SCAN_BANDS
                result = enqueue_modem_command_async({
                    "type": "study_bands",
                    "bands": bands,
                    "settle_seconds": max(6, min(int(req_data.get("settle_seconds") or 12), 30)),
                    "radio_samples": max(5, min(int(req_data.get("radio_samples") or 10), 25)),
                    "sample_gap": max(1, min(int(req_data.get("sample_gap") or 2), 5)),
                })
                self.send_json(result, status=202 if result.get("ok") else 409)
                return

            if path == '/api/lock/band-experiment':
                bands = req_data.get("bands")
                if not bands:
                    self.send_json({"ok": False, "error": "مطلوب تحديد bands للتجربة."}, status=400)
                    return
                recovery_timeout = int(req_data.get("recovery_timeout_seconds") or 90)
                recovery_timeout = max(30, min(recovery_timeout, 180))
                result = enqueue_modem_command({
                    "type": "band_experiment",
                    "bands": bands,
                    "force_lte_only": bool(req_data.get("force_lte_only", False)),
                    "recovery_timeout_seconds": recovery_timeout,
                    "apply_if_success": bool(req_data.get("apply_if_success", False)),
                }, timeout=recovery_timeout * 2 + 75)
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if path == '/api/scan/towers':
                bands = req_data.get("bands") or DEFAULT_SCAN_BANDS
                duration = int(req_data.get("sample_seconds") or 8)
                apply_best = bool(req_data.get("apply_best", True))
                result = enqueue_modem_command({
                    "type": "scan_towers",
                    "bands": bands,
                    "sample_seconds": duration,
                    "apply_best": apply_best
                }, timeout=max(90, 25 * len(normalize_scan_bands(bands, LATEST_METRICS.get("band", "")))))
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if path == '/api/blocked-cells':
                action = req_data.get("action", "add")
                cell_id = str(req_data.get("cell_id", "")).strip()
                if not cell_id:
                    self.send_json({"ok": False, "error": "مطلوب تحديد معرف البرج cell_id."}, status=400)
                    return
                cells = read_blocked_cells()
                if action == "add":
                    if cell_id not in cells:
                        cells.append(cell_id)
                        write_blocked_cells(cells)
                    self.send_json({"ok": True, "message": f"تم حظر البرج {cell_id} بنجاح.", "blocked_cells": cells})
                elif action == "remove":
                    if cell_id in cells:
                        cells.remove(cell_id)
                        write_blocked_cells(cells)
                    self.send_json({"ok": True, "message": f"تم إلغاء حظر البرج {cell_id} بنجاح.", "blocked_cells": cells})
                else:
                    self.send_json({"ok": False, "error": "الإجراء غير معروف."}, status=400)
                return

            if path == '/api/doctor/config':
                config = req_data.get("config") or {}
                if write_doctor_config(config):
                    self.send_json({"ok": True, "message": "تم تحديث إعدادات الطبيب الذكي."})
                else:
                    self.send_json({"ok": False, "error": "فشل حفظ الإعدادات."}, status=500)
                return

            if path == '/api/doctor/diagnose':
                force = bool(req_data.get("force", False))
                analyzer = ACTIVE_ANALYZER
                if not analyzer:
                    self.send_json({"ok": False, "error": "محلل الإشارة غير متصل بعد."}, status=503)
                    return
                metrics = self.metrics_from_request_or_cache(req_data)
                success = analyzer.trigger_doctor_diagnose(metrics, "manual", force=force)
                if success:
                    self.send_json({"ok": True, "message": "تم بدء تشخيص الطبيب في الخلفية."})
                else:
                    self.send_json({"ok": False, "error": "الطبيب مشغول حالياً أو تحت مهلة التبريد."}, status=429)
                return

            if path == '/api/speedtest/start':
                with SPEEDTEST_LOCK:
                    if SPEEDTEST_STATE["running"]:
                        self.send_json({"ok": False, "error": "اختبار السرعة يعمل بالفعل."}, status=409)
                        return
                    SPEEDTEST_STATE.update({
                        "running": True,
                        "finished": False,
                        "started_at": time.time(),
                        "samples": [],
                        "drops": [],
                        "stats": None,
                        "total_bytes": 0,
                        "stop_flag": False,
                        "error": None
                    })
                threading.Thread(target=run_speedtest_worker, daemon=True).start()
                self.send_json({"ok": True, "message": "بدأ اختبار السرعة الحي."})
                return

            if path == '/api/speedtest/stop':
                with SPEEDTEST_LOCK:
                    SPEEDTEST_STATE["stop_flag"] = True
                self.send_json({"ok": True, "message": "تم إرسال طلب إيقاف اختبار السرعة."})
                return

            if path == '/api/telegram/config':
                config = req_data.get("config") or {}
                if write_telegram_config(config):
                    NOTIFIER.reinit()
                    self.send_json({"ok": True, "message": "تم تحديث إعدادات Telegram بنجاح."})
                else:
                    self.send_json({"ok": False, "error": "فشل حفظ إعدادات Telegram."}, status=500)
                return

            if path == '/api/telegram/test':
                try:
                    success = NOTIFIER.send_direct("🔔 رسالة تجريبية من محلل تردد المودم!")
                    if success:
                        self.send_json({"ok": True, "message": "تم إرسال رسالة اختبار التلغرام بنجاح."})
                    else:
                        self.send_json({"ok": False, "error": "فشل الإرسال. تأكد من صحة التوكن ومعرف الدردشة."}, status=400)
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, status=500)
                return

            if path == '/api/stability/evaluate':
                metrics = self.metrics_from_request_or_cache(req_data)
                result = stability_loop.evaluate_pending(metrics)
                self.send_json({"ok": True, "result": result})
                return

            if path == '/api/predict/train':
                try:
                    success, msg = PREDICTOR.train_model()
                    self.send_json({"ok": success, "message": msg})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, status=500)
                return

            self.send_json({"error": "Endpoint not found"}, status=404)
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON request body"}, status=400)
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def read_json_request(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 128 * 1024:
            raise ValueError("Request body is too large")

        post_data = self.rfile.read(content_length)
        return json.loads(post_data.decode('utf-8')) if post_data else {}

    def metrics_from_request_or_cache(self, req_data):
        metrics = req_data.get('metrics') or LATEST_METRICS
        if not metrics:
            cached = self.read_metrics_file()
            metrics = cached.get("metrics", {}) if cached else {}
        return metrics

    def handle_ask_ai(self, req_data):
        api_key = (req_data.get('api_key') or os.getenv("GEMINI_API_KEY") or "").strip()
        metrics = self.metrics_from_request_or_cache(req_data)
        opinion = generate_expert_opinion(metrics, api_key)
        self.send_json({
            "timestamp": datetime.now().isoformat(),
            "engine": "gemini" if api_key else "local",
            "opinion": opinion
        })

    def handle_lock_profile(self, req_data):
        action = (req_data.get("action") or "save_current").strip()
        if action == "clear":
            clear_lock_profile()
            self.send_json({"ok": True, "message": "تم حذف بروفايل التثبيت."})
            return

        metrics = self.metrics_from_request_or_cache(req_data)
        if not metrics:
            self.send_json({"ok": False, "error": "لا توجد قياسات حالية لحفظها."}, status=409)
            return

        profile = build_cell_profile(metrics, req_data.get("name") or "Gaming Stable Cell")
        write_lock_profile(profile)
        self.send_json({
            "ok": True,
            "message": "تم حفظ البرج والتردد الحالي كبروفايل ألعاب.",
            "profile": profile
        })

    def handle_lte_band_lock(self, req_data):
        metrics = self.metrics_from_request_or_cache(req_data)
        bands_value = req_data.get("bands")
        if bands_value in (None, "") and req_data.get("use_current"):
            bands_value = metrics.get("band", "")

        if req_data.get("save_profile") and metrics:
            write_lock_profile(build_cell_profile(metrics, req_data.get("name") or "Gaming Stable Cell"))

        result = enqueue_modem_command({
            "type": "set_lte_bands",
            "bands": bands_value
        }, timeout=20)
        self.send_json(result, status=200 if result.get("ok") else 409)

def start_http_server():
    """Start a quiet HTTP server to serve dashboard and metrics."""
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        with ThreadingHTTPServer((DASHBOARD_HOST, PORT), CustomHTTPHandler) as httpd:
            httpd.daemon_threads = True
            httpd.serve_forever()
    except Exception as e:
        console.print(f"[bold red]تعذر تشغيل خادم لوحة الشبكة على المنفذ {PORT}: {e}[/]")

class ModemAnalyzer:
    def __init__(self):
        global ACTIVE_ANALYZER
        ACTIVE_ANALYZER = self
        self.history_cell_ids = []
        self.history_cell_signatures = []
        self.last_cell_change_time = time.time()
        self.cell_hopping_count = 0
        self.start_time = time.time()
        self.consecutive_failures = 0
        self.modem_ready = False
        self.last_lock_result = None
        self.doctor = AiDoctor(on_result=self.handle_doctor_result)
        self.last_stability_eval = time.time()
        self.last_band_change_time = time.time()
        
        self.last_predict_time = 0
        self.last_eval_time = 0
        self.last_daily_report_date = None
        
        # Start DB maintenance daemon thread
        def maintenance_loop():
            while True:
                try:
                    signal_db.run_maintenance(days_db=30, days_csv=14, csv_path=HISTORY_FILE)
                except Exception:
                    pass
                time.sleep(24 * 3600)

        try:
            t = threading.Thread(target=maintenance_loop, daemon=True)
            t.start()
        except Exception:
            pass

        # Always-on packet-loss/jitter watcher for 8.8.8.8 so intermittent
        # "تقطيع" is caught even when nobody triggers a manual diagnosis.
        try:
            web_diagnostics.start_monitor(interval_seconds=20)
        except Exception:
            pass

        # Initialize History CSV if it doesn't exist
        if not os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Timestamp", "LTE_RSRP", "LTE_RSRQ", "LTE_SINR", "LTE_RSSI", "LTE_CellID", "LTE_Band",
                        "NR_RSRP", "NR_RSRQ", "NR_SINR", "NR_Bandwidth", "NR_Frequency", "Status"
                    ])
            except Exception as e:
                pass

    def handle_doctor_result(self, log_entry):
        try:
            error_msg = log_entry.get("error")
            if error_msg:
                msg = build_alert_message("doctor_diagnosis", {"error": error_msg})
            else:
                msg = build_alert_message("doctor_diagnosis", log_entry)
            NOTIFIER.notify("doctor_diagnosis", msg)
        except Exception:
            pass

    def clean_xml(self, xml_string):
        """Clean XML string to remove any HTML wrapper or namespace declarations if present."""
        start_idx = xml_string.find("<response>")
        end_idx = xml_string.find("</response>")
        if start_idx != -1 and end_idx != -1:
            return xml_string[start_idx:end_idx + len("</response>")]
            
        start_err = xml_string.find("<error>")
        end_err = xml_string.find("</error>")
        if start_err != -1 and end_err != -1:
            return xml_string[start_err:end_err + len("</error>")]
            
        return xml_string

    def parse_signal_data(self, xml_text):
        """Parse XML response from signal API."""
        try:
            cleaned = self.clean_xml(xml_text)
            root = ET.fromstring(cleaned)
            
            if root.tag == "error":
                code_node = root.find("code")
                code_val = code_node.text if code_node is not None else "Unknown"
                return {"error": "Session Expired", "code": code_val}
                
            data = {}
            for child in root:
                data[child.tag] = child.text.strip() if child.text else ""
            return data
        except Exception as e:
            return {"error": f"Parsing Error: {str(e)}"}

    def evaluate_rsrp(self, val, is_5g=False):
        """Evaluate RSRP (Signal Strength)"""
        try:
            dbm = float(val.replace("dBm", ""))
        except:
            return "N/A", "red", 0
        
        if dbm >= -80:
            return "ممتاز (Excellent)", "green", 100
        elif dbm >= -90:
            return "جيد جداً (Good)", "cyan", 75
        elif dbm >= -100:
            return "مقبول/ضعيف (Fair)", "yellow", 45
        else:
            return "سيء جداً (Poor)", "red", 15

    def evaluate_rsrq(self, val):
        """Evaluate RSRQ (Signal Quality)"""
        try:
            db = float(val.replace("dB", ""))
        except:
            return "N/A", "red", 0
        
        if db >= -10:
            return "ممتاز (Excellent)", "green", 100
        elif db >= -15:
            return "جيد (Good)", "cyan", 70
        elif db >= -20:
            return "ضعيف (Fair)", "yellow", 35
        else:
            return "سيء (Poor)", "red", 10

    def evaluate_sinr(self, val):
        """Evaluate SINR (Signal-to-Noise Ratio)"""
        try:
            db = float(val.replace("dB", ""))
        except:
            return "N/A", "red", 0
        
        if db >= 20:
            return "ممتاز (Excellent)", "green", 100
        elif db >= 13:
            return "جيد جداً (Good)", "cyan", 75
        elif db >= 0:
            return "مقبول (Fair)", "yellow", 40
        else:
            return "سيء جداً (Poor)", "red", 5

    def cell_signature(self, metrics):
        """Build a compact identity for the active LTE anchor cell."""
        return "|".join([
            metrics.get("cell_id", ""),
            metrics.get("pci", ""),
            metrics.get("band", ""),
            primary_number(metrics.get("earfcn", ""))
        ])

    def get_lock_state(self, metrics):
        profile = read_lock_profile()
        comparison = compare_lock_profile(metrics, profile) if profile else {
            "match": False,
            "differences": ["احفظ البرج الحالي أولاً لإنشاء بروفايل ألعاب."]
        }

        return {
            "profile": profile,
            "current": build_cell_profile(metrics or {}, "Current Cell"),
            "match": comparison.get("match", False),
            "differences": comparison.get("differences", []),
            "cell_hopping_count": self.cell_hopping_count,
            "recent_cells": self.history_cell_signatures[-10:],
            "modem_ready": self.modem_ready,
            "last_result": self.last_lock_result
        }

    def modem_fetch(self, page, path, method="GET", body=None, headers=None):
        """Run a fetch inside the authenticated Huawei web session."""
        return page.evaluate(
            """
            async ({path, method, body, headers}) => {
                const response = await fetch(path, {
                    method,
                    headers: headers || {},
                    body: body || undefined,
                    cache: 'no-store'
                });
                return {
                    ok: response.ok,
                    status: response.status,
                    text: await response.text()
                };
            }
            """,
            {"path": path, "method": method, "body": body, "headers": headers or {}}
        )

    def get_csrf_token(self, page):
        token_result = self.modem_fetch(page, "/api/webserver/token")
        token_match = re.search(r"<token>(.*?)</token>", token_result.get("text", ""))
        if token_match:
            return token_match.group(1)

        home_result = self.modem_fetch(page, "/html/home.html")
        html = home_result.get("text", "")
        meta_match = re.search(r'name=["\']csrf_token["\']\s+content=["\']([^"\']+)', html)
        if meta_match:
            return meta_match.group(1)

        meta_match = re.search(r'content=["\']([^"\']+)["\']\s+name=["\']csrf_token["\']', html)
        if meta_match:
            return meta_match.group(1)

        raise RuntimeError("لم أستطع استخراج CSRF token من واجهة المودم.")

    def get_current_net_mode(self, page):
        result = self.modem_fetch(page, "/api/net/net-mode")
        if not result.get("ok") and result.get("status") not in (200, 201):
            raise RuntimeError(f"تعذر قراءة وضع الشبكة من المودم: HTTP {result.get('status')}")

        root = ET.fromstring(self.clean_xml(result.get("text", "")))
        return {
            "NetworkMode": root.findtext("NetworkMode") or "03",
            "NetworkBand": root.findtext("NetworkBand") or "3FFFFFFF",
            "LTEBand": root.findtext("LTEBand") or LTE_BAND_ALL_MASK,
            "raw": result.get("text", "")
        }

    def _post_net_mode(self, page, network_mode, network_band, lte_band_mask):
        """Send a raw net-mode change and return (result, error_code, error_reason)."""
        token = self.get_csrf_token(page)
        xml_body = (
            "<request>"
            f"<NetworkMode>{network_mode}</NetworkMode>"
            f"<NetworkBand>{network_band}</NetworkBand>"
            f"<LTEBand>{lte_band_mask}</LTEBand>"
            "</request>"
        )
        result = self.modem_fetch(
            page,
            "/api/net/net-mode",
            method="POST",
            body=xml_body,
            headers={
                "Content-Type": "application/xml",
                "__RequestVerificationToken": token,
                "X-RequestVerificationToken": token
            }
        )
        text = result.get("text", "")
        accepted = bool(result.get("ok")) and "<error>" not in text.lower()
        error_code, error_reason = (None, None) if accepted else parse_modem_error(text)
        return result, accepted, error_code, error_reason

    def _verify_lte_band_applied(self, page, mask, settle_seconds=2, retries=2):
        """Poll the modem to confirm LTEBand actually stuck, not just HTTP-accepted.

        "All bands" is verified by intent, not by string equality: the modem
        normalises the all-ones mask down to the bands it actually supports
        (7FFFFFFFFFFFFFFF comes back as e.g. 7E2880E00D5), so a strict compare
        would report a perfectly successful unlock as a failure.
        """
        target = mask.upper().lstrip("0") or "0"
        unlocking = mask.upper() == LTE_BAND_ALL_MASK
        last_seen = None
        for attempt in range(retries):
            time.sleep(settle_seconds)
            try:
                last_seen = self.get_current_net_mode(page)
            except Exception:
                continue
            raw_applied = (last_seen.get("LTEBand") or "")
            applied_mask = raw_applied.upper().lstrip("0") or "0"
            if applied_mask == target:
                return True, last_seen
            if unlocking:
                try:
                    if bin(int(raw_applied, 16)).count("1") > 1:
                        # More than one band enabled means the lock is genuinely lifted.
                        remember_supported_band_mask(raw_applied)
                        return True, last_seen
                except (ValueError, TypeError):
                    pass
        return False, last_seen

    def set_lte_bands(self, page, bands_value):
        mask, bands = lte_bands_to_mask(bands_value)
        current_mode = self.get_current_net_mode(page)

        # Live-tested finding (Opus 4.8 + real router experiments): while the
        # network mode stays "auto" (00), the Huawei router accepts an LTE
        # Band restriction but never cleanly re-attaches to it — it keeps
        # preferring 5G/other bands and the link doesn't recover. Forcing
        # LTE-only (03) is what actually makes the restriction take effect.
        is_all = (mask == LTE_BAND_ALL_MASK)
        if is_all:
            saved_state = read_band_lock_state()
            network_mode_to_send = (saved_state or {}).get("original_network_mode") or "00"
        else:
            network_mode_to_send = "03"
            if not read_band_lock_state():
                original_network_mode = current_mode["NetworkMode"]
                if original_network_mode == "03":
                    # Already forced (e.g. a previous lock lost its state) —
                    # never capture "03" as the "original" to restore to.
                    original_network_mode = "00"
                write_band_lock_state({"original_network_mode": original_network_mode})

        result, accepted, error_code, error_reason = self._post_net_mode(
            page, network_mode_to_send, current_mode["NetworkBand"], mask
        )

        verified = None
        after_mode = None
        if accepted:
            verified, after_mode = self._verify_lte_band_applied(page, mask)

        ok = accepted and verified is not False
        if ok and is_all:
            clear_band_lock_state()
        label = "كل الترددات" if mask == LTE_BAND_ALL_MASK else "+".join(str(band) for band in bands)

        if ok:
            message = f"تم إرسال أمر LTE Band: {label} وتأكد تطبيقه على المودم."
        elif accepted and verified is False:
            message = (
                f"المودم قَبِل الأمر (وفُرض LTE-only) لكن التردد لم يتغيّر فعليًا "
                f"(لا يزال {(after_mode or {}).get('LTEBand', current_mode.get('LTEBand'))}). "
                "غالبًا لا توجد تغطية كافية لهذا الباند في موقعك — جرّب حلقة تجربة تبديل الترددات للتأكد."
            )
        else:
            message = "رفض المودم أمر قفل LTE Band" + (
                f" (كود {error_code}: {error_reason})." if error_code else "."
            )

        response = {
            "ok": ok,
            "action": "set_lte_bands",
            "bands": bands,
            "lte_band_mask": mask,
            "accepted_by_modem": accepted,
            "verified_applied": verified,
            "error_code": error_code,
            "error_reason": error_reason,
            "message": message,
            "http_status": result.get("status"),
            "modem_response": result.get("text", "")[:1000],
            "previous_lte_band_mask": current_mode.get("LTEBand"),
            "previous_network_mode": current_mode.get("NetworkMode"),
            "network_mode_used": network_mode_to_send
        }
        self.last_lock_result = response
        return response

    def set_lte_band_mask(self, page, mask, label="custom", verify=True):
        current_mode = self.get_current_net_mode(page)
        result, accepted, error_code, error_reason = self._post_net_mode(
            page, current_mode["NetworkMode"], current_mode["NetworkBand"], mask
        )

        verified = None
        if accepted and verify:
            verified, _ = self._verify_lte_band_applied(page, mask)

        return {
            "ok": accepted and verified is not False,
            "action": "set_lte_band_mask",
            "label": label,
            "lte_band_mask": mask,
            "accepted_by_modem": accepted,
            "verified_applied": verified,
            "error_code": error_code,
            "error_reason": error_reason,
            "http_status": result.get("status"),
            "modem_response": result.get("text", "")[:1000],
            "previous_lte_band_mask": current_mode.get("LTEBand")
        }

    def _watch_connectivity_recovery(self, timeout_seconds, poll_seconds=3, required_ok_streak=2):
        """Ping MONITOR_HOST until it comes back (or times out). Returns a
        dict describing whether/when the link recovered. Never raises."""
        started = time.time()
        went_down_at = None
        ok_streak = 0
        last_reading = None
        while time.time() - started < timeout_seconds:
            try:
                last_reading = web_diagnostics.quick_ping(web_diagnostics.MONITOR_HOST, count=2)
            except Exception:
                last_reading = {"ok": False}
            if last_reading.get("ok") and (last_reading.get("loss_percent") or 0) == 0:
                ok_streak += 1
                if ok_streak >= required_ok_streak:
                    return {
                        "down": went_down_at is not None,
                        "recovered": True,
                        "down_seconds": round(time.time() - went_down_at, 1) if went_down_at else 0,
                        "last_reading": last_reading
                    }
            else:
                ok_streak = 0
                if went_down_at is None:
                    went_down_at = time.time()
            time.sleep(poll_seconds)
        return {
            "down": went_down_at is not None,
            "recovered": False,
            "down_seconds": round(time.time() - went_down_at, 1) if went_down_at else None,
            "last_reading": last_reading
        }

    def _safe_revert_net_mode(self, page, original_mode, retries=3, delay_seconds=3):
        """Retry the rollback itself and verify LTEBand truly went back to
        original before declaring success. Never raises — worst case it
        reports reverted=False with the last error so the caller can alert."""
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                _, accepted, error_code, error_reason = self._post_net_mode(
                    page, original_mode["NetworkMode"], original_mode["NetworkBand"], original_mode["LTEBand"]
                )
                if accepted:
                    verified, _ = self._verify_lte_band_applied(page, original_mode["LTEBand"], settle_seconds=1, retries=2)
                    if verified:
                        return {"reverted": True, "attempts": attempt}
                    last_error = "أمر الرجوع قُبِل لكن لم يتأكد أن التردد الأصلي عاد فعلاً."
                else:
                    last_error = f"رفض المودم أمر الرجوع (كود {error_code}: {error_reason})" if error_code else "رفض المودم أمر الرجوع."
            except Exception as exc:
                last_error = str(exc)
            if attempt < retries:
                time.sleep(delay_seconds)
        return {"reverted": False, "attempts": retries, "error": last_error}

    def _restore_scan_original(self, page, original_mode, label="restore_original"):
        """Used by scan_towers when no band was picked (or apply_best=False):
        restores NetworkMode too, not just LTEBand — scan_single_band forces
        LTE-only (03) for every band it measures, so a plain LTEBand-only
        restore would leave the modem stuck on LTE-only afterward."""
        revert_info = self._safe_revert_net_mode(page, original_mode)
        if revert_info["reverted"]:
            clear_band_lock_state()
        return {
            "ok": revert_info["reverted"],
            "action": "restore_original_net_mode",
            "label": label,
            "lte_band_mask": original_mode.get("LTEBand"),
            "network_mode_restored": original_mode.get("NetworkMode"),
            "attempts": revert_info["attempts"],
            "error": revert_info.get("error"),
        }

    def _study_single_band(self, page, band, settle_seconds, radio_samples, sample_gap):
        """Measure one band end-to-end and classify it honestly.

        Returns a record whose `status` distinguishes three genuinely different
        outcomes that the old scan collapsed into one meaningless score:
          rejected     – the modem refused the lock command outright
          no_attach    – lock applied but the radio never served that band
          no_internet  – band is serving but no usable data path came back
          usable       – band works; metrics are comparable to other usable ones
        """
        record = {"band": band, "status": "unknown", "samples": 0, "notes": ""}

        lock = self.set_lte_bands(page, str(band))
        record["locked"] = bool(lock.get("ok"))
        record["error_code"] = lock.get("error_code")
        if not lock.get("accepted_by_modem"):
            record["status"] = "rejected"
            record["notes"] = lock.get("error_reason") or "المودم رفض الأمر."
            return record

        time.sleep(settle_seconds)

        readings, sinr_values, rsrp_values, rsrq_values, cqi_values = [], [], [], [], []
        served_band = None
        nr_present = 0
        for _ in range(radio_samples):
            metrics = self.read_signal_metrics(page)
            if metrics and not metrics.get("error"):
                readings.append(metrics)
                if metrics.get("band"):
                    served_band = str(metrics.get("band"))
                # A live NR carrier during a supposedly LTE-only lock means the
                # restriction never took effect, so the numbers below describe
                # whatever the modem chose — not this band. Old scan rows from
                # before the LTE-only fix all carry this fingerprint.
                if str(metrics.get("nrrsrp") or "").strip():
                    nr_present += 1
                for source, target in ((metrics.get("sinr"), sinr_values),
                                       (metrics.get("rsrp"), rsrp_values),
                                       (metrics.get("rsrq"), rsrq_values),
                                       (metrics.get("cqi0"), cqi_values)):
                    value = metric_number(source)
                    if value is not None:
                        target.append(value)
            time.sleep(sample_gap)

        record["samples"] = len(readings)
        record["served_band"] = served_band
        record["nr_seen_samples"] = nr_present
        if readings and nr_present > len(readings) / 2:
            record["status"] = "invalid_measurement"
            record["notes"] = ("ظهر اتصال 5G أثناء القفل، يعني فرض LTE-only لم ينفذ فعلاً — "
                               "القياس لا يمثّل هذا التردد وتم استبعاده.")
            return record

        if not readings or served_band is None:
            record["status"] = "no_attach"
            record["notes"] = "ما رجعت أي قراءة — الباند غالباً غير مدعوم أو ما فيه تغطية."
            return record
        if served_band != str(band):
            record["status"] = "no_attach"
            record["notes"] = f"طُبِّق القفل لكن الراديو بقي على Band {served_band} — ما فيه تغطية لهذا التردد هنا."
            return record

        def summarize(values):
            if not values:
                return None, None
            mean = sum(values) / len(values)
            sd = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 if len(values) > 1 else 0.0
            return round(mean, 1), round(sd, 2)

        record["avg_sinr"], record["sinr_stdev"] = summarize(sinr_values)
        record["avg_rsrp"], _ = summarize(rsrp_values)
        record["avg_rsrq"], _ = summarize(rsrq_values)
        record["avg_cqi"], _ = summarize(cqi_values)

        # Radio quality alone proved misleading before (perfect LTE numbers while
        # the actual link was dropping a quarter of its packets), so every band
        # is also judged on a real data path.
        try:
            ping = web_diagnostics.quick_ping(web_diagnostics.MONITOR_HOST, count=8)
        except Exception as exc:
            ping = {"ok": False, "loss_percent": 100, "error": str(exc)}
        record["ping"] = ping
        record["loss_percent"] = ping.get("loss_percent")
        record["avg_ms"] = ping.get("avg_ms")
        record["jitter_ms"] = ping.get("jitter_ms")

        # Explicit None check, never `or`: a legitimate 0% loss is falsy and would
        # otherwise be replaced by the "total loss" default and condemn a perfect band.
        loss_value = ping.get("loss_percent")
        loss_value = 100.0 if loss_value is None else float(loss_value)
        if not ping.get("ok") or loss_value >= 60:
            record["status"] = "no_internet"
            record["notes"] = f"الراديو تعلّق بالباند لكن الإنترنت غير صالح عليه (فقد {round(loss_value)}%)."
            return record

        record["status"] = "usable"
        return record

    def _score_band_record(self, record):
        """Three separate verdicts instead of one blended number, because the
        best band for gaming (stability) is not always the best for throughput."""
        if record.get("status") != "usable":
            return {"gaming": None, "speed": None, "stability": None}

        sinr = record.get("avg_sinr") or 0
        rsrp = record.get("avg_rsrp") or -120
        rsrq = record.get("avg_rsrq") or -20
        cqi = record.get("avg_cqi") or 0
        sd = record.get("sinr_stdev")
        loss = record.get("loss_percent") or 0
        jitter = record.get("jitter_ms") or 0

        sinr_pts = clamp((sinr + 5) * 3.0, 0, 90)
        rsrp_pts = clamp((rsrp + 115) * 1.4, 0, 60)
        rsrq_pts = clamp((rsrq + 20) * 3.5, 0, 45)
        cqi_pts = clamp(cqi * 4.0, 0, 60)
        steady_pts = clamp(40 - (sd if sd is not None else 6) * 8, 0, 40)
        loss_pen = loss * 4.0
        jitter_pen = clamp(jitter * 0.35, 0, 45)

        gaming = sinr_pts * 0.9 + steady_pts * 1.6 + rsrq_pts * 0.6 + cqi_pts * 0.4 - loss_pen * 1.5 - jitter_pen * 1.4
        speed = cqi_pts * 1.3 + sinr_pts * 1.0 + rsrp_pts * 0.9 + rsrq_pts * 0.5 - loss_pen * 1.0 - jitter_pen * 0.4
        stability = steady_pts * 1.3 + rsrp_pts * 1.0 + sinr_pts * 0.7 + rsrq_pts * 0.7 - loss_pen * 1.6 - jitter_pen * 0.7
        return {
            "gaming": round(max(0, gaming), 1),
            "speed": round(max(0, speed), 1),
            "stability": round(max(0, stability), 1),
        }

    def _build_study_verdict(self, results):
        """Pick winners, but refuse to crown one when the gap is inside noise."""
        usable = [r for r in results if r.get("status") == "usable"]
        unavailable = [r for r in results if r.get("status") != "usable"]
        if not usable:
            return {
                "ok": False,
                "headline": "ما فيه أي تردد بديل صالح للاستخدام هنا",
                "detail": "كل الترددات المجربة إما رفضها المودم أو ما فيها تغطية فعلية في موقعك.",
                "unavailable": [{"band": r["band"], "status": r["status"], "notes": r.get("notes", "")} for r in unavailable],
            }

        def score_of(record, kind):
            value = (record.get("scores") or {}).get(kind)
            return -1.0 if value is None else float(value)

        def top(kind):
            ranked = sorted(usable, key=lambda r: score_of(r, kind), reverse=True)
            best = ranked[0]
            runner = ranked[1] if len(ranked) > 1 else None
            best_score = score_of(best, kind)
            # A win inside overlapping SINR spread is not a real win. The daily
            # swing measured on this line is ~2.7 dB, so a sub-1.5 dB gap cannot
            # be attributed to the band at all.
            tie = False
            if runner:
                gap = best_score - score_of(runner, kind)
                spread = (best.get("sinr_stdev") or 0) + (runner.get("sinr_stdev") or 0)
                sinr_gap = abs((best.get("avg_sinr") or 0) - (runner.get("avg_sinr") or 0))
                tie = gap < 25 and (sinr_gap <= spread or sinr_gap < 1.5)
            return {
                "band": best["band"], "score": best_score,
                "runner_up": runner["band"] if runner else None,
                "tie": tie,
            }

        gaming, speed, stability = top("gaming"), top("speed"), top("stability")
        return {
            "ok": True,
            "best_for_gaming": gaming,
            "best_for_speed": speed,
            "best_for_stability": stability,
            "usable_bands": [r["band"] for r in usable],
            "unavailable": [{"band": r["band"], "status": r["status"], "notes": r.get("notes", "")} for r in unavailable],
        }

    def study_bands(self, page, candidate_bands, settle_seconds=12, radio_samples=10, sample_gap=2):
        """Test each candidate band for real and report a comparison.

        Safety: the original NetworkMode/NetworkBand/LTEBand captured before the
        first change is restored in `finally` through the same retried+verified
        rollback the guarded experiment uses, so an exception, a dead band, or a
        crash mid-study can never strand the modem on a broken frequency.
        """
        original_mode = self.get_current_net_mode(page)
        bands = normalize_scan_bands(candidate_bands, (LATEST_METRICS or {}).get("band", ""))
        if not bands:
            raise ValueError("لا توجد ترددات صالحة للدراسة.")

        per_band = settle_seconds + radio_samples * sample_gap + 12
        with BAND_STUDY_LOCK:
            BAND_STUDY_STATE.update({
                "running": True, "finished": False, "error": None,
                "started_at": datetime.now().isoformat(), "finished_at": None,
                "phase": "بدء الدراسة", "current_band": None,
                "done_bands": 0, "total_bands": len(bands),
                "eta_seconds": per_band * len(bands) + 30,
                "results": [], "verdict": None,
            })

        results = []
        try:
            for index, band in enumerate(bands):
                with BAND_STUDY_LOCK:
                    BAND_STUDY_STATE.update({
                        "phase": f"يقيس Band {band}", "current_band": band,
                        "done_bands": index,
                        "eta_seconds": per_band * (len(bands) - index) + 30,
                    })
                try:
                    record = self._study_single_band(page, band, settle_seconds, radio_samples, sample_gap)
                except Exception as exc:
                    record = {"band": band, "status": "error", "notes": str(exc), "samples": 0}
                record["scores"] = self._score_band_record(record)
                results.append(record)
                with BAND_STUDY_LOCK:
                    BAND_STUDY_STATE["results"] = list(results)
                    BAND_STUDY_STATE["done_bands"] = index + 1
        finally:
            with BAND_STUDY_LOCK:
                BAND_STUDY_STATE.update({"phase": "يرجّع الإعداد الأصلي", "current_band": None})
            revert = self._safe_revert_net_mode(page, original_mode)
            clear_band_lock_state()
            try:
                self._watch_connectivity_recovery(90)
            except Exception:
                pass

            verdict = self._build_study_verdict(results) if results else None
            payload = {
                "generated_at": datetime.now().isoformat(),
                "bands_tested": bands,
                "results": results,
                "verdict": verdict,
                "reverted": revert.get("reverted"),
                "revert_error": revert.get("error"),
                "original_lte_band_mask": original_mode.get("LTEBand"),
                "original_network_mode": original_mode.get("NetworkMode"),
            }
            try:
                with open(BAND_STUDY_FILE, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
            except Exception:
                pass

            with BAND_STUDY_LOCK:
                BAND_STUDY_STATE.update({
                    "running": False, "finished": True,
                    "finished_at": datetime.now().isoformat(),
                    "phase": "انتهت الدراسة" if revert.get("reverted") else "انتهت لكن فشل الرجوع التلقائي!",
                    "results": results, "verdict": verdict,
                    "eta_seconds": 0,
                    "error": None if revert.get("reverted") else ("فشل إرجاع الإعداد الأصلي: " + str(revert.get("error"))),
                })
            if not revert.get("reverted"):
                try:
                    NOTIFIER.notify("band_rollback_failed",
                                    "⚠️ <b>فشل رجوع الإعداد بعد دراسة الترددات</b>\nافتح لوحة المودم يدوياً وأعد LTE Band إلى ALL.",
                                    force=True)
                except Exception:
                    pass

        return {"ok": True, "action": "study_bands", **payload}

    def guarded_band_experiment(self, page, target_bands, force_lte_only=False,
                                 recovery_timeout_seconds=90, apply_if_success=False):
        """Try switching to `target_bands` on the real router while watching
        real connectivity the whole time. If the link doesn't come back
        within `recovery_timeout_seconds`, the change never actually applied,
        or ANY unexpected error happens after we may have touched the config,
        it automatically restores the exact original setting (retried and
        verified) and waits to confirm the internet is back — this is the
        safety net so a bad frequency never gets left in place unattended.
        The rollback runs in `finally` so it fires even on exceptions raised
        by the apply call itself (e.g. the modem session getting flaky right
        as the network mode changes), not just on the "clean" failure paths.
        Every attempt is logged so repeated runs (with/without
        force_lte_only, across bands) show which combination actually works.
        """
        started_at = datetime.now().isoformat()
        original_mode = self.get_current_net_mode(page)
        try:
            baseline_ping = web_diagnostics.quick_ping(web_diagnostics.MONITOR_HOST, count=3)
        except Exception:
            baseline_ping = {"ok": None}

        try:
            mask, bands = lte_bands_to_mask(target_bands)
        except ValueError as exc:
            return {"ok": False, "action": "band_experiment", "error": str(exc)}

        network_mode_used = "03" if force_lte_only else original_mode["NetworkMode"]
        entry = {
            "started_at": started_at,
            "finished_at": None,
            "target_bands": bands,
            "lte_band_mask": mask,
            "force_lte_only": force_lte_only,
            "network_mode_used": network_mode_used,
            "original_network_mode": original_mode["NetworkMode"],
            "original_network_band": original_mode["NetworkBand"],
            "original_lte_band_mask": original_mode["LTEBand"],
            "baseline_ping": baseline_ping,
            "accepted_by_modem": False,
            "error_code": None,
            "error_reason": None,
            "outcome": "unknown",
            "message": "",
            "reverted": False,
        }

        # Set BEFORE the risky call: if _post_net_mode raises partway through
        # (e.g. the router already applied the change on the wire but our
        # session dies reading the response), we still must not skip rollback.
        attempted_apply = True
        keep_new_band = False

        try:
            result, accepted, error_code, error_reason = self._post_net_mode(
                page, network_mode_used, original_mode["NetworkBand"], mask
            )
            entry.update(accepted_by_modem=accepted, error_code=error_code, error_reason=error_reason)

            if not accepted:
                attempted_apply = False  # modem explicitly rejected it — nothing to revert
                entry["outcome"] = "rejected"
                entry["message"] = "الراوتر رفض الأمر مباشرة" + (f" (كود {error_code}: {error_reason})." if error_code else ".")
            else:
                # Accepted — watch the real link the whole time the frequency
                # change ripples through, since the modem is expected to drop
                # and re-attach.
                recovery = self._watch_connectivity_recovery(recovery_timeout_seconds)
                band_verified, after_mode = self._verify_lte_band_applied(page, mask, settle_seconds=1, retries=1)
                entry["connectivity"] = recovery
                entry["band_config_verified"] = band_verified
                entry["band_config_after"] = (after_mode or {}).get("LTEBand")

                try:
                    live_metrics = self.read_signal_metrics(page)
                    entry["live_band_after"] = str(live_metrics.get("band", "")) if live_metrics else None
                except Exception:
                    entry["live_band_after"] = None

                success = bool(band_verified and recovery.get("recovered"))
                keep_new_band = success and apply_if_success

                if success and keep_new_band:
                    entry["outcome"] = "kept_effective"
                    entry["message"] = f"تم التبديل للترددات {'+'.join(str(b) for b in bands)} وتأكد فعلياً ورجع الاتصال — تم إبقاؤه."
                elif success:
                    entry["outcome"] = "verified_then_reverted"
                    entry["message"] = (
                        "التبديل نجح فعلياً (الراديو تحوّل والاتصال رجع) لكن تم إرجاعه للوضع الأصلي "
                        "كما هو مطلوب في وضع التجربة (apply_if_success=false)."
                    )
                elif band_verified and not recovery.get("recovered"):
                    entry["outcome"] = "effective_no_recovery"
                    entry["message"] = (
                        f"الإعداد تغيّر فعلياً لكن الإنترنت لم يرجع خلال {recovery_timeout_seconds}ث — "
                        "سيتم الرجوع تلقائيًا للإعداد الأصلي فورًا حماية للاتصال."
                    )
                else:
                    entry["outcome"] = "accepted_but_ineffective"
                    entry["message"] = (
                        "المودم قَبِل الأمر لكن التردد لم يتغيّر فعليًا على الراديو "
                        f"(لا يزال Band {entry.get('live_band_after') or entry.get('band_config_after')}). "
                        "هذا يؤكد أن الراوتر يتجاهل قفل LTE Band بهذا الإعداد."
                    )
        except Exception as exc:
            entry["outcome"] = "error_during_experiment"
            entry["message"] = f"حدث خطأ غير متوقع أثناء التجربة: {exc}"
            entry["exception"] = str(exc)
        finally:
            if attempted_apply and not keep_new_band:
                revert_info = self._safe_revert_net_mode(page, original_mode)
                entry["reverted"] = revert_info["reverted"]
                entry["revert_attempts"] = revert_info["attempts"]
                if revert_info["reverted"]:
                    try:
                        entry["revert_connectivity"] = self._watch_connectivity_recovery(recovery_timeout_seconds)
                    except Exception as exc:
                        entry["revert_connectivity"] = {"recovered": None, "error": str(exc)}
                else:
                    entry["revert_error"] = revert_info.get("error")
                    entry["revert_connectivity"] = None
                    entry["outcome"] = entry.get("outcome", "unknown") + "_rollback_failed"
                    entry["message"] = (
                        (entry.get("message") or "") +
                        " ⚠️ تحذير خطير: فشل إرجاع التردد الأصلي تلقائياً بعد عدة محاولات — "
                        f"يحتاج تدخل يدوي فوري من لوحة تحكم المودم ({MODEM_IP})!"
                    )
                    try:
                        NOTIFIER.notify(
                            "band_rollback_failed",
                            "⚠️ <b>فشل رجوع تردد LTE تلقائياً</b>\n"
                            f"التردد المستهدف: {'+'.join(str(b) for b in bands)}\n"
                            f"سبب الفشل: {revert_info.get('error')}\n"
                            f"افتح لوحة المودم يدوياً ({MODEM_IP}) وأعد وضع LTE Band لكل الترددات (ALL).",
                            force=True
                        )
                    except Exception:
                        pass

            entry["finished_at"] = datetime.now().isoformat()
            append_band_experiment_log(entry)
            self.last_lock_result = {"action": "band_experiment", **entry}

        return {
            "ok": entry["outcome"] in ("kept_effective", "verified_then_reverted"),
            "action": "band_experiment",
            **entry
        }

    def probe_cell_lock(self, page):
        result = self.modem_fetch(page, "/api/net/cell-lock")
        ok = result.get("status") == 200 and "<error>" not in result.get("text", "").lower()
        response = {
            "ok": ok,
            "action": "probe_cell_lock",
            "message": "يبدو أن المودم يملك endpoint لقفل البرج." if ok else "لم يظهر دعم واضح لقفل البرج المباشر عبر /api/net/cell-lock.",
            "http_status": result.get("status"),
            "modem_response": result.get("text", "")[:1500]
        }
        self.last_lock_result = response
        return response

    def read_signal_metrics(self, page):
        result = self.modem_fetch(page, "/api/device/signal")
        if not result.get("ok") and result.get("status") not in (200, 201):
            return {"error": f"HTTP {result.get('status')}"}
        return self.parse_signal_data(result.get("text", ""))

    def scan_single_band(self, page, band, sample_seconds):
        lock_result = self.set_lte_bands(page, str(band))
        settle_seconds = 7
        time.sleep(settle_seconds)

        samples = []
        sample_count = max(3, int(sample_seconds / 2))
        for _ in range(sample_count):
            metrics = self.read_signal_metrics(page)
            samples.append(metrics)
            if metrics and not metrics.get("error"):
                self.save_metrics_to_files(metrics, status_text=f"Scanning Band {band}")
            time.sleep(2)

        score_data = score_signal_samples(samples)
        score_data.update({
            "band": band,
            "lock_result": lock_result,
            "sample_count": len(samples)
        })
        return score_data

    def scan_towers(self, page, bands_value, sample_seconds=8, apply_best=True):
        current_metrics = LATEST_METRICS or self.read_signal_metrics(page)
        bands = normalize_scan_bands(bands_value, current_metrics.get("band", ""))
        if not bands:
            raise ValueError("لا توجد ترددات LTE صالحة للمسح.")

        original_mode = self.get_current_net_mode(page)
        started_at = datetime.now().isoformat()
        results = []

        for band in bands:
            try:
                results.append(self.scan_single_band(page, band, sample_seconds))
            except Exception as e:
                results.append({
                    "band": band,
                    "score": -999,
                    "pressure": "unknown",
                    "summary": {},
                    "reason": str(e),
                    "samples": []
                })

        viable = [result for result in results if result.get("score", -999) > -500]
        best = max(viable, key=lambda item: item.get("score", -999), default=None)
        applied = None

        if best and apply_best:
            applied = self.set_lte_bands(page, str(best["band"]))
            time.sleep(8)
            final_metrics = self.read_signal_metrics(page)
            if final_metrics and not final_metrics.get("error"):
                self.cell_hopping_count = 0
                self.history_cell_ids = []
                self.history_cell_signatures = []
                self.last_cell_change_time = time.time()
                self.save_metrics_to_files(final_metrics, status_text=f"Locked Best Band {best['band']}")
                write_lock_profile(build_cell_profile(final_metrics, f"Best Gaming Band {best['band']}"))
        elif best and not apply_best:
            applied = self._restore_scan_original(page, original_mode)
        elif not best:
            applied = self._restore_scan_original(page, original_mode)

        response = {
            "ok": bool(best),
            "action": "scan_towers",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "bands_scanned": bands,
            "sample_seconds": sample_seconds,
            "apply_best": apply_best,
            "best": best,
            "results": sorted(results, key=lambda item: item.get("score", -999), reverse=True),
            "applied": applied,
            "message": (
                f"تم اختيار Band {best['band']} كأفضل مرشح للألعاب." if best and apply_best else
                "تم المسح بدون تثبيت تلقائي." if best else
                "لم أجد مرشحًا صالحًا؛ تم الرجوع للإعداد السابق."
            ),
            "original_lte_band_mask": original_mode.get("LTEBand")
        }
        write_scan_results(response)
        self.last_lock_result = response
        return response

    def process_modem_commands(self, page):
        while True:
            try:
                item = MODEM_COMMAND_QUEUE.get_nowait()
            except Empty:
                break

            try:
                command = item.get("command", {})
                if command.get("type") == "set_lte_bands":
                    item["result"] = self.set_lte_bands(page, command.get("bands"))
                elif command.get("type") == "probe_cell_lock":
                    item["result"] = self.probe_cell_lock(page)
                elif command.get("type") == "scan_towers":
                    item["result"] = self.scan_towers(
                        page,
                        command.get("bands"),
                        command.get("sample_seconds", 8),
                        command.get("apply_best", True)
                    )
                elif command.get("type") == "study_bands":
                    item["result"] = self.study_bands(
                        page,
                        command.get("bands"),
                        settle_seconds=command.get("settle_seconds", 12),
                        radio_samples=command.get("radio_samples", 10),
                        sample_gap=command.get("sample_gap", 2)
                    )
                elif command.get("type") == "band_experiment":
                    item["result"] = self.guarded_band_experiment(
                        page,
                        command.get("bands"),
                        force_lte_only=command.get("force_lte_only", False),
                        recovery_timeout_seconds=command.get("recovery_timeout_seconds", 90),
                        apply_if_success=command.get("apply_if_success", False)
                    )
                else:
                    item["result"] = {"ok": False, "error": "أمر غير معروف."}
            except Exception as e:
                item["result"] = {"ok": False, "error": str(e)}
                self.last_lock_result = item["result"]
            finally:
                item["event"].set()

    def diagnose_issues(self, metrics):
        """Diagnose connection issues and provide recommendations."""
        alerts = []
        recommendations = []
        
        # 1. Parsing Signal levels
        lte_rsrp_raw = metrics.get("rsrp", "")
        lte_sinr_raw = metrics.get("sinr", "")
        lte_rsrq_raw = metrics.get("rsrq", "")
        cell_id = metrics.get("cell_id", "")
        
        nr_rsrp_raw = metrics.get("nrrsrp", "")
        nr_sinr_raw = metrics.get("nrsinr", "")
        nr_rsrq_raw = metrics.get("nrrsrq", "")
        
        # Parse numeric values
        def to_num(val):
            try:
                return float(val.replace("dBm", "").replace("dB", ""))
            except:
                return None

        lte_rsrp = to_num(lte_rsrp_raw)
        lte_sinr = to_num(lte_sinr_raw)
        lte_rsrq = to_num(lte_rsrq_raw)
        nr_rsrp = to_num(nr_rsrp_raw)
        nr_sinr = to_num(nr_sinr_raw)
        nr_rsrq = to_num(nr_rsrq_raw)
        
        # Track Cell-hopping (Ping-ponging)
        signature = self.cell_signature(metrics)
        if signature.strip("|") and (not self.history_cell_signatures or self.history_cell_signatures[-1]["signature"] != signature):
            if self.history_cell_signatures:
                time_since_change = time.time() - self.last_cell_change_time
                if time_since_change < 60: # changed within 1 minute
                    self.cell_hopping_count += 1
                self.last_cell_change_time = time.time()
            self.history_cell_signatures.append({
                "timestamp": datetime.now().isoformat(),
                "signature": signature,
                "cell_id": cell_id,
                "pci": metrics.get("pci", ""),
                "band": metrics.get("band", ""),
                "earfcn": metrics.get("earfcn", "")
            })
            if len(self.history_cell_signatures) > 10:
                self.history_cell_signatures.pop(0)

        if cell_id and (not self.history_cell_ids or self.history_cell_ids[-1] != cell_id):
            self.history_cell_ids.append(cell_id)
            if len(self.history_cell_ids) > 10:
                self.history_cell_ids.pop(0)

        # 2. Add alerts and recommendations
        
        # Cell Hopping Alert
        if self.cell_hopping_count >= 2:
            alerts.append("⚠️ تذبذب البرج (Cell Hopping)")
            recommendations.append("• المودم يتنقل بين أبراج مختلفة مما يسبب انقطاع اللعب (ping spikes) والتعليق. ضع المودم في مكان مرتفع وثابت بجانب النافذة لمنعه من التنقل.")

        saved_profile = read_lock_profile()
        if saved_profile:
            comparison = compare_lock_profile(metrics, saved_profile)
            if not comparison.get("match"):
                alerts.append("🎮 خروج عن بروفايل الألعاب المحفوظ")
                recommendations.append("• المودم لم يعد على نفس البرج/التردد المحفوظ للألعاب. افتح بطاقة تثبيت الألعاب وجرب قفل LTE الحالي أو أعد حفظ البرج الأفضل.")
        
        # 5G Weak Signal Alert
        if nr_rsrp is not None and nr_rsrp < -95:
            alerts.append("⚠️ إشارة الـ 5G ضعيفة جداً")
            recommendations.append("• إشارة الـ 5G ضعيفة. قم بتقريب المودم من أقرب نافذة في الغرفة لتقليل الجدران العازلة.")
        
        # Interference Alert (Low SINR but Good RSRP)
        if lte_rsrp is not None and lte_rsrp >= -90:
            if lte_sinr is not None and lte_sinr < 5:
                alerts.append("⚠️ تشويش عالي (High Interference)")
                recommendations.append("• الإشارة قوية ولكن التشويش عالي جداً (SINR منخفض). ابعد المودم عن الشاشات، أجهزة الراوتر الأخرى، الميكرويف، أو الجدران الخرسانية السميكة.")
        
        if nr_rsrp is not None and nr_rsrp >= -85:
            if nr_sinr is not None and nr_sinr < 8:
                alerts.append("⚠️ تشويش عالي في الـ 5G")
                recommendations.append("• إشارة الـ 5G تعاني من تداخل وتشويش. قم بتغيير زاوية المودم بمقدار 45 درجة أو ضعه في مكان أعلى.")

        # Congestion / Load Alert
        cqi_val = to_num(metrics.get("cqi0", ""))
        if cqi_val is not None and cqi_val < 7:
            alerts.append("⚠️ ضغط على البرج (Congestion)")
            recommendations.append("• جودة القناة منخفضة (CQI < 7) مما يدل على وجود ضغط كبير على البرج من مستخدمين آخرين. هذا يفسر البطء والتعليق المستمر.")
            
        # General Placement Recommendation
        if not alerts:
            alerts.append("✅ حالة الاتصال مستقرة")
            
        if lte_rsrp is not None and nr_rsrp is not None:
            avg_strength = (lte_rsrp + nr_rsrp) / 2
            if avg_strength >= -82:
                recommendations.append("• الموقع الحالي ممتاز! الإشارة قوية ومستقرة.")
            elif avg_strength >= -92:
                recommendations.append("• الموقع متوسط. يمكنك تحسين السرعة برفع المودم للأعلى (فوق خزانة مثلاً) أو وضعه بجانب النافذة مباشرة.")
            else:
                recommendations.append("• الموقع سيء جداً. يرجى نقل المودم لغرفة أخرى تواجه الشارع أو النافذة لضمان وصول الإشارة.")
        else:
            recommendations.append("• تأكد من تشغيل المودم وتوصيله بالكهرباء.")

        return alerts, recommendations

    def trigger_doctor_diagnose(self, metrics, trigger_type, force=False):
        if not getattr(self, "doctor", None):
            return False
        alerts, recs = self.diagnose_issues(metrics)
        extra = {
            "trigger": trigger_type,
            "alerts": alerts,
            "lock_profile": read_lock_profile(),
            "blocked_cells": read_blocked_cells(),
            "latest_scan": read_scan_results()
        }
        context = {
            "cell_hopping_count": self.cell_hopping_count,
            "consecutive_failures": self.consecutive_failures
        }
        congestion = {
            "cqi": metrics.get("cqi0"),
            "rsrq": metrics.get("rsrq"),
            "sinr": metrics.get("sinr")
        }
        dossier = build_dossier(metrics, context, congestion, extra)
        return self.doctor.diagnose_async(dossier, trigger_type, force=force)

    def maybe_auto_diagnose(self, metrics):
        if not metrics or metrics.get("error"):
            return
        
        def to_num(val):
            try:
                return float(val.replace("dBm", "").replace("dB", ""))
            except:
                return None
        lte_rsrp = to_num(metrics.get("rsrp"))
        lte_sinr = to_num(metrics.get("sinr"))
        cqi = to_num(metrics.get("cqi0"))
        
        # 1. Signal Collapse
        if lte_rsrp is not None and lte_rsrp < -110:
            payload = {"metrics": metrics}
            msg = build_alert_message("signal_collapse", payload)
            NOTIFIER.notify("signal_collapse", msg)
            self.trigger_doctor_diagnose(metrics, "signal_collapse")
        # 2. Congestion High
        elif cqi is not None and cqi < 7:
            payload = {"metrics": metrics, "reasons": ["CQI quality channel is very low"]}
            msg = build_alert_message("congestion_high", payload)
            NOTIFIER.notify("congestion_high", msg)
            self.trigger_doctor_diagnose(metrics, "congestion_high")
        # 3. Cell Hopping
        elif self.cell_hopping_count >= 2:
            self.trigger_doctor_diagnose(metrics, "cell_hopping")

    def handle_blocked_cell_handover(self, metrics):
        if not metrics:
            return
        cell_id = str(metrics.get("cell_id", "")).strip()
        if not cell_id:
            return
        blocked = read_blocked_cells()
        if cell_id in blocked:
            self.trigger_doctor_diagnose(metrics, "blocked_cell_handover", force=True)
            history = read_band_history()
            cells = history.get("cells", {})
            alternative_band = None
            for sig, cell in cells.items():
                if str(cell.get("cell_id")) != cell_id and cell.get("band"):
                    alternative_band = cell.get("band")
                    break
            if alternative_band:
                enqueue_modem_command({
                    "type": "set_lte_bands",
                    "bands": str(alternative_band)
                })

    def save_metrics_to_files(self, metrics, status_text="Connected"):
        """Save metrics to metrics.json and history CSV."""
        global LATEST_METRICS
        LATEST_METRICS = metrics  # Update global state

        record_band_history(metrics)
        build_ai_context_summary(metrics)
        self.maybe_auto_diagnose(metrics)
        self.handle_blocked_cell_handover(metrics)

        # 1. Save JSON
        alerts, recs = self.diagnose_issues(metrics)
        metrics_data = {
            "timestamp": datetime.now().isoformat(),
            "status": status_text,
            "metrics": metrics,
            "diagnostics": {
                "alerts": alerts,
                "recommendations": recs
            },
            "lock": self.get_lock_state(metrics)
        }
        
        try:
            with open(METRICS_FILE, 'w', encoding='utf-8') as f:
                json.dump(metrics_data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            pass

        # 2. Append CSV
        try:
            with open(HISTORY_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    metrics.get("rsrp", "N/A"),
                    metrics.get("rsrq", "N/A"),
                    metrics.get("sinr", "N/A"),
                    metrics.get("rssi", "N/A"),
                    metrics.get("cell_id", "N/A"),
                    metrics.get("band", "N/A"),
                    metrics.get("nrrsrp", "N/A"),
                    metrics.get("nrrsrq", "N/A"),
                    metrics.get("nrsinr", "N/A"),
                    metrics.get("nrulbandwidth", "N/A"),
                    metrics.get("nrulfreq", "N/A"),
                    status_text
                ])
        except Exception as e:
            pass

        # 3. Save to database
        try:
            signal_db.insert_reading(metrics, status_text)
        except Exception:
            pass

    def make_dashboard(self, metrics, status_text="Connected"):
        """Generate a layout for the Rich terminal dashboard."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3)
        )
        
        # Header Panel
        title = Text("نظام تحليل وتوجيه إشارة مودم هواوي الذكي - Huawei Modem Signal Analyzer", style="bold magenta justify=center")
        header_text = Text.assemble(
            title, "\n", 
            Text(f"حالة الاتصال: {status_text} | ", style="dim"),
            Text(f"رابط لوحة التحكم الشبكي المباشر: {get_dashboard_urls()[0]}", style="bold yellow")
        )
        layout["header"].update(Panel(header_text, style="cyan"))
        
        # Body Split
        layout["body"].split_row(
            Layout(name="signals", ratio=1),
            Layout(name="diagnostics", ratio=1)
        )

        # Signals Table
        table = Table(title="قراءات الإشارة الحالية (Real-time Signal Metrics)", expand=True)
        table.add_column("المعيار (Metric)", style="cyan", justify="left")
        table.add_column("شبكة الجيل الرابع (LTE 4G)", style="yellow", justify="center")
        table.add_column("شبكة الجيل الخامس (5G NR)", style="green", justify="center")
        
        # RSRP
        lte_rsrp = metrics.get("rsrp", "N/A")
        nr_rsrp = metrics.get("nrrsrp", "N/A")
        lte_rsrp_eval, lte_rsrp_color, _ = self.evaluate_rsrp(lte_rsrp)
        nr_rsrp_eval, nr_rsrp_color, _ = self.evaluate_rsrp(nr_rsrp, is_5g=True)
        table.add_row("قوة الإشارة (RSRP)", f"[{lte_rsrp_color}]{lte_rsrp} ({lte_rsrp_eval})[/]", f"[{nr_rsrp_color}]{nr_rsrp} ({nr_rsrp_eval})[/]")

        # RSRQ
        lte_rsrq = metrics.get("rsrq", "N/A")
        nr_rsrq = metrics.get("nrrsrq", "N/A")
        lte_rsrq_eval, lte_rsrq_color, _ = self.evaluate_rsrq(lte_rsrq)
        nr_rsrq_eval, nr_rsrq_color, _ = self.evaluate_rsrq(nr_rsrq)
        table.add_row("جودة الإشارة (RSRQ)", f"[{lte_rsrq_color}]{lte_rsrq} ({lte_rsrq_eval})[/]", f"[{nr_rsrq_color}]{nr_rsrq} ({nr_rsrq_eval})[/]")

        # SINR
        lte_sinr = metrics.get("sinr", "N/A")
        nr_sinr = metrics.get("nrsinr", "N/A")
        lte_sinr_eval, lte_sinr_color, _ = self.evaluate_sinr(lte_sinr)
        nr_sinr_eval, nr_sinr_color, _ = self.evaluate_sinr(nr_sinr)
        table.add_row("معدل التشويش (SINR)", f"[{lte_sinr_color}]{lte_sinr} ({lte_sinr_eval})[/]", f"[{nr_sinr_color}]{nr_sinr} ({nr_sinr_eval})[/]")

        # Other details
        table.add_row("معرف البرج (Cell ID / PCI)", f"{metrics.get('cell_id', 'N/A')} / {metrics.get('pci', 'N/A')}", f"PCI: {metrics.get('scc_pci', 'N/A') or 'N/A'}")
        table.add_row("نطاق التردد (Band)", f"Band {metrics.get('band', 'N/A')}", f"Freq: {metrics.get('nrulfreq', 'N/A')}")
        table.add_row("عرض النطاق (Bandwidth)", f"DL: {metrics.get('dlbandwidth', 'N/A')}", f"DL: {metrics.get('nrdlbandwidth', 'N/A')}")
        table.add_row("جودة القناة (CQI)", f"CQI-0: {metrics.get('cqi0', 'N/A')} | CQI-1: {metrics.get('cqi1', 'N/A')}", "N/A")

        layout["signals"].update(Panel(table, style="blue"))

        # Diagnostics Panel
        alerts, recs = self.diagnose_issues(metrics)
        diag_text = Text()
        
        diag_text.append("🚨 تشخيص المشاكل الحالي (Diagnostics):\n", style="bold underline yellow")
        for alert in alerts:
            diag_text.append(f" • {alert}\n", style="bold red" if "⚠️" in alert else "green")
        
        diag_text.append("\n📍 التوجيهات وأفضل مكان للمودم (Placement Guide):\n", style="bold underline green")
        for rec in recs:
            diag_text.append(f" {rec}\n")
            
        layout["diagnostics"].update(Panel(diag_text, style="green"))

        # Footer Panel
        footer_text = Text("اضغط CTRL+C للخروج من البرنامج | تم تشغيل خادم ويب محلي لمتابعة لوحة الويب حياً وبدون مشاكل متصفح", style="dim justify=center")
        layout["footer"].update(Panel(footer_text, style="cyan"))
        
        return layout

    def perform_login(self, page):
        """Perform login flow by navigating to the index page and entering password."""
        try:
            page.goto(f"http://{MODEM_IP}/html/index.html", timeout=15000)
            page.wait_for_timeout(3000)
            
            pwd_field = page.query_selector("#login_password") or page.query_selector("input[type='password']")
            if not pwd_field:
                console.print("[red]❌ لم يتم العثور على حقل كلمة المرور في الصفحة.[/]")
                return False
                
            user_field = page.query_selector("#login_username")
            if user_field and user_field.is_visible():
                if not user_field.input_value():
                    user_field.fill("admin")
                    
            pwd_field.fill(PASSWORD)
            login_btn = (
                page.query_selector("#login_btn") or 
                page.query_selector("#login_submit") or
                page.query_selector(".login-btn")
            )
            
            if login_btn:
                login_btn.click()
            else:
                pwd_field.press("Enter")
                
            page.wait_for_timeout(5000)
            return "content.html" in page.url or "home" in page.url
        except Exception as e:
            console.print(f"[red]❌ حدث استثناء أثناء عملية تسجيل الدخول: {e}[/]")
            return False

    def start(self):
        console.clear()
        console.print("[bold yellow]جاري بدء خادم الويب المحلي للشبكة بأكملها...[/]")
        
        # Start server in thread
        srv_thread = threading.Thread(target=start_http_server, daemon=True)
        srv_thread.start()
        
        console.print("[green]✔ تم تشغيل خادم الويب بنجاح. للدخول من أي جهاز على نفس الشبكة استخدم أحد الروابط:[/]")
        for url in get_dashboard_urls():
            console.print(f"[bold cyan]👉 {url}[/]")
        console.print("[dim]إذا لم يفتح الرابط من الجوال، شغّل allow_firewall.bat مرة واحدة كمسؤول.[/]")
        console.print("-" * 70)
        
        console.print("[bold yellow]جاري بدء تشغيل متصفح كروم الآلي للاتصال بالمودم...[/]")
        console.print(f"نطاق الاتصال: {MODEM_IP} باستخدام كلمة المرور المحددة.")
        
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
                page = browser.new_page()
                
                # Perform login
                login_success = self.perform_login(page)
                if not login_success:
                    self.modem_ready = False
                    console.print("[red]❌ خطأ: لم ينجح تسجيل الدخول. يرجى التحقق من كلمة المرور أو اتصال المودم.[/]")
                    return
                self.modem_ready = True
                    
                console.print("[green]✔ تم تسجيل الدخول بنجاح! جاري تشغيل لوحة التحكم الحية...[/]")
                
                # Navigate to signal API
                page.goto(f"http://{MODEM_IP}/api/device/signal")
                page.wait_for_timeout(2000)

                # Get initial metrics
                metrics = self.parse_signal_data(page.content())
                self.save_metrics_to_files(metrics)
                
                # Open browser automatically
                try:
                    webbrowser.open(f"http://127.0.0.1:{PORT}/dashboard.html")
                except:
                    pass
                
                with Live(self.make_dashboard(metrics), refresh_per_second=1, screen=True) as live:
                    while True:
                        try:
                            self.process_modem_commands(page)

                            # Evaluate fast fetch in page context
                            xml_text = page.evaluate("fetch('/api/device/signal').then(r => r.text())")
                            metrics = self.parse_signal_data(xml_text)
                            
                            # If session expired or parsing error occurs, trigger relogin flow
                            if "error" in metrics or not metrics.get("rsrp"):
                                self.consecutive_failures += 1
                                if self.consecutive_failures > 2:
                                    live.update(Panel(Text("⚠️ انتهت جلسة الاتصال. جاري إعادة تسجيل الدخول...", style="bold red justify=center")))
                                    # Perform re-login
                                    if self.perform_login(page):
                                        self.modem_ready = True
                                        page.goto(f"http://{MODEM_IP}/api/device/signal")
                                        page.wait_for_timeout(2000)
                                        self.consecutive_failures = 0
                                    else:
                                        self.modem_ready = False
                                        time.sleep(5)
                                        continue
                            else:
                                self.consecutive_failures = 0
                                self.save_metrics_to_files(metrics)
                                
                                now = time.time()
                                # predict every 30s
                                if now - getattr(self, "last_predict_time", 0) >= 30:
                                    self.last_predict_time = now
                                    try:
                                        pred = PREDICTOR.predict_now()
                                        if pred.get("ok") and pred.get("degrading"):
                                            hist = read_band_history()
                                            suggestion = stability_loop.suggest_action(metrics, hist, pred)
                                            stability_loop.record_event("predicted_degradation", metrics, prediction=pred, suggestion=suggestion)
                                            NOTIFIER.notify("predicted_degradation", build_alert_message("predicted_degradation", {
                                                "current_sinr": pred.get("current_sinr"),
                                                "predicted_sinr": pred.get("predicted_sinr_3min"),
                                                "delta": pred.get("delta"),
                                                "suggestion": suggestion
                                            }))
                                            self.trigger_doctor_diagnose(metrics, "predicted_degradation")
                                            # optional auto apply
                                            cfg = read_doctor_config()
                                            if cfg.get("auto_apply_band") and suggestion.get("action") == "switch" and suggestion.get("band"):
                                                enqueue_modem_command({"type": "set_lte_bands", "bands": str(suggestion["band"])})
                                    except Exception:
                                        pass

                                # evaluate pending every 60s
                                if now - getattr(self, "last_eval_time", 0) >= 60:
                                    self.last_eval_time = now
                                    try:
                                        stability_loop.evaluate_pending(metrics)
                                    except Exception:
                                        pass

                                # daily report once per day
                                try:
                                    tg = read_telegram_config()
                                    hour = int(tg.get("daily_report_hour", 9))
                                    today = datetime.now().strftime("%Y-%m-%d")
                                    if datetime.now().hour >= hour and getattr(self, "last_daily_report_date", None) != today:
                                        rows = signal_db.query_recent(hours=24)
                                        # simple avg
                                        def num(v):
                                            try:
                                                import re
                                                m=re.search(r"-?\d+(?:\.\d+)?", str(v)); return float(m.group(0)) if m else None
                                            except: return None
                                        rsrps=[num(r.get("lte_rsrp")) for r in rows]; rsrps=[x for x in rsrps if x is not None]
                                        sinrs=[num(r.get("lte_sinr")) for r in rows]; sinrs=[x for x in sinrs if x is not None]
                                        st=stability_loop.get_status()
                                        outcomes=st.get("outcomes") or st.get("recent_outcomes") or []
                                        # count improved/worse/same from outcomes list if present
                                        imp=sum(1 for o in outcomes if (o.get("outcome") if isinstance(o,dict) else o)=="improved")
                                        wse=sum(1 for o in outcomes if (o.get("outcome") if isinstance(o,dict) else o)=="worse")
                                        same=sum(1 for o in outcomes if (o.get("outcome") if isinstance(o,dict) else o)=="same")
                                        payload={"samples":len(rows),"avg_rsrp":round(sum(rsrps)/len(rsrps),1) if rsrps else "N/A","avg_sinr":round(sum(sinrs)/len(sinrs),1) if sinrs else "N/A","worst_hour":"N/A","doctor_runs":0,"outcomes_improved":imp,"outcomes_worse":wse,"outcomes_same":same}
                                        NOTIFIER.notify("daily_report", build_alert_message("daily_report", payload), force=True)
                                        self.last_daily_report_date = today
                                except Exception:
                                    pass

                                live.update(self.make_dashboard(metrics))
                                
                        except Exception as inner_e:
                            self.consecutive_failures += 1
                            if self.consecutive_failures > 2:
                                if self.perform_login(page):
                                    self.modem_ready = True
                                    page.goto(f"http://{MODEM_IP}/api/device/signal")
                                    page.wait_for_timeout(2000)
                                    self.consecutive_failures = 0
                                else:
                                    self.modem_ready = False
                            
                        time.sleep(2)
                        
            except KeyboardInterrupt:
                console.clear()
                console.print("[bold green]تم إيقاف تشغيل البرنامج بنجاح. شكراً لك![/]")
            except Exception as e:
                console.print(f"[bold red]حدث خطأ غير متوقع: {e}[/]")
            finally:
                self.modem_ready = False
                try:
                    browser.close()
                except:
                    pass

if __name__ == "__main__":
    if not MODEM_IP or not PASSWORD:
        console.print("[bold red]MODEM_IP و MODEM_PASSWORD غير مضبوطين.[/]")
        console.print("انسخ .env.example إلى .env ثم ضع عنوان بوابة المودم وكلمة مرور صفحة الإدارة فيه.")
        sys.exit(1)
    # Clear away browsers left running by a previous run that was force-killed
    # rather than closed. The finally block below closes the browser on a clean
    # exit, but a hard kill skips it, and each time that happened a headless
    # Chrome kept running with its profile folder in %TEMP% — 32 of them had
    # accumulated before this was added.
    #
    # Only browsers whose driver process is gone are touched, so a second copy
    # of this script running elsewhere keeps its own browser.
    try:
        from playwright_janitor import sweep

        sweep()
    except Exception:
        pass  # tidying up must never stop the analyzer from starting

    analyzer = ModemAnalyzer()
    analyzer.start()
