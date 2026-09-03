# lte-signal-doctor

[العربية](README.md)

**Raw radio readings from a home LTE modem ← a live LAN dashboard, safe band experiments, and signal-quality prediction.**

A Python monitoring and diagnosis system for a home LTE modem.
In real use it collected **1,925,871 signal readings over 15 days**. The SQLite database grew to 532 MB.
The program logs into the modem admin page by itself. It reads the signal every few seconds.
One dashboard shows everything, and it opens from any device at home.

## The problem

The whole house runs on a single LTE modem.
The quality changes from hour to hour. Ping is good in the morning, but the line stutters at night.
The modem's own interface shows two weak numbers. It keeps no history.
So these questions had no answer: which band is best in gaming hours? Is the problem the tower or the Wi‑Fi?

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

The repository has 6,114 lines of Python in 19 files:

| File | Role | Lines |
|---|---|---|
| modem_analyzer.py | read loop, local server, band lab | 3,076 |
| dashboard.html | the whole dashboard in one file, no dependencies | 4,053 |
| signal_db.py | SQLite storage and band comparison studies | 530 |
| web_diagnostics.py | stutter check: local link vs radio vs carrier | 281 |
| telegram_alerts.py | signal-collapse alerts and a daily report | 258 |
| signal_predictor.py | ML prediction of SINR 3 minutes ahead | 237 |
| ai_doctor.py | automated diagnosis through any AI-model CLI | 229 |
| tower_insights.py + network_intelligence.py | explainable indicators from history | 335 |

The predictor is a Gradient Boosting model trained on 3,345 real samples.
Its mean absolute error is 0.694 dB when predicting SINR 3 minutes ahead.
The tower pressure index does not invent a user count. The modem gives no such number.
So the index explains its parts (CQI, SINR, RSRQ) instead of faking one.

## The key design decision

**The problem:** the band change travels over the same link it can kill.
A wrong band means no internet, and no remote way back.

**The decision:** every band experiment goes through `guarded_band_experiment`.
It saves the original setting and applies the change. Then it watches the real connection for up to 90 seconds.
The rollback runs inside `finally`, with retries and a check. It runs even if the modem session dies in the middle.

**Cost/benefit:** every experiment is up to a minute and a half slower.
In return, the modem never stays stuck on a dead band while nobody is home.

## Running it

```
pip install -r requirements.txt
playwright install chromium
copy .env.example .env    then fill in the modem gateway address and password
run_project.bat
```

The modem password and address stay only in `.env`. They are not in the code.
The dashboard opens at `http://<your-pc-address>:8000/dashboard.html` from any device in the LAN.
`allow_firewall.bat` opens the port for other devices. `watchdog.bat` restarts the app if it hangs.

Proof it works: 5 smoke tests. They need no modem and no external dependency.

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

The whole house depends on one LTE modem. Its quality changes with no clear reason.
Instead of guessing, I collected 1,925,871 readings. Now band and cell decisions rest on real data.
