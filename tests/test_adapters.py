"""Factory contracts exercise capability limits, feedback honesty and portability."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from custom_components.media_activities.adapters import (
    FEATURES, add_supply, build_apple_tv_equipment, build_entity_equipment,
    build_epson_equipment, build_hdfury_equipment, build_media_player_equipment,
    build_music_assistant_equipment, build_sony_equipment,
)
from custom_components.media_activities.schema import validate_config


def capability(equipment, name):
    return next(c for c in equipment["capabilities"] if c["id"] == name)


def validate_equipment(equipment):
    return validate_config({"name": "Portable test", "equipment": [equipment], "activities": []})


def test_tv_only_source_needs_no_receiver_supply_or_power():
    tv = build_media_player_equipment("media_player.tv", {"supported_features": 2048,
                                                            "source_list": ["Live TV", "HDMI 1"]})
    assert [c["id"] for c in tv["capabilities"]] == ["input"]
    assert capability(tv, "input")["operations"] == [{"action": "media_player.select_source",
        "target": {"entity_id": "media_player.tv"}, "data": {}, "idempotent": True,
        "any_value": True, "value_field": "source"}]
    assert capability(tv, "input")["observation"]["valid_when"] == [
        {"entity_id": "media_player.tv", "states": ["on", "idle", "playing", "paused", "buffering"]}
    ]
    validate_equipment(tv)


def test_all_advertised_features_generate_typed_supported_capabilities():
    tv = build_media_player_equipment("media_player.tv", {"friendly_name": "Television",
        "supported_features": sum(FEATURES.values()), "source_list": ["HDMI"],
        "sound_mode_list": ["Cinema"]})
    assert tv["name"] == "Television"
    assert {c["id"] for c in tv["capabilities"]} == {
        "power", "input", "sound_mode", "volume", "mute", "playback", "media"}
    assert capability(tv, "power")["observation"]["value_map"]["paused"] is True
    assert capability(tv, "power")["cleanup"] == {"value": False}
    assert capability(tv, "volume")["allowed_values"] == {"minimum": 0, "maximum": 1}
    assert capability(tv, "playback")["allowed_values"] == ["playing", "paused", "idle"]
    assert capability(tv, "media")["feedback_type"] == "command_only"
    assert capability(tv, "media")["operations"][0]["idempotent"] is False
    validate_equipment(tv)


def test_physical_power_is_not_generated_for_cast_playback_session():
    player = build_media_player_equipment("media_player.cast", {"supported_features": 384}, role="playback")
    assert [c["id"] for c in player["capabilities"]] == ["state"]
    assert capability(player, "state")["operations"] == []
    validate_equipment(player)


def test_missing_features_produces_observation_only_and_does_not_invent_actions():
    device = build_media_player_equipment("media_player.tv", {})
    assert device["capabilities"][0]["operations"] == []
    validate_equipment(device)


def test_feature_override_and_one_way_power():
    player = build_media_player_equipment("media_player.tv", {"supported_features": 384},
                                         supported_features={"MediaPlayerEntityFeature.TURN_OFF"},
                                         id="television", name="Custom")
    assert player["id"] == "television" and player["name"] == "Custom"
    assert [o["value"] for o in capability(player, "power")["operations"]] == [False]
    assert capability(player, "power")["cleanup"] == {"value": False}
    validate_equipment(player)


def test_on_only_player_does_not_invent_cleanup():
    player = build_media_player_equipment("media_player.tv", {}, supported_features={"TURN_ON"})
    assert "cleanup" not in capability(player, "power")
    validate_equipment(player)


@pytest.mark.parametrize("features", [0, None, frozenset(), [], (), {"PLAY", "PAUSE"}, ("PLAY",)])
def test_supported_feature_input_shapes(features):
    validate_equipment(build_media_player_equipment("media_player.tv", {"supported_features": features}))


@pytest.mark.parametrize("features", [-1, True, "128", 1.5, {"BOGUS"}, [128]])
def test_invalid_features_rejected(features):
    with pytest.raises(ValueError):
        build_media_player_equipment("media_player.tv", {}, supported_features=features)


@pytest.mark.parametrize("entity,id,role", [("tv", None, "physical"), ("switch.tv", None, "physical"),
    ("media_player.TV", None, "physical"), ("media_player.tv", "bad name", "physical"),
    ("media_player.tv", None, "nonsense")])
def test_invalid_identity_or_role_rejected(entity, id, role):
    with pytest.raises(ValueError):
        build_media_player_equipment(entity, {}, id=id, role=role)


def test_no_source_lists_remains_editable_string_and_copies_lists():
    attrs = {"source_list": ["HDMI"], "supported_features": FEATURES["SELECT_SOURCE"]}
    result = build_media_player_equipment("media_player.tv", attrs)
    attrs["source_list"].append("Mutated")
    assert capability(result, "input")["allowed_values"] == ["HDMI"]
    blank = build_media_player_equipment("media_player.tv", {}, supported_features={"SELECT_SOURCE", "SELECT_SOUND_MODE"})
    assert all(c["value_type"] == "string" for c in blank["capabilities"])
    validate_equipment(blank)


@pytest.mark.parametrize("entity,domain,attrs,capid,feedback", [
    ("switch.amplifier", None, {}, "power", "reported"),
    ("select.input", "select", {"options": ["A", "B"]}, "input", "reported"),
    ("select.input", None, {}, "input", "reported"),
    ("number.volume", "number", {"min": 0, "max": 100}, "value", "reported"),
    ("number.setting", None, {}, "value", "reported"),
    ("button.wake", "button", {}, "press", "command_only"),
    ("media_player.tv", None, {"supported_features": 128}, "power", "reported"),
])
def test_native_entities(entity, domain, attrs, capid, feedback):
    result = build_entity_equipment(entity, domain, attrs)
    assert capability(result, capid)["feedback_type"] == feedback
    validate_equipment(result)


@pytest.mark.parametrize("entity,domain", [("light.tv", None), ("switch.tv", "select"), ("broken", None)])
def test_unsupported_native_entities(entity, domain):
    with pytest.raises(ValueError):
        build_entity_equipment(entity, domain)


@pytest.mark.parametrize("policy", ["standby_on", "always_on", "managed", "unmanaged"])
def test_optional_supply_never_grants_implicit_cut_permission(policy):
    original = build_entity_equipment("switch.amplifier")
    saved = deepcopy(original)
    result = add_supply(original, "switch.socket", policy=policy)
    supply = capability(result, "supply")
    assert supply["supply_policy"] == policy
    assert all(o["value"] is True for o in supply["operations"])
    assert original == saved
    validate_equipment(result)
    with pytest.raises(ValueError, match="already"):
        add_supply(result, "switch.second_socket")


def test_bad_supply_rejected():
    device = build_entity_equipment("switch.amplifier")
    with pytest.raises(ValueError):
        add_supply(device, "light.socket")
    with pytest.raises(ValueError):
        add_supply(device, "switch.socket", policy="cut_freely")


def test_epson_does_not_claim_warmup_or_cooldown_is_confirmed_on_or_off():
    device = build_epson_equipment("media_player.projector", color_modes=["Cinema", "Game"])
    assert capability(device, "power")["observation"]["value_map"] == {"on": True, "off": False}
    assert capability(device, "power_state")["observation"] == {"entity_id": "media_player.projector"}
    mode = capability(device, "picture_mode")
    assert mode["timeout"] == 600
    assert mode["observation"]["attribute"] == "color_mode"
    assert mode["operations"][0]["action"] == "epson_projector_link.select_color_mode"
    assert "supply" not in [c["id"] for c in device["capabilities"]]
    validate_equipment(device)
    validate_equipment(build_epson_equipment("media_player.projector", supply_entity_id="switch.projector"))


def test_sony_features_remain_supported_and_supply_optional():
    device = build_sony_equipment("media_player.receiver", {"source_list": ["TV", "HDMI"]})
    assert {c["id"] for c in device["capabilities"]} == {"power", "input"}
    validate_equipment(device)
    overridden = build_sony_equipment("media_player.receiver", {"supported_features": FEATURES["VOLUME_SET"]},
                                     supply_entity_id="switch.receiver")
    assert {c["id"] for c in overridden["capabilities"]} == {"volume", "supply"}
    validate_equipment(overridden)


def test_apple_tv_remote_state_is_never_used_for_power_feedback():
    device = build_apple_tv_equipment("media_player.streamer", "remote.streamer", {"supported_features": 384})
    power = capability(device, "power")
    assert power["feedback_type"] == "command_only" and "observation" not in power
    assert [o["data"]["command"] for o in power["operations"]] == ["wakeup", "suspend"]
    validate_equipment(device)
    obs = {"entity_id": "binary_sensor.streamer_video", "value_map": {"on": True, "off": False}}
    observed = build_apple_tv_equipment("media_player.streamer", "remote.streamer", power_observation=obs,
                                       supply_entity_id="switch.streamer")
    obs["value_map"]["on"] = False
    assert capability(observed, "power")["observation"]["value_map"]["on"] is True
    validate_equipment(observed)


def test_hdfury_does_not_assume_ports_outputs_or_sockets():
    device = build_hdfury_equipment({"picture": "select.route_a", "sound": "select.route_b"},
                                    ports=["3", "1"], id="matrix", name="Matrix")
    assert [c["id"] for c in device["capabilities"]] == ["route_picture", "route_sound"]
    assert capability(device, "route_sound")["allowed_values"] == ["3", "1"]
    validate_equipment(device)
    validate_equipment(build_hdfury_equipment({"picture": "select.route_a"}, supply_entity_id="switch.matrix"))
    for routes in [{}, {"bad-name": "select.route"}, {"picture": "switch.route"}]:
        with pytest.raises(ValueError):
            build_hdfury_equipment(routes)


def test_music_assistant_is_one_shot_playback_not_physical_power():
    device = build_music_assistant_equipment("media_player.music", {"supported_features": 384 + 512 + 4096})
    assert "power" not in [c["id"] for c in device["capabilities"]]
    assert len([c for c in device["capabilities"] if c["id"] == "media"]) == 1
    media = capability(device, "media")
    assert media["feedback_type"] == "command_only"
    assert media["operations"][0]["action"] == "music_assistant.play_media"
    assert media["operations"][0]["data"] == {"media_type": "radio"}
    assert media["operations"][0]["idempotent"] is False
    validate_equipment(device)


def test_every_shipped_example_is_valid_and_observer_only():
    examples = Path(__file__).parents[1] / "examples"
    files = sorted(examples.glob("*.json"))
    assert len(files) == 6
    for path in files:
        data = json.loads(path.read_text())
        normalized = validate_config(data)
        assert normalized["observer_only"] is True, path.name
        assert normalized["activities"], path.name
