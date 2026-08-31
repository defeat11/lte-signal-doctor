"""Explainable network intelligence built from live and historical modem data."""

import math
import statistics
import threading
import time
from datetime import datetime

import signal_db


_CACHE = {"at": 0.0, "key": None, "value": None}
_CACHE_LOCK = threading.Lock()


def _num(value):
    return signal_db.to_num(value)


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _metric_score(value, bad, good):
    if value is None:
        return None
    return _clamp((value - bad) * 100.0 / (good - bad))


def _mean(values):
    clean = [v for v in values if v is not None]
    return statistics.fmean(clean) if clean else None


def _stdev(values):
    clean = [v for v in values if v is not None]
    return statistics.pstdev(clean) if len(clean) >= 2 else 0.0


def _round(value, digits=1):
    return round(value, digits) if value is not None else None


def _current_score(metrics):
    parts = {
        "lte_power": _metric_score(_num(metrics.get("rsrp")), -120, -70),
        "lte_quality": _metric_score(_num(metrics.get("sinr")), -5, 25),
        "lte_cleanliness": _metric_score(_num(metrics.get("rsrq")), -20, -3),
        "nr_power": _metric_score(_num(metrics.get("nrrsrp")), -120, -70),
        "nr_quality": _metric_score(_num(metrics.get("nrsinr")), -5, 25),
    }
    weights = {
        "lte_power": 0.20, "lte_quality": 0.35, "lte_cleanliness": 0.15,
        "nr_power": 0.10, "nr_quality": 0.20,
    }
    present = [(parts[k], weights[k]) for k in parts if parts[k] is not None]
    score = sum(v * w for v, w in present) / sum(w for _, w in present) if present else 0.0
    return score, {k: _round(v) for k, v in parts.items()}


def _history_analysis(series):
    sinr = [row.get("avg_sinr") for row in series]
    rsrp = [row.get("avg_rsrp") for row in series]
    recent_sinr = [v for v in sinr[-10:] if v is not None]
    baseline_sinr = [v for v in sinr[-130:-10] if v is not None]
    recent_rsrp = [v for v in rsrp[-10:] if v is not None]
    baseline_rsrp = [v for v in rsrp[-130:-10] if v is not None]

    current_sinr = _mean(recent_sinr)
    base_sinr = _mean(baseline_sinr)
    sigma = _stdev(baseline_sinr)
    z_score = ((current_sinr - base_sinr) / sigma) if current_sinr is not None and base_sinr is not None and sigma >= 0.5 else 0.0
    sinr_drop = (base_sinr - current_sinr) if current_sinr is not None and base_sinr is not None else 0.0

    current_rsrp = _mean(recent_rsrp)
    base_rsrp = _mean(baseline_rsrp)
    rsrp_drop = (base_rsrp - current_rsrp) if current_rsrp is not None and base_rsrp is not None else 0.0

    anomaly = "normal"
    if z_score <= -2.5 or sinr_drop >= 6 or rsrp_drop >= 10:
        anomaly = "critical"
    elif z_score <= -1.5 or sinr_drop >= 3 or rsrp_drop >= 6:
        anomaly = "warning"

    volatility = _stdev([v for v in sinr[-60:] if v is not None])
    stability_score = _clamp(100 - volatility * 12)
    return {
        "level": anomaly,
        "z_score": _round(z_score, 2),
        "sinr_drop_db": _round(sinr_drop),
        "rsrp_drop_db": _round(rsrp_drop),
        "baseline_sinr": _round(base_sinr),
        "recent_sinr": _round(current_sinr),
        "volatility_db": _round(volatility, 2),
        "stability_score": _round(stability_score),
    }


def _usage_windows():
    profile = signal_db.hourly_profile(days=14)
    ranked = []
    for hour, row in profile.items():
        if row.get("samples", 0) < 10 or row.get("avg_sinr") is None:
            continue
        score = (_metric_score(row.get("avg_sinr"), -5, 25) * 0.7 +
                 _metric_score(row.get("avg_rsrp"), -120, -70) * 0.3)
        ranked.append({
            "hour": hour, "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
            "score": _round(score), "avg_sinr": row.get("avg_sinr"),
            "avg_rsrp": row.get("avg_rsrp"), "samples": row.get("samples", 0),
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:3], list(reversed(ranked[-3:])) if ranked else []


def _rank_bands(history, current_band):
    by_band = {}
    for cell in (history or {}).get("cells", {}).values():
        band = str(cell.get("band", "")).strip()
        samples = int(cell.get("samples") or 0)
        sinr = _num(cell.get("avg_sinr"))
        rsrp = _num(cell.get("avg_rsrp"))
        if not band or samples < 3 or sinr is None:
            continue
        item = by_band.setdefault(band, {"weight": 0, "sinr_sum": 0.0, "rsrp_sum": 0.0, "rsrp_weight": 0})
        weight = min(samples, 5000)
        item["weight"] += weight
        item["sinr_sum"] += sinr * weight
        if rsrp is not None:
            item["rsrp_sum"] += rsrp * weight
            item["rsrp_weight"] += weight

    result = []
    for band, item in by_band.items():
        avg_sinr = item["sinr_sum"] / item["weight"]
        avg_rsrp = item["rsrp_sum"] / item["rsrp_weight"] if item["rsrp_weight"] else None
        confidence = _clamp(25 + 18 * math.log10(max(item["weight"], 1)))
        quality = _metric_score(avg_sinr, -5, 25) * 0.7
        quality += (_metric_score(avg_rsrp, -120, -70) if avg_rsrp is not None else 50) * 0.3
        result.append({
            "band": band, "score": _round(quality), "confidence": _round(confidence),
            "avg_sinr": _round(avg_sinr), "avg_rsrp": _round(avg_rsrp),
            "samples": item["weight"], "current": band == str(current_band),
        })
    result.sort(key=lambda x: (x["score"], x["confidence"]), reverse=True)
    return result


def build_report(metrics, band_history=None, prediction=None, hours=24):
    metrics = metrics or {}
    cache_key = (metrics.get("cell_id"), metrics.get("band"), metrics.get("sinr"), metrics.get("rsrp"), hours)
    with _CACHE_LOCK:
        if _CACHE["value"] is not None and _CACHE["key"] == cache_key and time.time() - _CACHE["at"] < 15:
            return _CACHE["value"]

    series = signal_db.query_aggregates(hours=hours, bucket="minute")
    live_score, components = _current_score(metrics)
    anomaly = _history_analysis(series)
    stability = anomaly["stability_score"]
    health_score = _clamp(live_score * 0.75 + stability * 0.25)
    best_windows, worst_windows = _usage_windows()
    bands = _rank_bands(band_history or {}, metrics.get("band"))

    pred = prediction or {}
    predicted_drop = abs(min(0, _num(pred.get("delta")) or 0))
    risk = _clamp((100 - health_score) * 0.55 + (100 - stability) * 0.25 + predicted_drop * 6)
    if anomaly["level"] == "warning":
        risk = max(risk, 45)
    elif anomaly["level"] == "critical":
        risk = max(risk, 75)

    current_band = str(metrics.get("band", ""))
    best_band = bands[0] if bands else None
    decision = {"action": "hold", "band": current_band, "confidence": 80, "reason": "الاتصال مستقر والباند الحالي مناسب."}
    if health_score < 45 or anomaly["level"] == "critical":
        if best_band and not best_band["current"] and best_band["score"] >= health_score + 8:
            decision = {"action": "switch", "band": best_band["band"], "confidence": best_band["confidence"], "reason": "تدهور مؤكد تاريخياً ويوجد باند بديل أفضل بوضوح."}
        else:
            decision = {"action": "inspect", "band": current_band, "confidence": 72, "reason": "يوجد تدهور، لكن البيانات لا تضمن أن تبديل الباند سيحسن الاتصال."}
    elif best_band and not best_band["current"] and best_band["score"] >= live_score + 15 and best_band["confidence"] >= 60:
        decision = {"action": "consider", "band": best_band["band"], "confidence": best_band["confidence"], "reason": "السجل يشير إلى بديل أقوى، لكن الحالة الحالية ليست حرجة."}

    if health_score >= 80:
        grade, label = "A", "ممتاز"
    elif health_score >= 65:
        grade, label = "B", "جيد"
    elif health_score >= 50:
        grade, label = "C", "متوسط"
    elif health_score >= 35:
        grade, label = "D", "ضعيف"
    else:
        grade, label = "F", "حرج"

    report = {
        "generated_at": datetime.now().isoformat(), "engine": "Network Intelligence v1",
        "health_score": _round(health_score), "grade": grade, "label": label,
        "risk_score": _round(risk), "components": components, "anomaly": anomaly,
        "decision": decision, "bands": bands[:6], "best_windows": best_windows,
        "worst_windows": worst_windows, "samples_minutes": len(series),
        "timeline": [{"ts": x.get("bucket_ts"), "sinr": x.get("avg_sinr"), "rsrp": x.get("avg_rsrp")} for x in series[-60:]],
    }
    with _CACHE_LOCK:
        _CACHE.update({"at": time.time(), "key": cache_key, "value": report})
    return report
