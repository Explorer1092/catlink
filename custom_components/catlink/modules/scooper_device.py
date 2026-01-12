"Scooper device module for CatLink integration."

from collections import deque
import datetime
import re
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .device import Device

if TYPE_CHECKING:
    from .devices_coordinator import DevicesCoordinator
from ..const import _LOGGER, DOMAIN
from ..models.additional_cfg import AdditionalDeviceConfig


class ScooperDevice(Device):
    """Scooper device class for CatLink integration."""

    logs: list
    coordinator_logs = None
    _pet_cache: dict  # Cache for pet info extracted from logs

    def __init__(
        self,
        dat: dict,
        coordinator: "DevicesCoordinator",
        additional_config: AdditionalDeviceConfig = None,
    ) -> None:
        super().__init__(dat, coordinator, additional_config)
        self._litter_weight_during_day = deque(
            maxlen=self.additional_config.max_samples_litter or 24
        )
        self._error_logs = deque(maxlen=20)
        self.empty_litter_box_weight = self.additional_config.empty_weight or 0.0
        self._pet_cache = {}

    async def async_init(self) -> None:
        """Initialize the device."""
        await super().async_init()
        self.logs = []
        self.coordinator_logs = DataUpdateCoordinator(
            self.account.hass,
            _LOGGER,
            name=f"{DOMAIN}-{self.id}-logs",
            update_method=self.update_logs,
            update_interval=datetime.timedelta(minutes=1),
        )
        await self.coordinator_logs.async_refresh()

    @property
    def modes(self) -> dict:
        """Return the modes of the device."""
        return {
            "00": "auto",
            "01": "manual",
            "02": "time",
            "03": "empty",
        }

    @property
    def actions(self) -> dict:
        """Return the actions of the device."""
        return {
            "00": "pause",
            "01": "start",
        }

    @property
    def _last_log(self):
        log = {}
        if self.logs:
            log = self.logs[0] or {}
        return log

    @property
    def last_log(self) -> str:
        """Return the last log of the device."""
        log = self._last_log
        if not log:
            return None
        return f"{log.get('time')} {log.get('event')}"

    def _extract_pets_from_logs(self) -> dict:
        """Extract pet information from logs.

        Returns a dict: {petId: petName}
        """
        pets = {}
        for log in self.logs:
            pet_id = log.get("petId")
            event = log.get("event", "")
            if pet_id and pet_id not in pets:
                # Extract pet name from event string (e.g., "wifi peed" -> "wifi")
                match = re.match(r"^(\S+)\s+", event)
                if match:
                    pets[pet_id] = match.group(1)
                else:
                    pets[pet_id] = f"pet_{pet_id}"
        self._pet_cache = pets
        return pets

    def _get_hours_since_event(self, pet_id: str, event_type: str) -> int | None:
        """Calculate hours since last event for a specific pet.

        Args:
            pet_id: The pet ID to filter by
            event_type: 'pee' or 'poop' to match against event field

        Returns:
            Hours since last event, or None if no matching event found
        """
        event_pattern = "peed" if event_type == "pee" else "pooped"

        # Use device timezone
        device_tz_id = self.data.get("timezoneId", "Asia/Shanghai")
        try:
            device_tz = ZoneInfo(device_tz_id)
        except Exception:
            device_tz = ZoneInfo("Asia/Shanghai")

        now = datetime.datetime.now(device_tz)

        for log in self.logs:
            if log.get("petId") == pet_id and event_pattern in log.get("event", ""):
                time_str = log.get("time", "")
                try:
                    # Parse time string (format: "YYYY-MM-DD HH:MM")
                    event_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                    event_time = event_time.replace(tzinfo=device_tz)
                    delta = now - event_time
                    return int(delta.total_seconds() / 3600)
                except (ValueError, TypeError) as exc:
                    _LOGGER.debug("Failed to parse time %s: %s", time_str, exc)
                    continue
        return None

    def _get_pet_sensor_value(self, pet_id: str, event_type: str) -> str:
        """Get sensor value for pet hours since event."""
        hours = self._get_hours_since_event(pet_id, event_type)
        if hours is None:
            return "unknown"
        return str(hours)

    @property
    def state(self) -> str:
        """Return the device state."""
        try:
            sta = self.detail.get("workStatus", "")
            dic = {
                "00": "idle",
                "01": "running",
                "02": "need_reset",
            }
            return dic.get(f"{sta}".strip(), sta)
        except Exception as exc:
            _LOGGER.error("Get device state failed: %s", exc)
            return "unknown"

    @property
    def litter_weight(self) -> float:
        """Return the litter weight."""
        litter_weight = 0
        try:
            catLitterWeight = self.detail.get(
                "catLitterWeight", self.empty_litter_box_weight
            )
            litter_weight = catLitterWeight - self.empty_litter_box_weight
            self._litter_weight_during_day.append(litter_weight)

        except Exception as exc:
            _LOGGER.error("Got litter weight failed: %s", exc)

        return litter_weight

    @property
    def litter_remaining_days(self) -> str:
        """Return the litter remaining days."""
        try:
            return self.detail.get("litterCountdown")
        except Exception as exc:
            _LOGGER.error("Get litter remaining days failed: %s", exc)
            return "unknown"

    @property
    def total_clean_time(self) -> int:
        """Return the total clean time."""
        try:
            return int(self.detail.get("inductionTimes", 0)) + int(
                self.detail.get("manualTimes", 0)
            )
        except Exception as exc:
            _LOGGER.error("Get total clean time failed: %s", exc)
            return 0

    @property
    def manual_clean_time(self) -> int:
        """Return the manual clean time."""
        try:
            return int(self.detail.get("manualTimes", 0))
        except Exception as exc:
            _LOGGER.error("Get manual clean time failed: %s", exc)
            return 0

    @property
    def deodorant_countdown(self) -> int:
        """Return the deodorant countdown."""
        try:
            return int(self.detail.get("deodorantCountdown", 0))
        except Exception as exc:
            _LOGGER.error("Get deodorant countdown failed: %s", exc)
            return 0

    @property
    def occupied(self) -> bool:
        """Return the occupied status."""
        # based on _litter_weight_during_day to determine if the litter box is occupied
        # check whether value is increasing at any point in the day
        # Now we can check which cat is using the litter box :)
        try:
            return any(
                self._litter_weight_during_day[i]
                < self._litter_weight_during_day[i + 1]
                for i in range(len(self._litter_weight_during_day) - 1)
            )
        except IndexError:
            return False

    @property
    def online(self) -> bool:
        """Return the online status."""
        try:
            return self.detail.get("online")
        except Exception as exc:
            _LOGGER.error("Get online status failed: %s", exc)
            return False

    @property
    def temperature(self) -> str:
        """Return the temperature."""
        return self.detail.get("temperature", "-")

    @property
    def humidity(self) -> str:
        """Return the humidity."""
        return self.detail.get("humidity", "-")

    @property
    def error(self) -> str:
        """Return the device error."""
        try:
            error = self.detail.get("currentMessage") or self.data.get(
                "currentErrorMessage", ""
            )
            if error and error.lower() != "device online":
                self._error_logs.append(
                    {
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "error": error,
                    }
                )
            return error
        except Exception as exc:
            _LOGGER.error("Get device error failed: %s", exc)
            return "unknown"

    @property
    def hass_sensor(self) -> dict:
        """Return the hass sensor of the device."""
        sensors = {
            "state": {
                "icon": "mdi:information",
                "state_attrs": self.state_attrs,
            },
            "last_log": {
                "icon": "mdi:message",
                "state_attrs": self.last_log_attrs,
            },
            "litter_weight": {
                "icon": "mdi:weight",
            },
            "litter_remaining_days": {
                "icon": "mdi:calendar",
            },
            "total_clean_time": {
                "icon": "mdi:timer",
            },
            "manual_clean_time": {
                "icon": "mdi:timer",
            },
            "deodorant_countdown": {
                "icon": "mdi:timer",
            },
            "occupied": {
                "icon": "mdi:cat",
            },
            "online": {
                "icon": "mdi:cloud",
            },
            "temperature": {
                "icon": "mdi:temperature-celsius",
                "state": self.temperature,
                "class": SensorDeviceClass.TEMPERATURE,
                "unit": UnitOfTemperature.CELSIUS,
                "state_class": SensorStateClass.MEASUREMENT,
            },
            "humidity": {
                "icon": "mdi:water-percent",
                "state": self.humidity,
                "class": SensorDeviceClass.HUMIDITY,
                "unit": PERCENTAGE,
                "state_class": SensorStateClass.MEASUREMENT,
            },
            "error": {
                "icon": "mdi:alert-circle",
                "state_attrs": self.error_attrs,
            },
        }

        # Add dynamic pet sensors based on logs
        pets = self._extract_pets_from_logs()
        for pet_id, pet_name in pets.items():
            # Hours since last pee
            sensors[f"{pet_name}_hours_since_pee"] = {
                "icon": "mdi:water",
                "name": f"{pet_name} 未排尿",
                "state": lambda pid=pet_id: self._get_pet_sensor_value(pid, "pee"),
                "unit": UnitOfTime.HOURS,
                "state_class": SensorStateClass.MEASUREMENT,
            }
            # Hours since last poop
            sensors[f"{pet_name}_hours_since_poop"] = {
                "icon": "mdi:emoticon-poop",
                "name": f"{pet_name} 未排便",
                "state": lambda pid=pet_id: self._get_pet_sensor_value(pid, "poop"),
                "unit": UnitOfTime.HOURS,
                "state_class": SensorStateClass.MEASUREMENT,
            }

        return sensors

    def last_log_attrs(self) -> dict:
        """Return the last log attributes of the device."""
        log = self._last_log
        return {
            **log,
            "logs": self.logs,
        }

    async def update_logs(self) -> list:
        """Update device logs for the last 7 days."""
        api = "token/device/scooper/stats/log/list"
        all_logs = []

        # Use device timezone for date calculation
        device_tz_id = self.data.get("timezoneId", "Asia/Shanghai")
        try:
            device_tz = ZoneInfo(device_tz_id)
        except Exception:
            device_tz = ZoneInfo("Asia/Shanghai")
        now_in_device_tz = datetime.datetime.now(device_tz)

        # Query logs for the last 7 days
        for days_ago in range(7):
            query_date = (now_in_device_tz - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")
            pms = {
                "deviceId": self.id,
                "date": query_date,
            }
            try:
                rsp = await self.account.request(api, pms)
                rows = rsp.get("data", {}).get("scooperLogs", {}).get("rows") or []
                # Add date to each log entry since API only returns time (HH:MM)
                for log in rows:
                    if "time" in log and len(log["time"]) <= 5:
                        log["time"] = f"{query_date} {log['time']}"
                all_logs.extend(rows)
            except (TypeError, ValueError) as exc:
                _LOGGER.error("Got device logs for %s on %s failed: %s", self.name, query_date, exc)

        if not all_logs:
            _LOGGER.warning("Got device logs for %s failed: no logs found", self.name)

        # Sort by time descending (newest first)
        all_logs.sort(key=lambda x: x.get("time", ""), reverse=True)
        self.logs = all_logs
        self._handle_listeners()
        return all_logs

    def state_attrs(self) -> dict:
        """Return the state attributes."""
        return {
            "mac": self.mac,
            "work_status": self.detail.get("workStatus"),
            "alarm_status": self.detail.get("alarmStatus"),
            "weight": self.detail.get("weight"),
            "litter_weight_kg": self.detail.get("catLitterWeight"),
            "total_clean_times": int(self.detail.get("inductionTimes", 0))
            + int(self.detail.get("manualTimes", 0)),
            "manual_clean_times": self.detail.get("manualTimes"),
            "key_lock": self.detail.get("keyLock"),
            "safe_time": self.detail.get("safeTime"),
            "pave_second": self.detail.get("catLitterPaveSecond"),
            "deodorant_countdown": self.detail.get("deodorantCountdown"),
            "litter_countdown": self.detail.get("litterCountdown"),
        }

    def error_attrs(self) -> dict:
        """Return the error attributes."""
        return {
            "error_logs": list(self._error_logs),
        }
