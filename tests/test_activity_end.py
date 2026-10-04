"""Source-session shutdown and bounded display handover behavior."""
from copy import deepcopy

import pytest

from custom_components.media_activities.engine import Engine
from custom_components.media_activities.schema import validate_config
from tests.test_engine import config, dispatch, observed, requirement


def setup(*, preserve=False):
    cfg = config()
    cfg['preserve_devices'] = preserve
    cfg['activities'][0].update(end_conditions=[{'entity_id': 'binary_sensor.source', 'state': 'off'}],
                                end_debounce=2, handover_timeout=180)
    cfg['activities'].append({'id': 'game', 'requirements': [requirement()]})
    cfg['detection_rules'] = [{'id': 'wake', 'activity': 'watch',
        'conditions': [{'entity_id': 'binary_sensor.source', 'state': 'on'}],
        'trigger_bindings': [{'entity_id': 'binary_sensor.source', 'from': 'off', 'to': 'on'}]}]
    engine = Engine(cfg)
    observed(engine, value='on')
    observed(engine, 'binary_sensor.source', 'on')
    engine.request('watch', 1)
    assert engine.tick(1) == []
    return engine


def end(engine):
    observed(engine, 'binary_sensor.source', 'off', now=10)
    assert engine.tick(10) == []
    assert engine.tick(12) == []
    assert engine.snapshot()['activity_id'] == 'idle'
    assert engine.snapshot()['observed_activity'] == 'idle'
    assert engine.snapshot()['phase'] == 'handover'


def test_source_end_holds_display_then_shuts_down_once():
    engine = setup()
    end(engine)
    assert engine.tick(191) == []
    shutdown, = engine.tick(192)
    assert shutdown['value'] is False
    dispatch(engine, shutdown, 192, value='off')
    assert engine.tick(193) == []
    observed(engine, value='on', now=195)
    assert engine.tick(195) == []  # idle cleanup is one-shot, not permanent enforcement


@pytest.mark.parametrize('activity', ['watch', 'game'])
def test_new_activity_cancels_handover_without_projector_cycle(activity):
    engine = setup()
    end(engine)
    if activity == 'watch':
        observed(engine, 'binary_sensor.source', 'on', now=20)
        assert engine.tick(22) == []
    else:
        engine.request('game', 20)
    assert engine.snapshot()['activity_id'] == activity
    assert engine.snapshot()['handover_until'] is None
    assert engine.tick(300) == []


def test_unknown_source_does_not_end_activity():
    engine = setup()
    observed(engine, 'binary_sensor.source', 'unavailable', now=10)
    assert engine.tick(300) == []
    assert engine.activity_id == 'watch'


def test_preservation_survives_automatic_idle_and_blocks_cleanup():
    engine = setup(preserve=True)
    end(engine)
    assert engine.tick(300) == []
    assert engine.preserve_devices is True
    assert engine.snapshot()['cleanup_status'] == 'preserved'
    engine.finish(301)
    shutdown, = engine.tick(301)
    assert shutdown['value'] is False
    assert engine.preserve_devices is False


def test_short_signal_loss_is_debounced_without_wake_or_shutdown():
    engine = setup()
    observed(engine, 'binary_sensor.source', 'off', now=10)
    assert engine.tick(10) == []
    observed(engine, 'binary_sensor.source', 'on', now=11)
    assert engine.tick(13) == []
    assert engine.activity_id == 'watch'


def test_restart_during_handover_does_not_execute_saved_cleanup():
    engine = setup()
    end(engine)
    restored = Engine(engine.config, 20, engine.export_state())
    observed(restored, value='on', now=21)
    observed(restored, 'binary_sensor.source', 'off', now=21)
    assert restored.tick(500) == []
    assert restored.snapshot()['phase'] == 'recovery_paused'


def test_finish_bypasses_handover():
    engine = setup()
    end(engine)
    engine.finish(15)
    shutdown, = engine.tick(15)
    assert shutdown['value'] is False


def test_initial_off_or_cached_source_does_not_abort_startup():
    engine = setup()
    engine.request('watch', 5)
    observed(engine, 'binary_sensor.source', 'off', now=5, fresh=False)
    assert engine.tick(300) == []
    assert engine.activity_id == 'watch'


def test_stable_standby_blocks_immediate_reverse_power_command():
    cfg = config()
    cfg['equipment'][0]['capabilities'][0]['operations'][0]['when'] = [
        {'entity_id': 'media_player.tv', 'state': 'off', 'stable_for': 10}]
    engine = Engine(cfg)
    observed(engine, value='off')
    engine.request('watch', 1)
    assert engine.tick(1) == []
    wake, = engine.tick(10)
    assert wake['value'] is True


def test_new_wake_retries_blocked_same_activity_without_resetting_budget():
    engine = setup()
    observed(engine, value='off', now=5)
    wake, = engine.tick(5)
    dispatch(engine, wake, 5, error='action_failed:DeviceError')
    # Exhaust its per-command retries.
    for at in (8, 15):
        commands = engine.tick(at)
        for command in commands:
            dispatch(engine, command, at, error='action_failed:DeviceError')
    engine.tick(20)
    assert engine.snapshot()['phase'] == 'blocked'
    observed(engine, 'binary_sensor.source', 'off', now=21)
    observed(engine, 'binary_sensor.source', 'on', now=22)
    commands = engine.tick(24)
    assert commands and commands[0]['value'] is True
    assert len(engine.recovery_budgets['tv.power']) == 2


@pytest.mark.parametrize('field', ['handover_timeout', 'end_debounce'])
def test_end_timing_rejects_invalid_values(field):
    cfg = deepcopy(config())
    cfg['activities'][0][field] = -1
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_any_shutdown_condition_can_end_with_other_source_unavailable():
    cfg = setup().config
    cfg['activities'][0]['end_condition_mode'] = 'any'
    cfg['activities'][0]['end_conditions'].append({'entity_id': 'media_player.source', 'state': 'off'})
    engine = Engine(cfg)
    observed(engine, value='on')
    observed(engine, 'binary_sensor.source', 'on')
    observed(engine, 'media_player.source', 'unavailable')
    engine.request('watch', 1)
    engine.tick(1)
    end(engine)


def test_any_positive_network_off_ends_before_hdmi_poll_arrives():
    cfg = setup().config
    cfg['activities'][0]['end_condition_mode'] = 'any'
    cfg['activities'][0]['end_conditions'].append({'entity_id': 'media_player.source', 'state': 'off'})
    engine = Engine(cfg)
    observed(engine, value='on')
    observed(engine, 'binary_sensor.source', 'on')
    observed(engine, 'media_player.source', 'on')
    engine.request('watch', 1)
    engine.tick(1)
    observed(engine, 'media_player.source', 'off', now=10)
    assert engine.tick(12) == []
    assert engine.tick(14) == []
    assert engine.activity_id == 'idle'


def test_retry_idle_respects_preservation():
    engine = setup(preserve=True)
    end(engine)
    engine.retry(20)
    assert engine.tick(20) == []
    assert engine.preserve_devices is True


def test_pending_source_end_supersedes_unsent_commands_but_tracks_submitted():
    engine = setup()
    observed(engine, value='off', now=5)
    wake, = engine.tick(5)
    engine.submitted(wake['id'], 5)
    end(engine)
    assert engine.commands[wake['id']]['submitted_at'] == 5
    assert engine.tick(100) == []


def test_invalid_end_condition_mode_is_rejected():
    cfg = config()
    cfg['activities'][0]['end_condition_mode'] = 'random'
    with pytest.raises(ValueError):
        validate_config(cfg)
