import json
import os
import re
import shlex
import time
import threading
import subprocess
from datetime import datetime

DOCTOR_LOG_FILE = "doctor_log.json"
DOCTOR_CONFIG_FILE = "doctor_config.json"

def read_doctor_config():
    try:
        if os.path.exists(DOCTOR_CONFIG_FILE):
            with open(DOCTOR_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"auto": True, "cooldown_minutes": 30}

def write_doctor_config(config):
    try:
        with open(DOCTOR_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False

def read_doctor_log():
    try:
        if os.path.exists(DOCTOR_LOG_FILE):
            with open(DOCTOR_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def write_doctor_log(logs):
    try:
        with open(DOCTOR_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False

def build_dossier(metrics, context, congestion, extra):
    metrics = metrics or {}
    context = context or {}
    congestion = congestion or {}
    extra = extra or {}
    
    allowed_keys = [
        "rsrp", "rsrq", "sinr", "rssi", "cell_id", "pci", "band", "earfcn",
        "cqi0", "cqi1", "nrrsrp", "nrsinr", "nrrsrq", "nrdlbandwidth", "plmn",
        "tac", "nei_cellid", "mode", "rrc_status"
    ]
    dossier_metrics = {k: metrics[k] for k in allowed_keys if k in metrics}
    
    return {
        "trigger": extra.get("trigger", "unknown"),
        "timestamp": datetime.now().isoformat(),
        "metrics": dossier_metrics,
        "ai_context": context,
        "congestion": congestion,
        "extra": {
            "alerts": extra.get("alerts", []),
            "lock_profile": extra.get("lock_profile"),
            "blocked_cells": extra.get("blocked_cells", []),
            "latest_scan": extra.get("latest_scan")
        }
    }

class AiDoctor:
    def __init__(self, on_result=None):
        self.lock = threading.Lock()
        self.on_result = on_result
        self.state = {
            "running": False,
            "last_run_at": 0,
            "last_trigger": "",
            "last_result": None,
            "error": None,
            "raw_text": ""
        }
        self.cooldowns = {}

    def get_status(self):
        config = read_doctor_config()
        logs = read_doctor_log()
        with self.lock:
            state_copy = self.state.copy()
        return {
            "state": state_copy,
            "config": config,
            "logs": logs[-10:]
        }

    def diagnose_async(self, dossier, trigger_type, force=False):
        config = read_doctor_config()
        cooldown_mins = config.get("cooldown_minutes", 30)
        now = time.time()
        
        with self.lock:
            if self.state["running"]:
                return False
                
            if not force and trigger_type in self.cooldowns:
                elapsed = now - self.cooldowns[trigger_type]
                if elapsed < cooldown_mins * 60:
                    return False
                    
            self.state["running"] = True
            self.state["last_run_at"] = now
            self.state["last_trigger"] = trigger_type
            self.state["error"] = None
            self.state["last_result"] = None
            self.state["raw_text"] = ""
            
            self.cooldowns[trigger_type] = now
            
        t = threading.Thread(target=self._run_diagnose, args=(dossier, trigger_type), daemon=True)
        t.start()
        return True

    def _run_diagnose(self, dossier, trigger_type):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        dossier_path = os.path.join(project_dir, "doctor_dossier.json")
        
        try:
            with open(dossier_path, "w", encoding="utf-8") as f:
                json.dump(dossier, f, ensure_ascii=False, indent=4)
        except Exception as e:
            with self.lock:
                self.state["running"] = False
                self.state["error"] = f"فشل كتابة dossier: {str(e)}"
            return

        prompt = 'أنت مهندس شبكات خلوية خبير. اقرأ الملف doctor_dossier.json في مجلد المشروع الحالي وشخّص مشكلة الشبكة: حدد السبب الجذري الأرجح، درجة الخطورة، وخطة من 3 خطوات مرتبة بالأثر. لا تعدّل أي ملف. اختم إجابتك بكتلة JSON واحدة بهذا الشكل بالضبط: {"root_cause": "...", "severity": "low|medium|high", "actions": ["...", "...", "..."], "suggested_band": "رقم باند أو فارغ"}'
        # أي CLI لنموذج ذكاء اصطناعي يصلح هنا: يستقبل البرومت كوسيطة أخيرة
        # ويطبع الإجابة على stdout. يُضبط الأمر عبر متغير البيئة AI_DOCTOR_CLI.
        cli = os.getenv("AI_DOCTOR_CLI", "").strip()
        if not cli:
            with self.lock:
                self.state["running"] = False
                self.state["error"] = "AI_DOCTOR_CLI غير مضبوط — ضع في .env أمر CLI لنموذج ذكاء اصطناعي (يستقبل البرومت كوسيطة ويطبع الرد)."
            return
        cmd = shlex.split(cli) + [prompt]
        
        error_msg = None
        structured = None
        raw_text = ""
        
        try:
            res = subprocess.run(
                cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=240
            )
            
            stdout = res.stdout
            stderr = res.stderr
            
            if res.returncode != 0:
                error_msg = f"فشل تشغيل أداة الذكاء الاصطناعي برمز خطأ {res.returncode}. تأكد من صحة AI_DOCTOR_CLI."
            elif "auth_required" in stdout.lower() or "not logged" in stdout.lower() or "auth_required" in stderr.lower():
                error_msg = "فشل المصادقة في أداة الذكاء الاصطناعي — سجّل الدخول إليها ثم أعد المحاولة."
            else:
                # مخرجات --json تكون مهرَّبة (\" و \n) — فكّ التهريب أولاً حتى يلتقط الـ regex كتلة التشخيص
                unescaped = stdout.replace('\\n', '\n').replace('\\"', '"')
                matches = list(re.finditer(r'\{\s*"root_cause".*?\}', unescaped, re.DOTALL))
                if matches:
                    last_match_str = matches[-1].group(0)
                    try:
                        structured = json.loads(last_match_str)
                    except Exception:
                        pass

                summary_marker = "summary:"
                summary_idx = unescaped.lower().find(summary_marker)
                if summary_idx != -1:
                    raw_text = unescaped[summary_idx + len(summary_marker):].strip()
                else:
                    raw_text = unescaped[-2000:] if len(unescaped) > 2000 else unescaped
                    
        except subprocess.TimeoutExpired:
            error_msg = "انتهت مهلة التشخيص (240 ثانية) دون استجابة."
        except Exception as e:
            error_msg = f"حدث خطأ أثناء تشغيل التشخيص: {str(e)}"
            
        metrics = dossier.get("metrics", {})
        congestion = dossier.get("congestion", {})
        dossier_summary = {
            "rsrp": metrics.get("rsrp"),
            "sinr": metrics.get("sinr"),
            "cell_id": metrics.get("cell_id"),
            "band": metrics.get("band"),
            "congestion_level": congestion.get("level")
        }
        
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger_type,
            "structured": structured,
            "raw_text": raw_text,
            "error": error_msg,
            "dossier_summary": dossier_summary
        }
        
        logs = read_doctor_log()
        logs.append(log_entry)
        logs = logs[-50:]
        write_doctor_log(logs)
        
        with self.lock:
            self.state["running"] = False
            self.state["last_result"] = structured
            self.state["error"] = error_msg
            self.state["raw_text"] = raw_text

        if self.on_result:
            try:
                self.on_result(log_entry)
            except Exception:
                pass
