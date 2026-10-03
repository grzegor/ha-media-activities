"""Compact operational and observed state."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .const import CLEANUP_STATES, PHASES
from .entity import MediaActivitiesEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StatusSensor(entry, key) for key in ("status", "observed_activity", "cleanup_status")])


class StatusSensor(MediaActivitiesEntity, SensorEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key)
        if key in ("status", "cleanup_status"):
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = PHASES if key == "status" else CLEANUP_STATES

    @property
    def native_value(self):
        value = self.snapshot.get("phase" if self.key == "status" else self.key)
        if self.key == "observed_activity":
            if value == "idle":
                return "Idle"
            return next((item["name"] for item in self.coordinator.config.get("activities", []) if item["id"] == value), value)
        return value

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        if self.key == "status":
            attributes.update({key: self.snapshot.get(key) for key in (
                "activity_id", "mode", "policy_id", "suspended", "policy_ready",
            )})
        return attributes
