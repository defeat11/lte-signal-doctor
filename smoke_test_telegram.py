import os
import sys

def run_test():
    import telegram_alerts
    
    test_config = "test_tg_config.json"
    telegram_alerts.TELEGRAM_CONFIG_FILE = test_config
    
    # Cleanup previous run test config if exists
    if os.path.exists(test_config):
        try:
            os.remove(test_config)
        except Exception:
            pass
            
    try:
        # 1. Test build_alert_message for all 5 types
        payloads = {
            "signal_collapse": {"metrics": {"cell_id": "123", "band": "3", "rsrp": "-110dBm", "sinr": "-2dB"}},
            "congestion_high": {"metrics": {"cell_id": "123", "band": "3"}, "reasons": ["High RSRQ count", "Low SINR"]},
            "auto_revert": {},
            "doctor_diagnosis": {
                "structured": {"root_cause": "برج مضغوط جداً", "severity": "medium", "actions": ["قفل باند 1", "تغيير موقع المودم"], "suggested_band": "1"},
                "error": None
            },
            "daily_report": {"samples": 5000, "avg_rsrp": -90.2, "avg_sinr": 12.5, "worst_hour": 18, "doctor_runs": 2}
        }
        
        for k, v in payloads.items():
            msg = telegram_alerts.build_alert_message(k, v)
            if not msg or len(msg) == 0:
                print(f"FAIL: build_alert_message returned empty for type {k}")
                sys.exit(1)
            # Basic sanity checks
            if k == "signal_collapse" and "انهيار" not in msg:
                print("FAIL: signal_collapse message is missing expected keywords")
                sys.exit(1)
            if k == "congestion_high" and "ازدحام" not in msg:
                print("FAIL: congestion_high message is missing expected keywords")
                sys.exit(1)
            if k == "auto_revert" and "تراجع" not in msg:
                print("FAIL: auto_revert message is missing expected keywords")
                sys.exit(1)
            if k == "doctor_diagnosis" and "طبيب" not in msg:
                print("FAIL: doctor_diagnosis message is missing expected keywords")
                sys.exit(1)
            if k == "daily_report" and "التقرير اليومي" not in msg:
                print("FAIL: daily_report message is missing expected keywords")
                sys.exit(1)

        # 2. Test enabled=False notify returns False
        notifier = telegram_alerts.TelegramNotifier()
        telegram_alerts.write_telegram_config({
            "enabled": False,
            "bot_token": "123456:ABC-DEF1234ghIkl-zyx987wvu4321",
            "chat_id": "987654321",
            "cooldown_minutes": 15,
            "daily_report_hour": 9
        })
        
        ok = notifier.notify("signal_collapse", "Test collapse msg")
        if ok:
            print("FAIL: notify returned True when enabled=False")
            sys.exit(1)

        # 3. Test cooldown logic
        telegram_alerts.write_telegram_config({
            "enabled": True,
            "bot_token": "123456:ABC-DEF1234ghIkl-zyx987wvu4321",
            "chat_id": "987654321",
            "cooldown_minutes": 15,
            "daily_report_hour": 9
        })
        
        # Reset cooldowns dict
        notifier.cooldowns = {}
        
        # First call triggers thread
        first_call = notifier.notify("signal_collapse", "Test message 1")
        if not first_call:
            print("FAIL: First notify returned False when enabled=True")
            sys.exit(1)
            
        # Second call within cooldown window must return False
        second_call = notifier.notify("signal_collapse", "Test message 2")
        if second_call:
            print("FAIL: Second notify returned True during cooldown window")
            sys.exit(1)

        # 4. Test masking in get_status
        status = notifier.get_status()
        masked_token = status["config"]["bot_token"]
        if masked_token != "123456...":
            print(f"FAIL: Token masking returned '{masked_token}' instead of '123456...'")
            sys.exit(1)
            
        print("SMOKE TG OK")
        sys.exit(0)
        
    except Exception as e:
        print(f"FAIL: Raised exception {str(e)}")
        sys.exit(1)
        
    finally:
        if os.path.exists(test_config):
            try:
                os.remove(test_config)
            except Exception:
                pass

if __name__ == "__main__":
    run_test()
