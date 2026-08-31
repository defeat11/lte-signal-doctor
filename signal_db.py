import sqlite3
import os
import csv
import time
import re
from datetime import datetime, timedelta

DB_FILE = "modem_signal.db"

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                lte_rsrp REAL,
                lte_rsrq REAL,
                lte_sinr REAL,
                lte_rssi TEXT,
                cell_id TEXT,
                pci TEXT,
                band TEXT,
                earfcn TEXT,
                nr_rsrp REAL,
                nr_rsrq REAL,
                nr_sinr REAL,
                nr_bandwidth TEXT,
                nr_freq TEXT,
                status TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_ts ON signal_log(ts);")
        # band_study() groups by band across ~780k rows; without this it scans the
        # whole table on every call.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_band ON signal_log(band, ts);")
        conn.commit()
    finally:
        conn.close()

def to_num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None

def insert_reading(metrics, status_text):
    if not metrics:
        return False
    conn = get_connection()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO signal_log (
                ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                cell_id, pci, band, earfcn,
                nr_rsrp, nr_rsrq, nr_sinr, nr_bandwidth, nr_freq,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ts,
            to_num(metrics.get("rsrp")),
            to_num(metrics.get("rsrq")),
            to_num(metrics.get("sinr")),
            str(metrics.get("rssi")) if metrics.get("rssi") is not None else None,
            str(metrics.get("cell_id")) if metrics.get("cell_id") is not None else None,
            str(metrics.get("pci")) if metrics.get("pci") is not None else None,
            str(metrics.get("band")) if metrics.get("band") is not None else None,
            str(metrics.get("earfcn")) if metrics.get("earfcn") is not None else None,
            to_num(metrics.get("nrrsrp")),
            to_num(metrics.get("nrrsrq")),
            to_num(metrics.get("nrsinr")),
            str(metrics.get("nrulbandwidth")) if metrics.get("nrulbandwidth") is not None else None,
            str(metrics.get("nrulfreq")) if metrics.get("nrulfreq") is not None else None,
            status_text
        ))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

def import_csv_once(csv_path):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signal_log")
        count = cursor.fetchone()[0]
        if count > 1000:
            return {"skipped": True, "count": count}
    except Exception:
        init_db()
        count = 0
    finally:
        conn.close()

    if not os.path.exists(csv_path):
        return {"skipped": True, "count": 0, "error": "CSV file not found"}

    start_time = time.time()
    rows_to_insert = []
    imported_count = 0

    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return {"imported": 0, "seconds": 0.0}

            for row in reader:
                if len(row) < 13:
                    continue
                if row[0] == "Timestamp":
                    continue
                
                ts = row[0]
                lte_rsrp = to_num(row[1])
                lte_rsrq = to_num(row[2])
                lte_sinr = to_num(row[3])
                lte_rssi = row[4]
                cell_id = row[5]
                band = row[6]
                nr_rsrp = to_num(row[7])
                nr_rsrq = to_num(row[8])
                nr_sinr = to_num(row[9])
                nr_bandwidth = row[10]
                nr_freq = row[11]
                status = row[12]
                
                rows_to_insert.append((
                    ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                    cell_id, None, band, None,
                    nr_rsrp, nr_rsrq, nr_sinr, nr_bandwidth, nr_freq,
                    status
                ))
                
                if len(rows_to_insert) >= 5000:
                    conn = get_connection()
                    try:
                        conn.executemany("""
                            INSERT INTO signal_log (
                                ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                                cell_id, pci, band, earfcn,
                                nr_rsrp, nr_rsrq, nr_sinr, nr_bandwidth, nr_freq,
                                status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, rows_to_insert)
                        conn.commit()
                        imported_count += len(rows_to_insert)
                    finally:
                        conn.close()
                    rows_to_insert = []

            if rows_to_insert:
                conn = get_connection()
                try:
                    conn.executemany("""
                        INSERT INTO signal_log (
                            ts, lte_rsrp, lte_rsrq, lte_sinr, lte_rssi,
                            cell_id, pci, band, earfcn,
                            nr_rsrp, nr_rsrq, nr_sinr, nr_bandwidth, nr_freq,
                            status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, rows_to_insert)
                    conn.commit()
                    imported_count += len(rows_to_insert)
                finally:
                    conn.close()

    except Exception as e:
        return {"error": str(e), "imported": imported_count, "seconds": time.time() - start_time}

    return {"imported": imported_count, "seconds": round(time.time() - start_time, 2)}

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def query_recent(hours=24):
    limit_dt = datetime.now() - timedelta(hours=hours)
    limit_str = limit_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    conn = get_connection()
    conn.row_factory = dict_factory
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM signal_log WHERE ts >= ? ORDER BY ts ASC", (limit_str,))
        return cursor.fetchall()
    except Exception:
        return []
    finally:
        conn.close()

def query_aggregates(hours=24, bucket="minute"):
    limit_dt = datetime.now() - timedelta(hours=hours)
    limit_str = limit_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    substr_len = 16 if bucket == "minute" else 13
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT 
                substr(ts, 1, {substr_len}) as bucket_ts,
                avg(lte_rsrp) as avg_rsrp,
                avg(lte_sinr) as avg_sinr,
                avg(nr_rsrp) as avg_nr_rsrp,
                count(*) as samples
            FROM signal_log
            WHERE ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts ASC
        """, (limit_str,))
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "bucket_ts": row[0],
                "avg_rsrp": round(row[1], 2) if row[1] is not None else None,
                "avg_sinr": round(row[2], 2) if row[2] is not None else None,
                "avg_nr_rsrp": round(row[3], 2) if row[3] is not None else None,
                "samples": row[4]
            })
        return results
    except Exception:
        return []
    finally:
        conn.close()

def hourly_profile(days=14):
    limit_dt = datetime.now() - timedelta(days=days)
    limit_str = limit_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    profile = {h: {"avg_rsrp": None, "avg_sinr": None, "samples": 0} for h in range(24)}
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                cast(substr(ts, 12, 2) as integer) as hr,
                avg(lte_rsrp) as avg_rsrp,
                avg(lte_sinr) as avg_sinr,
                count(*) as samples
            FROM signal_log
            WHERE ts >= ?
            GROUP BY hr
        """, (limit_str,))
        
        for row in cursor.fetchall():
            hr = row[0]
            if hr is not None and 0 <= hr <= 23:
                profile[hr] = {
                    "avg_rsrp": round(row[1], 2) if row[1] is not None else None,
                    "avg_sinr": round(row[2], 2) if row[2] is not None else None,
                    "samples": row[3]
                }
    except Exception:
        pass
    finally:
        conn.close()
    return profile

def band_study(min_trusted_samples=500):
    """Per-band summary mined from everything ever recorded.

    Sample counts differ by orders of magnitude (a band that served for weeks
    versus one touched for seconds during an old scan), so every row carries an
    explicit trust level instead of pretending a 3-sample average is comparable
    to a 588k-sample one.
    """
    conn = get_connection()
    bands = []
    try:
        cursor = conn.cursor()
        # Stdev via E[x^2]-E[x]^2 keeps this a single aggregate pass instead of
        # pulling ~780k rows into Python.
        cursor.execute("""
            SELECT band,
                   COUNT(*)                       AS samples,
                   AVG(lte_rsrp)                  AS rsrp,
                   AVG(lte_sinr)                  AS sinr,
                   AVG(lte_rsrq)                  AS rsrq,
                   AVG(lte_sinr * lte_sinr)       AS sinr_sq,
                   MIN(ts)                        AS first_ts,
                   MAX(ts)                        AS last_ts,
                   COUNT(DISTINCT substr(ts, 1, 10)) AS days,
                   COUNT(DISTINCT cell_id)        AS cells
            FROM signal_log
            WHERE band IS NOT NULL AND band NOT IN ('', 'N/A', '0')
              AND lte_rsrp IS NOT NULL AND lte_sinr IS NOT NULL
            GROUP BY band
        """)
        rows = cursor.fetchall()

        for row in rows:
            band, samples, rsrp, sinr, rsrq, sinr_sq, first_ts, last_ts, days, cells = row
            # A band can look great on average and still be unusable if it swings
            # every second, so stability is reported alongside the mean.
            if samples > 1 and sinr is not None and sinr_sq is not None:
                variance = max(0.0, sinr_sq - sinr * sinr)
                sinr_sd = variance ** 0.5
            else:
                sinr_sd = None

            if samples >= min_trusted_samples and days >= 2:
                trust, trust_label = "high", "موثوق — كان الباند الخادم فعلياً لفترة طويلة"
            elif samples >= 50:
                trust, trust_label = "medium", "متوسط — عينة محدودة"
            else:
                trust, trust_label = "low", "ضعيف — عينات قليلة جداً من مسح قديم، لا يُبنى عليها قرار"

            bands.append({
                "band": band,
                "samples": samples,
                "days": days,
                "distinct_cells": cells,
                "avg_rsrp": round(rsrp, 1) if rsrp is not None else None,
                "avg_sinr": round(sinr, 1) if sinr is not None else None,
                "avg_rsrq": round(rsrq, 1) if rsrq is not None else None,
                "sinr_stdev": round(sinr_sd, 2) if sinr_sd is not None else None,
                "first_seen": first_ts,
                "last_seen": last_ts,
                "trust": trust,
                "trust_label": trust_label,
            })
    except Exception:
        pass
    finally:
        conn.close()

    bands.sort(key=lambda item: item["samples"], reverse=True)
    trusted = [b for b in bands if b["trust"] == "high"]
    return {
        "bands": bands,
        "trusted_count": len(trusted),
        "needs_live_study": [b["band"] for b in bands if b["trust"] != "high"],
        "generated_at": datetime.now().isoformat(),
    }


def band_hourly_profile(band, days=30):
    """Average SINR per hour of day for one band — shows when it degrades."""
    limit_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    hours = {}
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cast(substr(ts, 12, 2) as integer) AS hr,
                   AVG(lte_sinr), AVG(lte_rsrp), COUNT(*)
            FROM signal_log
            WHERE band = ? AND ts >= ? AND lte_sinr IS NOT NULL
            GROUP BY hr
        """, (str(band), limit_str))
        for hr, sinr, rsrp, count in cursor.fetchall():
            if hr is not None:
                hours[hr] = {
                    "avg_sinr": round(sinr, 1) if sinr is not None else None,
                    "avg_rsrp": round(rsrp, 1) if rsrp is not None else None,
                    "samples": count,
                }
    except Exception:
        pass
    finally:
        conn.close()
    return hours


def db_stats():
    size_mb = 0.0
    if os.path.exists(DB_FILE):
        try:
            size_mb = round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
        except Exception:
            pass
            
    conn = get_connection()
    count = 0
    first_ts = None
    last_ts = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*), min(ts), max(ts) FROM signal_log")
        row = cursor.fetchone()
        if row:
            count = row[0]
            first_ts = row[1]
            last_ts = row[2]
    except Exception:
        pass
    finally:
        conn.close()
        
    return {
        "count": count,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "size_mb": size_mb
    }

def purge_old_data(days=30, dry_run=False):
    limit_dt = datetime.now() - timedelta(days=days)
    limit_str = limit_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    size_mb_before = 0.0
    if os.path.exists(DB_FILE):
        size_mb_before = round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
        
    deleted = 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signal_log WHERE ts < ?", (limit_str,))
        deleted = cursor.fetchone()[0]
        
        if not dry_run and deleted > 0:
            cursor.execute("DELETE FROM signal_log WHERE ts < ?", (limit_str,))
            conn.commit()
            conn.isolation_level = None
            cursor.execute("VACUUM")
            
        cursor.execute("SELECT COUNT(*) FROM signal_log")
        remaining = cursor.fetchone()[0]
    except Exception:
        remaining = 0
        try:
            cursor.execute("SELECT COUNT(*) FROM signal_log")
            remaining = cursor.fetchone()[0]
        except Exception:
            pass
    finally:
        conn.close()
        
    size_mb_after = 0.0
    if os.path.exists(DB_FILE):
        size_mb_after = round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
        
    return {
        "deleted": deleted if not dry_run else 0,
        "remaining": remaining,
        "cutoff_ts": limit_str,
        "size_mb_before": size_mb_before,
        "size_mb_after": size_mb_after if not dry_run else size_mb_before,
        "dry_run": dry_run
    }

def parse_csv_ts(ts_str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None

def trim_csv_history(csv_path, keep_days=14):
    if not csv_path or not os.path.exists(csv_path):
        return {"trimmed": False, "reason": "File not found"}
        
    # Check if small (e.g. less than 1KB)
    if os.path.getsize(csv_path) < 1000:
        return {"trimmed": False, "reason": "File too small to trim"}
        
    import tempfile
    temp_fd = None
    temp_path = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(csv_path)))
        
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f_in:
            reader = csv.reader(f_in)
            header = next(reader, None)
            if header is None:
                os.close(temp_fd)
                os.remove(temp_path)
                return {"trimmed": False, "reason": "Empty CSV"}
                
            cutoff = datetime.now() - timedelta(days=keep_days)
            written_rows = 0
            
            with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as f_out:
                writer = csv.writer(f_out)
                writer.writerow(header)
                
                for row in reader:
                    if not row:
                        continue
                    ts_val = parse_csv_ts(row[0])
                    if ts_val is None or ts_val >= cutoff:
                        writer.writerow(row)
                        written_rows += 1
                        
        os.replace(temp_path, csv_path)
        return {"trimmed": True, "csv_path": csv_path, "rows_written": written_rows}
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try:
                os.close(temp_fd)
            except Exception:
                pass
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return {"trimmed": False, "error": str(e)}

def run_maintenance(days_db=30, days_csv=14, csv_path='modem_history.csv'):
    db_res = purge_old_data(days=days_db)
    csv_res = trim_csv_history(csv_path, keep_days=days_csv)
    return {
        "db": db_res,
        "csv": csv_res
    }
