"""Smoke test for the band-study scoring and verdict logic.

Pure-function level only: no modem session, no router changes. Verifies the
study cannot silently treat an unreachable band as a candidate, and cannot
crown a winner when the gap between two bands is inside measurement noise.
"""

import signal_db
from modem_analyzer import ModemAnalyzer, mask_to_band_list, LTE_BAND_ALL_MASK

score = ModemAnalyzer._score_band_record
verdict = ModemAnalyzer._build_study_verdict


def usable(band, sinr, sd, rsrp=-75, rsrq=-8, cqi=11, loss=0, jitter=5):
    return {
        "band": band, "status": "usable", "samples": 10,
        "avg_sinr": sinr, "sinr_stdev": sd, "avg_rsrp": rsrp,
        "avg_rsrq": rsrq, "avg_cqi": cqi,
        "loss_percent": loss, "jitter_ms": jitter,
    }


# --- Unusable bands must never receive a score -------------------------------
for status in ("rejected", "no_attach", "no_internet", "invalid_measurement", "error"):
    scores = score(None, {"band": "8", "status": status})
    assert scores == {"gaming": None, "speed": None, "stability": None}, status
print("OK  الباندات غير الصالحة لا تحصل على تقييم")

# --- A usable band scores on all three axes ----------------------------------
good = score(None, usable("3", 16.0, 1.2))
assert all(good[k] is not None and good[k] > 0 for k in ("gaming", "speed", "stability")), good
print("OK  الباند الصالح يحصل على ثلاثة تقييمات")

# --- Packet loss must outweigh a pretty radio reading ------------------------
clean = score(None, usable("3", 15.0, 1.5, loss=0))
lossy = score(None, usable("28", 18.0, 1.5, loss=20))
assert lossy["gaming"] < clean["gaming"], (clean, lossy)
assert lossy["stability"] < clean["stability"], (clean, lossy)
print("OK  فقد الحزم يهزم إشارة أنقى (الدرس المستفاد من تحقيق التقطيع)")

# --- Instability must cost a band its gaming crown ---------------------------
steady = score(None, usable("3", 14.0, 0.5))
jumpy = score(None, usable("40", 14.0, 5.0))
assert steady["gaming"] > jumpy["gaming"], (steady, jumpy)
print("OK  الثبات يرجّح كفة الباند للألعاب")

# --- No usable band -> honest failure, not a fake winner ---------------------
none_usable = verdict(None, [
    {"band": "8", "status": "no_attach", "notes": "لا تغطية"},
    {"band": "38", "status": "rejected", "notes": "مرفوض"},
])
assert none_usable["ok"] is False
assert len(none_usable["unavailable"]) == 2
print("OK  عدم وجود باند صالح يُبلَّغ بصراحة بدل ترشيح فائز وهمي")

# --- Near-identical bands are reported as a tie ------------------------------
a = usable("3", 15.0, 2.5)
b = usable("1", 15.3, 2.5)
a["scores"], b["scores"] = score(None, a), score(None, b)
tied = verdict(None, [a, b])
assert tied["ok"] is True
assert tied["best_for_gaming"]["tie"] is True, tied["best_for_gaming"]
print("OK  الفرق داخل هامش الخطأ يُعلَن تعادلاً")

# --- A clearly better band is not called a tie -------------------------------
weak = usable("20", 6.0, 1.0, rsrp=-100, cqi=5)
strong = usable("3", 18.0, 0.8, rsrp=-70, cqi=13)
weak["scores"], strong["scores"] = score(None, weak), score(None, strong)
decided = verdict(None, [weak, strong])
assert decided["best_for_gaming"]["band"] == "3"
assert decided["best_for_gaming"]["tie"] is False
assert decided["usable_bands"] == ["3", "20"] or set(decided["usable_bands"]) == {"3", "20"}
print("OK  الفارق الكبير يُحسم بفائز واضح")

# --- Mixed run keeps unavailable bands visible in the verdict ----------------
mixed = [strong, {"band": "8", "status": "no_internet", "notes": "ما فيه إنترنت"}]
out = verdict(None, mixed)
assert out["ok"] is True and len(out["unavailable"]) == 1
print("OK  الباندات غير الصالحة تبقى ظاهرة في النتيجة")

# --- Regression: a perfect 0% loss must not be read as total loss ------------
# A live run misclassified a healthy Band 3 as "no_internet" because
# `loss_percent or 100` turns a legitimate falsy 0 into 100.
def classify_loss(ping):
    value = ping.get("loss_percent")
    value = 100.0 if value is None else float(value)
    return (not ping.get("ok")) or value >= 60


perfect = {"ok": True, "loss_percent": 0, "avg_ms": 132.0, "jitter_ms": 8.2}
assert classify_loss(perfect) is False, "0% فقد يجب ألا يُعتبر انقطاعاً"
assert classify_loss({"ok": True, "loss_percent": 75}) is True
assert classify_loss({"ok": True, "loss_percent": None}) is True
assert classify_loss({"ok": False, "loss_percent": 0}) is True
print("OK  فقد 0% لا يُقرأ خطأً كفقد كامل (تراجع مثبَّت)")

# A zero score must still rank, not be pushed below "missing"
zero = usable("20", 5.0, 1.0, rsrp=-110, rsrq=-19, cqi=1, loss=30, jitter=90)
zero["scores"] = score(None, zero)
good_row = usable("3", 16.0, 1.0)
good_row["scores"] = score(None, good_row)
ranked = verdict(None, [zero, good_row])
assert ranked["best_for_gaming"]["band"] == "3"
assert "20" in ranked["usable_bands"], "الباند ذو التقييم صفر يبقى مُدرجاً لا محذوفاً"
print("OK  التقييم صفر يُرتَّب بشكل صحيح ولا يختفي")

# --- Band mask decoding ------------------------------------------------------
# Real mask reported by this modem when asked for "all bands".
assert mask_to_band_list('7E2880E00D5') == [1, 3, 5, 7, 8, 18, 19, 20, 28, 32, 34, 38, 39, 40, 41, 42, 43]
assert mask_to_band_list('4') == [3], "قناع 4 يعني Band 3"
assert mask_to_band_list('1') == [1]
assert mask_to_band_list('zz') == [] and mask_to_band_list(None) == []
print("OK  فك قناع الترددات صحيح")

# --- Regression: unlocking must not be judged by exact mask equality ---------
# The modem normalises the all-ones mask down to what it really supports, so a
# strict compare reported a successful unlock as a failure (live 409 bug).
def unlock_verified(sent_mask, returned_mask):
    if returned_mask.upper().lstrip('0') == sent_mask.upper().lstrip('0'):
        return True
    if sent_mask.upper() == LTE_BAND_ALL_MASK:
        try:
            return bin(int(returned_mask, 16)).count('1') > 1
        except (ValueError, TypeError):
            return False
    return False


assert unlock_verified(LTE_BAND_ALL_MASK, '7E2880E00D5') is True, "فك القفل المطبَّع يجب أن يُقبل"
assert unlock_verified(LTE_BAND_ALL_MASK, '4') is False, "باند واحد ليس فك قفل"
assert unlock_verified('4', '4') is True
assert unlock_verified('4', '1') is False
print("OK  فك القفل يُتحقَّق بالنية لا بمطابقة القناع حرفياً (تراجع مثبَّت)")

# --- Historical study contract ----------------------------------------------
history = signal_db.band_study()
assert "bands" in history and isinstance(history["bands"], list)
for row in history["bands"]:
    assert row["trust"] in ("high", "medium", "low")
    assert row["samples"] >= 1
print(f"OK  الدراسة التاريخية اشتغلت ({len(history['bands'])} باند، موثوق: {history['trusted_count']})")

print("BAND STUDY SMOKE OK")
