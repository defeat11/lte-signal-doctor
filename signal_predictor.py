import json
import os
import time
import threading
import pickle
import math
from datetime import datetime, timedelta
import signal_db

try:
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error
    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False

MODEL_FILE = "predictor_model.pkl"
PREDICTOR_STATE_FILE = "predictor_state.json"

def diff_minutes(ts1, ts2):
    dt1 = datetime.strptime(ts1, "%Y-%m-%d %H:%M")
    dt2 = datetime.strptime(ts2, "%Y-%m-%d %H:%M")
    return int((dt2 - dt1).total_seconds() / 60)

def _load_series(hours=72):
    return [x for x in signal_db.query_aggregates(hours=hours, bucket="minute") if x.get("avg_sinr") is not None]

def _make_features(series, i):
    # Features from indices i-5 to i-1
    sinr_vals = [series[idx]["avg_sinr"] for idx in range(i-5, i-1 + 1)]
    rsrp_vals = [series[idx]["avg_rsrp"] for idx in range(i-5, i-1 + 1)]
    
    sinr_slope = sinr_vals[-1] - sinr_vals[0]
    rsrp_slope = rsrp_vals[-1] - rsrp_vals[0]
    
    dt = datetime.strptime(series[i-1]["bucket_ts"], "%Y-%m-%d %H:%M")
    hour = dt.hour
    sin_hour = math.sin(hour * 2 * math.pi / 24)
    cos_hour = math.cos(hour * 2 * math.pi / 24)
    weekday = dt.weekday()
    
    return sinr_vals + rsrp_vals + [sinr_slope, rsrp_slope, sin_hour, cos_hour, weekday]

def build_training_set(hours=72):
    series = _load_series(hours=hours)
    X = []
    y = []
    
    for i in range(5, len(series)):
        # Check contiguity of window (indices i-5 to i-1)
        is_contiguous = True
        for idx in range(i-5, i-1):
            if diff_minutes(series[idx]["bucket_ts"], series[idx+1]["bucket_ts"]) != 1:
                is_contiguous = False
                break
        if not is_contiguous:
            continue
            
        # Find target at t + 3 minutes
        target_val = None
        for k in range(i, min(i + 10, len(series))):
            if diff_minutes(series[i-1]["bucket_ts"], series[k]["bucket_ts"]) == 3:
                target_val = series[k]["avg_sinr"]
                break
                
        if target_val is not None:
            features = _make_features(series, i)
            X.append(features)
            y.append(target_val)
            
    return X, y, len(X)

class SignalPredictor:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = None
        self.state = {
            "trained": False,
            "last_train_at": None,
            "train_samples": 0,
            "train_mae": None,
            "last_prediction": None,
            "error": None
        }
        if not SKLEARN_AVAILABLE:
            self.state["error"] = "scikit-learn غير مثبتة"

    def _save_state(self):
        try:
            with open(PREDICTOR_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def _load_state(self):
        try:
            if os.path.exists(PREDICTOR_STATE_FILE):
                with open(PREDICTOR_STATE_FILE, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
        except Exception:
            pass

    def load_model(self):
        self._load_state()
        if os.path.exists(MODEL_FILE):
            try:
                with open(MODEL_FILE, "rb") as f:
                    self.model = pickle.load(f)
                with self.lock:
                    self.state["trained"] = True
            except Exception as e:
                with self.lock:
                    self.state["error"] = f"فشل تحميل النموذج: {str(e)}"
                    self._save_state()

    def train(self, hours=72):
        if not SKLEARN_AVAILABLE:
            with self.lock:
                self.state["error"] = "scikit-learn غير مثبتة"
                self._save_state()
            return False
            
        try:
            X, y, count = build_training_set(hours=hours)
            if count < 200:
                with self.lock:
                    self.state["error"] = "بيانات غير كافية بعد (العينات المتاحة: {})".format(count)
                    self._save_state()
                return False
                
            X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.15, random_state=42)
            
            model = GradientBoostingRegressor(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42)
            model.fit(X_train, y_train)
            
            preds = model.predict(X_val)
            mae = mean_absolute_error(y_val, preds)
            
            with open(MODEL_FILE, "wb") as f:
                pickle.dump(model, f)
                
            with self.lock:
                self.model = model
                self.state["trained"] = True
                self.state["last_train_at"] = datetime.now().isoformat()
                self.state["train_samples"] = count
                self.state["train_mae"] = round(float(mae), 3)
                self.state["error"] = None
                self._save_state()
            return True
        except Exception as e:
            with self.lock:
                self.state["error"] = f"فشل التدريب: {str(e)}"
                self._save_state()
            return False

    def predict_now(self):
        if not SKLEARN_AVAILABLE:
            return {"ok": False, "reason": "scikit-learn غير مثبتة"}
            
        if not self.model:
            return {"ok": False, "reason": "النموذج غير مدرّب بعد أو غير متوفر"}
            
        try:
            series = _load_series(hours=1)
            if len(series) < 5:
                return {"ok": False, "reason": f"البيانات غير كافية لعمل التنبؤ (يتطلب 5 دقائق متتالية، المتوفر: {len(series)})"}
                
            last_5 = series[-5:]
            for idx in range(4):
                if diff_minutes(last_5[idx]["bucket_ts"], last_5[idx+1]["bucket_ts"]) != 1:
                    return {"ok": False, "reason": "فجوة زمنية في آخر 5 دقائق من قياسات الشبكة"}
                    
            features = _make_features(last_5, 5)
            
            pred_val = self.model.predict([features])[0]
            current_sinr = last_5[-1]["avg_sinr"]
            predicted_sinr = round(float(pred_val), 2)
            delta = round(predicted_sinr - current_sinr, 2)
            
            degrading = (delta <= -3.0) or (predicted_sinr < 5.0)
            
            res = {
                "ok": True,
                "predicted_sinr_3min": predicted_sinr,
                "current_sinr": current_sinr,
                "delta": delta,
                "degrading": degrading,
                "at": datetime.now().isoformat()
            }
            with self.lock:
                self.state["last_prediction"] = res
                self._save_state()
            return res
        except Exception as e:
            return {"ok": False, "reason": f"خطأ أثناء التنبؤ: {str(e)}"}

    def retrain_loop_start(self):
        if not SKLEARN_AVAILABLE:
            return
            
        def loop():
            needs_immediate = False
            if not self.model:
                needs_immediate = True
            else:
                last_train = self.state.get("last_train_at")
                if last_train:
                    try:
                        last_dt = datetime.fromisoformat(last_train)
                        if datetime.now() - last_dt > timedelta(days=1):
                            needs_immediate = True
                    except Exception:
                        needs_immediate = True
                else:
                    needs_immediate = True
                    
            if needs_immediate:
                self.train()
                
            while True:
                time.sleep(24 * 3600)
                self.train()
                
        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def get_status(self):
        with self.lock:
            state_copy = self.state.copy()
        return {
            "model_loaded": self.model is not None,
            "sklearn_available": SKLEARN_AVAILABLE,
            "state": state_copy
        }
