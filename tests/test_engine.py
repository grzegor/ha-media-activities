"""Behavioral tests of orchestration, independent of any manufacturer or HA."""
import json

import pytest

from custom_components.media_activities.engine import Engine


def capability(cid="power", entity="media_player.tv", *, feedback="reported", cleanup=True, kind="power"):
    result = {"id": cid, "kind": kind, "value_type": "boolean", "feedback_type": feedback,
              "timeout": 10, "operations": [
                  {"value": True, "action": "media_player.turn_on", "target": {"entity_id": entity}, "idempotent": True},
                  {"value": False, "action": "media_player.turn_off", "target": {"entity_id": entity}, "idempotent": True}]}
    if feedback != "command_only":
        result["observation"] = {"entity_id": entity}
    if cleanup:
        result["cleanup"] = {"value": False}
    return result


def requirement(rid="tv_on", equipment="tv", cap="power", value=True, **kwargs):
    return {"id": rid, "equipment": equipment, "capability": cap, "value": value, **kwargs}


def config(*, feedback="reported", observer=False):
    return {"version": 1, "name": "Test", "observer_only": observer,
            "equipment": [{"id": "tv", "capabilities": [capability(feedback=feedback)]}],
            "activities": [{"id": "watch", "requirements": [requirement()]}]}


def observed(engine, entity="media_player.tv", value="off", now=0, attributes=None, fresh=True):
    engine.observe(entity, value, attributes or {}, now, fresh=fresh)


def dispatch(engine, command, now, *, value=None, entity=None, error=None):
    engine.submitted(command["id"], now)
    if value is not None:
        observed(engine, entity or command["target"].get("entity_id"), value, now + .1)
    engine.completed(command["id"], now + .2, error=error)


def test_simple_tv_without_outlet_and_service_return_not_confirmation():
    engine = Engine(config())
    observed(engine)
    rid = engine.request("watch", 1)
    command, = engine.tick(1)
    assert command["action"] == "media_player.turn_on"
    dispatch(engine, command, 1)
    assert engine.snapshot()["phase"] == "preparing"
    observed(engine, value="on", now=2)
    assert engine.tick(2) == []
    snap = engine.snapshot()
    assert snap["phase"] == "ready" and snap["verified_ready"]
    assert snap["request_id"] == rid
    assert engine.tick(3) == []


@pytest.mark.parametrize("feedback", ["command_only", "optimistic"])
def test_unverified_is_usable_one_shot_and_never_auto_retries(feedback):
    engine = Engine(config(feedback=feedback))
    if feedback == "optimistic":
        observed(engine)
    engine.request("watch", 0)
    command, = engine.tick(0)
    dispatch(engine, command, 0)
    assert engine.snapshot()["phase"] == "applied_unverified"
    assert not engine.snapshot()["verified_ready"]
    assert not engine.snapshot()["problem"]
    assert engine.tick(10000) == []


def test_source_selection_can_implicitly_wake_tv():
    cfg = config()
    cfg["equipment"][0]["capabilities"] = [{"id": "input", "kind": "input", "value_type": "string",
        "feedback_type": "reported", "observation": {"entity_id": "media_player.tv", "attribute": "source"},
        "operations": [{"any_value": True, "action": "media_player.select_source", "target": {"entity_id": "media_player.tv"}, "value_field": "source", "idempotent": True}]}]
    cfg["activities"][0]["requirements"] = [requirement(cap="input", value="HDMI 1")]
    engine = Engine(cfg)
    observed(engine, value="off", attributes={"source": "TV"})
    engine.request("watch", 0)
    command, = engine.tick(0)
    assert command["data"] == {"source": "HDMI 1"}
    engine.submitted(command["id"], 0)
    observed(engine, value="on", now=1, attributes={"source": "HDMI 1"})
    engine.completed(command["id"], 1)
    assert engine.snapshot()["verified_ready"]


def two_devices():
    cfg = config()
    cfg["equipment"].append({"id": "console", "capabilities": [capability(entity="media_player.console")]})
    cfg["activities"].append({"id": "game", "requirements": [requirement(), requirement("console_on", "console")]})
    return cfg


def test_preservation_keeps_unused_console_and_off_cleans_it():
    engine = Engine(two_devices())
    observed(engine, value="on")
    observed(engine, "media_player.console", "on")
    engine.set_preserve(True, 0)
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["cleanup_status"] == "preserved"
    engine.set_preserve(False, 1)
    command, = engine.tick(1)
    assert command["equipment_id"] == "console" and command["value"] is False
    dispatch(engine, command, 1, value="off")
    assert engine.snapshot()["cleanup_status"] == "complete"


def test_cleanup_failure_never_blocks_preparation():
    cfg = two_devices()
    cfg["equipment"][1]["capabilities"][0]["cleanup"]["guards"] = [{"entity_id": "binary_sensor.console_shutdown", "state": "on"}]
    engine = Engine(cfg)
    observed(engine)
    observed(engine, "media_player.console", "on")
    engine.request("watch", 0)
    command, = engine.tick(0)
    dispatch(engine, command, 0, value="on")
    engine.tick(1)
    assert engine.snapshot()["phase"] == "ready"
    assert not any(c["equipment_id"] == "console" for c in engine.tick(2))
    engine.tick(901)
    assert engine.snapshot()["phase"] == "ready"
    assert engine.snapshot()["cleanup_status"] == "blocked"


def test_finish_atomically_resets_preservation_and_does_not_wake_old_activity():
    engine = Engine(config())
    observed(engine, value="on")
    engine.request("watch", 0)
    engine.tick(0)
    engine.set_preserve(True, 1)
    engine.finish(2)
    command, = engine.tick(2)
    assert command["value"] is False
    assert not engine.snapshot()["preserve_devices"]
    assert engine.snapshot()["activity_id"] == "idle"
    dispatch(engine, command, 2, value="off")
    assert engine.snapshot()["phase"] == "ready"


def test_supersession_keeps_driver_lane_until_old_submission_resolves():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    first, = engine.tick(0)
    engine.submitted(first["id"], 0)
    engine.finish(1)
    # Cached Off is not sufficient to issue another command while driver owns lane.
    assert engine.tick(2) == []
    assert engine.tick(100) == []
    observed(engine, value="on", now=101)
    engine.completed(first["id"], 101)
    second, = engine.tick(102)
    assert second["value"] is False and second["generation"] != first["generation"]
    assert engine.snapshot()["activity_id"] == "idle"


def test_only_real_submission_consumes_attempt_and_reservations_cancel():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert next(iter(engine.goals.values()))["attempts"] == 0
    engine.finish(1)
    assert not engine.can_dispatch(cmd["id"], 1)
    assert engine.tick(1) == []


def test_failed_idempotent_action_three_attempts_and_no_flap_reset():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, error=RuntimeError("private text"))
    assert engine.tick(2) == []
    cmd, = engine.tick(2.2)
    dispatch(engine, cmd, 2.2, error="fail")
    assert engine.tick(7) == []
    cmd, = engine.tick(7.4)
    dispatch(engine, cmd, 7.4, error="fail")
    observed(engine, value="unavailable", now=8)
    observed(engine, value="off", now=9)
    assert engine.tick(10) == []
    assert engine.snapshot()["phase"] == "blocked"
    assert "private text" not in json.dumps(engine.export_state())
    engine.retry(11)
    assert len(engine.tick(11)) == 1


def test_non_idempotent_failure_does_not_retry():
    cfg = config()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["idempotent"] = False
    engine = Engine(cfg)
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, error="fail")
    assert engine.tick(100) == []
    assert engine.snapshot()["phase"] == "blocked"


def test_preservation_stops_later_recovery_but_explicit_retry_reconciles():
    engine = Engine(config())
    engine.set_preserve(True, 0)
    observed(engine, value="on")
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, value="off", now=1)
    assert engine.tick(1) == []
    assert not engine.snapshot()["verified_ready"]
    engine.retry(2)
    assert len(engine.tick(2)) == 1


def test_enabling_preservation_cancels_reserved_cleanup():
    engine = Engine(two_devices())
    observed(engine, value="on")
    observed(engine, "media_player.console", "on")
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert cmd["cleanup"]
    engine.set_preserve(True, 1)
    assert not engine.can_dispatch(cmd["id"], 1)
    assert engine.tick(1) == []


def test_ir_can_match_unavailable_but_waits_for_reported_network_feedback():
    cfg = config()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap["operations"].insert(0, {"value": True, "action": "button.press", "target": {"entity_id": "button.ir_wake"},
        "when": [{"entity_id": "media_player.tv", "states": ["unavailable", "unknown"]}], "transport": "ir", "idempotent": True})
    engine = Engine(cfg)
    observed(engine, value="unavailable")
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert cmd["action"] == "button.press"
    dispatch(engine, cmd, 0)
    assert engine.snapshot()["phase"] == "preparing"
    observed(engine, value="on", now=2)
    engine.tick(2)
    assert engine.snapshot()["verified_ready"]


def test_readiness_and_dependency_delay():
    cfg = config()
    cfg["equipment"][0]["capabilities"].append({"id": "input", "kind": "input", "value_type": "string", "feedback_type": "reported",
        "observation": {"entity_id": "media_player.tv", "attribute": "source"},
        "operations": [{"value": "HDMI", "action": "media_player.select_source", "target": {"entity_id": "media_player.tv"}, "data": {"source": "HDMI"}, "idempotent": True}]})
    cfg["activities"][0]["requirements"].append(requirement("input", cap="input", value="HDMI", depends_on=["tv_on"], min_delay=3))
    engine = Engine(cfg)
    observed(engine, attributes={"source": "TV"})
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    engine.submitted(cmd["id"], 0)
    observed(engine, value="on", now=1, attributes={"source": "TV"})
    engine.completed(cmd["id"], 1)
    assert engine.tick(2) == []
    cmd, = engine.tick(4)
    assert cmd["capability_id"] == "input"


def supply_config(*, feedback="reported", policy="managed", shared=False):
    cfg = config(feedback=feedback)
    cap = capability("supply", "switch.plug", kind="supply")
    cap.update(supply_policy=policy, consumers=["tv"])
    cap["cleanup"]["guards"] = [{"entity_id": "media_player.tv", "state": "off"}]
    cfg["equipment"].append({"id": "plug", "capabilities": [cap]})
    if shared:
        cfg["equipment"].append({"id": "radio", "capabilities": [capability(entity="media_player.radio")]})
        cap["consumers"].append("radio")
        cfg["activities"].append({"id": "radio", "requirements": [requirement("radio_on", "radio")]})
    return cfg


def test_optimistic_off_cannot_allow_supply_cut():
    engine = Engine(supply_config(feedback="optimistic"))
    observed(engine, value="off")
    observed(engine, "switch.plug", "on")
    engine.finish(0)
    commands = engine.tick(0)
    assert not any(c["equipment_id"] == "plug" for c in commands)


def test_shared_supply_retained_for_an_active_consumer():
    engine = Engine(supply_config(shared=True))
    observed(engine, value="off")
    observed(engine, "switch.plug", "on")
    observed(engine, "media_player.radio", "on")
    engine.request("radio", 0)
    assert engine.tick(0) == []
    assert any(b["reason"] == "shared_supply_in_use" for b in engine.snapshot()["blockers"])


def test_supply_off_invalidates_cached_logical_readiness():
    engine = Engine(supply_config())
    observed(engine, value="on", now=1)
    observed(engine, "switch.plug", "on", now=1)
    observed(engine, "switch.plug", "off", now=2)
    observed(engine, "switch.plug", "on", now=3)
    engine.request("watch", 3)
    assert not engine.snapshot()["verified_ready"]
    assert engine._read_cap("tv", "power")[1] is False
    observed(engine, value="on", now=4)
    engine.tick(4)
    assert engine.snapshot()["verified_ready"]


def test_policy_guard_rechecked_before_delayed_cut_and_stays_cancelled():
    cfg = supply_config(policy="standby_on")
    cfg["policies"] = [{"id": "away", "guards": [{"entity_id": "binary_sensor.vacancy_allowed", "state": "on"}],
        "requirements": [requirement("cut", "plug", "supply", False)]}]
    engine = Engine(cfg)
    observed(engine, value="off")
    observed(engine, "switch.plug", "on")
    observed(engine, "binary_sensor.vacancy_allowed", "on")
    engine.apply_policy("away", 0, origin="vacancy")
    cmd, = engine.tick(0)
    observed(engine, "binary_sensor.vacancy_allowed", "off", now=1)
    assert not engine.can_dispatch(cmd["id"], 1)
    engine.tick(1)
    observed(engine, "binary_sensor.vacancy_allowed", "on", now=2)
    assert engine.tick(2) == []
    assert engine.snapshot()["phase"] == "blocked"


def test_cancel_origin_does_not_cancel_unrelated_request_or_resume_old_activity():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0, origin="user")
    engine.cancel_origin("vacancy", 1)
    cmd, = engine.tick(1)
    engine.cancel_origin("user", 2)
    assert not engine.can_dispatch(cmd["id"], 2)
    assert engine.tick(3) == []


def test_restart_saved_intent_never_wakes_and_pending_commands_not_persisted():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    engine.submitted(cmd["id"], 0)
    persisted = json.loads(json.dumps(engine.export_state()))
    assert "commands" not in persisted
    restored = Engine(config(), 10, persisted)
    observed(restored, value="off", now=10)
    assert restored.tick(10) == []
    assert restored.snapshot()["phase"] == "recovery_paused"
    restored.retry(11)
    assert len(restored.tick(11)) == 1


def test_restart_can_adopt_fresh_existing_session():
    engine = Engine(config())
    engine.request("watch", 0)
    restored = Engine(config(), 10, engine.export_state())
    observed(restored, value="on", now=10, fresh=False)
    assert restored.tick(10) == []
    assert restored.snapshot()["phase"] == "recovery_paused"
    observed(restored, value="on", now=11)
    assert restored.tick(11) == []
    assert restored.snapshot()["verified_ready"]


def detection_config():
    cfg = config()
    cfg["equipment"][0]["capabilities"] = [{"id": "source", "kind": "input", "value_type": "string", "feedback_type": "reported",
        "observation": {"entity_id": "select.tv_source"}, "operations": [{"any_value": True, "action": "select.select_option", "target": {"entity_id": "select.tv_source"}, "value_field": "option", "idempotent": True}]}]
    cfg["activities"] = [{"id": a, "requirements": [requirement("source", cap="source", value=v)]} for a, v in [("watch", "TV"), ("game", "Game")]]
    cfg["detection_rules"] = [{"id": a, "activity": a, "conditions": [{"entity_id": "select.tv_source", "state": v}]} for a, v in [("watch", "TV"), ("game", "Game")]]
    return cfg


def test_external_source_change_adopted_but_own_ack_is_not_a_new_request():
    engine = Engine(detection_config())
    observed(engine, "select.tv_source", "TV")
    rid = engine.request("game", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, entity="select.tv_source", value="Game")
    engine.tick(3)
    assert engine.snapshot()["request_id"] == rid
    observed(engine, "select.tv_source", "TV", now=4)
    engine.tick(5)
    assert engine.snapshot()["activity_id"] == "game"
    engine.tick(6)
    assert engine.snapshot()["activity_id"] == "watch"
    assert engine.snapshot()["origin"] == "detected"


def test_ambiguous_or_unmapped_input_suspends_enforcement():
    engine = Engine(detection_config())
    observed(engine, "select.tv_source", "TV")
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "select.tv_source", "Other", now=1)
    assert engine.tick(3) == []
    assert engine.snapshot()["observed_activity"] == "manual_routing"
    assert engine.snapshot()["phase"] == "blocked"


def test_observer_mode_emits_no_actions_and_disabling_does_not_replay():
    engine = Engine(config(observer=True))
    observed(engine)
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["phase"] == "observing"
    engine.set_observer(False, 1)
    assert engine.tick(1) == []
    engine.retry(2)
    assert len(engine.tick(2)) == 1


def test_rolling_recovery_budget_persists_and_flapping_does_not_reset():
    engine = Engine(config())
    observed(engine, value="on")
    engine.request("watch", 0)
    engine.tick(0)
    for n in range(3):
        t = 10 + n * 10
        observed(engine, value="off", now=t)
        cmd, = engine.tick(t)
        dispatch(engine, cmd, t, value="on")
    observed(engine, value="off", now=50)
    assert engine.tick(50) == []
    assert engine.snapshot()["phase"] == "blocked"
    assert any(b["reason"] == "recovery_budget_exhausted" for b in engine.snapshot()["blockers"])
    assert len(engine.export_state()["recovery_budgets"]["tv.power"]) == 3


def test_event_ring_bounded_and_override_cleared_on_different_activity():
    engine = Engine(detection_config())
    observed(engine, "select.tv_source", "TV")
    engine.request("watch", 0)
    engine.set_override("tv", "source", "Game", 1)
    assert engine.overrides["tv.source"] == "Game"
    engine.request("game", 2)
    assert not engine.overrides
    for n in range(600):
        engine.set_preserve(bool(n % 2), n + 3)
    assert len(engine.snapshot()["events"]) == 500


def test_max_age_invalidates_verification_and_observe_only_never_dispatches():
    cfg = config()
    cfg["equipment"][0]["capabilities"][0]["observation"]["max_age"] = 5
    cfg["activities"][0]["requirements"][0]["confirmation"] = "observe_only"
    engine = Engine(cfg)
    observed(engine, value="off")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.tick(10) == []
    assert not engine.snapshot()["verified_ready"]


def test_nested_requested_value_field_and_invalid_public_requests():
    cfg = detection_config()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["value_field"] = "params.source"
    engine = Engine(cfg)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert cmd["data"] == {"params": {"source": "TV"}}
    with pytest.raises(ValueError):
        engine.request("missing", 0)
    with pytest.raises(ValueError):
        engine.set_preserve("yes", 0)
    with pytest.raises(ValueError):
        engine.set_observer(1, 0)
    with pytest.raises(ValueError):
        engine.apply_policy("missing", 0)
    with pytest.raises(ValueError):
        engine.set_override("missing", "power", True, 0)


def test_cached_reported_state_usable_for_explicit_request_but_not_cut():
    cfg = supply_config(policy="standby_on")
    cfg["policies"] = [{"id": "away", "guards": [{"entity_id": "input_boolean.allowed", "state": "on"}],
        "requirements": [requirement("cut", "plug", "supply", False)]}]
    engine = Engine(cfg)
    observed(engine, value="on", fresh=False)
    observed(engine, "switch.plug", "on", fresh=False)
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["verified_ready"]
    observed(engine, value="off", now=1, fresh=False)
    observed(engine, "input_boolean.allowed", "on", now=1, fresh=False)
    engine.apply_policy("away", 1)
    assert engine.tick(1) == []
    assert not engine.guard_failed  # Current HA helper state is usable.
    observed(engine, value="off", now=2, fresh=True)
    cmd, = engine.tick(2)
    assert cmd["equipment_id"] == "plug"


def test_required_observation_only_readiness_waits_without_fake_command():
    cfg = config()
    cfg["equipment"][0]["capabilities"].append({"id": "ready", "kind": "availability", "value_type": "boolean",
        "feedback_type": "reported", "observation": {"entity_id": "binary_sensor.ready"}, "operations": []})
    cfg["activities"][0]["requirements"].append(requirement("ready", cap="ready", confirmation="required"))
    engine = Engine(cfg)
    observed(engine, value="on")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["phase"] == "preparing"
    observed(engine, "binary_sensor.ready", "on", now=5)
    assert engine.tick(5) == []
    assert engine.snapshot()["verified_ready"]


def test_unobservable_readiness_without_action_is_explicitly_unverified():
    cfg = config()
    cfg["equipment"][0]["capabilities"].append({"id": "ready", "kind": "availability", "value_type": "boolean",
        "feedback_type": "command_only", "operations": []})
    cfg["activities"][0]["requirements"].append(requirement("ready", cap="ready", confirmation="best_effort"))
    engine = Engine(cfg)
    observed(engine, value="on")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["phase"] == "applied_unverified"
    assert not engine.snapshot()["verified_ready"]


def test_completed_finish_does_not_fight_later_manual_wake():
    engine = Engine(config())
    observed(engine, value="on")
    engine.finish(0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, value="off")
    observed(engine, value="on", now=100)
    assert engine.tick(100) == []


def test_detection_debounce_never_fights_manual_source_during_wait():
    engine = Engine(detection_config())
    observed(engine, "select.tv_source", "TV")
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "select.tv_source", "Game", now=1)
    assert engine.tick(1.5) == []
    assert engine.tick(2.5) == []
    engine.tick(3)
    assert engine.snapshot()["activity_id"] == "game"


def test_ephemeral_guards_and_overrides_are_validated():
    engine = Engine(detection_config())
    with pytest.raises(ValueError):
        engine.request("watch", 0, guards=[{"entity_id": "sensor.guard"}])
    engine.request("watch", 0)
    with pytest.raises(ValueError):
        engine.set_override("tv", "source", {"unexpected": "object"}, 1)


def test_initial_read_does_not_confirm_a_submitted_command():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    engine.submitted(cmd["id"], 0)
    observed(engine, value="on", now=1, fresh=False)
    engine.completed(cmd["id"], 1)
    assert engine.snapshot()["phase"] == "preparing"
    observed(engine, value="on", now=2)
    engine.tick(2)
    assert engine.snapshot()["verified_ready"]


def radio_and_tv():
    cfg = detection_config()
    cfg["equipment"].append({"id": "speaker", "capabilities": [capability(entity="media_player.speaker")]})
    cfg["activities"].append({"id": "radio", "requirements": [requirement("radio_on", "speaker")]})
    cfg["detection_rules"].append({"id": "radio", "activity": "radio", "conditions": [{"entity_id": "media_player.speaker", "state": "on"}]})
    return cfg


def test_external_intent_uses_affected_rules_not_still_active_unused_source():
    engine = Engine(radio_and_tv())
    observed(engine, "select.tv_source", "TV")
    observed(engine, "media_player.speaker", "off")
    engine.set_preserve(True, 0)
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "media_player.speaker", "on", now=1)
    assert engine.tick(3) == []
    assert engine.snapshot()["activity_id"] == "radio"
    assert engine.snapshot()["observed_activity"] == "radio"
    observed(engine, "select.tv_source", "Game", now=4)
    assert engine.tick(6) == []
    assert engine.snapshot()["activity_id"] == "game"
    assert engine.snapshot()["observed_activity"] == "game"
    assert not engine.ambiguous


def test_simultaneous_conflicting_external_intent_still_suspends():
    engine = Engine(radio_and_tv())
    observed(engine, "select.tv_source", "TV")
    observed(engine, "media_player.speaker", "off")
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "select.tv_source", "Game", now=1)
    observed(engine, "media_player.speaker", "on", now=1.5)
    assert engine.tick(3.5) == []
    assert engine.snapshot()["phase"] == "blocked"
    assert engine.snapshot()["observed_activity"] == "manual_routing"


def test_unrelated_attribute_update_is_not_new_source_intent():
    cfg = detection_config()
    cfg["detection_rules"][0]["conditions"] = [{"entity_id": "media_player.receiver", "attribute": "source", "state": "Bluetooth"}]
    cfg["detection_rules"][0]["trigger_entities"] = ["media_player.receiver"]
    engine = Engine(cfg)
    observed(engine, "select.tv_source", "Game")
    observed(engine, "media_player.receiver", "on", attributes={"source": "Bluetooth", "volume_level": .1})
    engine.request("game", 0)
    engine.tick(0)
    observed(engine, "media_player.receiver", "on", now=1, attributes={"source": "Bluetooth", "volume_level": .2})
    assert engine.tick(4) == []
    assert engine.snapshot()["activity_id"] == "game"


def test_manual_return_to_recently_commanded_value_not_mistaken_for_echo():
    engine = Engine(detection_config())
    observed(engine, "select.tv_source", "TV")
    engine.request("game", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, entity="select.tv_source", value="Game")
    observed(engine, "select.tv_source", "TV", now=1)
    engine.tick(3)
    assert engine.snapshot()["activity_id"] == "watch"
    observed(engine, "select.tv_source", "Game", now=4)
    engine.tick(6)
    assert engine.snapshot()["activity_id"] == "game"


def test_source_validity_prevents_remembered_off_input_from_skipping_wake():
    cfg = detection_config()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap["observation"] = {"entity_id": "media_player.tv", "attribute": "source",
        "valid_when": [{"entity_id": "media_player.tv", "states": ["on", "playing", "idle"]}]}
    engine = Engine(cfg)
    observed(engine, value="off", attributes={"source": "TV"})
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert cmd["value"] == "TV"
    engine.submitted(cmd["id"], 0)
    observed(engine, value="on", now=1, attributes={"source": "TV"})
    engine.completed(cmd["id"], 1)
    assert engine.snapshot()["verified_ready"]


def test_shutdown_playback_goal_does_not_permanently_block_safe_supply_cut():
    cfg = supply_config(policy="standby_on")
    cap = capability("playback", "media_player.radio", kind="playback")
    cfg["equipment"][0]["capabilities"].append(cap)
    cfg["policies"] = [{"id": "sleep", "requirements": [
        requirement("stop", cap="playback", value=False),
        requirement("off", value=False),
        requirement("cut", "plug", "supply", False, depends_on=["stop", "off"])]}]
    engine = Engine(cfg)
    observed(engine, value="off")
    observed(engine, "media_player.radio", "off")
    observed(engine, "switch.plug", "on")
    engine.apply_policy("sleep", 0)
    cmd, = engine.tick(0)
    assert cmd["equipment_id"] == "plug" and cmd["value"] is False


def one_shot_and_volume():
    cfg = config(feedback="command_only")
    cfg["equipment"][0]["capabilities"][0].update(kind="playback")
    cfg["equipment"][0]["capabilities"].append({"id": "volume", "kind": "volume", "value_type": "number",
        "feedback_type": "reported", "observation": {"entity_id": "media_player.tv", "attribute": "volume_level"},
        "operations": [{"any_value": True, "action": "media_player.volume_set", "target": {"entity_id": "media_player.tv"}, "value_field": "volume_level", "idempotent": True}]})
    return cfg


@pytest.mark.parametrize("already_completed", [True, False])
def test_session_volume_override_never_restarts_one_shot_playback(already_completed):
    engine = Engine(one_shot_and_volume())
    observed(engine, value="on", attributes={"volume_level": .1})
    engine.request("watch", 0)
    start, = engine.tick(0)
    engine.submitted(start["id"], 0)
    if already_completed:
        engine.completed(start["id"], .1)
    engine.set_override("tv", "volume", .2, 1)
    if not already_completed:
        assert engine.tick(1) == []
        engine.completed(start["id"], 2)
    commands = engine.tick(3)
    assert len(commands) == 1 and commands[0]["capability_id"] == "volume"
    engine.submitted(commands[0]["id"], 3)
    observed(engine, value="on", now=4, attributes={"volume_level": .2})
    engine.completed(commands[0]["id"], 4)
    assert engine.tick(5) == []
    assert engine.snapshot()["phase"] == "applied_unverified"


def test_preservation_disabled_does_not_replay_successful_command_only_start():
    engine = Engine(config(feedback="command_only"))
    engine.set_preserve(True, 0)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0)
    engine.set_preserve(False, 1)
    assert engine.tick(1) == []
    assert engine.snapshot()["phase"] == "applied_unverified"


def test_named_finish_policy_clears_activity_and_never_claims_verified_playback():
    cfg = config()
    cfg["policies"] = [{"id": "sleep", "finish_activity": True, "requirements": [requirement(value=False)]}]
    engine = Engine(cfg)
    observed(engine, value="on")
    engine.request("watch", 0)
    engine.tick(0)
    engine.set_preserve(True, 1)
    engine.apply_policy("sleep", 2)
    cmd, = engine.tick(2)
    dispatch(engine, cmd, 2, value="off")
    snap = engine.snapshot()
    assert snap["activity_id"] == "idle" and not snap["preserve_devices"]
    assert snap["mode"] == "policy" and snap["suspended"] and snap["policy_ready"]
    assert not snap["verified_ready"]
    assert engine.tick(3) == []
    engine.retry(4)
    assert engine.suspended


def test_idle_request_positive_guards_cannot_be_discarded():
    engine = Engine(config())
    observed(engine, value="on")
    observed(engine, "input_boolean.allowed", "off")
    engine.request("idle", 0, origin="vacancy", guards=[{"entity_id": "input_boolean.allowed", "state": "on"}])
    assert engine.tick(0) == []
    assert engine.snapshot()["phase"] == "blocked"


@pytest.mark.parametrize("policy,desired,reason", [("unmanaged", True, "supply_unmanaged"), ("always_on", False, "supply_retained"), ("standby_on", False, "supply_retained")])
def test_supply_policies_block_disallowed_activity_operations(policy, desired, reason):
    cfg = supply_config(policy=policy)
    cfg["activities"][0]["requirements"] = [requirement("plug", "plug", "supply", desired)]
    engine = Engine(cfg)
    observed(engine, value="off")
    observed(engine, "switch.plug", "off" if desired else "on")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert any(b["reason"] == reason for b in engine.snapshot()["blockers"])


def test_supply_on_does_not_require_shutdown_guard():
    cfg = supply_config()
    cfg["activities"][0]["requirements"] = [requirement("plug", "plug", "supply", True)]
    engine = Engine(cfg)
    observed(engine, "switch.plug", "off")
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    assert cmd["value"] is True


def test_pending_consumer_command_and_cached_shutdown_both_prevent_cut():
    cfg = supply_config()
    engine = Engine(cfg)
    observed(engine, value="on")
    observed(engine, "switch.plug", "on")
    engine.finish(0)
    cmd, = engine.tick(0)
    assert cmd["equipment_id"] == "tv"
    engine.submitted(cmd["id"], 0)
    observed(engine, value="off", now=1)
    assert engine.tick(1) == []
    engine.completed(cmd["id"], 1)
    cut, = engine.tick(2)
    assert cut["equipment_id"] == "plug"


def test_guard_and_prerequisite_changes_between_reservation_and_dispatch():
    cfg = config()
    op = cfg["equipment"][0]["capabilities"][0]["operations"][0]
    op.update(guards=[{"entity_id": "binary_sensor.safe", "state": "on"}], prerequisites=[{"entity_id": "binary_sensor.ready", "state": "on"}])
    engine = Engine(cfg)
    observed(engine)
    observed(engine, "binary_sensor.safe", "on")
    observed(engine, "binary_sensor.ready", "off")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    observed(engine, "binary_sensor.ready", "on", now=1)
    cmd, = engine.tick(1)
    observed(engine, "binary_sensor.safe", "off", now=2)
    assert not engine.can_dispatch(cmd["id"], 2)
    assert not engine.commands


def test_single_lane_and_cancelled_original_request_still_track_driver():
    engine = Engine(one_shot_and_volume())
    observed(engine, value="on", attributes={"volume_level": .1})
    engine.request("watch", 0)
    engine.set_override("tv", "volume", .2, 0)
    first, = engine.tick(0)
    engine.submitted(first["id"], 0)
    engine.cancel_origin("setting", 1)
    assert engine.tick(2) == []
    assert engine.commands
    engine.completed(first["id"], 3)
    assert not engine.commands


def test_confirmation_timeout_retries_and_best_effort_can_finish_unverified():
    cfg = config()
    cfg["activities"][0]["requirements"][0]["confirmation"] = "best_effort"
    cfg["equipment"][0]["capabilities"][0]["retry_policy"] = {"max_attempts": 1, "backoff": []}
    engine = Engine(cfg)
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0)
    assert engine.tick(11) == []
    assert engine.snapshot()["phase"] == "applied_unverified"


def test_required_confirmation_timeout_finitely_retries_then_blocks():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    for start in (0, 12, 27):
        cmd, = engine.tick(start)
        dispatch(engine, cmd, start)
        engine.tick(start + 10)
    assert engine.tick(100) == []
    assert engine.snapshot()["phase"] == "blocked"


def test_operation_route_unavailable_waits_bounded_without_fallback_spam():
    cfg = config()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["when"] = [{"entity_id": "sensor.transport", "state": "ir"}]
    engine = Engine(cfg)
    observed(engine)
    observed(engine, "sensor.transport", "ip")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.tick(901) == []
    assert engine.snapshot()["phase"] == "blocked"


def test_numeric_feedback_mapping_and_stale_safety_range():
    cfg = config()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap["observation"]["value_map"] = {"awake": True, "standby": False}
    cap["operations"][1]["guards"] = [{"entity_id": "sensor.watts", "minimum": 0, "maximum": 3, "max_age": 5}]
    engine = Engine(cfg)
    observed(engine, value="awake")
    observed(engine, "sensor.watts", "2")
    engine.finish(10)
    assert engine.tick(10) == []
    assert any(b["reason"] == "shutdown_unconfirmed" for b in engine.snapshot()["blockers"])
    engine.retry(11)
    observed(engine, "sensor.watts", "bad", now=11)
    assert engine.tick(11) == []
    engine.retry(12)
    observed(engine, "sensor.watts", "4", now=12)
    assert engine.tick(12) == []
    engine.retry(13)
    observed(engine, "sensor.watts", "2", now=13)
    assert len(engine.tick(13)) == 1


def test_number_state_values_are_normalized_without_nan_diagnostics():
    cfg = one_shot_and_volume()
    cap = cfg["equipment"][0]["capabilities"][1]
    cap["observation"] = {"entity_id": "number.volume"}
    cfg["activities"][0]["requirements"] = [requirement("volume", cap="volume", value=.3)]
    engine = Engine(cfg)
    observed(engine, "number.volume", ".3")
    engine.request("watch", 0)
    assert engine.tick(0) == []
    assert engine.snapshot()["verified_ready"]
    observed(engine, "number.volume", "nan", now=1)
    assert engine._read_cap("tv", "volume")[1] is False
    json.dumps(engine.snapshot(), allow_nan=False)
    observed(engine, "number.volume", "nonsense", now=2)
    assert engine._read_cap("tv", "volume")[1] is False


def test_observer_switch_cancels_unsubmitted_reservations_but_not_driver():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    engine.set_observer(True, 1)
    assert not engine.can_dispatch(cmd["id"], 1)
    engine.set_observer(True, 2)
    assert engine.tick(2) == []
    engine.completed("missing", 2)
    engine.submitted("missing", 2)
    engine.set_preserve(False, 2)


def test_session_override_requires_supported_action_and_active_session():
    cfg = config()
    cfg["equipment"][0]["capabilities"][0]["operations"] = cfg["equipment"][0]["capabilities"][0]["operations"][:1]
    cfg["equipment"][0]["capabilities"][0].pop("cleanup")
    engine = Engine(cfg)
    with pytest.raises(ValueError, match="current activity"):
        engine.set_override("tv", "power", True, 0)
    engine.request("watch", 0)
    with pytest.raises(ValueError, match="Unsupported"):
        engine.set_override("tv", "power", False, 0)


@pytest.mark.parametrize("confirmation,phase,problem", [("best_effort", "applied_unverified", False), ("required", "blocked", True)])
def test_failed_optional_media_stop_does_not_block_other_device_preparation(confirmation, phase, problem):
    cfg = config()
    radio = capability("radio", "media_player.music_assistant", kind="playback")
    radio["retry_policy"] = {"max_attempts": 1}
    cfg["equipment"][0]["capabilities"].append(radio)
    cfg["activities"][0]["requirements"] = [
        requirement("radio_stop", cap="radio", value=False, confirmation=confirmation),
        requirement(),
    ]
    engine = Engine(cfg)
    observed(engine, value="off")
    observed(engine, "media_player.music_assistant", "unavailable")
    engine.request("watch", 0)
    stop, = engine.tick(0)
    assert stop["capability_id"] == "radio"
    dispatch(engine, stop, 0, error="action_failed:HomeAssistantError")
    power, = engine.tick(1)
    assert power["capability_id"] == "power"
    dispatch(engine, power, 1, value="on")
    snapshot = engine.snapshot()
    assert snapshot["phase"] == phase
    assert snapshot["problem"] is problem
    assert not snapshot["verified_ready"]
    assert any(b["capability"] == "radio" and b["reason"] == "action_failed" for b in snapshot["blockers"])
    assert not any(e["reason"] == "command_applied_unverified" and e.get("command_id") == stop["id"] for e in snapshot["events"])
    assert engine.tick(200) == []
    # Real feedback can clear uncertainty without another command.
    observed(engine, "media_player.music_assistant", "off", now=201)
    assert engine.tick(201) == []
    assert engine.snapshot()["verified_ready"]


@pytest.mark.parametrize("confirmation,phase", [("best_effort", "applied_unverified"), ("required", "blocked")])
def test_reported_feedback_timeout_retains_configured_confirmation_policy(confirmation, phase):
    cfg = config()
    cfg["activities"][0]["requirements"][0]["confirmation"] = confirmation
    cfg["equipment"][0]["capabilities"][0]["retry_policy"] = {"max_attempts": 1}
    engine = Engine(cfg)
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0)
    assert engine.tick(11) == []
    assert engine.snapshot()["phase"] == phase
    assert engine.snapshot()["capabilities"][0]["feedback_type"] == "reported"
    assert any(b["reason"] == "confirmation_timeout" for b in engine.snapshot()["blockers"])


def test_failed_best_effort_one_shot_unblocks_explicit_dependency_once():
    cfg = config(feedback="command_only")
    cfg["equipment"].append({"id": "receiver", "capabilities": [capability(entity="media_player.receiver")]})
    cfg["activities"][0]["requirements"].append(requirement("receiver_on", "receiver", depends_on=["tv_on"]))
    engine = Engine(cfg)
    observed(engine, "media_player.receiver", "off")
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    dispatch(engine, cmd, 0, error="failed")
    receiver, = engine.tick(1)
    assert receiver["equipment_id"] == "receiver"
    dispatch(engine, receiver, 1, value="on")
    assert engine.snapshot()["phase"] == "applied_unverified"
    assert engine.tick(1000) == []


def test_shutdown_guard_waits_for_delayed_fresh_safety_confirmation():
    cfg = supply_config(policy="standby_on")
    supply = cfg["equipment"][1]["capabilities"][0]
    supply["operations"][1]["guards"] = [{"entity_id": "binary_sensor.safe_to_disconnect", "state": "on"}]
    cfg["policies"] = [{"id": "sleep", "finish_activity": True, "requirements": [
        requirement("off", value=False), requirement("cut", "plug", "supply", False, depends_on=["off"], min_delay=15)]}]
    engine = Engine(cfg)
    observed(engine, value="on")
    observed(engine, "switch.plug", "on")
    observed(engine, "binary_sensor.safe_to_disconnect", "off")
    engine.apply_policy("sleep", 0)
    off, = engine.tick(0)
    dispatch(engine, off, 0, value="off")
    assert engine.tick(1) == []
    assert engine.snapshot()["phase"] == "preparing"
    observed(engine, "binary_sensor.safe_to_disconnect", "on", now=16)
    cut, = engine.tick(16)
    assert cut["equipment_id"] == "plug"
    engine.submitted(cut["id"], 16)
    observed(engine, "switch.plug", "off", now=17)
    assert engine.tick(17) == []  # Report can arrive before the HA action returns.
    engine.completed(cut["id"], 17)
    assert engine.tick(18) == []
    assert engine.snapshot()["policy_ready"]
    assert not engine._read_cap("tv", "power")[1]  # Network readiness remains invalid.
    engine.request("watch", 19)
    assert not engine.snapshot()["verified_ready"]


def test_disabled_detection_cannot_block_a_shutdown_policy():
    cfg = config()
    cfg["policies"] = [{"id": "sleep", "finish_activity": True, "requirements": [requirement(value=False)]}]
    cfg["detection_rules"] = [{"id": "wake", "activity": "watch", "conditions": [{"entity_id": "binary_sensor.video", "state": "on"}],
        "guards": [{"entity_id": "input_boolean.detection_allowed", "state": "on"}]}]
    engine = Engine(cfg)
    observed(engine, value="on")
    observed(engine, "binary_sensor.video", "on")
    observed(engine, "input_boolean.detection_allowed", "off")
    engine.apply_policy("sleep", 0)
    off, = engine.tick(0)
    engine.submitted(off["id"], 0)
    observed(engine, "binary_sensor.video", "off", now=1)
    observed(engine, value="off", now=1)
    engine.completed(off["id"], 1)
    assert engine.tick(3) == []
    assert engine.snapshot()["policy_ready"]
    assert not engine.ambiguous


def test_detection_disabled_during_debounce_does_not_create_ambiguity():
    cfg = detection_config()
    for rule in cfg["detection_rules"]:
        rule["guards"] = [{"entity_id": "input_boolean.detection_allowed", "state": "on"}]
    engine = Engine(cfg)
    observed(engine, "select.tv_source", "TV")
    observed(engine, "input_boolean.detection_allowed", "on")
    engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "select.tv_source", "Game", now=1)
    observed(engine, "input_boolean.detection_allowed", "off", now=2)
    engine.tick(3)
    assert not engine.ambiguous
    assert engine.snapshot()["activity_id"] == "watch"


@pytest.mark.parametrize("previous,previous_fresh", [("unavailable", True), ("unknown", True), ("Game", False)])
def test_startup_or_network_reconnect_is_baseline_not_external_wake(previous, previous_fresh):
    cfg = detection_config()
    cfg["equipment"][0]["capabilities"].append(capability())
    cfg["activities"][0]["requirements"].append(requirement())
    original = Engine(cfg)
    original.request("watch", 0)
    engine = Engine(cfg, 100, original.export_state())
    observed(engine, value="off", now=100)
    observed(engine, "select.tv_source", previous, now=100, fresh=previous_fresh)
    observed(engine, "select.tv_source", "TV", now=101, fresh=True)
    assert engine.tick(104) == []
    assert engine.snapshot()["phase"] == "recovery_paused"
    assert not engine.snapshot()["pending_commands"]


def test_directional_wake_trigger_ignores_signal_loss_then_detects_real_wake():
    cfg = config()
    cfg["detection_rules"] = [{"id": "wake", "activity": "watch", "conditions": [{"entity_id": "binary_sensor.video", "state": "on"}],
        "trigger_bindings": [{"entity_id": "binary_sensor.video", "from": "off", "to": "on"}]}]
    engine = Engine(cfg)
    observed(engine, value="off")
    observed(engine, "binary_sensor.video", "on")
    engine.finish(0)
    engine.tick(0)
    observed(engine, "binary_sensor.video", "off", now=1)
    assert engine.tick(4) == []
    assert not engine.ambiguous
    assert engine.snapshot()["activity_id"] == "idle"
    observed(engine, "binary_sensor.video", "on", now=5)
    wake, = engine.tick(7)
    assert engine.snapshot()["activity_id"] == "watch"
    assert wake["value"] is True


def test_unresolved_driver_after_deadline_blocks_without_cancelling_then_recovers():
    engine = Engine(config())
    observed(engine)
    engine.request("watch", 0)
    cmd, = engine.tick(0)
    engine.submitted(cmd["id"], 0)
    assert engine.tick(11) == []
    assert engine.snapshot()["phase"] == "blocked"
    assert any(b["reason"] == "command_still_pending" for b in engine.snapshot()["blockers"])
    assert engine.tick(20) == []
    observed(engine, value="on", now=21)
    engine.completed(cmd["id"], 21)
    assert engine.snapshot()["verified_ready"]
    assert not engine.snapshot()["pending_commands"]


@pytest.mark.parametrize("previous_state", ["unavailable", "unknown"])
@pytest.mark.parametrize("explicit_binding", [False, True])
def test_attribute_reconnection_with_retained_source_cannot_wake_saved_activity(previous_state, explicit_binding):
    cfg = config()
    rule = {"id": "receiver_source", "activity": "watch", "conditions": [
        {"entity_id": "media_player.receiver", "attribute": "source", "state": "HDMI 1"}
    ]}
    if explicit_binding:
        rule["trigger_bindings"] = [{"entity_id": "media_player.receiver", "attribute": "source"}]
    cfg["detection_rules"] = [rule]
    original = Engine(cfg)
    original.request("watch", 0)
    engine = Engine(cfg, 100, original.export_state())
    observed(engine, value="off", now=100)
    observed(engine, "media_player.receiver", previous_state, now=100, attributes={"source": "HDMI 2"})
    # Both source attributes are known, but the prior entity state was unavailable.
    observed(engine, "media_player.receiver", "on", now=101, attributes={"source": "HDMI 1"})
    assert engine.tick(104) == []
    assert engine.snapshot()["phase"] == "recovery_paused"
    assert not engine.snapshot()["pending_commands"]
    assert not engine.ambiguous
    # Once reconnected, a genuine known-to-known source selection still works.
    observed(engine, "media_player.receiver", "on", now=105, attributes={"source": "HDMI 2"})
    engine.tick(108)
    observed(engine, "media_player.receiver", "on", now=109, attributes={"source": "HDMI 1"})
    command, = engine.tick(112)
    assert command["value"] is True
    assert engine.snapshot()["origin"] == "detected"


@pytest.mark.parametrize("next_state", ["unavailable", "unknown"])
def test_attribute_changes_while_entity_disconnects_do_not_create_ambiguity(next_state):
    cfg = config()
    cfg["detection_rules"] = [{"id": "receiver_source", "activity": "watch", "conditions": [
        {"entity_id": "media_player.receiver", "attribute": "source", "state": "HDMI 1"}
    ]}]
    engine = Engine(cfg)
    observed(engine, value="on")
    observed(engine, "media_player.receiver", "on", attributes={"source": "HDMI 2"})
    request_id = engine.request("watch", 0)
    engine.tick(0)
    observed(engine, "media_player.receiver", next_state, now=1, attributes={"source": "HDMI 1"})
    assert engine.tick(4) == []
    assert engine.snapshot()["request_id"] == request_id
    assert engine.snapshot()["verified_ready"]
    assert not engine.ambiguous
