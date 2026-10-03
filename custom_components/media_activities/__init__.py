"""Reusable multimedia activities using existing Home Assistant entities."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers import area_registry as ar, device_registry as dr

from .const import DOMAIN, PLATFORMS
from .coordinator import MediaCoordinator, SERVICE_ACTIONS
from .schema import controlled_entities, validate_config

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def validate_ownership(
    hass: HomeAssistant, candidate: dict, excluding_entry: str | None = None
) -> None:
    """Prevent two systems from writing the same entity."""
    from .config_flow import effective_config

    resources = controlled_entities(candidate)
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == excluding_entry:
            continue
        shared = resources & controlled_entities(effective_config(other))
        if shared:
            raise ValueError("Control resources already owned: " + ", ".join(sorted(shared)))


async def async_setup(hass: HomeAssistant, config: Mapping[str, Any]) -> bool:
    hass.data.setdefault(DOMAIN, {"coordinators": {}, "in_flight": {}})
    _register_actions(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .config_flow import effective_config

    await async_setup(hass, {})
    try:
        config = validate_config(effective_config(entry))
        validate_ownership(hass, config, entry.entry_id)
    except (ValueError, KeyError, TypeError) as exc:
        from .repairs import async_update_issues

        async_update_issues(hass, entry, [], configuration_error=str(exc))
        raise ConfigEntryError(str(exc)) from exc
    coordinator = MediaCoordinator(hass, entry, config)
    entry.runtime_data = coordinator
    hass.data[DOMAIN]["coordinators"][entry.entry_id] = coordinator
    try:
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if area_id := config.get("area_id"):
            # The area is explicit configuration. Do not invent an area when
            # its registry entry was removed or rely on deprecated suggestions.
            if ar.async_get(hass).async_get_area(area_id):
                registry = dr.async_get(hass)
                device = registry.async_get_device_by_identifier(
                    (DOMAIN, entry.entry_id), entry.entry_id
                )
                if device and device.area_id != area_id:
                    registry.async_update_device(device.id, area_id=area_id)
    except Exception:
        await coordinator.async_stop()
        hass.data[DOMAIN]["coordinators"].pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_updated))
    return True


async def _updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator and coordinator._reconfiguring:
        return
    from .config_flow import effective_config

    try:
        candidate = validate_config(effective_config(entry))
        validate_ownership(hass, candidate, entry.entry_id)
    except (ValueError, KeyError, TypeError) as exc:
        if coordinator:
            await coordinator.async_suspend_configuration(str(exc))
        else:
            from .repairs import async_update_issues

            async_update_issues(hass, entry, [], configuration_error=str(exc))
        return
    if coordinator and candidate == coordinator.config:
        if coordinator._configuration_error:
            # Even restoration of the old configuration needs fresh startup
            # evidence; never resume previously suspended work in this callback.
            hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = entry.runtime_data
    # Stop planning before unloading frontend entities.
    await coordinator.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN]["coordinators"].pop(entry.entry_id, None)
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version != 1:
        return False
    try:
        validate_config(dict(entry.data["config"]))
    except (ValueError, KeyError, TypeError):
        return False
    return True


def _register_actions(hass: HomeAssistant) -> None:
    base = {vol.Required("config_entry_id"): str}
    schemas = {
        "request_activity": {
            **base, vol.Required("activity_id"): str,
            vol.Optional("origin", default="user"): str,
            vol.Optional("guards"): [dict],
        },
        "finish": base,
        "retry": base,
        "set_device_setting": {
            **base, vol.Required("equipment_id"): str,
            vol.Required("capability_id"): str, vol.Required("value"): object,
        },
        "cancel_requests": {**base, vol.Required("origin"): str},
        "apply_policy": {
            **base, vol.Required("policy_id"): str,
            vol.Optional("origin", default="policy"): str,
        },
        "export_configuration": base,
        "import_configuration": {**base, vol.Required("configuration"): dict},
    }
    for action in SERVICE_ACTIONS:
        if not hass.services.has_service(DOMAIN, action):
            if action in {"export_configuration", "import_configuration"}:
                async_register_admin_service(
                    hass, DOMAIN, action, _handler(hass, action),
                    schema=vol.Schema(schemas[action]),
                    supports_response=SupportsResponse.OPTIONAL,
                )
                continue
            hass.services.async_register(
                DOMAIN, action, _handler(hass, action),
                schema=vol.Schema(schemas[action]),
                supports_response=SupportsResponse.OPTIONAL,
            )


def _handler(hass: HomeAssistant, action: str):
    async def handle(call: ServiceCall) -> dict:
        data = call.data
        coordinator = hass.data[DOMAIN]["coordinators"].get(data["config_entry_id"])
        if coordinator is None:
            raise HomeAssistantError("Media system is not loaded")
        try:
            if action == "request_activity":
                return await coordinator.async_request(
                    data["activity_id"], data["origin"], data.get("guards")
                )
            if action == "finish":
                return await coordinator.async_finish()
            if action == "retry":
                return await coordinator.async_retry()
            if action == "set_device_setting":
                return await coordinator.async_set_setting(
                    data["equipment_id"], data["capability_id"], data["value"]
                )
            if action == "cancel_requests":
                return await coordinator.async_cancel_origin(data["origin"])
            if action == "apply_policy":
                return await coordinator.async_apply_policy(data["policy_id"], data["origin"])
            if action == "export_configuration":
                return await coordinator.async_export()
            return await coordinator.async_import(data["configuration"])
        except (ValueError, KeyError, TypeError) as exc:
            raise HomeAssistantError(str(exc)) from exc
    return handle
