"""On-demand diagnosis for slow first-page loads and YouTube startup."""

import http.client
import os
import socket
import statistics
import subprocess
import threading
import time
from datetime import datetime


_LOCK = threading.Lock()
_STATE = {"running": False, "finished": False, "error": None, "result": None, "started_at": None}

# Continuous background connectivity monitor (separate from the on-demand
# slow-page diagnosis above). Tracks packet loss / jitter to a fixed host so
# intermittent "تقطيع" shows up even if nobody triggers a manual diagnosis.
MONITOR_HOST = "8.8.8.8"

# The modem gateway to ping in local-link checks; comes from the environment
# so no LAN address is written into the code.
GATEWAY_HOST = os.getenv("MODEM_IP", "").strip()
_MONITOR_LOCK = threading.Lock()
_MONITOR_STATE = {
    "enabled": False,
    "last_checked_at": None,
    "ok": None,
    "avg_ms": None,
    "jitter_ms": None,
    "loss_percent": None,
    "consecutive_bad": 0,
    "last_bad_at": None,
    "unstable": False,
    "history": [],  # last N samples: {t, avg_ms, loss_percent, jitter_ms, unstable}
}
_MONITOR_THREAD = None

# Thresholds used to flag "تقطيع" (instability) even when the link is not
# fully down: some packet loss, or round-trip jitter that is high relative
# to the average latency.
UNSTABLE_LOSS_PERCENT = 1
UNSTABLE_JITTER_MS = 25
MONITOR_HISTORY_LIMIT = 120


def _number(value):
    if value is None:
        return None
    import re
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _ping(host, count=6):
    try:
        result = subprocess.run(
            ["ping", "-n", str(count), "-w", "1500", host],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        import re
        loss = re.search(r"\((\d+)%\s*loss\)", result.stdout, re.IGNORECASE)
        average = re.search(r"Average\s*=\s*(\d+)ms", result.stdout, re.IGNORECASE)
        times = [float(m) for m in re.findall(r"time[=<](\d+(?:\.\d+)?)ms", result.stdout, re.IGNORECASE)]
        jitter = round(statistics.pstdev(times), 1) if len(times) >= 2 else 0.0
        return {
            "ok": result.returncode == 0,
            "avg_ms": float(average.group(1)) if average else None,
            "loss_percent": int(loss.group(1)) if loss else (0 if result.returncode == 0 else 100),
            "jitter_ms": jitter,
            "samples_ms": times,
        }
    except Exception as exc:
        return {"ok": False, "avg_ms": None, "loss_percent": 100, "jitter_ms": None, "error": str(exc)}


def _monitor_tick():
    reading = _ping(MONITOR_HOST, count=5)
    loss = reading.get("loss_percent") or 0
    jitter = reading.get("jitter_ms") or 0
    unstable = bool(loss >= UNSTABLE_LOSS_PERCENT or jitter >= UNSTABLE_JITTER_MS or not reading.get("ok"))
    now = datetime.now().isoformat()

    with _MONITOR_LOCK:
        consecutive_bad = _MONITOR_STATE["consecutive_bad"] + 1 if unstable else 0
        _MONITOR_STATE.update({
            "enabled": True,
            "last_checked_at": now,
            "ok": reading.get("ok"),
            "avg_ms": reading.get("avg_ms"),
            "jitter_ms": jitter,
            "loss_percent": loss,
            "consecutive_bad": consecutive_bad,
            "last_bad_at": now if unstable else _MONITOR_STATE["last_bad_at"],
            "unstable": unstable,
        })
        _MONITOR_STATE["history"].append({
            "t": now, "avg_ms": reading.get("avg_ms"), "loss_percent": loss,
            "jitter_ms": jitter, "unstable": unstable,
        })
        del _MONITOR_STATE["history"][:-MONITOR_HISTORY_LIMIT]


def _monitor_loop(interval_seconds):
    while True:
        try:
            _monitor_tick()
        except Exception:
            pass
        time.sleep(interval_seconds)


def start_monitor(interval_seconds=20):
    """Start the always-on 8.8.8.8 packet-loss/jitter watcher (idempotent)."""
    global _MONITOR_THREAD
    if _MONITOR_THREAD and _MONITOR_THREAD.is_alive():
        return False
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop, args=(interval_seconds,), daemon=True)
    _MONITOR_THREAD.start()
    return True


def get_health():
    """Latest continuous-monitor snapshot for MONITOR_HOST (8.8.8.8)."""
    with _MONITOR_LOCK:
        return dict(_MONITOR_STATE, history=list(_MONITOR_STATE["history"][-30:]))


def quick_ping(host=MONITOR_HOST, count=3):
    """Public one-shot ping helper for callers outside this module (e.g. the
    guarded band-switch experiment that needs to watch recovery live)."""
    return _ping(host, count=count)


def _dns(host):
    started = time.perf_counter()
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        elapsed = (time.perf_counter() - started) * 1000
        return {"host": host, "ok": True, "ms": round(elapsed, 1), "addresses": len(addresses)}
    except Exception as exc:
        return {"host": host, "ok": False, "ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)}


def _https_ttfb(host, path="/generate_204"):
    started = time.perf_counter()
    connection = None
    try:
        connection = http.client.HTTPSConnection(host, timeout=8)
        connection.request("GET", path, headers={"User-Agent": "SmartModemDiagnostics/1.0", "Connection": "close"})
        response = connection.getresponse()
        first_byte_ms = (time.perf_counter() - started) * 1000
        response.read(64)
        return {"host": host, "ok": True, "ttfb_ms": round(first_byte_ms, 1), "status": response.status}
    except Exception as exc:
        return {"host": host, "ok": False, "ttfb_ms": round((time.perf_counter() - started) * 1000, 1), "error": str(exc)}
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass


def _diagnose(metrics, pressure):
    if GATEWAY_HOST:
        gateway = _ping(GATEWAY_HOST, count=5)
    else:
        gateway = {"ok": None, "avg_ms": None, "loss_percent": 0, "jitter_ms": None}
    internet = _ping("1.1.1.1", count=8)
    google_dns = _ping(MONITOR_HOST, count=8)
    dns = [_dns(host) for host in ("www.youtube.com", "www.google.com", "www.cloudflare.com")]
    web = [_https_ttfb("www.youtube.com"), _https_ttfb("www.google.com")]

    dns_times = [item["ms"] for item in dns if item.get("ok")]
    web_times = [item["ttfb_ms"] for item in web if item.get("ok")]
    dns_avg = statistics.fmean(dns_times) if dns_times else None
    dns_max = max(dns_times) if dns_times else None
    web_avg = statistics.fmean(web_times) if web_times else None

    sinr = _number(metrics.get("sinr"))
    rsrq = _number(metrics.get("rsrq"))
    cqi_values = [_number(metrics.get("cqi0")), _number(metrics.get("cqi1"))]
    cqi_values = [value for value in cqi_values if value is not None]
    cqi = statistics.fmean(cqi_values) if cqi_values else None
    pressure_index = float((pressure or {}).get("index") or 0)

    local_bad = gateway.get("ok") is False or (gateway.get("avg_ms") or 0) > 10 or (gateway.get("loss_percent", 0) or 0) > 0
    radio_bad = pressure_index >= 55 or (sinr is not None and sinr < 8) or (rsrq is not None and rsrq < -13) or (cqi is not None and cqi < 7)
    internet_choppy = (internet.get("loss_percent", 0) or 0) > 1 or (internet.get("jitter_ms") or 0) >= UNSTABLE_JITTER_MS
    google_choppy = (google_dns.get("loss_percent", 0) or 0) > 1 or (google_dns.get("jitter_ms") or 0) >= UNSTABLE_JITTER_MS
    wan_slow = (internet.get("avg_ms") or 0) >= 70 or internet_choppy or google_choppy
    dns_slow = (dns_avg or 0) >= 120 or (dns_max or 0) >= 300
    web_slow = (web_avg or 0) >= 650

    if local_bad:
        cause = "local_link"
        title = "المشكلة بين جهازك والمودم"
        tower_related = False
        explanation = "التأخير أو فقد الحزم يبدأ قبل الوصول إلى شبكة الجوال؛ افحص Wi‑Fi والكابل ومكان الجهاز."
    elif radio_bad:
        cause = "tower_radio_load"
        title = "المشكلة مرجحة من القطاع أو جودة القناة"
        tower_related = True
        explanation = "مؤشرات الراديو أو ضغط القطاع غير طبيعية، ولذلك قد يتأخر أول اتصال بالمواقع."
    elif wan_slow:
        cause = "operator_latency"
        tower_related = False
        if internet_choppy or google_choppy:
            title = "تقطيع (packet loss/jitter) في الإنترنت وليس بطء ثابت"
            explanation = "زمن الرحلة إلى 1.1.1.1 و8.8.8.8 غير ثابت أو فيه فقد حزم متقطع رغم أن الاتصال بالمودم سليم؛ هذا نمط تقطيع من مسار المشغل/الـ backhaul وليس بطء عادي، وقد لا يظهر في متوسط واحد لذلك يُفحص الآن في كل مرة."
        else:
            title = "زمن شبكة المشغل مرتفع"
            explanation = "الاتصال بالمودم سليم والقطاع غير مزدحم، لكن زمن الرحلة بعد المودم مرتفع؛ المرجح مسار المشغل أو الـ backhaul وليس قوة الإشارة."
    elif dns_slow:
        cause = "dns_delay"
        title = "خادم DNS يؤخر بداية فتح المواقع"
        tower_related = False
        explanation = "الاتصال نفسه جيد، لكن ترجمة أسماء المواقع تستغرق وقتًا قبل بدء HTTPS."
    elif web_slow:
        cause = "remote_or_tls_delay"
        title = "تأخر إنشاء HTTPS أو أول استجابة"
        tower_related = False
        explanation = "DNS والراديو طبيعيان، لكن المصافحة المشفرة أو أول استجابة بطيئة على مسار الإنترنت."
    else:
        cause = "healthy"
        title = "الاستجابة طبيعية أثناء الاختبار"
        tower_related = False
        explanation = "لم يظهر خلل مستمر الآن؛ إن كان البطء يحدث أثناء التحميل فشغّل الاختبار وقت المشكلة لكشف bufferbloat أو ازدحام متقطع."

    recommendations = []
    if internet_choppy or google_choppy:
        recommendations.append("التقطيع متقطع (loss/jitter) وليس بطء ثابت؛ راقب مؤشر الاتصال المستمر على 8.8.8.8 في اللوحة لرصد نمط تكرره عبر الوقت.")
    if dns_slow:
        recommendations.append("جرّب DNS مباشرًا مثل 1.1.1.1 و1.0.0.1 بدل DNS المودم، ثم أعد الاختبار.")
    if wan_slow and not radio_bad:
        recommendations.append("قارن Band آخر في نفس الوقت؛ إذا بقي Ping مرتفعًا فالمشكلة من مسار المشغل وليست من القطاع الحالي.")
    if radio_bad:
        recommendations.append("شغّل مسح الباندات واختر الأقل ضغطًا، ثم أعد فحص فتح المواقع.")
    if not recommendations:
        recommendations.append("أعد الاختبار لحظة ظهور البطء للحصول على عينة ممثلة للمشكلة.")

    return {
        "generated_at": datetime.now().isoformat(),
        "cause": cause, "title": title, "tower_related": tower_related,
        "explanation": explanation, "recommendations": recommendations,
        "verdict_confidence": 90 if local_bad or radio_bad or wan_slow else 75,
        "measurements": {
            "gateway": gateway, "internet": internet, "google_dns_8888": google_dns, "dns": dns, "https": web,
            "internet_choppy": internet_choppy, "google_choppy": google_choppy,
            "dns_avg_ms": round(dns_avg, 1) if dns_avg is not None else None,
            "dns_max_ms": round(dns_max, 1) if dns_max is not None else None,
            "https_avg_ttfb_ms": round(web_avg, 1) if web_avg is not None else None,
            "tower_pressure": pressure_index, "lte_sinr": sinr, "lte_rsrq": rsrq,
            "cqi_avg": round(cqi, 1) if cqi is not None else None,
        },
    }


def _worker(metrics, pressure):
    try:
        result = _diagnose(metrics, pressure)
        with _LOCK:
            _STATE.update({"running": False, "finished": True, "error": None, "result": result})
    except Exception as exc:
        with _LOCK:
            _STATE.update({"running": False, "finished": True, "error": str(exc), "result": None})


def start(metrics, pressure):
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update({"running": True, "finished": False, "error": None, "result": None, "started_at": time.time()})
    threading.Thread(target=_worker, args=(dict(metrics or {}), dict(pressure or {})), daemon=True).start()
    return True


def status():
    with _LOCK:
        return dict(_STATE)
