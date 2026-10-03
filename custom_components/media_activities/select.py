"""Select an activity using its stable internal ID."""

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError

from .const import IDLE
from .entity import MediaActivitiesEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ActivitySelect(entry)])


class ActivitySelect(MediaActivitiesEntity, SelectEntity):
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry):
        super().__init__(entry, "activity")

    @property
    def _activities(self):
        return self.coordinator.config.get("activities", [])

    @property
    def _labels(self):
        names = [a["name"] for a in self._activities]
        return {
            a["id"]: (a["name"] if names.count(a["name"]) == 1 and a["name"] != "Idle" else f"{a['name']} ({a['id']})")
            for a in self._activities if a["id"] != IDLE
        }

    @property
    def options(self):
        return ["Idle", *self._labels.values()]

    @property
    def current_option(self):
        activity = self.snapshot.get("activity_id", IDLE)
        return "Idle" if activity in (None, IDLE) else self._labels.get(activity)

    async def async_select_option(self, option):
        if option == "Idle":
            await self.coordinator.async_finish()
            return
        activity = next((key for key, value in self._labels.items() if value == option), None)
        if activity is None:
            raise ServiceValidationError("Unknown activity option")
        await self.coordinator.async_request(activity, origin="user")
