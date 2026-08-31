# lte-signal-doctor

[العربية](README.md)

**Raw radio readings from a home LTE modem ← a live LAN dashboard, safe band experiments, and signal-quality prediction.**

A Python monitoring and diagnosis system for a home LTE modem.
In real operation it collected **1,925,871 signal readings over 15 days**, in a 532 MB SQLite database.
The program logs into the modem's admin page by itself, reads the signal every few seconds, and serves everything on one dashboard that opens from any device at home.

## The problem

The whole house runs on a single LTE modem.
Its quality swings hour to hour: excellent ping in the morning, painful stutter at night.
The modem's stock interface shows two poor numbers and keeps no history at all.
So questions like these had no answer: which band is best at gaming hours? Is the problem the tower or the Wi‑Fi?

## How it works

```
Playwright logs into the modem admin page (once per session)
        │
        ▼ pulls signal XML through the modem API with a CSRF token
modem_analyzer.py ── read loop ──► SQLite (WAL) + CSV + metrics.json
        │
        ▼ local HTTP server with 39 API routes
dashboard.html (4,053 lines, one file) on any device in the LAN
        │
        ▼ layers on top of the data
SINR prediction │ tower pressure index │ Telegram alerts │ AI doctor │ band lab
```

The repository holds 6,114 lines of Python across 19 files:

| File | Role | Lines |
|---|---|---|
| modem_analyzer.py | read loop, local server, band lab | 3,076 |
| dashboard.html | the whole dashboard in one dependency-free file | 4,053 |
| signal_db.py | SQLite storage and band comparison studies | 530 |
| web_diagnostics.py | stutter triage: local link vs radio vs carrier | 281 |
| telegram_alerts.py | signal-collapse alerts and a daily report | 258 |
| signal_predictor.py | ML prediction of SINR 3 minutes ahead | 237 |
| ai_doctor.py | automated diagnosis through any AI-model CLI | 229 |
| tower_insights.py + network_intelligence.py | explainable indicators from history | 335 |

The predictor is a Gradient Boosting model trained on 3,345 real samples.
Its mean absolute error is 0.694 dB when predicting SINR 3 minutes ahead.
The tower pressure index invents no user count: the modem does not expose one, so the index explains its components (CQI, SINR, RSRQ) instead of faking a number.

## The key design decision

**The problem:** changing the modem's band travels over the very link the change can kill.
A wrong band means no internet, and no remote way back.

**The decision:** every band experiment goes through `guarded_band_experiment`.
It saves the original setting, applies the change, then watches real connectivity for up to 90 seconds.
The rollback runs inside `finally` with retries and verification, so it fires even if the modem session collapses mid-apply.

**Cost/benefit:** every experiment is up to a minute and a half slower.
In return, the modem never stays stuck on a dead band while nobody is home.

## Running it

```
pip install -r requirements.txt
playwright install chromium
copy .env.example .env    then fill in the modem gateway address and password
run_project.bat
```

The modem password and address live only in `.env`; neither exists in the code.
The dashboard opens at `http://<your-pc-address>:8000/dashboard.html` from any device in the LAN.
`allow_firewall.bat` opens the port for other devices, and `watchdog.bat` restarts the app if it hangs.

Proof it works — 5 smoke tests that need no modem and no external dependency:

```
$ python smoke_test_db.py && python smoke_test_tower_insights.py &&
  python smoke_test_intelligence.py && python smoke_test_stability.py &&
  python smoke_test_telegram.py
SMOKE OK
TOWER INSIGHTS SMOKE OK
INTELLIGENCE SMOKE OK
STABILITY SMOKE OK
SMOKE TG OK
```

## Why I built it

The whole house depends on an LTE modem whose quality swings with no visible explanation.
Instead of guessing, I collected 1,925,871 readings and let band and cell decisions rest on them.
