"""Native flow behavior: useful short setup, stable subentries, no actuation."""

from copy import deepcopy
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResultType

from custom_components.media_activities.config_flow import (
    MediaActivitiesConfigFlow, EquipmentSubentryFlow, ActivitySubentryFlow,
    build_basic_activity, configuration_subentries, effective_config, store_config, validate_ownership,
)
from custom_components.media_activities.adapters import build_media_player_equipment
from custom_components.media_activities.schema import validate_config


def tv_config():
    equipment = build_media_player_equipment("media_player.television", {
        "supported_features": int(MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF | MediaPlayerEntityFeature.SELECT_SOURCE),
        "source_list": ["Live TV", "HDMI 1"],
    }, id="television", name="Television")
    activity = build_basic_activity([equipment], {"name": "Watch TV", "television__input": "Live TV"}, set())
    return validate_config({"name": "Living room", "equipment": [equipment], "activities": [activity]})


def make_entry(config, initialized=True):
    return ConfigEntry(
        domain="media_activities", title=config["name"], data={"config": config, "subentries_initialized": initialized},
        options={}, source="user", unique_id="system_one", version=1, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=configuration_subentries(config) if initialized else [],
    )


def make_flow(hass):
    flow = MediaActivitiesConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    return flow


@pytest.mark.asyncio
async def test_short_setup_generates_tv_without_outlet(hass):
    config = tv_config()
    hass.states.async_set("media_player.television", "off", {
        "friendly_name": "Television", "source_list": ["Live TV", "HDMI 1"],
        "supported_features": int(MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF | MediaPlayerEntityFeature.SELECT_SOURCE),
    })
    flow = make_flow(hass)
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as command:
        first = await flow.async_step_user({"name": "Living room", "advanced": False})
        assert first["step_id"] == "equipment"
        activity = await flow.async_step_equipment({"entities": ["media_player.television"]})
        assert activity["step_id"] == "activity"
        review = await flow.async_step_activity({"name": "Watch TV", "television__input": "Live TV", "add_another": False})
        assert review["step_id"] == "review"
        with patch.object(flow, "async_set_unique_id", new=AsyncMock()):
            result = await flow.async_step_review({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["config"]["observer_only"] is True
        assert len(result["subentries"]) == 2
        assert result["data"]["config"]["activities"] == config["activities"]
        assert not any(cap["kind"] == "supply" for cap in result["data"]["config"]["equipment"][0]["capabilities"])
        command.assert_not_awaited()


@pytest.mark.asyncio
async def test_advanced_import_enforces_observer_mode(hass):
    config = tv_config()
    config["observer_only"] = False
    flow = make_flow(hass)
    result = await flow.async_step_advanced({"configuration": config})
    assert result["step_id"] == "review"
    assert flow._config["observer_only"] is True
    assert config["observer_only"] is False


@pytest.mark.asyncio
async def test_invalid_input_and_missing_entity_return_form_errors(hass):
    flow = make_flow(hass)
    await flow.async_step_user({"name": "Test"})
    result = await flow.async_step_equipment({"entities": ["media_player.gone"]})
    assert result["errors"] == {"base": "entity_missing"}
    result = await flow.async_step_advanced({"configuration": {"name": "Bad", "version": 99}})
    assert result["errors"] == {"base": "invalid_configuration"}


@pytest.mark.asyncio
async def test_authoritative_subentries_prevent_deleted_activity_resurrection(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    activity = next(s for s in entry.subentries.values() if s.subentry_type == "activity")
    # The shared fixture loads the real registries used by public removal.
    hass.config_entries.async_remove_subentry(entry, activity.subentry_id)
    assert effective_config(entry)["activities"] == []
    assert len(entry.data["config"]["activities"]) == 1


@pytest.mark.asyncio
async def test_store_config_preserves_stable_subentry_ids(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    ids = {s.unique_id: s.subentry_id for s in entry.subentries.values()}
    config = effective_config(entry)
    config["equipment"][0]["name"] = "New TV name"
    store_config(hass, entry, config)
    assert ids == {s.unique_id: s.subentry_id for s in entry.subentries.values()}
    assert effective_config(entry)["equipment"][0]["name"] == "New TV name"


@pytest.mark.asyncio
async def test_cross_system_control_conflict(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    with pytest.raises(ValueError, match="controlled_resource_conflict"):
        validate_ownership(hass, tv_config())
    validate_ownership(hass, tv_config(), entry.entry_id)


def test_input_that_wakes_tv_does_not_add_unsupported_power():
    equipment = build_media_player_equipment("media_player.tv", {
        "supported_features": int(MediaPlayerEntityFeature.SELECT_SOURCE), "source_list": ["Live TV"],
    }, id="tv")
    activity = build_basic_activity([equipment], {"name": "TV", "tv__input": "Live TV"}, set())
    config = validate_config({"name": "TV", "equipment": [equipment], "activities": [activity]})
    assert len(config["activities"][0]["requirements"]) == 1
    assert config["activities"][0]["requirements"][0]["depends_on"] == []


@pytest.mark.asyncio
async def test_reconfigure_subentry_keeps_internal_id(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    subentry = next(s for s in entry.subentries.values() if s.subentry_type == "activity")
    flow = ActivitySubentryFlow()
    flow.hass = hass
    flow.handler = (entry.entry_id, "activity")
    flow.context = {"source": "reconfigure", "subentry_id": subentry.subentry_id}
    invalid = deepcopy(dict(subentry.data))
    invalid["id"] = "changed_id"
    result = await flow.async_step_reconfigure({"configuration": invalid})
    assert result["errors"]["base"] == "invalid_configuration"
    valid = deepcopy(dict(subentry.data))
    valid["name"] = "Television"
    result = await flow.async_step_reconfigure({"configuration": valid})
    assert result["type"] is FlowResultType.ABORT
    assert subentry.data["id"] == "watch_tv"
    assert subentry.data["name"] == "Television"


def item_flow(hass, entry, kind, cls):
    flow = cls()
    flow.hass = hass
    flow.handler = (entry.entry_id, kind)
    flow.context = {"source": "user"}
    return flow


@pytest.mark.asyncio
async def test_initial_forms_and_advanced_entry_path(hass):
    flow = make_flow(hass)
    form = await flow.async_step_user()
    assert form["step_id"] == "user"
    form = await flow.async_step_user({"name": "Cinema", "area_id": "cinema", "advanced": True})
    assert form["step_id"] == "advanced"
    assert flow._config["area_id"] == "cinema"
    assert not form.get("errors")


@pytest.mark.asyncio
async def test_multiple_basic_activities_and_invalid_input(hass):
    flow = make_flow(hass)
    flow._config = tv_config()
    result = await flow.async_step_activity({"name": "Games", "television__input": "invalid source"})
    assert result["errors"]["base"] == "invalid_configuration"
    assert len(flow._config["activities"]) == 1
    result = await flow.async_step_activity({"name": "Games", "television__input": "HDMI 1", "add_another": True})
    assert result["step_id"] == "activity"
    assert len(flow._config["activities"]) == 2
    assert [a["id"] for a in flow._config["activities"]] == ["watch_tv", "games"]


@pytest.mark.asyncio
async def test_no_equipment_or_no_supported_requirement_is_actionable(hass):
    flow = make_flow(hass)
    await flow.async_step_user({"name": "Test"})
    result = await flow.async_step_equipment({"entities": []})
    assert result["errors"]["base"] == "no_supported_requirements"
    hass.states.async_set("media_player.passive", "on", {"supported_features": 0})
    await flow.async_step_equipment({"entities": ["media_player.passive"]})
    result = await flow.async_step_activity({"name": "Watch"})
    assert result["errors"]["base"] == "no_supported_requirements"


@pytest.mark.asyncio
async def test_review_rechecks_ownership_changed_during_flow(hass):
    flow = make_flow(hass)
    flow._config = tv_config()
    competing = make_entry(tv_config())
    hass.config_entries._entries[competing.entry_id] = competing
    result = await flow.async_step_review({})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "controlled_resource_conflict"


@pytest.mark.asyncio
async def test_full_reconfigure_updates_same_entry_in_observer_mode(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    flow = make_flow(hass)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    assert (await flow.async_step_reconfigure())["step_id"] == "reconfigure"
    invalid = await flow.async_step_reconfigure({"configuration": {"name": "Bad", "version": 20}})
    assert invalid["errors"]["base"] == "invalid_configuration"
    config = tv_config()
    config["name"] = "Renamed system"
    config["observer_only"] = False
    result = await flow.async_step_reconfigure({"configuration": config})
    assert result["type"] is FlowResultType.ABORT
    assert len(hass.config_entries.async_entries("media_activities")) == 1
    assert entry.title == "Renamed system"
    assert effective_config(entry)["observer_only"] is True


@pytest.mark.asyncio
async def test_equipment_removal_leaves_invalid_config_editable(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    equipment = next(s for s in entry.subentries.values() if s.subentry_type == "equipment")
    hass.config_entries.async_remove_subentry(entry, equipment.subentry_id)
    raw = effective_config(entry)
    assert raw["equipment"] == []
    with pytest.raises(ValueError):
        validate_config(raw)
    flow = make_flow(hass)
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    assert (await flow.async_step_reconfigure())["step_id"] == "reconfigure"
    raw["activities"] = []
    result = await flow.async_step_reconfigure({"configuration": raw})
    assert result["type"] is FlowResultType.ABORT
    assert effective_config(entry)["activities"] == []


@pytest.mark.asyncio
async def test_equipment_subentry_convenience_and_advanced(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    flow = item_flow(hass, entry, "equipment", EquipmentSubentryFlow)
    assert (await flow.async_step_user())["step_id"] == "user"
    bad = await flow.async_step_user({"entity_id": "switch.gone"})
    assert bad["errors"]["base"] == "entity_missing"
    hass.states.async_set("switch.amp", "off", {"friendly_name": "Amplifier"})
    result = await flow.async_step_user({"entity_id": "switch.amp", "name": "Record amplifier"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["id"] == "amp"
    assert result["title"] == "Record amplifier"
    assert (await flow.async_step_user({"advanced": True}))["step_id"] == "advanced"
    invalid = await flow.async_step_advanced({"configuration": {"id": "broken", "capabilities": [{"id": "power"}]}})
    assert invalid["errors"]["base"] == "invalid_configuration"
    result = await flow.async_step_advanced({"configuration": {"id": "extra", "name": "Extra", "capabilities": []}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "equipment:extra"


@pytest.mark.asyncio
async def test_activity_subentry_convenience_and_advanced(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    flow = item_flow(hass, entry, "activity", ActivitySubentryFlow)
    assert (await flow.async_step_user())["step_id"] == "user"
    invalid = await flow.async_step_user({"name": "Game", "television__input": "missing"})
    assert invalid["errors"]["base"] == "invalid_configuration"
    result = await flow.async_step_user({"name": "Game", "television__input": "HDMI 1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == "activity:game"
    assert (await flow.async_step_user({"advanced": True}))["step_id"] == "advanced"


@pytest.mark.asyncio
async def test_store_import_adds_and_removes_subentries_without_resurrection(hass):
    entry = make_entry(tv_config())
    hass.config_entries._entries[entry.entry_id] = entry
    config = effective_config(entry)
    config["activities"] = [{**config["activities"][0], "id": "new_activity", "name": "New"}]
    config["equipment"].append({"id": "extra", "name": "Extra", "capabilities": []})
    store_config(hass, entry, config)
    assert {sub.unique_id for sub in entry.subentries.values()} == {"equipment:television", "equipment:extra", "activity:new_activity"}
    assert effective_config(entry)["activities"][0]["id"] == "new_activity"


def test_legacy_entry_without_subentries_and_identifier_disambiguation():
    config = tv_config()
    assert effective_config(make_entry(config, initialized=False)) == config
    equipment = config["equipment"]
    activity = build_basic_activity(equipment, {"name": "12 movies", "television__input": "Live TV"}, {"item_12_movies"})
    assert activity["id"] == "item_12_movies_2"
    activity = build_basic_activity(equipment, {"name": "Idle"}, set())
    assert activity["id"] == "idle_2"


@pytest.mark.asyncio
async def test_unknown_input_choices_use_free_text_and_no_power_dependency(hass):
    hass.states.async_set("media_player.network", "on", {"supported_features": int(MediaPlayerEntityFeature.SELECT_SOURCE)})
    flow = make_flow(hass)
    await flow.async_step_user({"name": "Network"})
    result = await flow.async_step_equipment({"entities": ["media_player.network"]})
    assert result["step_id"] == "activity"
    result = await flow.async_step_activity({"name": "App", "network__input": "My app"})
    assert result["step_id"] == "review"
    assert flow._config["activities"][0]["requirements"][0]["value"] == "My app"


def test_supported_subentry_types_are_native():
    assert MediaActivitiesConfigFlow.async_get_supported_subentry_types(None) == {
        "equipment": EquipmentSubentryFlow, "activity": ActivitySubentryFlow,
    }
