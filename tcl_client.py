import datetime
import hashlib
import json
import random
import string
import time
from typing import Any, Dict, List, Optional

import boto3
import boto3.session
import jwt
import requests


DEFAULT_APP_LOGIN_URL = "https://pa.account.tcl.com/account/login?clientId=54148614"
DEFAULT_APP_CLOUD_URL = "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
DEFAULT_APP_ID = "wx6e1af3fa84fbe523"


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _jwt_sub(token: str) -> str:
    decoded = jwt.decode(token, options={"verify_signature": False})
    return decoded.get("sub", "")


class TCLHomeClient:
    """Standalone client for TCL Home Cloud API & AWS IoT Device Shadow."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.country_abbr: str = "ID"
        self.sso_username: Optional[str] = None
        self.cloud_urls: Dict[str, Any] = {}
        self.saas_token: Optional[str] = None
        self.cognito_token: Optional[str] = None
        self.aws_creds: Dict[str, Any] = {}
        self.iot_client = None
        self.last_auth_time: float = 0

    def authenticate(self) -> Dict[str, Any]:
        """Perform full authentication chain: TCL SSO -> Cloud URLs -> SaaS/Cognito -> AWS IoT."""
        pw_hash = _md5_hex(self.password)
        login_payload = {
            "equipment": 2,
            "password": pw_hash,
            "osType": 1,
            "username": self.username,
            "clientVersion": "4.8.1",
            "osVersion": "6.0",
            "deviceModel": "AndroidAndroid SDK built for x86",
            "captchaRule": 2,
            "channel": "app",
        }
        login_headers = {
            "th_platform": "android",
            "th_version": "4.8.1",
            "th_appbulid": "830",
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }
        resp = requests.post(
            DEFAULT_APP_LOGIN_URL, json=login_payload, headers=login_headers, timeout=15
        )
        resp.raise_for_status()
        auth_data = resp.json()
        if auth_data.get("status") != 1:
            raise ValueError(f"Login TCL Home gagal: {auth_data}")

        self.token = auth_data.get("token")
        self.refresh_token = auth_data.get("refresh_token") or auth_data.get("refreshtoken")
        user_info = auth_data.get("user", {})
        self.sso_username = user_info.get("username", self.username)
        self.country_abbr = (
            user_info.get("country_abbr") or user_info.get("countryAbbr") or "ID"
        )

        # 2. Get regional cloud URLs
        cloud_resp = requests.post(
            DEFAULT_APP_CLOUD_URL,
            json={"ssoId": self.sso_username, "ssoToken": self.token},
            headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"},
            timeout=15,
        )
        cloud_resp.raise_for_status()
        self.cloud_urls = cloud_resp.json().get("data", {})
        cloud_url = self.cloud_urls.get("cloud_url")
        aws_region = self.cloud_urls.get("cloud_region")
        if not cloud_url or not aws_region:
            raise ValueError(f"Gagal mendapatkan Cloud URL TCL: {cloud_resp.json()}")

        # 3. Exchange for SaaS Token and Cognito Token
        refresh_resp = requests.post(
            f"{cloud_url}/v3/auth/refresh_tokens",
            json={
                "userId": self.sso_username,
                "ssoToken": self.token,
                "appId": DEFAULT_APP_ID,
            },
            headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"},
            timeout=15,
        )
        refresh_resp.raise_for_status()
        rt_data = refresh_resp.json().get("data", {})
        self.saas_token = rt_data.get("saas_token") or rt_data.get("saasToken")
        self.cognito_token = rt_data.get("cognito_token") or rt_data.get("cognitoToken")
        if not self.saas_token or not self.cognito_token:
            raise ValueError(f"Gagal mendapatkan SaaS/Cognito token: {refresh_resp.json()}")

        # 4. Get AWS Cognito Identity Credentials
        identity_id = _jwt_sub(self.cognito_token)
        cognito_url = f"https://cognito-identity.{aws_region}.amazonaws.com/"
        cognito_resp = requests.post(
            cognito_url,
            json={
                "IdentityId": identity_id,
                "Logins": {"cognito-identity.amazonaws.com": self.cognito_token},
            },
            headers={
                "User-agent": "aws-sdk-android/2.22.6 Linux/6.1.23-android14 Dalvik/2.1.0/0 en_US",
                "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                "content-type": "application/x-amz-json-1.1",
            },
            timeout=15,
        )
        cognito_resp.raise_for_status()
        creds = cognito_resp.json().get("Credentials", {})
        self.aws_creds = creds

        session = boto3.session.Session(
            region_name=aws_region,
            aws_access_key_id=creds.get("AccessKeyId") or creds.get("access_key_id"),
            aws_secret_access_key=creds.get("SecretKey") or creds.get("secret_key"),
            aws_session_token=creds.get("SessionToken") or creds.get("session_token"),
        )
        self.iot_client = session.client(service_name="iot-data")
        self.last_auth_time = time.time()
        return {
            "username": self.sso_username,
            "country": self.country_abbr,
            "region": aws_region,
        }

    def _ensure_auth(self) -> None:
        if not self.iot_client or (time.time() - self.last_auth_time) > 2400:
            self.authenticate()

    def _signed_headers(self) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
        sign = _md5_hex(timestamp + nonce + (self.saas_token or ""))
        return {
            "platform": "android",
            "appversion": "5.4.1",
            "thomeversion": "4.8.1",
            "accesstoken": self.saas_token or "",
            "accept-language": "en",
            "timestamp": timestamp,
            "nonce": nonce,
            "sign": sign,
            "user-agent": "Android",
            "content-type": "application/json; charset=UTF-8",
        }

    def get_devices(self) -> List[Dict[str, Any]]:
        """List all TCL Home devices attached to the user's account."""
        self._ensure_auth()
        device_url = self.cloud_urls["device_url"]
        resp = requests.post(
            f"{device_url}/v3/user/get_things",
            json={},
            headers=self._signed_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        raw_list = resp.json().get("data", [])
        devices = []
        for item in raw_list:
            devices.append(
                {
                    "device_id": item.get("device_id") or item.get("deviceId"),
                    "name": item.get("nick_name")
                    or item.get("nickName")
                    or item.get("device_name")
                    or item.get("deviceName"),
                    "category": item.get("category"),
                    "device_type": item.get("device_type") or item.get("deviceType"),
                    "is_online": bool(int(item.get("is_online") or item.get("isOnline") or 0)),
                    "firmware": item.get("firmware_version") or item.get("firmwareVersion"),
                }
            )
        return devices

    def get_ac_status(self, device_id: str) -> Dict[str, Any]:
        """Fetch real-time state from AWS IoT Thing Shadow for the TCL AC."""
        self._ensure_auth()
        response = self.iot_client.get_thing_shadow(thingName=device_id)
        payload = json.loads(response["payload"].read().decode("utf-8"))
        state = payload.get("state", {})
        reported = state.get("reported", {})
        desired = state.get("desired", {})
        merged = {**reported, **desired}

        work_mode_map = {
            0: "Auto",
            1: "Cool",
            2: "Dry (Dehumidify)",
            3: "Fan",
            4: "Heat",
        }
        work_mode_num = int(merged.get("workMode", 1))

        return {
            "device_id": device_id,
            "power": bool(int(merged.get("powerSwitch", 0))),
            "current_temperature": merged.get("currentTemperature"),
            "target_temperature": merged.get("targetTemperature"),
            "external_temperature": merged.get("externalUnitTemperature"),
            "external_fan_speed": merged.get("externalUnitFanSpeed"),
            "coil_temperature": merged.get("internalUnitCoilTemperature"),
            "compressor_frequency": merged.get("compressorFrequency"),
            "eev_open_degree": merged.get("OutDoorEEVTarOpenDegree"),
            "work_mode_code": work_mode_num,
            "work_mode": work_mode_map.get(work_mode_num, f"Mode {work_mode_num}"),
            "eco_mode": bool(int(merged.get("ECO", 0))),
            "ai_eco_savein": bool(int(merged.get("AIECOSwitch", 0)) or int(merged.get("ECO", 0))),
            "ai_eco_status": int(merged.get("AIECOStatus", 0)),
            "self_learn_switch": bool(int(merged.get("selfLearnSwitch", 0))),
            "turbo_mode": bool(int(merged.get("turbo", 0))),
            "sleep_mode": int(merged.get("sleep", 0)),
            "silence_switch": bool(int(merged.get("silenceSwitch", 0))),
            "soft_wind": bool(int(merged.get("softWind", 0)) or int(merged.get("coolFeelWind", 0))),
            "wind_speed": int(merged.get("windSpeed", 0)),
            "wind_speed_7_gear": int(merged.get("windSpeed7Gear", 0)),
            "wind_speed_percentage": int(merged.get("windSpeedPercentage", 0)),
            "wind_speed_auto": bool(int(merged.get("windSpeedAutoSwitch", 0))),
            "vertical_swing": bool(int(merged.get("verticalWind", merged.get("verticalSwitch", 0)))),
            "vertical_direction": int(merged.get("verticalDirection", 8)),
            "horizontal_swing": bool(int(merged.get("horizontalWind", merged.get("horizontalSwitch", 0)))),
            "horizontal_direction": int(merged.get("horizontalDirection", 8)),
            "healthy": bool(int(merged.get("healthy", 0))),
            "self_clean": bool(int(merged.get("selfClean", 0))),
            "self_clean_status": int(merged.get("selfCleanStatus", 0)),
            "self_clean_percentage": int(merged.get("selfCleanPercentage", 0)),
            "anti_moldew": bool(int(merged.get("antiMoldew", 0))),
            "filter_block_status": int(merged.get("filterBlockStatus", 0)),
            "error_code": merged.get("errorCode") or [],
            "screen_display": bool(int(merged.get("screen", 1))),
            "beep_switch": bool(int(merged.get("beepSwitch", 1))),
            "generator_mode": int(merged.get("generatorMode", 0)),
            "raw_reported": reported,
        }

    def get_tcl_energy_summary(self, device_id: str) -> Dict[str, Any]:
        """Get TCL's built-in daily/monthly electricity (kWh), AI SaveIn savings, and daily work time (hours)."""
        self._ensure_auth()
        device_url = self.cloud_urls["device_url"]
        now = datetime.datetime.now()
        year_month = now.strftime("%Y/%m")
        today_iso = now.strftime("%Y-%m-%d")
        yest_iso = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        result: Dict[str, Any] = {
            "today_kwh": 0.0,
            "yesterday_kwh": 0.0,
            "month_total_kwh": 0.0,
            "month_ai_saved_kwh": 0.0,
            "today_work_hours": 0.0,
            "today_ai_work_hours": 0.0,
            "today_ai_work_str": "0j 0m",
            "yesterday_work_hours": 0.0,
            "month_work_hours": 0.0,
            "daily_details": [],
            "work_time_details": [],
        }
        try:
            e_resp = requests.get(
                f"{device_url}/v3/ac/{device_id}/power/consumption/info/{year_month}",
                headers=self._signed_headers(),
                timeout=10,
            )
            if e_resp.status_code == 200:
                data = e_resp.json().get("data", {})
                curr_stat = data.get("currStatisticsRes") or data.get("curr_statistics_res") or {}
                result["month_total_kwh"] = round(
                    float(curr_stat.get("totalElectricity") or curr_stat.get("total_electricity") or 0.0), 2
                )
                result["month_ai_saved_kwh"] = round(
                    float(curr_stat.get("aiElectricity") or curr_stat.get("ai_electricity") or 0.0), 2
                )
                details = data.get("consumptionDetails") or data.get("consumption_details") or []
                result["daily_details"] = details
                for d in details:
                    if d.get("date") == today_iso:
                        result["today_kwh"] = round(float(d.get("consumption") or 0.0), 2)
                    elif d.get("date") == yest_iso:
                        result["yesterday_kwh"] = round(float(d.get("consumption") or 0.0), 2)
        except Exception:
            pass

        try:
            w_resp = requests.get(
                f"{device_url}/v3/ac/{device_id}/work-time/info/{year_month}",
                headers=self._signed_headers(),
                timeout=10,
            )
            if w_resp.status_code == 200:
                w_data = w_resp.json().get("data", {})
                curr_w = w_data.get("currentTotalWorkTime") or w_data.get("current_total_work_time") or {}
                result["month_work_hours"] = round(
                    float(curr_w.get("workTime") or curr_w.get("work_time") or 0.0) / 60.0, 1
                )
                w_details = w_data.get("workTimeDetails") or w_data.get("work_time_details") or []
                result["work_time_details"] = w_details
                for wd in w_details:
                    if wd.get("date") == today_iso:
                        result["today_work_hours"] = round(
                            float(wd.get("workTime") or wd.get("work_time") or 0.0) / 60.0, 1
                        )
                        ai_mins = float(wd.get("aiWorkTime") or wd.get("ai_work_time") or 0.0)
                        result["today_ai_work_hours"] = round(ai_mins / 60.0, 1)
                        ai_m_int = int(round(ai_mins))
                        result["today_ai_work_str"] = f"{ai_m_int // 60}j {ai_m_int % 60}m"
                    elif wd.get("date") == yest_iso:
                        result["yesterday_work_hours"] = round(
                            float(wd.get("workTime") or wd.get("work_time") or 0.0) / 60.0, 1
                        )
        except Exception:
            pass
        return result

    def set_ac_state(self, device_id: str, desired_changes: Dict[str, Any]) -> Dict[str, Any]:
        """Publish control commands (e.g., powerSwitch, targetTemperature, AIECOSwitch, ECO) to AWS IoT."""
        self._ensure_auth()
        topic = f"$aws/things/{device_id}/shadow/update"
        payload = json.dumps(
            {
                "state": {"desired": desired_changes},
                "clientToken": f"mobile_{int(time.time())}",
            }
        )
        self.iot_client.publish(topic=topic, qos=1, payload=payload)
        return {"status": "sent", "device_id": device_id, "changes": desired_changes}
