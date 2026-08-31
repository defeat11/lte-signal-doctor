import os
from datetime import datetime, timedelta

import network_intelligence
import signal_db


def run_test():
    test_db = "test_intelligence.db"
    signal_db.DB_FILE = test_db
    try:
        signal_db.init_db()
        conn = signal_db.get_connection()
        start = datetime.now() - timedelta(minutes=180)
        rows = []
        for i in range(180):
            sinr = 18.0 if i < 170 else 5.0
            rows.append(((start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), -75.0, -8.0, sinr, "3"))
        conn.executemany("INSERT INTO signal_log (ts,lte_rsrp,lte_rsrq,lte_sinr,band) VALUES (?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        history = {"cells": {"a": {"band": "3", "avg_sinr": 8, "avg_rsrp": -85, "samples": 200}, "b": {"band": "1", "avg_sinr": 20, "avg_rsrp": -72, "samples": 300}}}
        report = network_intelligence.build_report({"band": "3", "sinr": "5dB", "rsrp": "-90dBm", "rsrq": "-12dB"}, history, {"delta": -4})
        assert 0 <= report["health_score"] <= 100
        assert report["anomaly"]["level"] in ("warning", "critical")
        assert report["bands"][0]["band"] == "1"
        assert report["decision"]["action"] in ("switch", "consider", "inspect")
        assert len(report["timeline"]) == 60
        print("INTELLIGENCE SMOKE OK")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(test_db + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    run_test()
