import os
import sys
import math
from datetime import datetime, timedelta

def run_test():
    import signal_db
    import signal_predictor
    
    test_db = "test_pred.db"
    test_model = "test_model.pkl"
    test_state = "test_state.json"
    
    # 1. Override paths
    signal_db.DB_FILE = test_db
    signal_predictor.MODEL_FILE = test_model
    signal_predictor.PREDICTOR_STATE_FILE = test_state
    
    # Cleanup previous runs
    for f in [test_db, test_db + "-wal", test_db + "-shm", test_model, test_state]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
                
    try:
        # 2. Init database
        signal_db.init_db()
        
        # 3. Insert ~600 consecutive readings directly
        conn = signal_db.get_connection()
        try:
            cursor = conn.cursor()
            start_dt = datetime.now() - timedelta(minutes=650)
            
            rows = []
            for k in range(650):
                ts = (start_dt + timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S")
                # Create a simple sine wave for SINR and RSRP values
                lte_rsrp = float(-75.0 + 5.0 * math.sin(k * 0.05))
                lte_rsrq = -12.0
                lte_sinr = float(15.0 + 4.0 * math.sin(k * 0.05))
                lte_rssi = "-60dBm"
                cell_id = "12345"
                pci = "342"
                band = "3"
                earfcn = "1650"
                
                rows.append((
                    ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                    cell_id, pci, band, earfcn,
                    None, None, None, None, None,
                    "Connected"
                ))
                
            cursor.executemany("""
                INSERT INTO signal_log (
                    ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                    cell_id, pci, band, earfcn,
                    nr_rsrp, nr_rsrq, nr_sinr, nr_bandwidth, nr_freq,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
        finally:
            conn.close()
            
        # 4. Check build_training_set
        X, y, count = signal_predictor.build_training_set(hours=72)
        if count < 200:
            print(f"FAIL: build_training_set returned only {count} rows (expected > 200)")
            sys.exit(1)
            
        # Verify features dimension
        if len(X[0]) != 15:
            print(f"FAIL: Feature vector length is {len(X[0])} instead of 15")
            sys.exit(1)
            
        # 5. Check model training
        predictor = signal_predictor.SignalPredictor()
        success = predictor.train(hours=72)
        if not success:
            print(f"FAIL: predictor.train returned False: {predictor.state['error']}")
            sys.exit(1)
            
        if not predictor.state["trained"] or predictor.state["train_mae"] is None:
            print("FAIL: Predictor state shows not trained or missing MAE")
            sys.exit(1)
            
        # 6. Check predict_now
        pred = predictor.predict_now()
        if not pred.get("ok"):
            print(f"FAIL: predictor.predict_now returned not ok: {pred.get('reason')}")
            sys.exit(1)
            
        predicted = pred.get("predicted_sinr_3min")
        current = pred.get("current_sinr")
        if not isinstance(predicted, float) or not isinstance(current, float):
            print(f"FAIL: Prediction values are not floats: predicted={predicted}, current={current}")
            sys.exit(1)
            
        print("SMOKE PRED OK")
        sys.exit(0)
        
    except Exception as e:
        print(f"FAIL: Raised exception {str(e)}")
        sys.exit(1)
        
    finally:
        # Cleanup WAL files as well
        for f in [test_db, test_db + "-wal", test_db + "-shm", test_model, test_state]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

if __name__ == "__main__":
    run_test()
