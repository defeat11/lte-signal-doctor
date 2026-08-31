import os
import sys
import datetime

def run_test():
    import stability_loop
    import signal_db
    
    # Force isolated state files
    test_state_file = "test_stability_state.json"
    stability_loop.STATE_FILE = test_state_file
    
    test_db = "test_stability.db"
    signal_db.DB_FILE = test_db
    
    # Cleanup previous test files if they exist
    for f in (test_state_file, test_db, test_db + "-wal", test_db + "-shm"):
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
                
    try:
        # 1. Test database setup and old data purging
        signal_db.init_db()
        conn = signal_db.get_connection()
        cursor = conn.cursor()
        
        # Insert historical and recent readings
        cursor.execute("INSERT INTO signal_log (ts, lte_rsrp, lte_sinr, status) VALUES ('2020-01-01 12:00:00', -80, 10, 'old')")
        cursor.execute("INSERT INTO signal_log (ts, lte_rsrp, lte_sinr, status) VALUES ('2020-01-02 12:00:00', -85, 8, 'old')")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO signal_log (ts, lte_rsrp, lte_sinr, status) VALUES (?, -70, 15, 'recent')", (now_str,))
        conn.commit()
        conn.close()
        
        # Test dry_run purge (should delete 0 rows)
        res = signal_db.purge_old_data(days=30, dry_run=True)
        if res["deleted"] != 0:
            print(f"FAIL: purge_old_data dry_run deleted {res['deleted']} instead of 0")
            sys.exit(1)
            
        # Test actual purge (should delete 2 rows)
        res_act = signal_db.purge_old_data(days=30, dry_run=False)
        if res_act["deleted"] != 2:
            print(f"FAIL: purge_old_data deleted {res_act['deleted']} instead of 2")
            sys.exit(1)
        if res_act["remaining"] != 1:
            print(f"FAIL: remaining is {res_act['remaining']} instead of 1")
            sys.exit(1)
            
        # 2. Test record_event
        fake_metrics = {"rsrp": -75, "sinr": 12, "band": "1", "cell_id": "9999"}
        pred = {"ok": True, "degrading": True}
        sugg = {"action": "switch", "band": "3", "reason": "Better signal", "confidence": 0.8}
        
        ev_id = stability_loop.record_event("predicted_degradation", fake_metrics, prediction=pred, suggestion=sugg)
        if not ev_id:
            print("FAIL: record_event did not return an event ID")
            sys.exit(1)
            
        # 3. Test get_status
        status = stability_loop.get_status()
        if status["last_decision"]["id"] != ev_id:
            print("FAIL: last_decision ID mismatch")
            sys.exit(1)
        if len(status["pending"]) != 0:
            print("FAIL: pending should be 0 since action is not yet marked applied")
            sys.exit(1)
            
        # 4. Test mark_action_applied
        ok = stability_loop.mark_action_applied(ev_id, "Switched to Band 3")
        if not ok:
            print("FAIL: mark_action_applied returned False")
            sys.exit(1)
            
        status2 = stability_loop.get_status()
        if len(status2["pending"]) != 1:
            print("FAIL: pending count is not 1 after marking as applied")
            sys.exit(1)
            
        # 5. Test suggest_action
        band_hist = {
            "cells": {
                "3|9998|342": {
                    "band": "3",
                    "best_rsrp": -65.0,
                    "best_sinr": 18.0
                }
            }
        }
        sugg_res = stability_loop.suggest_action(fake_metrics, band_hist, pred)
        if sugg_res["action"] != "switch" or sugg_res["band"] != "3":
            print(f"FAIL: suggest_action returned wrong suggestion: {sugg_res}")
            sys.exit(1)
            
        # 6. Test evaluate_pending (mock maturation)
        state = stability_loop._load_state()
        for ev in state["events"]:
            if ev["id"] == ev_id:
                ev["applied_at"] = ev["applied_at"] - 400
        stability_loop._save_state(state)
        
        current_metrics = {"rsrp": -62, "sinr": 17, "band": "3", "cell_id": "9998"}
        updated = stability_loop.evaluate_pending(current_metrics, min_age_sec=300)
        if not updated:
            print("FAIL: evaluate_pending did not evaluate the event")
            sys.exit(1)
            
        status3 = stability_loop.get_status()
        if len(status3["pending"]) != 0:
            print("FAIL: pending is not 0 after evaluation")
            sys.exit(1)
        if len(status3["outcomes"]) != 1:
            print("FAIL: outcomes count is not 1 after evaluation")
            sys.exit(1)
        if status3["outcomes"][0]["outcome"] != "improved":
            print(f"FAIL: outcome is {status3['outcomes'][0]['outcome']} instead of 'improved'")
            sys.exit(1)
            
        print("STABILITY SMOKE OK")
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL: Exception raised: {str(e)}")
        sys.exit(1)
    finally:
        # Cleanup test files
        for f in (test_state_file, test_db, test_db + "-wal", test_db + "-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

if __name__ == "__main__":
    run_test()
