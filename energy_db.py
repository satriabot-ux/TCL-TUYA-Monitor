import datetime
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

WIB_TZ = datetime.timezone(datetime.timedelta(hours=7))

if os.environ.get("VERCEL"):
    DB_PATH = Path("/tmp/energy_history.db")
else:
    DB_PATH = Path(__file__).parent / "energy_history.db"

MONTH_NAMES_ID = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
]


def now_wib() -> datetime.datetime:
    return datetime.datetime.now(WIB_TZ)


class EnergyDatabase:
    """SQLite database to record real-time Tuya Watt/kWh and TCL AC metrics for Daily (Hourly), Weekly (Daily), and Monthly (12-Month) charts."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS power_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    date_str TEXT NOT NULL,
                    hour_str TEXT NOT NULL,
                    watts REAL NOT NULL,
                    volts REAL DEFAULT 0,
                    amps REAL DEFAULT 0,
                    delta_kwh REAL DEFAULT 0,
                    room_temp REAL,
                    target_temp REAL,
                    outdoor_temp REAL,
                    ac_power INTEGER DEFAULT 0
                )
                """
            )
            try:
                conn.execute("ALTER TABLE power_samples ADD COLUMN delta_kwh REAL DEFAULT 0")
            except Exception:
                pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_date ON power_samples(date_str, hour_str)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_energy (
                    date_str TEXT PRIMARY KEY,
                    tuya_kwh REAL DEFAULT 0,
                    tcl_kwh REAL DEFAULT 0,
                    tcl_ai_saved_kwh REAL DEFAULT 0,
                    work_hours REAL DEFAULT 0,
                    avg_watts REAL DEFAULT 0,
                    max_watts REAL DEFAULT 0,
                    updated_at INTEGER
                )
                """
            )
            conn.commit()

    def record_sample(
        self,
        watts: float,
        volts: float = 0.0,
        amps: float = 0.0,
        room_temp: float | None = None,
        target_temp: float | None = None,
        outdoor_temp: float | None = None,
        ac_power: bool = False,
        interval_sec: float = 5.0,
    ) -> None:
        now = now_wib()
        ts = int(now.timestamp())
        date_str = now.strftime("%Y-%m-%d")
        hour_str = now.strftime("%H:00")

        # Incremental kWh collected during this polling interval
        delta_kwh = max(0.0, (float(watts) / 1000.0) * (interval_sec / 3600.0))

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO power_samples (ts, date_str, hour_str, watts, volts, amps, delta_kwh, room_temp, target_temp, outdoor_temp, ac_power)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    date_str,
                    hour_str,
                    float(watts),
                    float(volts),
                    float(amps),
                    delta_kwh,
                    room_temp,
                    target_temp,
                    outdoor_temp,
                    1 if ac_power else 0,
                ),
            )

            # Update daily aggregated table (00:00 -> 00:00 per date_str)
            row = conn.execute(
                "SELECT tuya_kwh, max_watts FROM daily_energy WHERE date_str = ?",
                (date_str,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO daily_energy (date_str, tuya_kwh, avg_watts, max_watts, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (date_str, delta_kwh, float(watts), float(watts), ts),
                )
            else:
                new_tuya_kwh = float(row["tuya_kwh"] or 0.0) + delta_kwh
                new_max = max(float(row["max_watts"] or 0.0), float(watts))
                conn.execute(
                    """
                    UPDATE daily_energy
                    SET tuya_kwh = ?, max_watts = ?, updated_at = ?
                    WHERE date_str = ?
                    """,
                    (new_tuya_kwh, new_max, ts, date_str),
                )
            conn.commit()

    def sync_tcl_history(
        self,
        details: List[Dict[str, Any]],
        work_details: List[Dict[str, Any]] | None = None,
    ) -> None:
        """Upsert historical daily kWh, AI SaveIn kWh, and Work Hours returned from TCL Cloud."""
        ts = int(now_wib().timestamp())
        work_by_date: Dict[str, float] = {}
        if work_details:
            for w in work_details:
                d_str = w.get("date")
                if d_str and len(d_str) == 10:
                    mins = float(w.get("workTime") or w.get("work_time") or 0.0)
                    work_by_date[d_str] = round(mins / 60.0, 2)

        with self._connect() as conn:
            for item in details:
                d_str = item.get("date")
                if not d_str or len(d_str) != 10:
                    continue
                consumption = float(item.get("consumption") or 0.0)
                ai_saved = float(item.get("aiConsumption") or item.get("ai_consumption") or 0.0)
                w_hours = work_by_date.get(d_str, 0.0)
                row = conn.execute(
                    "SELECT date_str FROM daily_energy WHERE date_str = ?", (d_str,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO daily_energy (date_str, tcl_kwh, tcl_ai_saved_kwh, work_hours, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (d_str, consumption, ai_saved, w_hours, ts),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE daily_energy
                        SET tcl_kwh = ?, tcl_ai_saved_kwh = ?, work_hours = ?, updated_at = ?
                        WHERE date_str = ?
                        """,
                        (consumption, ai_saved, w_hours, ts, d_str),
                    )
            conn.commit()

    def get_today_hourly_chart(self) -> List[Dict[str, Any]]:
        """Get 24-hour breakdown for today (00:00 to 23:00 WIB), updating every hour and resetting at 00:00."""
        today_str = now_wib().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT hour_str,
                       ROUND(AVG(watts), 1) AS avg_watts,
                       ROUND(MAX(watts), 1) AS peak_watts,
                       ROUND(SUM(delta_kwh), 3) AS sum_kwh,
                       ROUND(AVG(room_temp), 1) AS avg_temp,
                       COUNT(*) AS samples
                FROM power_samples
                WHERE date_str = ?
                GROUP BY hour_str
                ORDER BY hour_str ASC
                """,
                (today_str,),
            ).fetchall()

        by_hour = {r["hour_str"]: dict(r) for r in rows}
        result = []
        for h in range(24):
            h_key = f"{h:02d}:00"
            if h_key in by_hour:
                avg_w = float(by_hour[h_key]["avg_watts"] or 0.0)
                sum_kwh = float(by_hour[h_key]["sum_kwh"] or 0.0)
                # Use accumulated delta_kwh if present, otherwise fallback to avg_w / 1000
                effective_hour_kwh = round(sum_kwh if sum_kwh > 0 else (avg_w / 1000.0), 3)
                result.append(
                    {
                        "label": h_key,
                        "avg_watts": avg_w,
                        "peak_watts": float(by_hour[h_key]["peak_watts"] or 0.0),
                        "kwh": effective_hour_kwh,
                        "avg_temp": by_hour[h_key]["avg_temp"],
                    }
                )
            else:
                result.append(
                    {
                        "label": h_key,
                        "avg_watts": 0.0,
                        "peak_watts": 0.0,
                        "kwh": 0.0,
                        "avg_temp": None,
                    }
                )
        return result

    def get_daily_range(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get daily energy consumption per date (00:00 to 00:00 WIB) for the last N days, updating live for today."""
        today = now_wib().date()
        start_date = (today - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT date_str,
                       ROUND(tuya_kwh, 3) AS tuya_kwh,
                       ROUND(tcl_kwh, 3) AS tcl_kwh,
                       ROUND(tcl_ai_saved_kwh, 3) AS tcl_ai_saved_kwh,
                       ROUND(work_hours, 2) AS work_hours,
                       ROUND(max_watts, 1) AS max_watts
                FROM daily_energy
                WHERE date_str >= ?
                ORDER BY date_str ASC
                """,
                (start_date,),
            ).fetchall()
        by_date = {r["date_str"]: dict(r) for r in rows}

        # Calculate today's live hourly kWh sum so today's date bar updates live continuously
        today_hourly = self.get_today_hourly_chart()
        today_hourly_kwh = round(sum(h["kwh"] for h in today_hourly), 3)

        out = []
        for i in range(days):
            d = today - datetime.timedelta(days=(days - 1 - i))
            d_str = d.strftime("%Y-%m-%d")
            item = by_date.get(d_str, {})
            tuya_val = float(item.get("tuya_kwh") or 0.0)
            if d == today:
                tuya_val = max(tuya_val, today_hourly_kwh)
            tcl_val = float(item.get("tcl_kwh") or 0.0)
            effective_kwh = max(tuya_val, tcl_val)
            w_hours = round(float(item.get("work_hours") or 0.0), 1)
            total_mins = int(round(float(item.get("work_hours") or 0.0) * 60))
            hrs_part = total_mins // 60
            mins_part = total_mins % 60
            label_id = f"{d.day:02d} {MONTH_NAMES_ID[d.month]}"
            out.append(
                {
                    "date": d_str,
                    "label": label_id,
                    "kwh": round(effective_kwh, 2),
                    "tuya_kwh": round(tuya_val, 2),
                    "tcl_kwh": round(tcl_val, 2),
                    "ai_saved_kwh": round(float(item.get("tcl_ai_saved_kwh") or 0.0), 2),
                    "work_hours": w_hours,
                    "work_duration_str": f"{hrs_part}j {mins_part}m" if total_mins > 0 else "0j 0m",
                    "max_watts": float(item.get("max_watts") or 0.0),
                }
            )
        return out

    def get_monthly_by_month(self) -> List[Dict[str, Any]]:
        """Aggregate consumption by Month only (without dates, e.g. 'Nov 2026'), updating live from daily/weekly data."""
        now = now_wib()
        year = now.year
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT date_str,
                       tuya_kwh,
                       tcl_kwh,
                       tcl_ai_saved_kwh,
                       work_hours
                FROM daily_energy
                WHERE date_str LIKE ?
                ORDER BY date_str ASC
                """,
                (f"{year}-%",),
            ).fetchall()

        today_str = now.strftime("%Y-%m-%d")
        today_hourly_kwh = round(sum(h["kwh"] for h in self.get_today_hourly_chart()), 3)

        monthly_buckets: Dict[str, Dict[str, float]] = {}
        for m in range(1, 13):
            m_key = f"{year}-{m:02d}"
            monthly_buckets[m_key] = {
                "kwh": 0.0,
                "ai_saved_kwh": 0.0,
                "work_hours": 0.0,
            }

        seen_today = False
        for r in rows:
            d_str = r["date_str"]
            m_key = d_str[:7]
            if m_key not in monthly_buckets:
                continue
            tuya_v = float(r["tuya_kwh"] or 0.0)
            if d_str == today_str:
                tuya_v = max(tuya_v, today_hourly_kwh)
                seen_today = True
            tcl_v = float(r["tcl_kwh"] or 0.0)
            eff_kwh = max(tuya_v, tcl_v)
            monthly_buckets[m_key]["kwh"] += eff_kwh
            monthly_buckets[m_key]["ai_saved_kwh"] += float(r["tcl_ai_saved_kwh"] or 0.0)
            monthly_buckets[m_key]["work_hours"] += float(r["work_hours"] or 0.0)

        if not seen_today:
            cur_m_key = now.strftime("%Y-%m")
            if cur_m_key in monthly_buckets:
                monthly_buckets[cur_m_key]["kwh"] += today_hourly_kwh

        out = []
        for m in range(1, 13):
            m_key = f"{year}-{m:02d}"
            b = monthly_buckets[m_key]
            total_mins = int(round(b["work_hours"] * 60))
            hrs_part = total_mins // 60
            mins_part = total_mins % 60
            out.append(
                {
                    "month": m_key,
                    "label": f"{MONTH_NAMES_ID[m]} {year}",
                    "short_label": MONTH_NAMES_ID[m],
                    "kwh": round(b["kwh"], 2),
                    "ai_saved_kwh": round(b["ai_saved_kwh"], 2),
                    "work_hours": round(b["work_hours"], 1),
                    "work_duration_str": f"{hrs_part}j {mins_part}m" if total_mins > 0 else "0j 0m",
                }
            )
        return out
