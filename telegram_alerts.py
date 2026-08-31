import json
import os
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime

TELEGRAM_CONFIG_FILE = "telegram_config.json"

def read_telegram_config():
    try:
        if os.path.exists(TELEGRAM_CONFIG_FILE):
            with open(TELEGRAM_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "cooldown_minutes": 15,
        "daily_report_hour": 9
    }

def write_telegram_config(config):
    try:
        with open(TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False

class TelegramNotifier:
    def __init__(self):
        self.lock = threading.Lock()
        self.cooldowns = {}
        self.stats = {
            "sent": 0,
            "failed": 0,
            "last_sent_at": None,
            "last_error": None
        }

    def _send_raw(self, text):
        config = read_telegram_config()
        enabled = config.get("enabled", False)
        token = config.get("bot_token", "").strip()
        chat_id = config.get("chat_id", "").strip()

        if not enabled or not token or not chat_id:
            res = {"ok": False, "error": "غير مفعّل أو الإعدادات ناقصة"}
            with self.lock:
                self.stats["failed"] += 1
                self.stats["last_error"] = res["error"]
            return res

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": "LTESignalDoctor/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                if resp_data.get("ok"):
                    with self.lock:
                        self.stats["sent"] += 1
                        self.stats["last_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    return {"ok": True}
                else:
                    err = resp_data.get("description", "خطأ غير معروف من تليجرام")
                    with self.lock:
                        self.stats["failed"] += 1
                        self.stats["last_error"] = err
                    return {"ok": False, "error": err}
        except Exception as e:
            err_msg = str(e)
            with self.lock:
                self.stats["failed"] += 1
                self.stats["last_error"] = err_msg
            return {"ok": False, "error": err_msg}

    def notify(self, alert_type, text, force=False):
        config = read_telegram_config()
        if not config.get("enabled", False):
            return False

        cooldown_mins = config.get("cooldown_minutes", 15)
        now = time.time()

        with self.lock:
            if not force and alert_type in self.cooldowns:
                elapsed = now - self.cooldowns[alert_type]
                if elapsed < cooldown_mins * 60:
                    return False
            self.cooldowns[alert_type] = now

        t = threading.Thread(target=self._send_raw, args=(text,), daemon=True)
        t.start()
        return True

    def test_send(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"⚙️ <b>رسالة اختبار تليجرام</b>\nتم إرسال الرسالة بنجاح في: <code>{ts}</code>"
        return self._send_raw(text)

    def get_status(self):
        config = read_telegram_config()
        token = config.get("bot_token", "").strip()
        masked_token = ""
        if token:
            masked_token = token[:6] + "..." if len(token) > 6 else token + "..."
        
        config_masked = config.copy()
        config_masked["bot_token"] = masked_token

        with self.lock:
            stats_copy = self.stats.copy()

        return {
            "config": config_masked,
            "stats": stats_copy
        }

def build_alert_message(alert_type, payload):
    if not payload:
        payload = {}
        
    metrics = payload.get("metrics", {})
    cell_id = metrics.get("cell_id", "N/A")
    band = metrics.get("band", "N/A")
    rsrp = metrics.get("rsrp", "N/A")
    sinr = metrics.get("sinr", "N/A")

    if alert_type == "signal_collapse":
        return (
            f"🚨 <b>انهيار إشارة الشبكة</b>\n\n"
            f"الخلية (Cell ID): <code>{cell_id}</code>\n"
            f"التردد (Band): <code>{band}</code>\n"
            f"مستوى RSRP: <code>{rsrp}</code>\n"
            f"مستوى SINR: <code>{sinr}</code>"
        )
    elif alert_type == "congestion_high":
        reasons = payload.get("reasons", [])
        reasons_text = "\n".join([f"- {r}" for r in reasons]) if reasons else "- غير محدد"
        return (
            f"🏗️ <b>ازدحام برج عالي</b>\n\n"
            f"الخلية (Cell ID): <code>{cell_id}</code>\n"
            f"التردد (Band): <code>{band}</code>\n"
            f"الأسباب:\n<code>{reasons_text}</code>"
        )
    elif alert_type == "auto_revert":
        return (
            f"↩️ <b>تراجع تلقائي للباند</b>\n\n"
            f"تم التراجع التلقائي للتردد السابق بعد تأكيد انقطاع الاتصال بالإنترنت على التردد الجديد."
        )
    elif alert_type == "doctor_diagnosis":
        structured = payload.get("structured")
        error_msg = payload.get("error")
        
        if error_msg:
            return (
                f"🩺 <b>فشل تشخيص طبيب الشبكة</b>\n\n"
                f"الخطأ: <code>{error_msg}</code>"
            )
            
        if structured and structured.get("root_cause"):
            root = structured.get("root_cause", "")
            sev = structured.get("severity", "")
            actions = structured.get("actions", [])
            s_band = structured.get("suggested_band", "")
            
            sev_ar = {"low": "منخفضة", "medium": "متوسطة", "high": "عالية"}.get(sev, sev)
            actions_text = "\n".join([f"{i+1}. {a}" for i, a in enumerate(actions)])
            s_band_text = f"\nالباند المقترح: <code>{s_band}</code>" if s_band else ""
            
            return (
                f"🩺 <b>تشخيص طبيب الشبكة الذكي</b>\n\n"
                f"السبب الجذري: <b>{root}</b>\n"
                f"الخطورة: <b>{sev_ar}</b>\n\n"
                f"خطة العمل المقترحة:\n{actions_text}"
                f"{s_band_text}"
            )
        else:
            raw = payload.get("raw_text", "")
            snippet = raw[:400] + "..." if len(raw) > 400 else raw
            return (
                f"🩺 <b>تشخيص طبيب الشبكة (نص خام)</b>\n\n"
                f"<code>{snippet}</code>"
            )
    elif alert_type == "daily_report":
        samples = payload.get("samples", 0)
        avg_rsrp = payload.get("avg_rsrp", "N/A")
        avg_sinr = payload.get("avg_sinr", "N/A")
        worst_hour = payload.get("worst_hour", "N/A")
        doctor_runs = payload.get("doctor_runs", 0)
        
        worst_text = f"{worst_hour}:00" if isinstance(worst_hour, int) else worst_hour
        
        outcomes_text = ""
        imp = payload.get("outcomes_improved")
        wse = payload.get("outcomes_worse")
        same = payload.get("outcomes_same")
        if imp is not None or wse is not None:
            outcomes_text = (
                f"\n\n♻️ <b>نتائج حلقة الاستقرار</b>:\n"
                f"تحسينات ناجحة (Improved): <code>{imp or 0}</code>\n"
                f"تراجعات أداء (Worse): <code>{wse or 0}</code>\n"
                f"دون تغيير (Same): <code>{same or 0}</code>"
            )
        
        return (
            f"📊 <b>التقرير اليومي لأداء الشبكة</b>\n\n"
            f"عدد القراءات (آخر 24 ساعة): <code>{samples}</code>\n"
            f"متوسط LTE RSRP: <code>{avg_rsrp} dBm</code>\n"
            f"متوسط LTE SINR: <code>{avg_sinr} dB</code>\n"
            f"أسوأ ساعة أداءً: <code>{worst_text}</code>\n"
            f"عدد تشخيصات الطبيب: <code>{doctor_runs}</code>"
            f"{outcomes_text}"
        )
    elif alert_type == "predicted_degradation":
        current_sinr = payload.get("current_sinr", "N/A")
        predicted_sinr = payload.get("predicted_sinr", "N/A")
        delta = payload.get("delta", "N/A")
        suggestion = payload.get("suggestion")
        
        sugg_text = ""
        if suggestion and isinstance(suggestion, dict):
            action = suggestion.get("action")
            band = suggestion.get("band")
            reason = suggestion.get("reason")
            if action == "switch" and band:
                sugg_text = f"\n💡 <b>الإجراء المقترح للاستقرار</b>: التبديل إلى التردد (Band): <code>{band}</code>\nالسبب: <i>{reason}</i>\n"
            else:
                sugg_text = f"\n💡 <b>الإجراء المقترح للاستقرار</b>: الإبقاء على التردد الحالي ({suggestion.get('reason', 'مستقر')})\n"
                
        return (
            f"⚠️🔮 <b>تنبؤ بتدهور الإشارة خلال 3 دقائق</b>\n\n"
            f"مستوى SINR الحالي: <code>{current_sinr} dB</code>\n"
            f"المتوقع بعد 3 دقائق: <code>{predicted_sinr} dB</code>\n"
            f"الهبوط المتوقع: <code>{delta} dB</code>\n"
            f"{sugg_text}\n"
            f"💡 <i>نصيحة: يرجى تجنب تشغيل الألعاب أو التحميلات الثقيلة حالياً، أو تفعيل تثبيت التردد البديل إذا استمر التدهور.</i>"
        )
    return "تنبيه شبكة غير معروف"
