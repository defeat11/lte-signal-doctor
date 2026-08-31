import os
import sys

def run_test():
    import signal_db
    
    # 1. Force isolated database file
    test_db = "test_smoke.db"
    signal_db.DB_FILE = test_db
    
    # Cleanup previous test if any
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass
            
    try:
        # 2. Init DB
        signal_db.init_db()
        
        # 3. Test insert_reading
        fake_metrics = {
            "rsrp": "-74dBm",
            "rsrq": "-12dB",
            "sinr": "16dB",
            "rssi": "-60dBm",
            "cell_id": "12345",
            "pci": "342",
            "band": "3",
            "earfcn": "1650",
            "nrrsrp": "-80dBm",
            "nrrsrq": "-15dB",
            "nrsinr": "18dB",
            "nrulbandwidth": "20MHz",
            "nrulfreq": "3500000"
        }
        
        for _ in range(3):
            ok = signal_db.insert_reading(fake_metrics, "Test Status")
            if not ok:
                print("FAIL: insert_reading returned False")
                sys.exit(1)
                
        # 4. Test query_recent
        recent = signal_db.query_recent(1)
        if len(recent) != 3:
            print(f"FAIL: query_recent(1) returned {len(recent)} rows instead of 3")
            sys.exit(1)
            
        row = recent[0]
        if row["lte_rsrp"] != -74.0:
            print(f"FAIL: lte_rsrp is {row['lte_rsrp']} instead of -74.0")
            sys.exit(1)
        if row["lte_sinr"] != 16.0:
            print(f"FAIL: lte_sinr is {row['lte_sinr']} instead of 16.0")
            sys.exit(1)
        if row["nr_rsrp"] != -80.0:
            print(f"FAIL: nr_rsrp is {row['nr_rsrp']} instead of -80.0")
            sys.exit(1)
        if row["cell_id"] != "12345":
            print(f"FAIL: cell_id is {row['cell_id']} instead of '12345'")
            sys.exit(1)
        if row["pci"] != "342":
            print(f"FAIL: pci is {row['pci']} instead of '342'")
            sys.exit(1)
            
        # 5. Test query_aggregates
        aggs = signal_db.query_aggregates(1, "minute")
        if not aggs or len(aggs) < 1:
            print("FAIL: query_aggregates returned empty list")
            sys.exit(1)
        if aggs[0]["samples"] != 3:
            print(f"FAIL: query_aggregates sample count is {aggs[0]['samples']} instead of 3")
            sys.exit(1)
            
        # 6. Test hourly_profile
        profile = signal_db.hourly_profile(1)
        if len(profile) != 24:
            print(f"FAIL: hourly_profile length is {len(profile)} instead of 24")
            sys.exit(1)
            
        # 7. Test db_stats
        stats = signal_db.db_stats()
        if stats["count"] != 3:
            print(f"FAIL: db_stats count is {stats['count']} instead of 3")
            sys.exit(1)
        if not stats["first_ts"] or not stats["last_ts"]:
            print("FAIL: db_stats timestamps are empty")
            sys.exit(1)
            
        print("SMOKE OK")
        sys.exit(0)
        
    except Exception as e:
        print(f"FAIL: Raised exception {str(e)}")
        sys.exit(1)
        
    finally:
        # Cleanup WAL files as well
        for ext in ["", "-wal", "-shm"]:
            fpath = test_db + ext
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass

if __name__ == "__main__":
    run_test()
