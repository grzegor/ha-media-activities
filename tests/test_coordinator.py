"""HA transport tests use actual event/service registries, no devices."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace, MappingProxyType
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
import shutil

import pytest
from homeassistant.core import callback, Event
from homeassistant.exceptions import HomeAssistantError, ConfigEntryError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import area_registry, device_registry, issue_registry

from custom_components.media_activities.coordinator import MediaCoordinator, replace_entity
from custom_components.media_activities import _register_actions, _updated, async_setup_entry, async_unload_entry, async_migrate_entry, validate_ownership


def tv_config(observer=False):
    return {
        'version': 1, 'name': 'Test room', 'observer_only': observer,
        'equipment': [{'id': 'tv', 'capabilities': [{
            'id': 'input', 'kind': 'input', 'value_type': 'string',
            'feedback_type': 'reported',
            'observation': {'entity_id': 'media_player.tv', 'attribute': 'source'},
            'operations': [{'any_value': True, 'action': 'media_player.select_source',
                            'target': {'entity_id': 'media_player.tv'}, 'data': {},
                            'value_field': 'source', 'idempotent': True}],
        }]}],
        'activities': [{'id': 'watch', 'name': 'Watch TV', 'requirements': [
            {'id': 'source', 'equipment': 'tv', 'capability': 'input',
             'value': 'HDMI 2', 'confirmation': 'required'}]}],
    }


async def start(hass, config=None, entry_id='test_room'):
    hass.data.setdefault('media_activities', {'coordinators': {}, 'in_flight': {}})
    entry = SimpleNamespace(entry_id=entry_id, title=(config or tv_config())["name"])
    coordinator = MediaCoordinator(hass, entry, config or tv_config())
    entry.runtime_data = coordinator
    hass.data['media_activities']['coordinators'][entry_id] = coordinator
    await coordinator.async_start()
    return coordinator


async def settle(hass):
    for _ in range(4):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_real_service_dispatch_and_confirmation(hass):
    calls = []
    @callback
    def select(call):
        calls.append(dict(call.data))
        hass.states.async_set('media_player.tv', 'on', {'source': call.data['source']})
    hass.services.async_register('media_player', 'select_source', select)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    result = await coordinator.async_request('watch')
    await settle(hass)
    assert result['accepted']
    assert len(calls) == 1
    assert calls[0]['source'] == 'HDMI 2'
    assert coordinator.snapshot['verified_ready']
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_observer_never_calls_services(hass):
    calls = []
    hass.services.async_register('media_player', 'select_source', lambda call: calls.append(call))
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass, tv_config(True))
    result = await coordinator.async_request('watch')
    await settle(hass)
    assert result['observer_only']
    assert not calls
    assert coordinator.snapshot['events']
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_service_actions_submit_and_export(hass):
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 2'})
    coordinator = await start(hass, tv_config(True))
    _register_actions(hass)
    result = await hass.services.async_call('media_activities', 'request_activity',
        {'config_entry_id': 'test_room', 'activity_id': 'watch'},
        blocking=True, return_response=True)
    assert result['accepted']
    exported = await hass.services.async_call('media_activities', 'export_configuration',
        {'config_entry_id': 'test_room'}, blocking=True, return_response=True)
    assert exported['configuration']['name'] == 'Test room'
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call('media_activities', 'finish',
            {'config_entry_id': 'missing'}, blocking=True, return_response=True)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_request('missing')
    await coordinator.async_stop()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_retry()


@pytest.mark.asyncio
async def test_configuration_actions_require_admin_for_user_calls(hass):
    from unittest.mock import AsyncMock
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    coordinator = await start(hass, tv_config(True))
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
    _register_actions(hass)
    for action, extra in [("export_configuration", {}), ("import_configuration", {"configuration": tv_config()})]:
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                'media_activities', action, {'config_entry_id': 'test_room', **extra},
                blocking=True, return_response=True, context=Context(user_id='ordinary_user'),
            )
    assert coordinator.snapshot['observer_only']
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_unload_does_not_cancel_submitted_device_command(hass):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def select(call):
        entered.set()
        await release.wait()
    hass.services.async_register('media_player', 'select_source', select)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    await coordinator.async_request('watch')
    await asyncio.wait_for(entered.wait(), 1)
    await coordinator.async_stop()
    assert coordinator._tasks
    assert all(not task.cancelled() for task in coordinator._tasks)
    assert hass.data['media_activities']['in_flight']
    release.set()
    await settle(hass)
    assert not hass.data['media_activities']['in_flight']


@pytest.mark.asyncio
async def test_submitted_command_errors_are_redacted(hass):
    async def select(call):
        raise RuntimeError('secret-customer-token')
    hass.services.async_register('media_player', 'select_source', select)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    await coordinator.async_request('watch')
    await settle(hass)
    assert 'secret-customer-token' not in str(coordinator.snapshot)
    assert 'RuntimeError' in str(coordinator.snapshot)
    await coordinator.async_stop()


def test_registry_rename_rewrites_only_exact_references():
    before = {'target': {'entity_id': ['media_player.old']},
              'description': 'Look at media_player.old for notes'}
    after = replace_entity(before, 'media_player.old', 'media_player.new')
    assert after['target']['entity_id'] == ['media_player.new']
    assert after['description'] == before['description']


@pytest.mark.asyncio
async def test_invalid_live_configuration_suspends_control_and_retains_repair(hass):
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    broken = tv_config()
    broken['activities'][0]['requirements'][0]['capability'] = 'missing'
    with patch('custom_components.media_activities.config_flow.effective_config', return_value=broken):
        await _updated(hass, coordinator.entry)
    assert coordinator.snapshot['observer_only'] is True
    assert coordinator.snapshot['phase'] == 'blocked'
    assert coordinator.snapshot['problem'] is True
    with pytest.raises(HomeAssistantError, match='configuration needs repair'):
        await coordinator.async_set_observer(False)
    issue_id = 'test_room_invalid_configuration'
    assert issue_registry.async_get(hass).async_get_issue('media_activities', issue_id)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 2'})
    await settle(hass)
    assert issue_registry.async_get(hass).async_get_issue('media_activities', issue_id)
    assert coordinator.snapshot['observer_only'] is True
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_repaired_configuration_schedules_fresh_reload(hass):
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    await coordinator.async_suspend_configuration('Invalid reference')
    with patch('custom_components.media_activities.config_flow.effective_config', return_value=coordinator.config), \
         patch.object(type(hass.config_entries), 'async_schedule_reload') as reload:
        await _updated(hass, coordinator.entry)
        reload.assert_called_once_with('test_room')
    assert coordinator.snapshot['observer_only'] is True
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_area_assignment_uses_real_device_and_area_registries(hass):
    config = tv_config(True)
    area = area_registry.async_get(hass).async_get_or_create('Cinema')
    config['area_id'] = area.id
    config_entry = ConfigEntry(
        domain='media_activities', title='Cinema', data={'config': config}, options={},
        source='user', unique_id='cinema', version=1, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=[],
    )
    hass.config_entries._entries[config_entry.entry_id] = config_entry
    hass.states.async_set('media_player.tv', 'off', {'source': 'HDMI 1'})
    registry = device_registry.async_get(hass)
    async def add_platforms(entry, platforms):
        registry.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=None,
            identifiers={('media_activities', entry.entry_id)}, name='Cinema')
    with patch.object(type(hass.config_entries), 'async_forward_entry_setups', new=AsyncMock(side_effect=add_platforms)):
        assert await async_setup_entry(hass, config_entry)
    device = registry.async_get_device_by_identifier(('media_activities', config_entry.entry_id), config_entry.entry_id)
    assert device.area_id == area.id
    with patch.object(type(hass.config_entries), 'async_unload_platforms', new=AsyncMock(return_value=True)):
        assert await async_unload_entry(hass, config_entry)
    assert config_entry.entry_id not in hass.data['media_activities']['coordinators']


@pytest.mark.asyncio
async def test_missing_entity_repair_clears_after_entity_returns(hass):
    coordinator = await start(hass, tv_config(True))
    registry = issue_registry.async_get(hass)
    assert registry.async_get_issue('media_activities', 'test_room_entity_missing')
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    await settle(hass)
    assert registry.async_get_issue('media_activities', 'test_room_entity_missing') is None
    await coordinator.async_stop()


def registered_entry(hass, config):
    entry = ConfigEntry(domain='media_activities', title=config['name'], data={'config': config}, options={},
        source='user', unique_id='registered', version=1, minor_version=1,
        discovery_keys=MappingProxyType({}), subentries_data=[])
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


async def registered_start(hass, config):
    entry = registered_entry(hass, config)
    hass.data.setdefault('media_activities', {'coordinators': {}, 'in_flight': {}})
    coordinator = MediaCoordinator(hass, entry, config)
    entry.runtime_data = coordinator
    hass.data['media_activities']['coordinators'][entry.entry_id] = coordinator
    await coordinator.async_start()
    return coordinator


@pytest.mark.asyncio
async def test_native_service_settings_policy_cancel_and_finish(hass):
    config = tv_config(True)
    config['policies'] = [{'id': 'standby', 'name': 'Standby', 'requirements': []}]
    coordinator = await registered_start(hass, config)
    _register_actions(hass)
    _register_actions(hass)  # Reload does not replace existing service handlers.
    base = {'config_entry_id': coordinator.entry.entry_id}
    async def call(action, **data):
        return await hass.services.async_call('media_activities', action, base | data, blocking=True, return_response=True)
    await call('request_activity', activity_id='watch')
    assert (await call('set_device_setting', equipment_id='tv', capability_id='input', value='HDMI 3'))['accepted']
    assert (await call('retry'))['accepted']
    assert (await call('apply_policy', policy_id='standby', origin='trip'))['accepted']
    assert (await call('cancel_requests', origin='trip'))['accepted']
    await coordinator.async_set_preserve(True)
    assert (await call('finish'))['accepted']
    assert coordinator.snapshot['preserve_devices'] is False
    assert coordinator.snapshot['activity_id'] == 'idle'
    diagnostics = await coordinator.async_diagnostics()
    assert diagnostics['runtime']['pending_action_count'] == 0
    assert diagnostics['snapshot']['events']
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_observer_switch_persists_configuration_without_resuming_intent(hass):
    coordinator = await registered_start(hass, tv_config(True))
    await coordinator.async_request('watch')
    await coordinator.async_set_observer(False)
    await settle(hass)
    assert coordinator.entry.data['config']['observer_only'] is False
    assert coordinator.snapshot['phase'] == 'recovery_paused'
    assert not coordinator._reconfiguring
    assert not coordinator._tasks
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_configuration_import_is_observer_only_and_waits_for_submitted_actions(hass):
    coordinator = await registered_start(hass, tv_config(True))
    candidate = tv_config(False)
    candidate['name'] = 'Imported system'
    marker = Mock()
    coordinator._tasks.add(marker)
    with pytest.raises(HomeAssistantError, match='submitted media actions'):
        await coordinator.async_import(candidate)
    assert coordinator.entry.title != 'Imported system'
    coordinator._tasks.remove(marker)
    _register_actions(hass)
    with patch.object(type(hass.config_entries), 'async_schedule_reload') as reload:
        result = await hass.services.async_call('media_activities', 'import_configuration',
            {'config_entry_id': coordinator.entry.entry_id, 'configuration': candidate}, blocking=True, return_response=True)
        assert result['observer_only'] is True
        reload.assert_called_once_with(coordinator.entry.entry_id)
    assert coordinator.entry.title == 'Imported system'
    assert coordinator.entry.data['config']['observer_only'] is True
    with patch.object(type(hass.config_entries), 'async_schedule_reload'):
        assert (await coordinator.async_import(candidate, keep_observer=True))['observer_only'] is False
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_registry_rename_and_remove_events_use_safe_import(hass):
    coordinator = await start(hass, tv_config(True))
    with patch.object(coordinator, 'async_import', new=AsyncMock()) as importer:
        coordinator._registry_changed(Event('entity_registry_updated', {
            'action': 'update', 'changes': {'entity_id': 'media_player.tv'}, 'entity_id': 'media_player.renamed'}))
        await settle(hass)
        importer.assert_awaited_once()
        assert importer.call_args.kwargs == {'keep_observer': True}
        assert 'media_player.renamed' in str(importer.call_args.args[0])
        assert 'media_player.tv' not in str(importer.call_args.args[0])
    coordinator._registry_changed(Event('entity_registry_updated', {'action': 'remove', 'entity_id': 'media_player.tv'}))
    assert issue_registry.async_get(hass).async_get_issue('media_activities', 'test_room_entity_missing')
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_report_listener_and_unsubscribe_do_not_actuate_after_close(hass):
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass, tv_config(True))
    listener = Mock()
    remove = coordinator.async_add_listener(listener)
    await coordinator.async_request('watch')
    assert listener.call_count > 0
    remove()
    listener.reset_mock()
    event = Event('state_reported', {'entity_id': 'media_player.tv'})
    coordinator._state_reported(event)
    listener.assert_not_called()
    coordinator._timed_step(None)
    await coordinator.async_stop()
    coordinator._state_reported(event)
    coordinator._state_changed(event)
    coordinator._step()
    coordinator._schedule_tick()
    assert not coordinator._tasks
    assert coordinator._timer is None


@pytest.mark.asyncio
async def test_unregistered_device_action_fails_without_leaking_details(hass):
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await start(hass)
    await coordinator.async_request('watch')
    await settle(hass)
    assert 'HomeAssistantError' in str(coordinator.snapshot)
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_setup_failure_unsubscribes_and_removes_runtime(hass):
    config_entry = registered_entry(hass, tv_config(True))
    with patch.object(type(hass.config_entries), 'async_forward_entry_setups', new=AsyncMock(side_effect=RuntimeError('platform failed'))):
        with pytest.raises(RuntimeError, match='platform failed'):
            await async_setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator._closed
    assert coordinator._unsubscribers == []
    assert config_entry.entry_id not in hass.data['media_activities']['coordinators']


@pytest.mark.asyncio
async def test_invalid_setup_raises_config_error_and_creates_repair(hass):
    broken = tv_config()
    broken['activities'][0]['requirements'][0]['capability'] = 'absent'
    config_entry = registered_entry(hass, broken)
    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, config_entry)
    assert issue_registry.async_get(hass).async_get_issue('media_activities', config_entry.entry_id + '_invalid_configuration')
    await _updated(hass, config_entry)
    assert issue_registry.async_get(hass).async_get_issue('media_activities', config_entry.entry_id + '_invalid_configuration')


@pytest.mark.asyncio
async def test_update_noop_reconfiguration_guard_and_changed_config(hass):
    coordinator = await registered_start(hass, tv_config(True))
    with patch.object(type(hass.config_entries), 'async_schedule_reload') as reload:
        await _updated(hass, coordinator.entry)
        reload.assert_not_called()
        coordinator._reconfiguring = True
        await _updated(hass, coordinator.entry)
        reload.assert_not_called()
        coordinator._reconfiguring = False
        changed = deepcopy(coordinator.config)
        changed['name'] = 'Updated'
        with patch('custom_components.media_activities.config_flow.effective_config', return_value=changed):
            await _updated(hass, coordinator.entry)
        reload.assert_called_once_with(coordinator.entry.entry_id)
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_migration_validates_version_and_configuration(hass):
    entry = registered_entry(hass, tv_config(True))
    assert await async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry(entry, version=2)
    assert not await async_migrate_entry(hass, entry)
    hass.config_entries.async_update_entry(entry, version=1, data={'config': {'name': 'Bad', 'version': 99}})
    assert not await async_migrate_entry(hass, entry)


@pytest.mark.asyncio
async def test_runtime_ownership_rejects_competing_system(hass):
    other = registered_entry(hass, tv_config(True))
    with pytest.raises(ValueError, match='Control resources already owned'):
        validate_ownership(hass, tv_config())
    validate_ownership(hass, tv_config(), other.entry_id)


@pytest.mark.asyncio
async def test_full_native_flow_platform_setup_control_unload_and_reload(hass):
    """Smoke the real HA loader, flow manager and all entity platforms."""
    from homeassistant import loader
    from homeassistant.helpers import frame, entity_registry
    from homeassistant.setup import async_setup_component
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.config_entries import ConfigEntryState

    integration = Path(__file__).resolve().parents[1] / 'custom_components' / 'media_activities'
    destination = Path(hass.config.path('custom_components/media_activities'))
    await hass.async_add_executor_job(shutil.copytree, integration, destination)
    frame.async_setup(hass)
    loader.async_setup(hass)
    custom = await loader.async_get_custom_components(hass)
    assert 'media_activities' in custom
    assert await async_setup_component(hass, 'media_activities', {})
    calls = []
    @callback
    def source_selected(call):
        calls.append(dict(call.data))
        hass.states.async_set('media_player.tv', 'on', {'source': call.data['source']})
    hass.services.async_register('media_player', 'select_source', source_selected)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})

    first = await hass.config_entries.flow.async_init('media_activities', context={'source': 'user'})
    assert first['type'] is FlowResultType.FORM
    advanced = await hass.config_entries.flow.async_configure(first['flow_id'], {'name': 'Test room', 'advanced': True})
    assert advanced['step_id'] == 'advanced'
    review = await hass.config_entries.flow.async_configure(advanced['flow_id'], {'configuration': tv_config(True)})
    assert review['step_id'] == 'review'
    result = await hass.config_entries.flow.async_configure(review['flow_id'], {})
    assert result['type'] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    config_entry = result['result']
    assert config_entry.state is ConfigEntryState.LOADED
    assert len(config_entry.subentries) == 2
    registry = entity_registry.async_get(hass)
    activity = registry.async_get_entity_id('select', 'media_activities', config_entry.entry_id + '_activity')
    observer = registry.async_get_entity_id('switch', 'media_activities', config_entry.entry_id + '_observer_only')
    ready = registry.async_get_entity_id('binary_sensor', 'media_activities', config_entry.entry_id + '_verified_ready')
    assert activity and observer and ready
    assert len(entity_registry.async_entries_for_config_entry(registry, config_entry.entry_id)) == 10
    assert not calls
    await hass.services.async_call('switch', 'turn_off', {'entity_id': observer}, blocking=True)
    await hass.services.async_call('select', 'select_option', {'entity_id': activity, 'option': 'Watch TV'}, blocking=True)
    await settle(hass)
    assert len(calls) == 1
    assert calls[0]['source'] == 'HDMI 2'
    assert hass.states.get(ready).state == 'on'
    old = config_entry.runtime_data
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert old._closed and not old._unsubscribers and not old._listeners
    assert old._timer is None
    assert config_entry.entry_id not in hass.data['media_activities']['coordinators']
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await settle(hass)
    assert config_entry.runtime_data is not old
    assert len(calls) == 1  # Persisted intent alone never turns equipment on.
    assert registry.async_get_entity_id('select', 'media_activities', config_entry.entry_id + '_activity') == activity
    assert len(entity_registry.async_entries_for_config_entry(registry, config_entry.entry_id)) == 10
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.asyncio
async def test_registry_renames_coalesce_while_submitted_action_settles(hass):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def select(call):
        calls.append(dict(call.data))
        entered.set()
        await release.wait()

    config = tv_config(False)
    config['equipment'][0]['capabilities'].append({
        'id': 'link', 'kind': 'availability', 'value_type': 'boolean',
        'feedback_type': 'reported', 'observation': {'entity_id': 'binary_sensor.tv_link'},
        'operations': [],
    })
    hass.services.async_register('media_player', 'select_source', select)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    hass.states.async_set('binary_sensor.tv_link', 'on')
    coordinator = await registered_start(hass, config)
    await coordinator.async_request('watch')
    await asyncio.wait_for(entered.wait(), 1)

    with patch.object(type(hass.config_entries), 'async_schedule_reload') as reload:
        coordinator._registry_changed(Event('entity_registry_updated', {
            'action': 'update', 'changes': {'entity_id': 'media_player.tv'},
            'entity_id': 'media_player.temporary_name'}))
        await asyncio.sleep(0)
        # Exercise the new indexed subscription, not just the direct callback.
        hass.bus.async_fire('entity_registry_updated', {
            'action': 'update', 'changes': {'entity_id': 'media_player.temporary_name'},
            'old_entity_id': 'media_player.temporary_name',
            'entity_id': 'media_player.television'})
        hass.bus.async_fire('entity_registry_updated', {
            'action': 'update', 'changes': {'entity_id': 'binary_sensor.tv_link'},
            'old_entity_id': 'binary_sensor.tv_link',
            'entity_id': 'binary_sensor.television_link'})
        for _ in range(4):
            await asyncio.sleep(0)
        assert coordinator.snapshot['observer_only'] is True
        assert coordinator._pending_registry_config is not None
        assert coordinator.entry.data['config']['observer_only'] is False
        assert 'media_player.television' not in str(coordinator.entry.data)
        with pytest.raises(HomeAssistantError, match='submitted media actions'):
            await coordinator.async_import(config)
        with pytest.raises(HomeAssistantError, match='Entity renames'):
            await coordinator.async_retry()
        reload.assert_not_called()
        release.set()
        await settle(hass)
        reload.assert_called_once_with(coordinator.entry.entry_id)

    stored = coordinator.entry.data['config']
    assert stored['observer_only'] is False
    assert stored['equipment'][0]['capabilities'][0]['observation']['entity_id'] == 'media_player.television'
    assert stored['equipment'][0]['capabilities'][0]['operations'][0]['target']['entity_id'] == 'media_player.television'
    assert stored['equipment'][0]['capabilities'][1]['observation']['entity_id'] == 'binary_sensor.television_link'
    assert coordinator._pending_registry_config is None
    assert coordinator._registry_rename_task is None
    assert len(calls) == 1
    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_unload_cancels_rename_waiter_but_preserves_bindings_and_device_call(hass):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def select(call):
        entered.set()
        await release.wait()

    hass.services.async_register('media_player', 'select_source', select)
    hass.states.async_set('media_player.tv', 'on', {'source': 'HDMI 1'})
    coordinator = await registered_start(hass, tv_config(False))
    await coordinator.async_request('watch')
    await asyncio.wait_for(entered.wait(), 1)
    coordinator._registry_changed(Event('entity_registry_updated', {
        'action': 'update', 'changes': {'entity_id': 'media_player.tv'}, 'entity_id': 'media_player.renamed'}))
    waiter = coordinator._registry_rename_task
    await asyncio.sleep(0)
    with patch.object(type(hass.config_entries), 'async_schedule_reload') as reload:
        await coordinator.async_stop()
        reload.assert_not_called()
    assert waiter.cancelled()
    assert coordinator._registry_rename_task is None
    assert coordinator._tasks and all(not task.cancelled() for task in coordinator._tasks)
    assert coordinator.entry.data['config']['observer_only'] is False
    assert 'media_player.renamed' in str(coordinator.entry.data)
    coordinator._registry_changed(Event('entity_registry_updated', {
        'action': 'update', 'changes': {'entity_id': 'media_player.renamed'}, 'entity_id': 'media_player.ignored'}))
    assert 'media_player.ignored' not in str(coordinator.entry.data)
    release.set()
    await settle(hass)
    assert not hass.data['media_activities']['in_flight']
