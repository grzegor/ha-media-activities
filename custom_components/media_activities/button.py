"""Explicit bounded retry and atomic finish commands."""

from homeassistant.components.button import ButtonEntity

from .entity import MediaActivitiesEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MediaButton(entry, "retry"), MediaButton(entry, "finish")])


class MediaButton(MediaActivitiesEntity, ButtonEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key)
        self._attr_icon = "mdi:refresh" if key == "retry" else "mdi:stop-circle-outline"

    async def async_press(self):
        if self.key == "retry":
            await self.coordinator.async_retry()
        else:
            await self.coordinator.async_finish()
