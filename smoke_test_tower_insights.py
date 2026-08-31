import os
from datetime import datetime

import signal_db
import tower_insights


def run_test():
    test_db = "test_tower_insights.db"
    signal_db.DB_FILE = test_db
    try:
        signal_db.init_db()
        profile = tower_insights.build_tower_profile({
            "cell_id": "12345678", "pci": "101", "band": "3",
            "rsrp": "-69dBm", "rsrq": "-6dB", "sinr": "17dB",
            "cqi0": "8", "cqi1": "11", "nrsinr": "18dB",
        })
        assert profile["enodeb_id"] == 48225
        assert profile["sector_id"] == 78
        assert 0 <= profile["pressure"]["index"] <= 100
        assert profile["pressure"]["connected_users"]["available"] is False
        print("TOWER INSIGHTS SMOKE OK")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(test_db + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    run_test()
