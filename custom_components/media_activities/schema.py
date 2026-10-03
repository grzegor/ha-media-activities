"""Versioned, finite configuration validation, independent of Home Assistant."""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

KINDS = {"power", "input", "route", "playback", "setting", "availability", "supply", "volume", "mute", "picture_mode", "sound_mode"}
FEEDBACK = {"reported", "optimistic", "command_only"}
CONFIRMATIONS = {"required", "best_effort", "observe_only"}
SUPPLY_POLICIES = {"unmanaged", "always_on", "standby_on", "managed"}
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTITY = re.compile(r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$")


def _error(path: str, message: str) -> None:
    raise ValueError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _error(path, "must be an object")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _error(path, "must be a list")
    return value


def _identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _error(path, "must be a lowercase identifier")
    return value


def _number(value: Any, path: str, minimum: float = 0, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _error(path, "must be a finite number")
    if value < minimum or (positive and value <= 0):
        _error(path, f"must be {'positive' if positive else f'at least {minimum}'}")
    return value


def _scalar(value: Any, path: str) -> Any:
    if not isinstance(value, (str, bool, int, float)) or isinstance(value, float) and not math.isfinite(value):
        _error(path, "must be a string, boolean or finite number")
    return value


def _json_value(value: Any, path: str) -> None:
    """Reject non-JSON objects/nonfinite numbers before persistence or dispatch."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _error(path, "must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _error(path, "object keys must be strings")
            _json_value(item, path + "." + key)
        return
    _error(path, "must contain only JSON values")


def _entity(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _ENTITY.fullmatch(value):
        _error(path, "must be a Home Assistant entity ID")
    return value


def _predicate(value: dict, path: str) -> dict:
    _mapping(value, path)
    _entity(value.get("entity_id"), path + ".entity_id")
    if "attribute" in value and (not isinstance(value["attribute"], str) or not value["attribute"]):
        _error(path, "attribute must be a nonempty string")
    comparisons = set(value) & {"state", "states", "minimum", "maximum"}
    if not comparisons:
        _error(path, "needs state, states or a numeric range")
    if "state" in value:
        _scalar(value["state"], path + ".state")
    if "states" in value:
        if not _list(value["states"], path + ".states"):
            _error(path, "states must not be empty")
        for item in value["states"]:
            _scalar(item, path + ".states")
    for key in ("minimum", "maximum"):
        if key in value:
            _number(value[key], path + "." + key, -float("inf"))
    if "minimum" in value and "maximum" in value and value["minimum"] > value["maximum"]:
        _error(path, "minimum exceeds maximum")
    if "max_age" in value:
        _number(value["max_age"], path + ".max_age", positive=True)
    return value


def _predicates(items: Any, path: str) -> list[dict]:
    return [_predicate(p, f"{path}[{i}]") for i, p in enumerate(_list(items, path))]


def _typed(value: Any, cap: dict, path: str) -> None:
    _scalar(value, path)
    vt = cap["value_type"]
    if vt == "boolean" and not isinstance(value, bool):
        _error(path, "must be boolean")
    if vt == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        _error(path, "must be numeric")
    if vt in {"string", "enum"} and not isinstance(value, str):
        _error(path, "must be a string")
    allowed = cap.get("allowed_values")
    if isinstance(allowed, list) and value not in allowed:
        _error(path, f"not one of {allowed!r}")
    if isinstance(allowed, dict):
        if not isinstance(value, (float, int)) or isinstance(value, bool):
            _error(path, "range requires a numeric value")
        if value < allowed.get("minimum", -float("inf")) or value > allowed.get("maximum", float("inf")):
            _error(path, "outside allowed range")


def _requirements(items: Any, caps: dict, path: str) -> list[dict]:
    result = _list(items, path)
    ids = set()
    bindings = set()
    for i, req in enumerate(result):
        p = f"{path}[{i}]"
        _mapping(req, p)
        rid = _identifier(req.get("id"), p + ".id")
        if rid in ids:
            _error(p, "duplicate requirement ID")
        ids.add(rid)
        key = (req.get("equipment"), req.get("capability"))
        if key not in caps:
            _error(p, "unknown equipment/capability")
        if key in bindings:
            _error(p, "a capability may have only one desired value per profile")
        bindings.add(key)
        cap = caps[key]
        _typed(req.get("value"), cap, p + ".value")
        req.setdefault("confirmation", "required" if cap["feedback_type"] == "reported" else "best_effort")
        if req["confirmation"] not in CONFIRMATIONS:
            _error(p, "unsupported confirmation policy")
        if req["confirmation"] == "required" and cap["feedback_type"] != "reported":
            _error(p, "required confirmation needs reported feedback")
        observation_only = not cap["operations"] and (cap.get("observation") or req["confirmation"] == "best_effort")
        if req["confirmation"] != "observe_only" and not observation_only and not any(op.get("any_value") or ("value" in op and op["value"] == req["value"]) for op in cap["operations"]):
            _error(p, "desired value has no supported operation")
        req.setdefault("depends_on", [])
        _list(req["depends_on"], p + ".depends_on")
        for dep in req["depends_on"]:
            _identifier(dep, p + ".depends_on")
        _number(req.setdefault("min_delay", 0), p + ".min_delay")
        req.setdefault("guards", [])
        _predicates(req["guards"], p + ".guards")
    by_id = {r["id"]: r for r in result}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(rid: str) -> None:
        if rid not in by_id:
            _error(path, f"unknown dependency {rid}")
        if rid in visiting:
            _error(path, "dependency cycle")
        if rid in visited:
            return
        visiting.add(rid)
        for dep in by_id[rid]["depends_on"]:
            visit(dep)
        visiting.remove(rid)
        visited.add(rid)

    for rid in by_id:
        visit(rid)
    return result


def validate_config(config: dict) -> dict:
    """Return a detached normalized v1 configuration or raise ValueError."""
    cfg = deepcopy(_mapping(config, "configuration"))
    _json_value(cfg, "configuration")
    if cfg.get("version", 1) != 1:
        _error("version", "unsupported configuration version")
    cfg["version"] = 1
    if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
        _error("name", "must be a nonempty string")
    for key, default in (("observer_only", True), ("preserve_devices", False)):
        if not isinstance(cfg.setdefault(key, default), bool):
            _error(key, "must be boolean")
    equipment = _list(cfg.setdefault("equipment", []), "equipment")
    caps: dict[tuple[str, str], dict] = {}
    equipment_ids: set[str] = set()
    resources: dict[str, str] = {}
    for i, item in enumerate(equipment):
        p = f"equipment[{i}]"
        _mapping(item, p)
        eid = _identifier(item.get("id"), p + ".id")
        if eid in equipment_ids:
            _error(p, "duplicate equipment ID")
        equipment_ids.add(eid)
        item.setdefault("name", eid.replace("_", " ").title())
        for j, cap in enumerate(_list(item.setdefault("capabilities", []), p + ".capabilities")):
            cp = f"{p}.capabilities[{j}]"
            _mapping(cap, cp)
            cid = _identifier(cap.get("id"), cp + ".id")
            if (eid, cid) in caps:
                _error(cp, "duplicate capability ID")
            if cap.get("kind") not in KINDS:
                _error(cp, "unsupported capability kind")
            cap.setdefault("value_type", "boolean" if cap["kind"] in {"power", "availability", "supply", "mute"} else "string")
            if cap["value_type"] not in {"boolean", "number", "string", "enum"}:
                _error(cp, "unsupported value_type")
            allowed = cap.get("allowed_values")
            if allowed is not None:
                if not isinstance(allowed, (list, dict)) or not allowed:
                    _error(cp, "allowed_values must be a nonempty list or range")
                if isinstance(allowed, list):
                    for val in allowed:
                        _scalar(val, cp + ".allowed_values")
                else:
                    if not set(allowed) <= {"minimum", "maximum"}:
                        _error(cp, "range supports minimum and maximum only")
                    for val in allowed.values():
                        _number(val, cp + ".allowed_values", -float("inf"))
            cap.setdefault("feedback_type", "reported" if cap.get("observation") else "command_only")
            if cap["feedback_type"] not in FEEDBACK:
                _error(cp, "unsupported feedback_type")
            obs = cap.get("observation")
            if cap["feedback_type"] != "command_only" and not obs:
                _error(cp, "reported/optimistic feedback requires an observation")
            if obs:
                _mapping(obs, cp + ".observation")
                _entity(obs.get("entity_id"), cp + ".observation.entity_id")
                if "attribute" in obs and not isinstance(obs["attribute"], str):
                    _error(cp, "observation attribute must be a string")
                if "value_map" in obs:
                    _mapping(obs["value_map"], cp + ".observation.value_map")
                    for val in obs["value_map"].values():
                        _typed(val, cap, cp + ".observation.value_map")
                if "max_age" in obs:
                    _number(obs["max_age"], cp + ".observation.max_age", positive=True)
                _predicates(obs.setdefault("valid_when", []), cp + ".observation.valid_when")
            _number(cap.setdefault("timeout", 30), cp + ".timeout", positive=True)
            _predicates(cap.setdefault("prerequisites", []), cp + ".prerequisites")
            retry = _mapping(cap.setdefault("retry_policy", {}), cp + ".retry_policy")
            attempts = retry.setdefault("max_attempts", 3)
            if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
                _error(cp, "max_attempts must be an integer from 1 to 10")
            for delay in _list(retry.setdefault("backoff", [2, 5]), cp + ".retry_policy.backoff"):
                _number(delay, cp + ".retry_policy.backoff")
            cap.setdefault("operations", [])
            for k, op in enumerate(_list(cap["operations"], cp + ".operations")):
                opath = f"{cp}.operations[{k}]"
                _mapping(op, opath)
                if ("value" in op) == bool(op.get("any_value")):
                    _error(opath, "specify exactly one of value or any_value")
                if "value" in op:
                    _typed(op["value"], cap, opath + ".value")
                if not isinstance(op.get("action"), str) or not _ENTITY.fullmatch(op["action"]):
                    _error(opath, "action must be domain.action")
                target = _mapping(op.setdefault("target", {}), opath + ".target")
                for field in target:
                    if field != "entity_id":
                        _error(opath, "v1 requires fixed entity targets; area/device targets cannot establish resource ownership")
                entities = target.get("entity_id", [])
                if isinstance(entities, str):
                    entities = [entities]
                resource_ids = op.setdefault("resource_ids", [])
                _list(resource_ids, opath + ".resource_ids")
                if not entities and not resource_ids:
                    _error(opath, "an action must declare target.entity_id or resource_ids")
                for entity_id in _list(entities, opath + ".target.entity_id") + resource_ids:
                    _entity(entity_id, opath + ".target.entity_id")
                    owner = resources.setdefault(entity_id, eid)
                    if owner != eid:
                        _error(opath, f"controlled resource {entity_id} is already owned by {owner}")
                data = _mapping(op.setdefault("data", {}), opath + ".data")
                if set(data) & {"entity_id", "device_id", "area_id"}:
                    _error(opath, "action addressing belongs in a fixed target, not data")
                if op.get("any_value") and not op.get("value_field"):
                    _error(opath, "any_value requires value_field")
                if "value_field" in op and (not isinstance(op["value_field"], str) or not op["value_field"]):
                    _error(opath, "value_field must be a nonempty string")
                if op.get("value_field", "").split(".")[0] in {"entity_id", "device_id", "area_id"}:
                    _error(opath, "requested values cannot change an action target")
                if op.get("value_field"):
                    parts = op["value_field"].split(".")
                    if not all(parts):
                        _error(opath, "value_field must contain nonempty dotted path segments")
                    existing = data
                    for part in parts[:-1]:
                        existing = existing.get(part, {})
                        if not isinstance(existing, dict):
                            _error(opath, "value_field parent conflicts with fixed action data")
                if not isinstance(op.setdefault("idempotent", False), bool):
                    _error(opath, "idempotent must be boolean")
                for field in ("when", "guards", "prerequisites"):
                    _predicates(op.setdefault(field, []), opath + "." + field)
                _number(op.setdefault("min_delay", 0), opath + ".min_delay")
                op.setdefault("transport", "ha")
            if "cleanup" in cap:
                cleanup = _mapping(cap["cleanup"], cp + ".cleanup")
                _typed(cleanup.get("value"), cap, cp + ".cleanup.value")
                _predicates(cleanup.setdefault("guards", []), cp + ".cleanup.guards")
                if not any(o.get("any_value") or o.get("value") == cleanup["value"] for o in cap["operations"]):
                    _error(cp, "cleanup has no supported operation")
            if cap["kind"] == "supply":
                cap.setdefault("supply_policy", "standby_on")
                if cap["supply_policy"] not in SUPPLY_POLICIES:
                    _error(cp, "invalid supply_policy")
                cap.setdefault("consumers", [eid])
                _list(cap["consumers"], cp + ".consumers")
            caps[(eid, cid)] = cap
    for (eid, cid), cap in caps.items():
        if cap["kind"] == "supply" and not set(cap["consumers"]) <= equipment_ids:
            _error(f"{eid}.{cid}", "unknown supply consumer")
    activities = _list(cfg.setdefault("activities", []), "activities")
    activity_ids = set()
    for i, activity in enumerate(activities):
        p = f"activities[{i}]"
        _mapping(activity, p)
        aid = _identifier(activity.get("id"), p + ".id")
        if aid == "idle" or aid in activity_ids:
            _error(p, "duplicate or reserved activity ID")
        activity_ids.add(aid)
        activity.setdefault("name", aid.replace("_", " ").title())
        _requirements(activity.setdefault("requirements", []), caps, p + ".requirements")
    policies = _list(cfg.setdefault("policies", []), "policies")
    policy_ids = set()
    for i, policy in enumerate(policies):
        p = f"policies[{i}]"
        _mapping(policy, p)
        pid = _identifier(policy.get("id"), p + ".id")
        if pid in policy_ids:
            _error(p, "duplicate policy ID")
        policy_ids.add(pid)
        _predicates(policy.setdefault("guards", []), p + ".guards")
        _requirements(policy.setdefault("requirements", []), caps, p + ".requirements")
        if policy.setdefault("suspend_activity", True) is not True:
            _error(p, "v1 policies must suspend activity enforcement")
        if not isinstance(policy.setdefault("finish_activity", False), bool):
            _error(p, "finish_activity must be boolean")
    detection_ids = set()
    for i, rule in enumerate(_list(cfg.setdefault("detection_rules", []), "detection_rules")):
        p = f"detection_rules[{i}]"
        _mapping(rule, p)
        rid = _identifier(rule.get("id"), p + ".id")
        if rid in detection_ids:
            _error(p, "duplicate detection rule ID")
        detection_ids.add(rid)
        if rule.get("activity") not in activity_ids:
            _error(p, "unknown detection activity")
        if not _predicates(rule.setdefault("conditions", []), p + ".conditions"):
            _error(p, "detection needs at least one condition")
        _predicates(rule.setdefault("guards", []), p + ".guards")
        rule.setdefault("trigger_entities", [c["entity_id"] for c in rule["conditions"]])
        for entity_id in _list(rule["trigger_entities"], p + ".trigger_entities"):
            _entity(entity_id, p + ".trigger_entities")
        if "trigger_bindings" in rule:
            for binding in _list(rule["trigger_bindings"], p + ".trigger_bindings"):
                _mapping(binding, p + ".trigger_bindings")
                _entity(binding.get("entity_id"), p + ".trigger_bindings.entity_id")
                if "attribute" in binding and not isinstance(binding["attribute"], str):
                    _error(p, "trigger binding attribute must be a string")
                for direction in ("from", "to"):
                    if direction in binding:
                        _scalar(binding[direction], p + ".trigger_bindings." + direction)
        _number(rule.setdefault("debounce", 2), p + ".debounce")
    limits = _mapping(cfg.setdefault("limits", {}), "limits")
    for key, default in (("preparation_timeout", 900), ("recovery_episodes", 3), ("recovery_window", 3600), ("event_limit", 500)):
        _number(limits.setdefault(key, default), "limits." + key, positive=True)
        if key in {"recovery_episodes", "event_limit"} and not isinstance(limits[key], int):
            _error("limits." + key, "must be an integer")
    if limits["event_limit"] > 500:
        _error("limits.event_limit", "must not exceed 500")
    return cfg


def required_entities(config: dict) -> set[str]:
    """Return all referenced entity IDs, including prerequisites and targets."""
    result: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "entity_id":
                    if isinstance(child, str):
                        result.add(child)
                    elif isinstance(child, list):
                        result.update(e for e in child if isinstance(e, str))
                elif key in {"trigger_entities", "resource_ids"}:
                    result.update(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(config)
    return result


def controlled_entities(config: dict) -> set[str]:
    """Entities addressed by mutating actions, for cross-entry ownership checks."""
    result: set[str] = set()
    for item in config.get("equipment", []):
        for cap in item.get("capabilities", []):
            for op in cap.get("operations", []):
                entities = op.get("target", {}).get("entity_id", [])
                result.update([entities] if isinstance(entities, str) else entities)
                result.update(op.get("resource_ids", []))
    return result
