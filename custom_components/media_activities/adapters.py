"""Pure configuration factories; all device communication stays with HA integrations.

Factories generate ordinary editable configuration, never register callbacks or call
services. Feature values match HA's public MediaPlayerEntityFeature contract.
"""
from __future__ import annotations

from collections.abc import Mapping, Set
from copy import deepcopy
import re
from typing import Any

FEATURES = {
    "PAUSE": 1, "VOLUME_SET": 4, "VOLUME_MUTE": 8, "TURN_ON": 128,
    "TURN_OFF": 256, "PLAY_MEDIA": 512, "SELECT_SOURCE": 2048,
    "STOP": 4096, "PLAY": 16384, "SELECT_SOUND_MODE": 65536,
}
_POWER_MAP = {"on": True, "idle": True, "playing": True, "paused": True,
              "buffering": True, "off": False, "standby": False}


def _entity(entity_id: str, domain: str | None = None) -> str:
    if not isinstance(entity_id, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", entity_id):
        raise ValueError("An existing HA entity ID is required")
    if domain and entity_id.split(".", 1)[0] != domain:
        raise ValueError(f"Expected a {domain} entity")
    return entity_id


def _base(entity_id: str, attributes: Mapping[str, Any], id: str | None,
          name: str | None, role: str) -> dict[str, Any]:
    _entity(entity_id)
    identifier = id or entity_id.split(".", 1)[1]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        raise ValueError("Equipment ID must be a lowercase stable identifier")
    return {"id": identifier,
            "name": name or attributes.get("friendly_name") or identifier.replace("_", " ").title(),
            "role": role, "capabilities": []}


def _operation(action: str, entity_id: str, *, value: Any = None,
               any_value: bool = False, value_field: str | None = None,
               data: Mapping[str, Any] | None = None,
               idempotent: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"action": action, "target": {"entity_id": entity_id},
                              "data": deepcopy(dict(data or {})), "idempotent": idempotent}
    result["any_value" if any_value else "value"] = True if any_value else value
    if value_field:
        result["value_field"] = value_field
    return result


def _cap(identifier: str, kind: str, value_type: str, *, entity_id: str | None = None,
         attribute: str | None = None, value_map: Mapping[str, Any] | None = None,
         operations: list[dict[str, Any]] | None = None, allowed: list[Any] | None = None,
         feedback: str = "reported", timeout: float = 30) -> dict[str, Any]:
    result: dict[str, Any] = {"id": identifier, "kind": kind, "value_type": value_type,
                              "feedback_type": feedback, "operations": operations or [],
                              "timeout": timeout}
    if entity_id:
        result["observation"] = {"entity_id": entity_id}
        if attribute:
            result["observation"]["attribute"] = attribute
        if value_map is not None:
            result["observation"]["value_map"] = deepcopy(dict(value_map))
    if allowed:
        result["allowed_values"] = list(allowed)
    return result


def _features(raw: int | Set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, bool):
        raise ValueError("supported_features must be a bitmask or feature names")
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError("supported_features cannot be negative")
        return {name for name, flag in FEATURES.items() if raw & flag}
    if isinstance(raw, (set, frozenset, list, tuple)) and all(isinstance(v, str) for v in raw):
        names = {v.upper().rsplit(".", 1)[-1] for v in raw}
        if names - FEATURES.keys():
            raise ValueError("Unsupported media-player feature name")
        return names
    raise ValueError("supported_features must be a bitmask or feature names")


def build_media_player_equipment(
    entity_id: str, attributes: Mapping[str, Any], *, id: str | None = None,
    name: str | None = None, role: str = "physical",
    supported_features: int | Set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Generate only advertised capabilities; playback-session entities omit power.

    A physical media player may use idle/playing/etc. as power-on observations.
    Select role='playback' for Cast/session entities whose state cannot establish
    physical power. Generated reported bindings must be commissioned against the
    chosen integration; a feature bit proves action support, not readback fidelity.
    """
    _entity(entity_id, "media_player")
    if role not in {"physical", "playback", "display", "audio", "source", "accessory"}:
        raise ValueError("Unsupported media-player role")
    result = _base(entity_id, attributes, id, name, role)
    caps = result["capabilities"]
    features = _features(supported_features if supported_features is not None
                         else attributes.get("supported_features", 0))
    if role != "playback" and features & {"TURN_ON", "TURN_OFF"}:
        ops = [_operation(f"media_player.turn_{suffix}", entity_id, value=value)
               for feature, suffix, value in [("TURN_ON", "on", True), ("TURN_OFF", "off", False)]
               if feature in features]
        caps.append(_cap("power", "power", "boolean", entity_id=entity_id,
                         value_map=_POWER_MAP, operations=ops, timeout=120))
        if "TURN_OFF" in features:
            caps[-1]["cleanup"] = {"value": False}
    for feature, identifier, attr, list_attr, field, action in [
        ("SELECT_SOURCE", "input", "source", "source_list", "source", "select_source"),
        ("SELECT_SOUND_MODE", "sound_mode", "sound_mode", "sound_mode_list", "sound_mode", "select_sound_mode"),
    ]:
        if feature in features:
            choices = attributes.get(list_attr)
            choices = list(choices) if isinstance(choices, (list, tuple)) and choices else None
            caps.append(_cap(identifier, "input" if identifier == "input" else "setting",
                             "enum" if choices else "string", entity_id=entity_id, attribute=attr,
                             allowed=choices, operations=[_operation(f"media_player.{action}", entity_id,
                                                                   any_value=True, value_field=field)]))
            caps[-1]["observation"]["valid_when"] = [
                {"entity_id": entity_id, "states": ["on", "idle", "playing", "paused", "buffering"]}
            ]
    if "VOLUME_SET" in features:
        caps.append(_cap("volume", "setting", "number", entity_id=entity_id, attribute="volume_level",
                         operations=[_operation("media_player.volume_set", entity_id, any_value=True,
                                                value_field="volume_level")]))
        caps[-1]["allowed_values"] = {"minimum": 0, "maximum": 1}
    if "VOLUME_MUTE" in features:
        caps.append(_cap("mute", "setting", "boolean", entity_id=entity_id, attribute="is_volume_muted",
                         operations=[_operation("media_player.volume_mute", entity_id, any_value=True,
                                                value_field="is_volume_muted")]))
    playback = [("PLAY", "playing", "media_play"), ("PAUSE", "paused", "media_pause"),
                ("STOP", "idle", "media_stop")]
    supported_playback = [(value, action) for feature, value, action in playback if feature in features]
    if supported_playback:
        caps.append(_cap("playback", "playback", "enum", entity_id=entity_id,
                         allowed=[v for v, _ in supported_playback],
                         operations=[_operation(f"media_player.{action}", entity_id, value=value)
                                     for value, action in supported_playback]))
    if "PLAY_MEDIA" in features:
        caps.append(_cap("media", "playback", "string", feedback="command_only",
                         operations=[_operation("media_player.play_media", entity_id, any_value=True,
                                                value_field="media_content_id", data={"media_content_type": "music"},
                                                idempotent=False)]))
    if not caps:
        caps.append(_cap("state", "setting", "string", entity_id=entity_id))
    return result


def build_entity_equipment(
    entity_id: str, domain: str | None = None, attributes: Mapping[str, Any] | None = None,
    *, id: str | None = None, name: str | None = None,
) -> dict[str, Any]:
    """Generate select, switch, number, button, or media-player bindings."""
    domain = domain or entity_id.split(".", 1)[0]
    _entity(entity_id, domain)
    attrs = attributes or {}
    if domain == "media_player":
        return build_media_player_equipment(entity_id, attrs, id=id, name=name)
    result = _base(entity_id, attrs, id, name, "accessory")
    if domain == "switch":
        capability = _cap("power", "power", "boolean", entity_id=entity_id,
                          value_map={"on": True, "off": False},
                          operations=[_operation(f"switch.turn_{suffix}", entity_id, value=value)
                                      for suffix, value in [("on", True), ("off", False)]])
    elif domain == "select":
        choices = attrs.get("options")
        capability = _cap("input", "input", "enum" if choices else "string", entity_id=entity_id,
                          allowed=list(choices) if choices else None,
                          operations=[_operation("select.select_option", entity_id, any_value=True,
                                                 value_field="option")])
    elif domain == "number":
        capability = _cap("value", "setting", "number", entity_id=entity_id,
                          operations=[_operation("number.set_value", entity_id, any_value=True,
                                                 value_field="value")])
        limits = {key: attrs[attr] for key, attr in [("minimum", "min"), ("maximum", "max")]
                  if isinstance(attrs.get(attr), (int, float)) and not isinstance(attrs.get(attr), bool)}
        if limits:
            capability["allowed_values"] = limits
    elif domain == "button":
        capability = _cap("press", "setting", "enum", feedback="command_only", allowed=["press"],
                          operations=[_operation("button.press", entity_id, value="press", idempotent=False)])
    else:
        raise ValueError("Supported entities: media_player, select, switch, number, button")
    result["capabilities"].append(capability)
    return result


def add_supply(equipment: Mapping[str, Any], entity_id: str, *, policy: str = "standby_on") -> dict[str, Any]:
    """Return a copy with an explicitly supplied optional socket binding.

    This factory never grants permission to cut power. Safe supply removal needs
    a separately configured policy and confirmed safety prerequisites.
    """
    _entity(entity_id, "switch")
    if policy not in {"unmanaged", "always_on", "standby_on", "managed"}:
        raise ValueError("Unsupported supply policy")
    result = deepcopy(dict(equipment))
    if any(c["id"] == "supply" for c in result["capabilities"]):
        raise ValueError("Supply is already configured")
    cap = _cap("supply", "supply", "boolean", entity_id=entity_id,
               value_map={"on": True, "off": False},
               operations=[] if policy == "unmanaged" else [_operation("switch.turn_on", entity_id, value=True)])
    cap["supply_policy"] = policy
    result["capabilities"].append(cap)
    return result


def build_epson_equipment(entity_id: str, attributes: Mapping[str, Any] | None = None, *,
                          id: str | None = None, name: str | None = None,
                          color_modes: list[str] | None = None,
                          color_mode_action: str = "epson_projector_link.select_color_mode",
                          supply_entity_id: str | None = None) -> dict[str, Any]:
    """Epson Projector Link preset. Queued mode commands still require readback."""
    result = build_media_player_equipment(entity_id, attributes or {}, id=id, name=name,
                                         role="display", supported_features={"TURN_ON", "TURN_OFF"})
    result["capabilities"][0]["observation"]["value_map"] = {"on": True, "off": False}
    result["capabilities"][0]["timeout"] = 600
    result["capabilities"].extend([
        _cap("power_state", "availability", "string", entity_id=entity_id),
        _cap("picture_mode", "setting", "enum" if color_modes else "string", entity_id=entity_id,
             attribute="color_mode", allowed=color_modes, timeout=600,
             operations=[_operation(color_mode_action, entity_id, any_value=True, value_field="color_mode")]),
    ])
    return add_supply(result, supply_entity_id) if supply_entity_id else result


def build_sony_equipment(entity_id: str, attributes: Mapping[str, Any] | None = None, *,
                         id: str | None = None, name: str | None = None,
                         supply_entity_id: str | None = None) -> dict[str, Any]:
    """Sony Songpal physical state; bind Cast or MA separately as playback."""
    attrs = dict(attributes or {})
    attrs.setdefault("supported_features", FEATURES["TURN_ON"] | FEATURES["TURN_OFF"] | FEATURES["SELECT_SOURCE"])
    result = build_media_player_equipment(entity_id, attrs, id=id, name=name, role="audio")
    return add_supply(result, supply_entity_id) if supply_entity_id else result


def build_apple_tv_equipment(entity_id: str, remote_entity_id: str, attributes: Mapping[str, Any] | None = None,
                             *, id: str | None = None, name: str | None = None,
                             power_observation: Mapping[str, Any] | None = None,
                             supply_entity_id: str | None = None) -> dict[str, Any]:
    """Use native remote wakeup/suspend; never treat the remote entity as power feedback."""
    _entity(remote_entity_id, "remote")
    result = build_media_player_equipment(entity_id, attributes or {}, id=id, name=name, role="playback")
    result["role"] = "source"
    power = _cap("power", "power", "boolean", feedback="reported" if power_observation else "command_only",
                 operations=[_operation("remote.send_command", remote_entity_id, value=value,
                                        data={"command": command})
                             for value, command in [(True, "wakeup"), (False, "suspend")]], timeout=120)
    if power_observation:
        power["observation"] = deepcopy(dict(power_observation))
    result["capabilities"].append(power)
    return add_supply(result, supply_entity_id) if supply_entity_id else result


def build_hdfury_equipment(routes: Mapping[str, str], *, id: str = "hdmi_router", name: str = "HDMI router",
                           ports: list[str] | None = None,
                           supply_entity_id: str | None = None) -> dict[str, Any]:
    """Named output routes are independent; no input-number assignments are assumed."""
    if not routes:
        raise ValueError("At least one output route is required")
    result = _base(next(iter(routes.values())), {}, id, name, "router")
    for channel, entity_id in routes.items():
        _entity(entity_id, "select")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", channel):
            raise ValueError("Route identifiers must be lowercase stable identifiers")
        result["capabilities"].append(_cap(f"route_{channel}", "route", "enum" if ports else "string",
                                          entity_id=entity_id, allowed=ports, timeout=90,
                                          operations=[_operation("select.select_option", entity_id,
                                                                 any_value=True, value_field="option")]))
    return add_supply(result, supply_entity_id) if supply_entity_id else result


def build_music_assistant_equipment(entity_id: str, attributes: Mapping[str, Any] | None = None, *,
                                    id: str | None = None, name: str | None = None,
                                    media_type: str = "radio") -> dict[str, Any]:
    """Playback-session preset; start media once and do not infer physical power."""
    result = build_media_player_equipment(entity_id, attributes or {}, id=id, name=name, role="playback")
    result["capabilities"] = [cap for cap in result["capabilities"] if cap["id"] != "media"]
    result["capabilities"].append(_cap("media", "playback", "string", feedback="command_only",
                                     operations=[_operation("music_assistant.play_media", entity_id,
                                                            any_value=True, value_field="media_id",
                                                            data={"media_type": media_type}, idempotent=False)]))
    return result
