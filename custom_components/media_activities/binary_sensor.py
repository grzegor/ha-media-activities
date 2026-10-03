"""Readiness is separate from incomplete cleanup."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import MediaActivitiesEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MediaBinarySensor(entry, "verified_ready"), MediaBinarySensor(entry, "problem")])


class MediaBinarySensor(MediaActivitiesEntity, BinarySensorEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key)
        if key == "problem":
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        else:
            self._attr_icon = "mdi:check-circle-outline"

    @property
    def is_on(self):
        return bool(self.snapshot.get(self.key, False))
