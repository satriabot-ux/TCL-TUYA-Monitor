import datetime
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
import hashlib

from energy_db import EnergyDatabase
from tcl_client import TCLHomeClient
from tuya_client import TuyaPlugClient

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

app = FastAPI(title="TCL SaveIn & Tuya Smart Plug Unified Monitor")

db = EnergyDatabase()
history_log: deque = deque(maxlen=30)
tcl_instance: TCLHomeClient | None = None
tuya_instance: TuyaPlugClient | None = None
cached_ac_meta: Dict[str, Any] = {}
cached_ac_state: Dict[str, Any] = {}
last_ac_poll_ts: float = 0.0
last_sample_ts: float = 0.0
last_tcl_energy_sync_ts: float = 0.0
cached_tcl_energy: Dict[str, Any] = {}


def load_config() -> Dict[str, Any]:
    cfg = {
        "tcl_username": os.environ.get("TCL_USERNAME", ""),
        "tcl_password": os.environ.get("TCL_PASSWORD", ""),
        "tcl_device_id": os.environ.get("TCL_DEVICE_ID", ""),
        "tuya_mode": os.environ.get("TUYA_MODE", "cloud"),
        "tuya_region": os.environ.get("TUYA_REGION", "sg"),
        "tuya_device_id": os.environ.get("TUYA_DEVICE_ID", ""),
        "tuya_api_key": os.environ.get("TUYA_API_KEY", ""),
        "tuya_api_secret": os.environ.get("TUYA_API_SECRET", ""),
        "tuya_local_ip": os.environ.get("TUYA_LOCAL_IP", ""),
        "tuya_local_key": os.environ.get("TUYA_LOCAL_KEY", ""),
        "tuya_local_version": os.environ.get("TUYA_LOCAL_VERSION", "3.4"),
        "pln_rate": float(os.environ.get("PLN_RATE", "1444.70")),
        "app_password": os.environ.get("APP_PASSWORD", "azzam2021"),
    }
    if CONFIG_PATH.exists():
        try:
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in file_cfg.items():
                if v not in ("", None):
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def get_expected_token() -> str:
    pw = str(load_config().get("app_password") or "azzam2021").strip()
    return hashlib.sha256(f"tcl_tuya_auth::{pw}".encode("utf-8")).hexdigest()


def verify_auth(x_app_token: str | None) -> None:
    if not x_app_token or x_app_token != get_expected_token():
        raise HTTPException(status_code=401, detail="Unauthorized: Password akses salah atau belum login")


def save_config_file(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        try:
            Path("/tmp/config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass


def init_clients() -> None:
    global tcl_instance, tuya_instance
    cfg = load_config()
    if cfg.get("tcl_username") and cfg.get("tcl_password"):
        tcl_instance = TCLHomeClient(cfg["tcl_username"], cfg["tcl_password"])
    else:
        tcl_instance = None

    if cfg.get("tuya_device_id") and (cfg.get("tuya_api_key") or cfg.get("tuya_local_key")):
        tuya_instance = TuyaPlugClient(
            mode=cfg.get("tuya_mode", "cloud"),
            api_region=cfg.get("tuya_region", "sg"),
            api_key=cfg.get("tuya_api_key", ""),
            api_secret=cfg.get("tuya_api_secret", ""),
            device_id=cfg.get("tuya_device_id", ""),
            local_ip=cfg.get("tuya_local_ip", ""),
            local_key=cfg.get("tuya_local_key", ""),
            local_version=cfg.get("tuya_local_version", "3.4"),
        )
    else:
        tuya_instance = None


init_clients()


def _local_tuya_heartbeat_loop() -> None:
    """When running locally on the home Wi-Fi, send UPDATEDPS every 4 seconds so the Tuya plug pushes live Watts to Tuya Cloud & Vercel."""
    if os.environ.get("VERCEL"):
        return
    while True:
        try:
            if tuya_instance and tuya_instance.local_ip and tuya_instance.local_key:
                tuya_instance.get_plug_metrics()
        except Exception:
            pass
        time.sleep(4.0)


threading.Thread(target=_local_tuya_heartbeat_loop, daemon=True).start()


class ConfigUpdateModel(BaseModel):
    tcl_username: str = ""
    tcl_password: str = ""
    tcl_device_id: str = ""
    tuya_mode: str = "cloud"
    tuya_region: str = "sg"
    tuya_device_id: str = ""
    tuya_api_key: str = ""
    tuya_api_secret: str = ""
    tuya_local_ip: str = ""
    tuya_local_key: str = ""
    tuya_local_version: str = "3.4"
    pln_rate: float = 1444.70


class LoginModel(BaseModel):
    password: str


class AcControlModel(BaseModel):
    desired: Dict[str, Any]


@app.post("/api/auth/login")
def login_app(body: LoginModel):
    expected_pw = str(load_config().get("app_password") or "azzam2021").strip()
    if body.password.strip() == expected_pw:
        return {"status": "ok", "token": get_expected_token()}
    raise HTTPException(status_code=401, detail="Password salah!")


@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/manifest.json")
def serve_manifest():
    return FileResponse(BASE_DIR / "manifest.json")


@app.get("/api/config")
def get_current_config(x_app_token: str | None = Header(default=None)):
    verify_auth(x_app_token)
    cfg = load_config()
    safe_cfg = {**cfg}
    safe_cfg["tcl_password"] = "***" if cfg.get("tcl_password") else ""
    safe_cfg["tuya_api_secret"] = "***" if cfg.get("tuya_api_secret") else ""
    safe_cfg["app_password"] = "***"
    return safe_cfg


@app.post("/api/config")
def update_config(body: ConfigUpdateModel, x_app_token: str | None = Header(default=None)):
    verify_auth(x_app_token)
    old = load_config()
    new_cfg = body.model_dump()
    if not new_cfg["tcl_password"] or new_cfg["tcl_password"] == "***":
        new_cfg["tcl_password"] = old.get("tcl_password", "")
    if not new_cfg["tuya_api_secret"] or new_cfg["tuya_api_secret"] == "***":
        new_cfg["tuya_api_secret"] = old.get("tuya_api_secret", "")
    new_cfg["app_password"] = old.get("app_password", "azzam2021")
    save_config_file(new_cfg)
    init_clients()
    return {"status": "ok"}


@app.get("/api/status")
def get_combined_status(x_app_token: str | None = Header(default=None)):
    verify_auth(x_app_token)
    global cached_ac_meta, cached_ac_state, last_ac_poll_ts, last_sample_ts, last_tcl_energy_sync_ts, cached_tcl_energy
    cfg = load_config()
    warnings: List[str] = []
    ac_state: Dict[str, Any] = {}
    plug_state: Dict[str, Any] = {}
    now_ts = time.time()

    # 1. Read TCL SaveIn AC (cached for 8s so 2s fast Watt polling stays sub-100ms)
    if tcl_instance:
        try:
            dev_id = cfg.get("tcl_device_id")
            if not dev_id:
                devices = tcl_instance.get_devices()
                if devices:
                    dev_id = devices[0]["device_id"]
                    cached_ac_meta = devices[0]
                    cfg["tcl_device_id"] = dev_id
                    save_config_file(cfg)
                else:
                    warnings.append("Tidak ada perangkat AC ditemukan di akun TCL Home.")
            if dev_id:
                if (now_ts - last_ac_poll_ts) >= 8.0 or not cached_ac_state:
                    cached_ac_state = tcl_instance.get_ac_status(dev_id)
                    cached_ac_state["name"] = cached_ac_meta.get("name") or f"AC TCL SAVEIN ({dev_id[:6]})"
                    last_ac_poll_ts = now_ts
                ac_state = cached_ac_state

                if (now_ts - last_tcl_energy_sync_ts) > 120 or not cached_tcl_energy:
                    cached_tcl_energy = tcl_instance.get_tcl_energy_summary(dev_id)
                    if cached_tcl_energy.get("daily_details"):
                        db.sync_tcl_history(
                            cached_tcl_energy["daily_details"],
                            cached_tcl_energy.get("work_time_details"),
                        )
                    last_tcl_energy_sync_ts = now_ts
        except Exception as e:
            warnings.append(f"TCL Home Error: {str(e)}")
            ac_state = cached_ac_state
    else:
        warnings.append("Klik tombol 'Konfigurasi' di kanan atas untuk mengisi akun TCL Home & Tuya.")

    # 2. Read Tuya Smart Plug Wattage
    if tuya_instance:
        try:
            plug_state = tuya_instance.get_plug_metrics()
        except Exception as e:
            warnings.append(f"Tuya Plug Error: {str(e)}")

    # 3. Record correlation sample into SQLite & in-memory history
    now_ts = time.time()
    interval_sec = min(60.0, max(1.0, now_ts - last_sample_ts)) if last_sample_ts > 0 else 5.0
    last_sample_ts = now_ts

    if plug_state or ac_state:
        watts = float(plug_state.get("power_watts") or 0.0)
        volts = float(plug_state.get("voltage_volts") or 0.0)
        amps = float(plug_state.get("current_amps") or 0.0)
        db.record_sample(
            watts=watts,
            volts=volts,
            amps=amps,
            room_temp=ac_state.get("current_temperature"),
            target_temp=ac_state.get("target_temperature"),
            outdoor_temp=ac_state.get("external_temperature"),
            ac_power=bool(ac_state.get("power")),
            interval_sec=interval_sec,
        )
        history_log.appendleft(
            {
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "room_temp": ac_state.get("current_temperature"),
                "target_temp": ac_state.get("target_temperature"),
                "watts": plug_state.get("power_watts"),
            }
        )

    return {
        "ac": ac_state,
        "ac_energy": cached_tcl_energy,
        "plug": plug_state,
        "pln_rate": cfg.get("pln_rate", 1444.70),
        "history": list(history_log),
        "warnings": warnings,
    }


@app.get("/api/analytics")
def get_analytics(x_app_token: str | None = Header(default=None)):
    """Return Daily (24h hourly), Weekly (7d by date 00:00-00:00), and Monthly (by month without date) energy & cost analytics."""
    verify_auth(x_app_token)
    global last_tcl_energy_sync_ts, cached_tcl_energy
    cfg = load_config()
    rate = float(cfg.get("pln_rate") or 1444.70)

    # Ensure TCL Cloud historical kWh & Work-Time are synced (every 45s so today's date updates live)
    if tcl_instance and ((time.time() - last_tcl_energy_sync_ts) > 45 or not cached_tcl_energy):
        try:
            dev_id = cfg.get("tcl_device_id")
            if not dev_id:
                devices = tcl_instance.get_devices()
                if devices:
                    dev_id = devices[0]["device_id"]
            if dev_id:
                cached_tcl_energy = tcl_instance.get_tcl_energy_summary(dev_id)
                if cached_tcl_energy.get("daily_details"):
                    db.sync_tcl_history(
                        cached_tcl_energy["daily_details"],
                        cached_tcl_energy.get("work_time_details"),
                    )
                last_tcl_energy_sync_ts = time.time()
        except Exception:
            pass

    hourly_today = db.get_today_hourly_chart()
    weekly_days = db.get_daily_range(7)
    monthly_months = db.get_monthly_by_month()

    week_total_kwh = round(sum(d["kwh"] for d in weekly_days), 2)
    week_ai_saved_kwh = round(sum(d["ai_saved_kwh"] for d in weekly_days), 2)
    week_work_hours = round(sum(d["work_hours"] for d in weekly_days), 1)

    # Current month and year totals aggregated from monthly_months
    wib_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    cur_month_key = wib_now.strftime("%Y-%m")
    cur_month_obj = next((m for m in monthly_months if m["month"] == cur_month_key), None)
    month_total_kwh = cur_month_obj["kwh"] if cur_month_obj else round(sum(m["kwh"] for m in monthly_months), 2)
    month_ai_saved_kwh = cur_month_obj["ai_saved_kwh"] if cur_month_obj else round(sum(m["ai_saved_kwh"] for m in monthly_months), 2)
    month_work_hours = cur_month_obj["work_hours"] if cur_month_obj else round(sum(m["work_hours"] for m in monthly_months), 1)

    today_kwh = weekly_days[-1]["kwh"] if weekly_days else 0.0
    today_ai_saved = weekly_days[-1]["ai_saved_kwh"] if weekly_days else 0.0
    today_work_hours = weekly_days[-1]["work_hours"] if weekly_days else 0.0
    today_work_str = weekly_days[-1]["work_duration_str"] if weekly_days else "0j 0m"

    return {
        "pln_rate": rate,
        "summary": {
            "today_kwh": today_kwh,
            "today_cost_rp": round(today_kwh * rate),
            "today_ai_saved_kwh": today_ai_saved,
            "today_work_hours": today_work_hours,
            "today_work_str": today_work_str,
            "week_kwh": week_total_kwh,
            "week_cost_rp": round(week_total_kwh * rate),
            "week_ai_saved_kwh": week_ai_saved_kwh,
            "week_work_hours": week_work_hours,
            "month_kwh": month_total_kwh,
            "month_cost_rp": round(month_total_kwh * rate),
            "month_ai_saved_kwh": month_ai_saved_kwh,
            "month_ai_saved_rp": round(month_ai_saved_kwh * rate),
            "month_work_hours": month_work_hours,
        },
        "daily_hourly": hourly_today,
        "weekly": weekly_days,
        "monthly": monthly_months,
    }


@app.post("/api/ac/control")
def control_ac(body: AcControlModel, x_app_token: str | None = Header(default=None)):
    verify_auth(x_app_token)
    global last_ac_poll_ts
    cfg = load_config()
    if not tcl_instance:
        raise HTTPException(status_code=400, detail="Akun TCL Home belum dikonfigurasi")
    dev_id = cfg.get("tcl_device_id")
    if not dev_id:
        devices = tcl_instance.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="Device AC TCL tidak ditemukan")
        dev_id = devices[0]["device_id"]
    last_ac_poll_ts = 0.0
    return tcl_instance.set_ac_state(dev_id, body.desired)
