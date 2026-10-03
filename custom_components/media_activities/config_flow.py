"""Native setup and subentry editors for independently coordinated systems."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigSubentry, ConfigSubentryFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .adapters import build_entity_equipment, build_media_player_equipment
from .const import DOMAIN
from .schema import controlled_entities, validate_config


def effective_config(entry: ConfigEntry) -> dict:
    """Read authoritative subentries; deletions must not resurrect old items."""
    config = deepcopy(dict(entry.data["config"]))
    if entry.data.get("subentries_initialized"):
        for kind, key in (("equipment", "equipment"), ("activity", "activities")):
            config[key] = [deepcopy(dict(item.data)) for item in entry.subentries.values() if item.subentry_type == kind]
    # A native subentry deletion can temporarily leave a dangling reference.
    # Keep that editable in Reconfigure; activation and saves validate it.
    return config


def configuration_subentries(config: dict) -> list[dict]:
    return [
        {"subentry_type": kind, "unique_id": f"{kind}:{item['id']}", "title": item["name"], "data": deepcopy(item)}
        for kind, key in (("equipment", "equipment"), ("activity", "activities"))
        for item in config.get(key, [])
    ]


@callback
def store_config(hass: HomeAssistant, entry: ConfigEntry, config: dict) -> None:
    """Synchronize subentries using public APIs without executing device actions.

    The runtime update listener coalesces these synchronous changes into one reload.
    Import callers suspend their coordinator before calling this function.
    """
    config = validate_config(config)
    desired = {item["unique_id"]: item for item in configuration_subentries(config)}
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type not in {"equipment", "activity"}:
            continue
        item = desired.pop(subentry.unique_id, None)
        if item is None:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        else:
            hass.config_entries.async_update_subentry(entry, subentry, data=item["data"], title=item["title"])
    for item in desired.values():
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            data=MappingProxyType(item["data"]), subentry_type=item["subentry_type"],
            title=item["title"], unique_id=item["unique_id"],
        ))
    hass.config_entries.async_update_entry(entry, title=config["name"], data={
        **entry.data, "config": config, "subentries_initialized": True,
    })


def validate_ownership(hass: HomeAssistant, config: dict, exclude_entry_id: str | None = None) -> None:
    """Two systems may observe the same entity, but may not both control it."""
    ours = controlled_entities(config)
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == exclude_entry_id:
            continue
        theirs = controlled_entities(effective_config(other))
        if ours & theirs:
            raise ValueError("controlled_resource_conflict")


def _item_id(name: str, used: set[str]) -> str:
    base = slugify(name) or "item"
    if not base[0].isalpha():
        base = f"item_{base}"
    result = base
    suffix = 2
    while result in used or result == "idle":
        result = f"{base}_{suffix}"
        suffix += 1
    return result


def _make_equipment(hass: HomeAssistant, entity_id: str, used: set[str], name: str | None = None) -> dict:
    state = hass.states.get(entity_id)
    if state is None:
        raise ValueError("entity_missing")
    name = name or state.attributes.get("friendly_name", entity_id)
    equipment_id = _item_id(entity_id.split(".", 1)[1], used)
    if entity_id.startswith("media_player."):
        return build_media_player_equipment(entity_id, dict(state.attributes), id=equipment_id, name=name)
    return build_entity_equipment(entity_id, attributes=dict(state.attributes), id=equipment_id, name=name)


def _activity_fields(equipment: list[dict]) -> dict:
    """Inputs are chosen in native selectors; power requirements are generated."""
    fields: dict = {vol.Required("name", default="Watch media"): selector.TextSelector()}
    for item in equipment:
        for cap in item.get("capabilities", []):
            if cap["kind"] not in {"input", "route"} or not cap.get("operations"):
                continue
            key = f"{item['id']}__{cap['id']}"
            choices = cap.get("allowed_values")
            if isinstance(choices, list):
                fields[vol.Optional(key)] = selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[{"value": str(value), "label": str(value)} for value in choices], mode=selector.SelectSelectorMode.DROPDOWN,
                ))
            else:
                fields[vol.Optional(key)] = selector.TextSelector()
    return fields


def build_basic_activity(equipment: list[dict], values: dict, used: set[str]) -> dict:
    """Generate one editable activity from supported power and chosen inputs."""
    requirements = []
    for item in equipment:
        power_req = None
        for cap in item.get("capabilities", []):
            field = f"{item['id']}__{cap['id']}"
            if cap["kind"] == "power" and any(op.get("value") is True or op.get("any_value") for op in cap.get("operations", [])):
                value = True
            elif field in values and cap["kind"] in {"input", "route"}:
                value = values[field]
            else:
                continue
            rid = f"{item['id']}_{cap['id']}"
            req = {"id": rid, "equipment": item["id"], "capability": cap["id"], "value": value,
                   "confirmation": "required" if cap.get("feedback_type") == "reported" else "best_effort"}
            if cap["kind"] == "power":
                power_req = rid
            requirements.append(req)
        if power_req:
            for req in requirements:
                if req["equipment"] == item["id"] and req["id"] != power_req:
                    req["depends_on"] = [power_req]
    if not requirements:
        raise ValueError("no_supported_requirements")
    return {"id": _item_id(values["name"], used), "name": values["name"], "requirements": requirements}


def _error_code(error: Exception) -> str:
    return str(error) if str(error) in {"controlled_resource_conflict", "entity_missing", "no_supported_requirements"} else "invalid_configuration"


class MediaActivitiesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a system without requiring a router, receiver, outlet or JSON."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self):
        self._config: dict[str, Any] = {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"equipment": EquipmentSubentryFlow, "activity": ActivitySubentryFlow}

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._config = {"version": 1, "name": user_input["name"], "observer_only": True,
                            "equipment": [], "activities": []}
            if user_input.get("area_id"):
                self._config["area_id"] = user_input["area_id"]
            if user_input.get("advanced", False):
                return await self.async_step_advanced()
            return await self.async_step_equipment()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("name"): selector.TextSelector(),
            vol.Optional("area_id"): selector.AreaSelector(),
            vol.Optional("advanced", default=False): selector.BooleanSelector(),
        }))

    async def async_step_equipment(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                equipment = []
                for entity_id in user_input["entities"]:
                    equipment.append(_make_equipment(self.hass, entity_id, {item["id"] for item in equipment}))
                if not equipment:
                    raise ValueError("no_supported_requirements")
                self._config["equipment"] = equipment
                validate_ownership(self.hass, validate_config(self._config))
                return await self.async_step_activity()
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="equipment", errors=errors, data_schema=vol.Schema({
            vol.Required("entities"): selector.EntitySelector(selector.EntitySelectorConfig(
                domain="media_player", multiple=True,
            )),
        }))

    async def async_step_activity(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                activity = build_basic_activity(self._config["equipment"], user_input, {a["id"] for a in self._config["activities"]})
                candidate = validate_config({**self._config, "activities": [*self._config["activities"], activity]})
                self._config = candidate
                if user_input.get("add_another"):
                    return await self.async_step_activity()
                return await self.async_step_review()
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        fields = _activity_fields(self._config["equipment"])
        fields[vol.Optional("add_another", default=False)] = selector.BooleanSelector()
        return self.async_show_form(step_id="activity", errors=errors, data_schema=vol.Schema(fields))

    async def async_step_advanced(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                candidate = deepcopy(user_input["configuration"])
                candidate["observer_only"] = True
                self._config = validate_config(candidate)
                validate_ownership(self.hass, self._config)
                return await self.async_step_review()
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="advanced", errors=errors, data_schema=vol.Schema({
            vol.Required("configuration", default=self._config): selector.ObjectSelector(),
        }))

    async def async_step_review(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._config = validate_config(self._config)
                validate_ownership(self.hass, self._config)
            except ValueError as err:
                errors["base"] = _error_code(err)
            else:
                await self.async_set_unique_id(str(uuid4()))
                return self.async_create_entry(title=self._config["name"], data={
                    "config": self._config, "subentries_initialized": True,
                }, subentries=configuration_subentries(self._config))
        return self.async_show_form(step_id="review", errors=errors, data_schema=vol.Schema({}),
            description_placeholders={"name": self._config["name"],
                "equipment": ", ".join(item["name"] for item in self._config["equipment"]) or "—",
                "activities": ", ".join(item["name"] for item in self._config["activities"]) or "—"}, last_step=True)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                config = validate_config(user_input["configuration"])
                validate_ownership(self.hass, config, entry.entry_id)
                config["observer_only"] = True
                store_config(self.hass, entry, config)
                return self.async_abort(reason="reconfigure_successful")
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="reconfigure", errors=errors, data_schema=vol.Schema({
            vol.Required("configuration", default=effective_config(entry)): selector.ObjectSelector(),
        }))


class _ItemSubentryFlow(ConfigSubentryFlow):
    """Validate a subentry against its complete system before accepting edits."""

    kind: str
    config_key: str

    def _validate_item(self, item, old_id=None):
        entry = self._get_entry()
        config = effective_config(entry)
        config[self.config_key] = [x for x in config[self.config_key] if x["id"] != old_id] + [item]
        config = validate_config(config)
        validate_ownership(self.hass, config, entry.entry_id)
        return next(x for x in config[self.config_key] if x["id"] == item["id"])

    async def async_step_advanced(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                item = self._validate_item(user_input["configuration"])
                return self.async_create_entry(title=item["name"], data=item, unique_id=f"{self.kind}:{item['id']}")
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="advanced", errors=errors, data_schema=vol.Schema({
            vol.Required("configuration"): selector.ObjectSelector(),
        }))

    async def async_step_reconfigure(self, user_input=None):
        subentry = self._get_reconfigure_subentry()
        errors = {}
        if user_input is not None:
            try:
                item = deepcopy(user_input["configuration"])
                if item.get("id") != subentry.data["id"]:
                    raise ValueError("Stable IDs cannot be changed")
                item = self._validate_item(item, subentry.data["id"])
                return self.async_update_and_abort(self._get_entry(), subentry, data=item, title=item["name"])
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="reconfigure", errors=errors, data_schema=vol.Schema({
            vol.Required("configuration", default=dict(subentry.data)): selector.ObjectSelector(),
        }))


class EquipmentSubentryFlow(_ItemSubentryFlow):
    kind = "equipment"
    config_key = "equipment"

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("advanced"):
                return await self.async_step_advanced()
            try:
                config = effective_config(self._get_entry())
                item = _make_equipment(self.hass, user_input["entity_id"], {x["id"] for x in config["equipment"]}, user_input.get("name"))
                item = self._validate_item(item)
                return self.async_create_entry(title=item["name"], data=item, unique_id=f"equipment:{item['id']}")
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Optional("entity_id"): selector.EntitySelector(selector.EntitySelectorConfig(domain=["media_player", "select", "switch", "number", "button"])),
            vol.Optional("name"): selector.TextSelector(),
            vol.Optional("advanced", default=False): selector.BooleanSelector(),
        }))


class ActivitySubentryFlow(_ItemSubentryFlow):
    kind = "activity"
    config_key = "activities"

    async def async_step_user(self, user_input=None):
        config = effective_config(self._get_entry())
        errors = {}
        if user_input is not None:
            if user_input.get("advanced"):
                return await self.async_step_advanced()
            try:
                item = build_basic_activity(config["equipment"], user_input, {x["id"] for x in config["activities"]})
                item = self._validate_item(item)
                return self.async_create_entry(title=item["name"], data=item, unique_id=f"activity:{item['id']}")
            except (ValueError, TypeError, KeyError) as err:
                errors["base"] = _error_code(err)
        fields = _activity_fields(config["equipment"])
        fields[vol.Optional("advanced", default=False)] = selector.BooleanSelector()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema(fields))
