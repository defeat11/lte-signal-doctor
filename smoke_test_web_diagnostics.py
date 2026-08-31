import web_diagnostics


def run_test():
    original_ping = web_diagnostics._ping
    original_dns = web_diagnostics._dns
    original_web = web_diagnostics._https_ttfb
    try:
        web_diagnostics._ping = lambda host, count=6: {"ok": True, "avg_ms": 1 if host.startswith("192.") else 85, "loss_percent": 0}
        web_diagnostics._dns = lambda host: {"host": host, "ok": True, "ms": 15}
        web_diagnostics._https_ttfb = lambda host, path="/generate_204": {"host": host, "ok": True, "ttfb_ms": 510, "status": 204}
        result = web_diagnostics._diagnose({"sinr": "17dB", "rsrq": "-6dB", "cqi0": "9", "cqi1": "11"}, {"index": 24})
        assert result["cause"] == "operator_latency"
        assert result["tower_related"] is False
        print("WEB DIAGNOSTICS SMOKE OK")
    finally:
        web_diagnostics._ping = original_ping
        web_diagnostics._dns = original_dns
        web_diagnostics._https_ttfb = original_web


if __name__ == "__main__":
    run_test()
