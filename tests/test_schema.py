"""Configuration contracts reject ambiguity before any device actions exist."""
from copy import deepcopy

import pytest

from custom_components.media_activities.schema import controlled_entities, required_entities, validate_config


def base():
    return {"version": 1, "name": "Television", "equipment": [{"id": "tv", "capabilities": [{
        "id": "source", "kind": "input", "value_type": "string", "observation": {"entity_id": "media_player.tv", "attribute": "source"},
        "operations": [{"any_value": True, "action": "media_player.select_source", "target": {"entity_id": "media_player.tv"}, "value_field": "source"}]}]}],
        "activities": [{"id": "watch", "requirements": [{"id": "source", "equipment": "tv", "capability": "source", "value": "TV"}]}]}


def test_detached_normalized_config_and_defaults():
    cfg = base()
    normalized = validate_config(cfg)
    assert "observer_only" not in cfg
    assert normalized["observer_only"] is True
    assert normalized["preserve_devices"] is False
    cap = normalized["equipment"][0]["capabilities"][0]
    assert cap["feedback_type"] == "reported"
    assert cap["timeout"] == 30
    assert cap["retry_policy"] == {"max_attempts": 3, "backoff": [2, 5]}
    assert cap["operations"][0]["idempotent"] is False
    assert validate_config(normalized) == normalized


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(version=99),
    lambda c: c.update(name=""),
    lambda c: c.update(observer_only="yes"),
    lambda c: c["equipment"].append(deepcopy(c["equipment"][0])),
    lambda c: c["equipment"][0]["capabilities"].append(deepcopy(c["equipment"][0]["capabilities"][0])),
    lambda c: c["equipment"][0]["capabilities"][0].update(timeout=-1),
    lambda c: c["equipment"][0]["capabilities"][0].update(value_type="object"),
    lambda c: c["equipment"][0]["capabilities"][0].update(feedback_type="assumed"),
    lambda c: c["equipment"][0]["capabilities"][0].update(feedback_type="command_only"),
    lambda c: c["equipment"][0]["capabilities"][0]["operations"][0].update(value="TV"),
    lambda c: c["equipment"][0]["capabilities"][0]["operations"][0].update(value_field=""),
    lambda c: c["equipment"][0]["capabilities"][0]["operations"][0].update(action="shell unsafe"),
    lambda c: c["activities"][0]["requirements"][0].update(capability="missing"),
    lambda c: c["activities"][0]["requirements"][0].update(depends_on=["missing"]),
    lambda c: c["activities"][0]["requirements"][0].update(depends_on=["source"]),
    lambda c: c["activities"][0]["requirements"].append(deepcopy(c["activities"][0]["requirements"][0])),
    lambda c: c["activities"][0].update(id="idle"),
    lambda c: c.update(limits={"event_limit": 501}),
])
def test_rejects_invalid_configuration(mutate):
    cfg = base()
    # The command-only case must explicitly request a required confirmation.
    cfg["activities"][0]["requirements"][0]["confirmation"] = "required"
    mutate(cfg)
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_controlled_resource_cannot_belong_to_two_equipment_items():
    cfg = base()
    duplicate = deepcopy(cfg["equipment"][0])
    duplicate["id"] = "second_tv"
    cfg["equipment"].append(duplicate)
    with pytest.raises(ValueError, match="already owned"):
        validate_config(cfg)


def test_all_entities_including_guards_policies_detection_and_operations():
    cfg = base()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap["prerequisites"] = [{"entity_id": "binary_sensor.ready", "state": "on"}]
    cap["operations"][0]["when"] = [{"entity_id": "sensor.transport", "states": ["ir", "ip"]}]
    cap["operations"][0]["guards"] = [{"entity_id": "input_boolean.allow", "state": "on"}]
    cfg["detection_rules"] = [{"id": "detected", "activity": "watch", "conditions": [{"entity_id": "select.source", "state": "TV"}], "trigger_entities": ["sensor.trigger"]}]
    cfg["policies"] = [{"id": "standby", "guards": [{"entity_id": "binary_sensor.allowed", "state": "on"}], "requirements": []}]
    cfg = validate_config(cfg)
    assert required_entities(cfg) == {"media_player.tv", "binary_sensor.ready", "sensor.transport", "input_boolean.allow", "select.source", "sensor.trigger", "binary_sensor.allowed"}
    assert controlled_entities(cfg) == {"media_player.tv"}


def test_supply_is_optional_and_defaults_to_retained_standby():
    cfg = base()
    cfg["equipment"].append({"id": "plug", "capabilities": [{"id": "supply", "kind": "supply", "observation": {"entity_id": "switch.plug"}, "operations": []}]})
    cap = validate_config(cfg)["equipment"][1]["capabilities"][0]
    assert cap["supply_policy"] == "standby_on"
    assert cap["consumers"] == ["plug"]
    cfg["equipment"][1]["capabilities"][0]["consumers"] = ["missing"]
    with pytest.raises(ValueError, match="unknown supply consumer"):
        validate_config(cfg)


@pytest.mark.parametrize("predicate", [
    {"entity_id": "sensor.x"}, {"entity_id": "bad", "state": "on"},
    {"entity_id": "sensor.x", "states": []}, {"entity_id": "sensor.x", "state": []},
    {"entity_id": "sensor.x", "minimum": 10, "maximum": 1},
    {"entity_id": "sensor.x", "state": "on", "max_age": 0},
])
def test_predicate_validation(predicate):
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["prerequisites"] = [predicate]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_numeric_range_and_finite_values():
    cfg = base()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap.update(kind="volume", value_type="number", allowed_values={"minimum": 0, "maximum": 1})
    cfg["activities"][0]["requirements"][0]["value"] = .3
    assert validate_config(cfg)
    cfg["activities"][0]["requirements"][0]["value"] = 2
    with pytest.raises(ValueError):
        validate_config(cfg)


@pytest.mark.parametrize("target,data", [
    ({"area_id": "living_room"}, {}),
    ({"device_id": "abcdef"}, {}),
    ({"entity_id": "media_player.tv"}, {"entity_id": "media_player.other"}),
    ({}, {}),
])
def test_ownership_cannot_be_bypassed_with_unbounded_targets(target, data):
    cfg = base()
    op = cfg["equipment"][0]["capabilities"][0]["operations"][0]
    op.update(target=target, data=data)
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_targetless_ha_action_requires_declared_resource_ids():
    cfg = base()
    op = cfg["equipment"][0]["capabilities"][0]["operations"][0]
    op.update(target={}, action="esphome.send_ir", resource_ids=["remote.ir_blaster"])
    normalized = validate_config(cfg)
    assert controlled_entities(normalized) == {"remote.ir_blaster"}
    assert "remote.ir_blaster" in required_entities(normalized)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1, 2}, object()])
def test_non_json_action_data_rejected(bad):
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["data"] = {"invalid": bad}
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_value_injection_cannot_change_controlled_entity():
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["value_field"] = "entity_id"
    with pytest.raises(ValueError):
        validate_config(cfg)


@pytest.mark.parametrize("value", [None, [], "text", 1])
def test_top_level_must_be_mapping(value):
    with pytest.raises(ValueError):
        validate_config(value)


@pytest.mark.parametrize("update", [
    {"id": "Invalid Id"}, {"kind": "arbitrary_program"}, {"timeout": "ten"},
    {"timeout": True}, {"allowed_values": []}, {"allowed_values": {"step": 1}},
    {"feedback_type": "reported", "observation": None},
    {"observation": {"entity_id": "media_player.tv", "attribute": 1}},
    {"retry_policy": {"max_attempts": 0}}, {"retry_policy": {"max_attempts": 2.5}},
    {"operations": {}}, {"cleanup": {"value": "Off"}},
    {"kind": "supply", "value_type": "string", "supply_policy": "guess"},
])
def test_invalid_capability_contracts(update):
    cfg = base()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap.update(update)
    if "cleanup" in update:
        cap["operations"] = [{"value": "TV", "action": "media_player.select_source", "target": {"entity_id": "media_player.tv"}}]
    with pytest.raises(ValueError):
        validate_config(cfg)


@pytest.mark.parametrize("update", [
    {"idempotent": "yes"}, {"any_value": False, "value": "TV", "value_field": 1},
    {"value_field": "params.source", "data": {"params": "not_an_object"}},
    {"value_field": ".source"}, {"target": {"entity_id": 1}},
    {"data": {1: "bad_key"}},
])
def test_invalid_operation_contracts(update):
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["operations"][0].update(update)
    with pytest.raises(ValueError):
        validate_config(cfg)


@pytest.mark.parametrize("value_type,value,allowed", [
    ("boolean", "yes", None), ("number", True, None), ("string", 10, None),
    ("enum", "A", ["B"]), ("string", "A", {"minimum": 0}),
])
def test_desired_values_are_typed_and_constrained(value_type, value, allowed):
    cfg = base()
    cap = cfg["equipment"][0]["capabilities"][0]
    cap["value_type"] = value_type
    if allowed is not None:
        cap["allowed_values"] = allowed
    cfg["activities"][0]["requirements"][0]["value"] = value
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_distinct_requirement_ids_cannot_command_same_capability_twice():
    cfg = base()
    second = deepcopy(cfg["activities"][0]["requirements"][0])
    second.update(id="conflicting", value="Game")
    cfg["activities"][0]["requirements"].append(second)
    with pytest.raises(ValueError, match="only one desired"):
        validate_config(cfg)


def test_unsupported_required_value_and_confirmation_policy():
    cfg = base()
    cfg["activities"][0]["requirements"][0]["confirmation"] = "pretend"
    with pytest.raises(ValueError):
        validate_config(cfg)
    cfg["activities"][0]["requirements"][0]["confirmation"] = "required"
    cfg["equipment"][0]["capabilities"][0]["operations"] = [{"value": "Game", "action": "media_player.select_source", "target": {"entity_id": "media_player.tv"}}]
    with pytest.raises(ValueError, match="no supported operation"):
        validate_config(cfg)


@pytest.mark.parametrize("policies", [
    [{"id": "sleep"}, {"id": "sleep"}],
    [{"id": "sleep", "suspend_activity": False}],
    [{"id": "sleep", "finish_activity": "yes"}],
])
def test_policy_contract_validation(policies):
    cfg = base()
    cfg["policies"] = policies
    with pytest.raises(ValueError):
        validate_config(cfg)


@pytest.mark.parametrize("change", [
    {"activity": "missing"}, {"conditions": []},
    {"conditions": [{"entity_id": "sensor.source", "state": "TV", "attribute": 1}]},
    {"trigger_bindings": [{"entity_id": "media_player.tv", "attribute": 1}]},
])
def test_detection_contract_validation(change):
    cfg = base()
    rule = {"id": "manual", "activity": "watch", "conditions": [{"entity_id": "sensor.source", "state": "TV"}]}
    rule.update(change)
    cfg["detection_rules"] = [rule]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_duplicate_detection_rejected_and_trigger_bindings_exported():
    cfg = base()
    rule = {"id": "manual", "activity": "watch", "conditions": [{"entity_id": "sensor.source", "state": "TV"}],
        "trigger_bindings": [{"entity_id": "media_player.tv", "attribute": "source"}]}
    cfg["detection_rules"] = [rule]
    assert "media_player.tv" in required_entities(validate_config(cfg))
    cfg["detection_rules"].append(deepcopy(rule))
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_resource_entity_list_and_integer_limits():
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["operations"][0]["target"]["entity_id"] = ["media_player.tv", "media_player.second"]
    assert controlled_entities(validate_config(cfg)) == {"media_player.tv", "media_player.second"}
    assert "media_player.second" in required_entities(cfg)
    cfg["limits"] = {"recovery_episodes": 1.5}
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_observation_mapping_and_numeric_predicates_are_supported():
    cfg = base()
    cfg["equipment"][0]["capabilities"][0]["observation"]["value_map"] = {"0": "TV", "1": "Game"}
    cfg["equipment"][0]["capabilities"][0]["prerequisites"] = [{"entity_id": "sensor.temperature", "minimum": -5, "maximum": 40}]
    assert validate_config(cfg)
