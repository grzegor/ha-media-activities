"""Explicit preservation and observer-only controls."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import MediaActivitiesEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PolicySwitch(entry, "preserve_devices"), PolicySwitch(entry, "observer_only")])


class PolicySwitch(MediaActivitiesEntity, SwitchEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key)
        self._attr_icon = "mdi:hand-back-right-outline" if key == "preserve_devices" else "mdi:eye-outline"
        if key == "observer_only":
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        return bool(self.snapshot.get(self.key, self.key == "observer_only"))

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)

    async def _set(self, value):
        if self.key == "preserve_devices":
            await self.coordinator.async_set_preserve(value)
        else:
            await self.coordinator.async_set_observer(value)
