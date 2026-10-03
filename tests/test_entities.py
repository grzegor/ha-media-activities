"""Native entity actions delegate once; status never overstates readiness."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.media_activities.select import ActivitySelect
from custom_components.media_activities.switch import PolicySwitch
from custom_components.media_activities.sensor import StatusSensor
from custom_components.media_activities.binary_sensor import MediaBinarySensor
from custom_components.media_activities.button import MediaButton
from custom_components.media_activities.diagnostics import redact_diagnostics
from custom_components.media_activities.diagnostics import async_get_config_entry_diagnostics
from custom_components.media_activities.repairs import async_update_issues, async_create_fix_flow
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry


def entry():
    coordinator = SimpleNamespace(
        config={"activities": [{"id": "tv", "name": "Watch TV"}]},
        snapshot={"activity_id": "tv", "phase": "ready", "cleanup_status": "blocked", "verified_ready": True,
                  "problem": True, "preserve_devices": False, "observer_only": True, "blockers": []},
        async_request=AsyncMock(), async_finish=AsyncMock(), async_retry=AsyncMock(),
        async_set_preserve=AsyncMock(), async_set_observer=AsyncMock(),
    )
    return SimpleNamespace(entry_id="test_system", title="Living room", runtime_data=coordinator)


@pytest.mark.asyncio
async def test_select_and_finish_dispatch_once():
    config_entry = entry()
    entity = ActivitySelect(config_entry)
    assert entity.unique_id == "test_system_activity"
    assert entity.options == ["Idle", "Watch TV"]
    assert entity.current_option == "Watch TV"
    await entity.async_select_option("Watch TV")
    config_entry.runtime_data.async_request.assert_awaited_once_with("tv", origin="user")
    await entity.async_select_option("Idle")
    config_entry.runtime_data.async_finish.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_policy_switches_use_separate_coordinator_actions():
    config_entry = entry()
    preserve = PolicySwitch(config_entry, "preserve_devices")
    observer = PolicySwitch(config_entry, "observer_only")
    assert preserve.is_on is False
    assert observer.is_on is True
    await preserve.async_turn_on()
    await observer.async_turn_off()
    config_entry.runtime_data.async_set_preserve.assert_awaited_once_with(True)
    config_entry.runtime_data.async_set_observer.assert_awaited_once_with(False)


def test_cleanup_failure_does_not_replace_preparation_status():
    config_entry = entry()
    assert StatusSensor(config_entry, "status").native_value == "ready"
    assert StatusSensor(config_entry, "cleanup_status").native_value == "blocked"
    assert MediaBinarySensor(config_entry, "verified_ready").is_on is True
    assert MediaBinarySensor(config_entry, "problem").is_on is True


@pytest.mark.asyncio
async def test_explicit_retry_and_finish_buttons():
    config_entry = entry()
    await MediaButton(config_entry, "retry").async_press()
    await MediaButton(config_entry, "finish").async_press()
    config_entry.runtime_data.async_retry.assert_awaited_once_with()
    config_entry.runtime_data.async_finish.assert_awaited_once_with()


def test_duplicate_activity_names_remain_selectable():
    config_entry = entry()
    config_entry.runtime_data.config["activities"].append({"id": "tv2", "name": "Watch TV"})
    assert ActivitySelect(config_entry).options == ["Idle", "Watch TV (tv)", "Watch TV (tv2)"]


def test_diagnostics_redact_nested_credentials_sources_and_action_payloads():
    raw = {"name": "Private room", "events": [{"reason": "waiting_for_feedback", "desired": "Private app"}],
           "operations": [{"action": "media_player.play_media", "data": {"token": "secret", "media_id": "https://private/file"}}],
           "access_token": "secret", "source_list": ["Private source"], "value": 22, "url": "https://host"}
    redacted = redact_diagnostics(raw)
    assert redacted["events"][0]["reason"] == "waiting_for_feedback"
    assert redacted["events"][0]["desired"] == "**REDACTED**"
    assert redacted["operations"][0]["data"] == "**REDACTED**"
    assert redacted["access_token"] == "**REDACTED**"
    assert redacted["value"] == 22
    assert "Private" not in str(redacted)


@pytest.mark.asyncio
async def test_unknown_activity_rejected_without_dispatch():
    config_entry = entry()
    entity = ActivitySelect(config_entry)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("Not configured")
    config_entry.runtime_data.async_request.assert_not_awaited()
    config_entry.runtime_data.snapshot["activity_id"] = "idle"
    assert entity.current_option == "Idle"
    config_entry.runtime_data.snapshot["activity_id"] = "removed"
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_entity_listener_registration_and_removal():
    config_entry = entry()
    callbacks = []
    unsubscribe = Mock()
    def listen(callback):
        callbacks.append(callback)
        return unsubscribe
    config_entry.runtime_data.async_add_listener = listen
    entity = ActivitySelect(config_entry)
    await entity.async_added_to_hass()
    assert len(callbacks) == 1
    with patch.object(entity, "async_write_ha_state") as write:
        callbacks[0]()
        write.assert_called_once_with()
    assert entity._on_remove == [unsubscribe]
    for remove in entity._on_remove:
        remove()
    unsubscribe.assert_called_once_with()


@pytest.mark.asyncio
async def test_all_native_platforms_add_required_entities():
    from custom_components.media_activities import select, switch, sensor, binary_sensor, button
    entities = []
    config_entry = entry()
    for platform in (select, switch, sensor, binary_sensor, button):
        await platform.async_setup_entry(None, config_entry, entities.extend)
    assert len(entities) == 10
    assert len({entity.unique_id for entity in entities}) == 10
    assert {entity.key for entity in entities} == {
        "activity", "preserve_devices", "observer_only", "status", "observed_activity",
        "cleanup_status", "verified_ready", "problem", "retry", "finish",
    }


def test_status_attributes_bounded_and_observed_names_friendly():
    config_entry = entry()
    config_entry.runtime_data.snapshot.update(
        request_id="request_one", blockers=[{"reason": "waiting_for_feedback"} for _ in range(20)], observed_activity="tv")
    entity = StatusSensor(config_entry, "observed_activity")
    assert entity.native_value == "Watch TV"
    assert len(entity.extra_state_attributes["blockers"]) == 8
    assert entity.extra_state_attributes["request_id"] == "request_one"
    config_entry.runtime_data.snapshot["observed_activity"] = "idle"
    assert entity.native_value == "Idle"
    config_entry.runtime_data.snapshot.update(mode="policy", policy_id="standby", suspended=True, policy_ready=True)
    attributes = StatusSensor(config_entry, "status").extra_state_attributes
    assert attributes["mode"] == "policy"
    assert attributes["policy_id"] == "standby"
    assert attributes["suspended"] is True
    assert attributes["policy_ready"] is True
    assert "events" not in attributes


@pytest.mark.asyncio
async def test_switches_both_directions_and_safe_default():
    config_entry = entry()
    await PolicySwitch(config_entry, "preserve_devices").async_turn_off()
    await PolicySwitch(config_entry, "observer_only").async_turn_on()
    config_entry.runtime_data.async_set_preserve.assert_awaited_once_with(False)
    config_entry.runtime_data.async_set_observer.assert_awaited_once_with(True)
    config_entry.runtime_data.snapshot.clear()
    assert PolicySwitch(config_entry, "observer_only").is_on is True
    assert MediaBinarySensor(config_entry, "verified_ready").is_on is False


@pytest.mark.asyncio
async def test_diagnostics_loaded_unloaded_and_extended_history():
    config_entry = entry()
    result = await async_get_config_entry_diagnostics(None, config_entry)
    assert "snapshot" in result
    config_entry.runtime_data.async_diagnostics = AsyncMock(return_value={
        "snapshot": {"events": [{"reason": "waiting_for_feedback", "token": "private"}]}})
    result = await async_get_config_entry_diagnostics(None, config_entry)
    assert result["snapshot"]["events"][0]["token"] == "**REDACTED**"
    unloaded = SimpleNamespace(data={"config": {"name": "Private home"}})
    result = await async_get_config_entry_diagnostics(None, unloaded)
    assert result == {"configuration": {"name": "**REDACTED**"}, "loaded": False}
    config_entry.runtime_data.async_diagnostics = lambda: {"url": "https://private/path"}
    assert (await async_get_config_entry_diagnostics(None, config_entry))["url"] == "**REDACTED**"


def test_diagnostics_unknown_objects_and_embedded_urls():
    assert redact_diagnostics({"x": "go to https://example.test/private"})["x"] == "**REDACTED**"
    assert redact_diagnostics({"x": object()})["x"] == "object"
    assert redact_diagnostics({"expected": ["private"]})["expected"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_repairs_appear_only_for_persistent_configuration_errors(hass):
    config_entry = entry()
    registry = issue_registry.async_get(hass)
    async_update_issues(hass, config_entry, ["media_player.missing"], "Invalid capability reference")
    assert registry.async_get_issue("media_activities", "test_system_entity_missing")
    assert registry.async_get_issue("media_activities", "test_system_invalid_configuration")
    async_update_issues(hass, config_entry, [], None)
    assert registry.async_get_issue("media_activities", "test_system_entity_missing") is None
    assert registry.async_get_issue("media_activities", "test_system_invalid_configuration") is None


@pytest.mark.asyncio
async def test_repair_for_removed_system_aborts(hass):
    flow = await async_create_fix_flow(hass, "missing", {"entry_id": "missing"})
    flow.hass = hass
    flow.context = {"source": "user"}
    result = await flow.async_step_init()
    assert result["reason"] == "entry_removed"
