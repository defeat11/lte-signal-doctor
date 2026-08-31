"""Tower identity and explainable radio-load estimation.

The modem does not expose the scheduler's connected-user count. This module
therefore reports a pressure index, never a fabricated subscriber count.
"""

import math

import signal_db


def _num(value):
    return signal_db.to_num(value)


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _parse_cell(cell_id):
    try:
        value = int(str(cell_id).strip())
        return value >> 8, value & 0xFF
    except (TypeError, ValueError):
        return None, None


def _average(values):
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def estimate_pressure(metrics, recent_minutes=120):
    metrics = metrics or {}
    cqi_values = [_num(metrics.get("cqi0")), _num(metrics.get("cqi1"))]
    cqi = _average(cqi_values)
    sinr = _num(metrics.get("sinr"))
    rsrq = _num(metrics.get("rsrq"))

    components = {}
    if cqi is not None:
        components["cqi"] = _clamp((15.0 - cqi) / 11.0 * 100.0)
    if sinr is not None:
        components["sinr"] = _clamp((23.0 - sinr) / 23.0 * 100.0)
    if rsrq is not None:
        components["rsrq"] = _clamp((-3.0 - rsrq) / 15.0 * 100.0)

    series = signal_db.query_aggregates(hours=max(1, math.ceil(recent_minutes / 60)), bucket="minute")
    historical = [row.get("avg_sinr") for row in series[-recent_minutes:] if row.get("avg_sinr") is not None]
    baseline_sinr = _average(historical)
    if sinr is not None and baseline_sinr is not None:
        components["historical_drop"] = _clamp((baseline_sinr - sinr) * 12.0)

    weights = {"cqi": 0.38, "sinr": 0.27, "rsrq": 0.25, "historical_drop": 0.10}
    present = [(components[name], weights[name]) for name in components]
    index = sum(value * weight for value, weight in present) / sum(weight for _, weight in present) if present else 0.0

    if index >= 68:
        level, label = "high", "مرتفع"
    elif index >= 38:
        level, label = "medium", "متوسط"
    else:
        level, label = "low", "منخفض"

    reasons = []
    if cqi is not None:
        reasons.append(f"متوسط CQI الحالي {cqi:.1f}/15؛ كلما ارتفع كان استغلال القناة أفضل.")
    if sinr is not None:
        reasons.append(f"LTE SINR يساوي {sinr:.1f} dB؛ {'نظيف ومستقر' if sinr >= 15 else 'متأثر بالتشويش أو الحمل'}.")
    if rsrq is not None:
        reasons.append(f"LTE RSRQ يساوي {rsrq:.1f} dB؛ {'جودة ممتازة' if rsrq >= -9 else 'قد يدل على حمل أو تداخل'}.")
    if baseline_sinr is not None:
        reasons.append(f"متوسط SINR لآخر ساعتين {baseline_sinr:.1f} dB مقارنة بالقراءة الحالية.")

    field_confidence = min(1.0, len(components) / 4.0)
    sample_confidence = min(1.0, len(historical) / 60.0)
    confidence = round((field_confidence * 0.65 + sample_confidence * 0.35) * 100)

    return {
        "index": round(index, 1),
        "level": level,
        "label": label,
        "confidence": confidence,
        "components": {name: round(value, 1) for name, value in components.items()},
        "reasons": reasons,
        "history_samples": len(historical),
        "connected_users": {
            "available": False,
            "value": None,
            "message": "عدد المستخدمين المتصلين بالقطاع لا ترسله شبكة المشغل إلى المودم؛ الرقم متاح للمشغل فقط.",
        },
    }


def build_tower_profile(metrics):
    metrics = metrics or {}
    enodeb_id, sector_id = _parse_cell(metrics.get("cell_id"))
    return {
        "cell_id": metrics.get("cell_id", ""),
        "enodeb_id": enodeb_id,
        "sector_id": sector_id,
        "pci": metrics.get("pci", ""),
        "tac": metrics.get("tac", ""),
        "plmn": metrics.get("plmn", ""),
        "lte": {
            "band": metrics.get("band", ""),
            "earfcn": metrics.get("earfcn", ""),
            "dl_frequency": metrics.get("dlfrequency", ""),
            "ul_frequency": metrics.get("ulfrequency", ""),
            "bandwidth": metrics.get("dlbandwidth", ""),
            "rsrp": metrics.get("rsrp", ""),
            "rsrq": metrics.get("rsrq", ""),
            "sinr": metrics.get("sinr", ""),
            "cqi0": metrics.get("cqi0", ""),
            "cqi1": metrics.get("cqi1", ""),
            "dl_mcs": metrics.get("dl_mcs", ""),
            "ul_mcs": metrics.get("ul_mcs", ""),
            "rrc_status": metrics.get("rrc_status", ""),
        },
        "nr5g": {
            "pci": metrics.get("scc_pci", ""),
            "arfcn": metrics.get("nrearfcn", ""),
            "dl_frequency": metrics.get("nrdlfreq", ""),
            "ul_frequency": metrics.get("nrulfreq", ""),
            "bandwidth": metrics.get("nrdlbandwidth", ""),
            "rsrp": metrics.get("nrrsrp", ""),
            "rsrq": metrics.get("nrrsrq", ""),
            "sinr": metrics.get("nrsinr", ""),
        },
        "pressure": estimate_pressure(metrics),
    }
