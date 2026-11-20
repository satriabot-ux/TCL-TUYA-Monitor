import threading
from typing import Any, Dict, List, Optional
import tinytuya


class TuyaPlugClient:
    """Client to read actual Watt, Voltage, Current, and Total kWh from Tuya Smart Plug with real-time UPDATEDPS refresh."""

    def __init__(
        self,
        mode: str = "cloud",
        api_region: str = "sg",
        api_key: str = "",
        api_secret: str = "",
        device_id: str = "",
        local_ip: str = "",
        local_key: str = "",
        local_version: str = "3.4",
    ) -> None:
        self.mode = mode  # "cloud" or "local"
        self.api_region = api_region
        self.api_key = api_key
        self.api_secret = api_secret
        self.device_id = device_id
        self.local_ip = local_ip
        self.local_key = local_key
        self.local_version = float(local_version) if local_version else 3.4
        self._cloud: Optional[tinytuya.Cloud] = None
        self._local_dev: Optional[tinytuya.OutletDevice] = None
        self._lock = threading.Lock()
        self._last_dps: Dict[str, Any] = {}

    def _get_cloud(self) -> tinytuya.Cloud:
        if not self._cloud:
            self._cloud = tinytuya.Cloud(
                apiRegion=self.api_region,
                apiKey=self.api_key,
                apiSecret=self.api_secret,
                apiDeviceID=self.device_id,
            )
        return self._cloud

    def _get_local_dev(self) -> tinytuya.OutletDevice:
        if self._local_dev is None:
            dev = tinytuya.OutletDevice(self.device_id, self.local_ip, self.local_key)
            dev.set_version(self.local_version)
            dev.set_socketPersistent(True)
            dev.set_socketTimeout(2.5)
            self._local_dev = dev
        return self._local_dev

    def list_cloud_devices(self) -> List[Dict[str, Any]]:
        cloud = self._get_cloud()
        return cloud.getdevices() or []

    def get_plug_metrics(self) -> Dict[str, Any]:
        """Return normalized real-time power metrics (Watts, Volts, Amps, Switch State)."""
        if self.local_ip and self.local_key and self.device_id and self.mode in ("local", "auto", "cloud"):
            with self._lock:
                try:
                    dev = self._get_local_dev()
                    # If we don't have initial state yet, query full status once
                    if not self._last_dps:
                        st = dev.status()
                        if isinstance(st, dict) and st.get("dps"):
                            self._last_dps.update(st["dps"])

                    # Force the Tuya Smart Plug hardware chip to wake up and measure live Watt/Volt/Amp
                    upd = dev.updatedps([17, 18, 19, 20])
                    if isinstance(upd, dict) and upd.get("dps"):
                        self._last_dps.update(upd["dps"])
                    else:
                        # Reconnect once if persistent socket dropped
                        dev.close()
                        self._local_dev = None
                        dev = self._get_local_dev()
                        st = dev.status()
                        if isinstance(st, dict) and st.get("dps"):
                            self._last_dps.update(st["dps"])
                        upd = dev.updatedps([17, 18, 19, 20])
                        if isinstance(upd, dict) and upd.get("dps"):
                            self._last_dps.update(upd["dps"])

                    dps = self._last_dps
                    if dps:
                        raw_power = dps.get("19") or dps.get("cur_power") or 0
                        raw_voltage = dps.get("20") or dps.get("cur_voltage") or 0
                        raw_current = dps.get("18") or dps.get("cur_current") or 0
                        add_ele = dps.get("17") or 0
                        switch_on = dps.get("1") if "1" in dps else True

                        watts = float(raw_power) / 10.0
                        volts = float(raw_voltage) / 10.0
                        amps = float(raw_current) / 1000.0
                        total_kwh = float(add_ele) / 1000.0 if float(add_ele) > 100 else float(add_ele) / 100.0

                        return {
                            "source": "Local Live (UPDATEDPS v3.4)",
                            "device_id": self.device_id,
                            "switch_on": bool(switch_on),
                            "power_watts": round(watts, 1),
                            "voltage_volts": round(volts, 1),
                            "current_amps": round(amps, 3),
                            "current_ma": int(raw_current),
                            "accumulated_kwh": round(total_kwh, 3),
                            "raw": dict(dps),
                        }
                except Exception:
                    if self._local_dev:
                        try:
                            self._local_dev.close()
                        except Exception:
                            pass
                        self._local_dev = None

        # Cloud Mode fallback (works anywhere via internet)
        cloud = self._get_cloud()
        resp = cloud.getstatus(self.device_id)
        if isinstance(resp, dict) and resp.get("Error"):
            raise ValueError(f"{resp.get('Error')} ({resp.get('Err')}): {resp.get('Payload')}")
        result_list = resp.get("result", []) if isinstance(resp, dict) else []
        kv = {item.get("code"): item.get("value") for item in result_list if isinstance(item, dict)}

        raw_power = kv.get("cur_power", 0) or 0
        raw_voltage = kv.get("cur_voltage", 0) or 0
        raw_current = kv.get("cur_current", 0) or 0
        add_ele = kv.get("add_ele", 0) or 0
        switch_on = kv.get("switch_1", kv.get("switch", True))

        volts = float(raw_voltage) / 10.0 if float(raw_voltage) > 500 else float(raw_voltage)
        watts = float(raw_power) / 10.0
        amps = float(raw_current) / 1000.0
        total_kwh = float(add_ele) / 1000.0 if float(add_ele) > 100 else float(add_ele) / 100.0

        return {
            "source": "Tuya Cloud",
            "device_id": self.device_id,
            "switch_on": bool(switch_on),
            "power_watts": round(watts, 1),
            "voltage_volts": round(volts, 1),
            "current_amps": round(amps, 3),
            "current_ma": int(raw_current),
            "accumulated_kwh": round(total_kwh, 3),
            "raw": kv,
        }
