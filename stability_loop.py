import json
import os
import time
import uuid
import threading
from datetime import datetime

STATE_FILE = "stability_state.json"
state_lock = threading.Lock()

def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                state.setdefault("events", [])
                return state
        except Exception:
            pass
    return {"events": []}

def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def record_event(event_type, before_metrics, prediction=None, suggestion=None):
    with state_lock:
        state = _load_state()
        
        # Clean metrics to keep essential parameters
        cleaned_metrics = {
            "rsrp": before_metrics.get("rsrp"),
            "rsrq": before_metrics.get("rsrq"),
            "sinr": before_metrics.get("sinr"),
            "rssi": before_metrics.get("rssi"),
            "cell_id": before_metrics.get("cell_id"),
            "pci": before_metrics.get("pci"),
            "band": before_metrics.get("band"),
            "earfcn": before_metrics.get("earfcn"),
            "nrrsrp": before_metrics.get("nrrsrp"),
            "nrrsrq": before_metrics.get("nrrsrq"),
            "nrsinr": before_metrics.get("nrsinr")
        }
        
        event_id = f"ev_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        event = {
            "id": event_id,
            "timestamp": time.time(),
            "iso_timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "before_metrics": cleaned_metrics,
            "prediction": prediction,
            "suggestion": suggestion,
            "applied": False,
            "applied_at": None,
            "action_detail": None,
            "outcome": None,
            "evaluated_at": None,
            "after_metrics": None
        }
        
        state["events"].append(event)
        
        # Trim log to last 100 entries
        if len(state["events"]) > 100:
            state["events"] = state["events"][-100:]
            
        _save_state(state)
        return event_id

def safe_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        if v is not None:
            import re
            match = re.search(r"-?\d+(?:\.\d+)?", str(v))
            if match:
                return float(match.group(0))
    return None

def suggest_action(metrics, band_history, prediction):
    current_band = str(metrics.get("band", ""))
    
    # Check if degrading
    is_degrading = False
    if prediction and prediction.get("degrading"):
        is_degrading = True
    else:
        # Fallback check on current SINR
        sinr = safe_float(metrics.get("sinr"))
        if sinr is not None and sinr < 3.0:
            is_degrading = True
            
    if not is_degrading:
        return {
            "action": "hold",
            "band": current_band,
            "reason": "Signal is stable and not degrading.",
            "confidence": 0.9
        }
        
    # Find best alternative from band history
    candidates = []
    for key, entry in band_history.get("cells", {}).items():
        b = str(entry.get("band", ""))
        if b and b != current_band:
            candidates.append(entry)
            
    if not candidates:
        return {
            "action": "hold",
            "band": current_band,
            "reason": "Signal is degrading but no alternative bands found in history.",
            "confidence": 0.2
        }
        
    # Sort candidates by best SINR and best RSRP
    def get_rank_key(entry):
        rsrp_val = safe_float(entry.get("best_rsrp")) or -999.0
        sinr_val = safe_float(entry.get("best_sinr")) or -999.0
        return (sinr_val, rsrp_val)
        
    candidates.sort(key=get_rank_key, reverse=True)
    best_cand = candidates[0]
    target_band = best_cand["band"]
    
    reason = f"Current band {current_band} is degrading. Suggested alternative is Band {target_band} based on historical best metrics (SINR: {best_cand.get('best_sinr')}dB, RSRP: {best_cand.get('best_rsrp')}dBm)."
    
    return {
        "action": "switch",
        "band": target_band,
        "reason": reason,
        "confidence": 0.85
    }

def mark_action_applied(event_id, action_detail):
    with state_lock:
        state = _load_state()
        updated = False
        for ev in state["events"]:
            if ev["id"] == event_id:
                ev["applied"] = True
                ev["applied_at"] = time.time()
                ev["action_detail"] = action_detail
                updated = True
                break
        if updated:
            _save_state(state)
        return updated

def evaluate_pending(current_metrics, min_age_sec=300, max_age_sec=900):
    with state_lock:
        state = _load_state()
        now = time.time()
        updated = False
        
        for ev in state["events"]:
            # Evaluate if applied, not yet evaluated, and has matured
            if ev.get("applied") and ev.get("outcome") is None:
                applied_at = ev.get("applied_at") or ev.get("timestamp")
                age = now - applied_at
                
                if age >= min_age_sec:
                    # Perform comparison
                    before = ev["before_metrics"]
                    
                    before_sinr = safe_float(before.get("sinr"))
                    current_sinr = safe_float(current_metrics.get("sinr"))
                    before_rsrp = safe_float(before.get("rsrp"))
                    current_rsrp = safe_float(current_metrics.get("rsrp"))
                    
                    # Fallback to NR if LTE is missing
                    before_nrsinr = safe_float(before.get("nrsinr"))
                    current_nrsinr = safe_float(current_metrics.get("nrsinr"))
                    if before_sinr is None: before_sinr = before_nrsinr
                    if current_sinr is None: current_sinr = current_nrsinr
                    
                    outcome = "same"
                    if before_sinr is not None and current_sinr is not None:
                        diff_sinr = current_sinr - before_sinr
                        if diff_sinr >= 2.0:
                            outcome = "improved"
                        elif diff_sinr <= -2.0:
                            outcome = "worse"
                        else:
                            if before_rsrp is not None and current_rsrp is not None:
                                diff_rsrp = current_rsrp - before_rsrp
                                if diff_rsrp >= 5.0:
                                    outcome = "improved"
                                elif diff_rsrp <= -5.0:
                                    outcome = "worse"
                                    
                    ev["outcome"] = outcome
                    ev["evaluated_at"] = datetime.now().isoformat()
                    ev["after_metrics"] = {
                        "rsrp": current_rsrp,
                        "sinr": current_sinr,
                        "band": current_metrics.get("band"),
                        "cell_id": current_metrics.get("cell_id")
                    }
                    updated = True
                    
        if updated:
            _save_state(state)
        return updated

def get_status():
    with state_lock:
        state = _load_state()
        
    events = state.get("events", [])
    
    pending = [ev for ev in events if ev.get("applied") and ev.get("outcome") is None]
    outcomes = [ev for ev in events if ev.get("outcome") is not None]
    
    # Sort outcomes to show latest first
    outcomes.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    
    last_decision = None
    if events:
        # Latest event in the list
        last_decision = events[-1]
        
    return {
        "last_decision": last_decision,
        "pending": pending,
        "outcomes": outcomes[:20]
    }

def trim_log(max_entries=100):
    with state_lock:
        state = _load_state()
        if len(state["events"]) > max_entries:
            state["events"] = state["events"][-max_entries:]
            _save_state(state)
