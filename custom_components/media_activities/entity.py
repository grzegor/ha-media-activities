"""Entities presenting one coordinator, without commanding physical devices."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME, VERSION


class MediaActivitiesEntity(Entity):
    """Base for event-driven system entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self.entry = entry
        self.coordinator = entry.runtime_data
        self.key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=NAME,
            model="Activity coordinator",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def snapshot(self) -> dict[str, Any]:
        return self.coordinator.snapshot

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose only a compact public status, never full config or event logs."""
        snapshot = self.snapshot
        return {
            "request_id": snapshot.get("request_id"),
            "blockers": list(snapshot.get("blockers") or [])[:8],
            "observer_only": snapshot.get("observer_only", True),
        }
