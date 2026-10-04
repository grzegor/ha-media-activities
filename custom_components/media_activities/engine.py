"""Deterministic media coordinator; no Home Assistant imports or I/O.

The caller supplies time and observations, dispatches returned HA actions, and
reports submission/completion. A completed service call is not device feedback.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
from typing import Any

from .schema import _predicates as validate_guards, _typed as validate_value, validate_config

_UNKNOWN = {"unknown", "unavailable", "none", "null", ""}
_DONE = {"confirmed", "applied", "unverified", "observed"}


def _equal(left: Any, right: Any) -> bool:
    # Python considers True == 1: that is not a typed capability match.
    return left == right and (isinstance(left, bool) == isinstance(right, bool))


def _known(value: Any) -> bool:
    """Only actual known scalar transitions can express external intent."""
    return isinstance(value, (str, bool, int, float)) and not (
        isinstance(value, str) and value.lower() in _UNKNOWN
    ) and not (isinstance(value, float) and not math.isfinite(value))


class Engine:
    """One system, one current intent, serialized physical equipment lanes."""

    def __init__(self, config: dict, now: float = 0, persisted: dict | None = None):
        self.config = validate_config(config)
        self.now = float(now)
        self.equipment = {e["id"]: e for e in self.config["equipment"]}
        self.caps = {(e["id"], c["id"]): c for e in self.config["equipment"] for c in e["capabilities"]}
        self.activities = {a["id"]: a for a in self.config["activities"]}
        self.policies = {p["id"]: p for p in self.config["policies"]}
        self.observations: dict[str, dict] = {}
        self.goals: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.events: deque = deque(maxlen=int(self.config["limits"]["event_limit"]))
        self.generation = 0
        self.sequence = 0
        self.activity_id = "idle"
        self.request_id: str | None = None
        self.origin: str | None = None
        self.mode = "none"
        self.policy_id: str | None = None
        self.request_guards: list[dict] = []
        self.preserve_devices = self.config["preserve_devices"]
        self.observer_only = self.config["observer_only"]
        self.overrides: dict[str, Any] = {}
        self.phase = "observing"
        self.cleanup_status = "not_needed"
        self.observed_activity = "unknown"
        self.started = self.now
        self.preparation_done = False
        self.suspended = False
        self.restart_paused = False
        self.guard_failed = False
        self.ambiguous = False
        self.recovery_budgets: dict[str, list[float]] = {}
        self.invalidated_at: dict[str, float] = {}
        self._expected: list[dict] = []
        self._detection_due: float | None = None
        self._detection_affected: set[str] = set()
        self._last_observed_candidate: str | None = None
        self._last_event_key: tuple | None = None
        self._end_armed = False
        self._end_due: float | None = None
        self.handover_until: float | None = None
        self.ended_activity: str | None = None
        if persisted:
            self._restore(persisted)
        self._event("engine_started", observer_only=self.observer_only, restart_paused=self.restart_paused)

    def _event(self, reason: str, **fields: Any) -> None:
        event = {"at": self.now, "generation": self.generation, "request_id": self.request_id, "reason": reason, **fields}
        key = (reason, repr(sorted(fields.items())), self.generation)
        if key != self._last_event_key:
            self.events.append(event)
            self._last_event_key = key

    def _restore(self, saved: dict) -> None:
        self.generation = int(saved.get("generation", 0))
        self.sequence = int(saved.get("sequence", 0))
        self.preserve_devices = bool(saved.get("preserve_devices", self.preserve_devices))
        self.overrides = deepcopy(saved.get("overrides", {}))
        self.recovery_budgets = deepcopy(saved.get("recovery_budgets", {}))
        for event in saved.get("events", [])[-self.events.maxlen:]:
            if isinstance(event, dict):
                self.events.append(event)
        activity = saved.get("activity_id", "idle")
        self.activity_id = activity if activity in self.activities else "idle"
        self.origin = saved.get("origin")
        self.request_guards = deepcopy(saved.get("request_guards", []))
        self.policy_id = saved.get("policy_id") if saved.get("policy_id") in self.policies else None
        self.mode = saved.get("mode", "activity" if self.activity_id != "idle" else "none")
        if self.mode not in {"activity", "finish", "policy", "none", "handover", "automatic_idle"}:
            self.mode = "none"
        self.suspended = bool(saved.get("suspended", False))
        if self.activity_id != "idle" or self.mode in {"finish", "policy", "handover", "automatic_idle"}:
            self.restart_paused = True
            self.phase = "recovery_paused"
            self._build_goals()

    def _new_request(self, origin: str) -> str:
        self.generation += 1
        self.sequence += 1
        self.request_id = f"request_{self.generation}_{self.sequence}"
        self.origin = origin
        self.started = self.now
        self.preparation_done = False
        self.guard_failed = False
        self.ambiguous = False
        self.restart_paused = False
        self._detection_due = None
        self._detection_affected.clear()
        self._end_armed = False
        self._end_due = None
        self.handover_until = None
        self.ended_activity = None
        # Unsubmitted reservations are cancellable. Submitted device work is not.
        for cid, command in list(self.commands.items()):
            if command["submitted_at"] is None:
                del self.commands[cid]
        self._event("request_accepted", origin=origin, activity_id=self.activity_id, mode=self.mode)
        return self.request_id

    def request(self, activity_id: str, now: float, origin: str = "user", guards: list | None = None) -> str:
        validate_guards(guards or [], "request.guards")
        if activity_id == "idle":
            rid = self.finish(now, origin)
            self.request_guards = deepcopy(guards or [])
            return rid
        if activity_id not in self.activities:
            raise ValueError(f"Unknown activity: {activity_id}")
        self.now = float(now)
        if activity_id != self.activity_id:
            self.overrides.clear()
        self.activity_id = activity_id
        self.mode = "activity"
        self.policy_id = None
        self.suspended = False
        self.request_guards = deepcopy(guards or [])
        rid = self._new_request(origin)
        self._build_goals()
        self._refresh_status()
        return rid

    def finish(self, now: float, origin: str = "user") -> str:
        self.now = float(now)
        self.activity_id = "idle"
        self.mode = "finish"
        self.policy_id = None
        self.preserve_devices = False
        self.overrides.clear()
        self.suspended = False
        self.request_guards = []
        rid = self._new_request(origin)
        self._build_goals()
        self._refresh_status()
        return rid

    def retry(self, now: float) -> str:
        if self.mode in {"handover", "automatic_idle"}:
            self.now = float(now)
            self.mode = "automatic_idle"
            rid = self._new_request("retry")
            self._build_goals()
            self._refresh_status()
            return rid
        self.now = float(now)
        self.suspended = self.mode == "policy"
        rid = self._new_request("retry")
        self._build_goals()
        self._refresh_status()
        return rid

    def set_preserve(self, value: bool, now: float) -> None:
        self.now = float(now)
        if not isinstance(value, bool):
            raise ValueError("preserve_devices must be boolean")
        if value == self.preserve_devices:
            return
        self.preserve_devices = value
        self._event("preservation_changed", value=value)
        if value:
            for cid, cmd in list(self.commands.items()):
                if cmd["submitted_at"] is None and (cmd["cleanup"] or cmd.get("recovery")):
                    self._discard_reservation(cid)
            if self.mode != "finish":
                for key, goal in list(self.goals.items()):
                    if goal["cleanup"] and not self._command_for_goal(key):
                        del self.goals[key]
        elif self.activity_id != "idle" and not self.suspended:
            previous = deepcopy(self.goals)
            self._new_request("preservation_changed")
            self._build_goals()
            self._retain_unchanged_goals(previous)
        self._refresh_status()

    def set_override(self, equipment_id: str, capability_id: str, value: Any, now: float) -> str:
        key = (equipment_id, capability_id)
        if key not in self.caps:
            raise ValueError("Unknown equipment/capability")
        cap = self.caps[key]
        validate_value(value, cap, "setting.value")
        if not self._matching_operations(cap, value):
            raise ValueError("Unsupported setting value")
        if self.activity_id == "idle" or self.mode != "activity":
            raise ValueError("Device settings require a current activity")
        self.now = float(now)
        previous = deepcopy(self.goals)
        self.overrides[f"{equipment_id}.{capability_id}"] = deepcopy(value)
        rid = self._new_request("setting")
        self._build_goals()
        self._retain_unchanged_goals(previous)
        self._refresh_status()
        return rid

    def apply_policy(self, policy_id: str, now: float, origin: str = "policy") -> str:
        if policy_id not in self.policies:
            raise ValueError(f"Unknown policy: {policy_id}")
        self.now = float(now)
        self.mode = "policy"
        self.policy_id = policy_id
        self.suspended = True
        if self.policies[policy_id]["finish_activity"]:
            self.activity_id = "idle"
            self.preserve_devices = False
            self.overrides.clear()
        self.request_guards = deepcopy(self.policies[policy_id]["guards"])
        rid = self._new_request(origin)
        self._build_goals()
        self._refresh_status()
        return rid

    def cancel_origin(self, origin: str, now: float) -> None:
        self.now = float(now)
        if self.origin != origin:
            return
        self.generation += 1
        self.suspended = True
        self.mode = "none"
        self.goals.clear()
        for cid, cmd in list(self.commands.items()):
            if cmd["submitted_at"] is None:
                del self.commands[cid]
        self._event("request_cancelled", origin=origin)
        self._refresh_status()

    def set_observer(self, value: bool, now: float) -> None:
        self.now = float(now)
        if not isinstance(value, bool):
            raise ValueError("observer_only must be boolean")
        if self.observer_only == value:
            return
        self.observer_only = value
        for cid, cmd in list(self.commands.items()):
            if cmd["submitted_at"] is None:
                self._discard_reservation(cid)
        # Leaving observer mode never replays intent collected during observation.
        if not value and self.mode != "none":
            self.restart_paused = True
        self._event("observer_mode_changed", value=value)
        self._refresh_status()

    def _make_goal(self, req: dict, *, cleanup: bool = False) -> dict:
        key = ("cleanup:" if cleanup else "requirement:") + req["id"]
        value = self.overrides.get(f"{req['equipment']}.{req['capability']}", req["value"]) if not cleanup and self.mode == "activity" else req["value"]
        return {"key": key, **deepcopy(req), "value": deepcopy(value), "cleanup": cleanup,
                "status": "pending", "reason": None, "attempts": 0, "next_try": self.now,
                "started": self.now, "satisfied_at": None, "recovery": False, "ever_done": False, "confirmed_fresh": False}

    def _build_goals(self) -> None:
        self.goals = {}
        if self.mode == "activity" and self.activity_id in self.activities:
            requirements = deepcopy(self.activities[self.activity_id]["requirements"])
            present = {(r["equipment"], r["capability"]) for r in requirements}
            for binding, value in self.overrides.items():
                eid, cid = binding.split(".", 1)
                if (eid, cid) in self.caps and (eid, cid) not in present:
                    cap = self.caps[(eid, cid)]
                    requirements.append({"id": f"override_{eid}_{cid}", "equipment": eid, "capability": cid, "value": value,
                                         "confirmation": "required" if cap["feedback_type"] == "reported" else "best_effort", "depends_on": [], "guards": [], "min_delay": 0})
        elif self.mode == "policy" and self.policy_id:
            requirements = deepcopy(self.policies[self.policy_id]["requirements"])
        else:
            requirements = []
        for req in requirements:
            goal = self._make_goal(req)
            self.goals[goal["key"]] = goal
        self._add_cleanup()
        for goal in self.goals.values():
            for command in self.commands.values():
                if command["submitted_at"] is not None and command["equipment_id"] == goal["equipment"] and command["capability_id"] == goal["capability"] and _equal(command["value"], goal["value"]):
                    command["adopted_generation"] = self.generation
                    command["goal_key"] = goal["key"]
                    goal.update(status="inflight", attempts=1, reason="waiting_for_feedback")

    def _retain_unchanged_goals(self, previous: dict[str, dict]) -> None:
        """A session setting/policy edit must not replay unrelated one-shot work."""
        for key, goal in list(self.goals.items()):
            old = previous.get(key)
            if not old or old["equipment"] != goal["equipment"] or old["capability"] != goal["capability"] or not _equal(old["value"], goal["value"]):
                continue
            self.goals[key] = deepcopy(old)
            adopted = False
            for cmd in self.commands.values():
                if cmd["goal_key"] == key and _equal(cmd["value"], goal["value"]) and cmd["submitted_at"] is not None:
                    cmd["adopted_generation"] = self.generation
                    adopted = True
            if self.goals[key]["status"] in {"reserved", "inflight"} and not adopted:
                self.goals[key]["status"] = "pending"

    def _command_is_current(self, command: dict) -> bool:
        return command.get("adopted_generation", command["generation"]) == self.generation

    def _add_cleanup(self) -> None:
        if self.mode not in {"activity", "finish", "automatic_idle"} or (self.preserve_devices and self.mode != "finish"):
            return
        required_equipment = {g["equipment"] for g in self.goals.values() if not g["cleanup"]}
        required_caps = {(g["equipment"], g["capability"]) for g in self.goals.values() if not g["cleanup"]}
        for (eid, cid), cap in self.caps.items():
            if "cleanup" not in cap or (eid, cid) in required_caps:
                continue
            # A used piece of equipment is not unused merely because its profile
            # only mentions source rather than power. Explicit goals can turn it off.
            if eid in required_equipment:
                continue
            if cap["kind"] == "supply" and cap["supply_policy"] != "managed":
                continue
            req = {"id": f"{eid}_{cid}", "equipment": eid, "capability": cid,
                   "value": cap["cleanup"]["value"], "confirmation": "required" if cap["feedback_type"] == "reported" else "best_effort",
                   "depends_on": [], "guards": deepcopy(cap["cleanup"].get("guards", [])), "min_delay": 0}
            key = "cleanup:" + req["id"]
            if key not in self.goals:
                self.goals[key] = self._make_goal(req, cleanup=True)

    def observe(self, entity_id: str, state: Any, attributes: dict | None, now: float, fresh: bool = True) -> None:
        self.now = float(now)
        old = self.observations.get(entity_id)
        attrs = deepcopy(attributes or {})
        changed = old is not None and (not _equal(old["state"], state) or old["attributes"] != attrs)
        self.observations[entity_id] = {"state": state, "attributes": attrs, "at": self.now, "fresh": fresh,
                                        "changed_at": self.now if changed or old is None else old["changed_at"]}
        expected_fields: set[str | None] = set()
        acknowledged = []
        for record in self._expected:
            if record["entity_id"] == entity_id and record["expires"] >= self.now:
                cap = self.caps[record["capability"]]
                value, valid, _ = self._read_cap(*record["capability"])
                if valid and _equal(value, record["value"]):
                    expected_fields.add(cap.get("observation", {}).get("attribute"))
                    acknowledged.append(record)
        for record in acknowledged:
            self._expected.remove(record)
        for (eid, cid), cap in self.caps.items():
            if cap["kind"] != "supply" or cap.get("observation", {}).get("entity_id") != entity_id:
                continue
            value, valid, _ = self._read_cap(eid, cid)
            if valid and value is False:
                for consumer in cap["consumers"]:
                    self.invalidated_at[consumer] = self.now
        # Integrations can retain a known source attribute while the entity is
        # unavailable. Reconnection must establish a baseline even when those
        # retained attributes change; it is not evidence of a manual selection.
        if changed and fresh and old.get("fresh") and _known(old["state"]) and _known(state):
            affected = []
            for rule in self.config["detection_rules"]:
                if not self._predicates(rule["guards"]):
                    continue
                bindings = rule.get("trigger_bindings")
                if bindings is None and entity_id in rule["trigger_entities"]:
                    fields = [p.get("attribute") for p in rule["conditions"] if p["entity_id"] == entity_id] or [None]
                    bindings = [{"entity_id": entity_id, **({"attribute": f} if f else {})} for f in fields]
                for binding in bindings or []:
                    if binding["entity_id"] != entity_id:
                        continue
                    field = binding.get("attribute")
                    before = old["attributes"].get(field) if field else old["state"]
                    after = attrs.get(field) if field else state
                    if field in expected_fields or not _known(before) or not _known(after) or _equal(before, after):
                        continue
                    if "from" in binding and not _equal(before, binding["from"]):
                        continue
                    if "to" in binding and not _equal(after, binding["to"]):
                        continue
                    affected.append(rule)
                    break
            if affected:
                self._detection_affected.update(r["id"] for r in affected)
                self._detection_due = self.now + max(r["debounce"] for r in affected)
        self._refresh_status()

    def _raw(self, entity_id: str, attribute: str | None = None) -> tuple[Any, dict | None]:
        obs = self.observations.get(entity_id)
        if not obs:
            return None, None
        if isinstance(obs["state"], str) and obs["state"].lower() in _UNKNOWN:
            return None, obs
        return obs["attributes"].get(attribute) if attribute else obs["state"], obs

    def _read_cap(self, equipment: str, capability: str) -> tuple[Any, bool, str | None]:
        cap = self.caps[(equipment, capability)]
        binding = cap.get("observation")
        if not binding:
            return None, False, "command_applied_unverified"
        value, obs = self._raw(binding["entity_id"], binding.get("attribute"))
        if obs is None:
            return None, False, "entity_missing"
        if value is None or isinstance(value, str) and value.lower() in _UNKNOWN:
            return value, False, "waiting_for_network"
        if "max_age" in binding and self.now - obs["at"] > binding["max_age"]:
            return value, False, "waiting_for_feedback"
        if not self._predicates(binding.get("valid_when", [])):
            return value, False, "waiting_for_prerequisite"
        if cap["kind"] != "supply" and obs["at"] <= self.invalidated_at.get(equipment, -float("inf")):
            return value, False, "waiting_for_feedback"
        if "value_map" in binding:
            map_key = str(value).lower() if isinstance(value, bool) else str(value)
            value = binding["value_map"].get(map_key, value)
        elif cap["value_type"] == "boolean" and isinstance(value, str):
            value = {"on": True, "off": False, "true": True, "false": False}.get(value.lower(), value)
        elif cap["value_type"] == "number" and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return value, False, "waiting_for_feedback"
        if isinstance(value, float) and not math.isfinite(value):
            return None, False, "waiting_for_feedback"
        return value, True, None

    def _predicate(self, predicate: dict, *, safety: bool = False, allow_unavailable: bool = False) -> bool:
        value, obs = self._raw(predicate.get("entity_id", ""), predicate.get("attribute"))
        if allow_unavailable and obs and not predicate.get("attribute"):
            value = obs["state"]
        if obs is None or value is None:
            return False
        if not allow_unavailable and isinstance(value, str) and value.lower() in _UNKNOWN:
            return False
        if "max_age" in predicate and self.now - obs["at"] > predicate["max_age"]:
            return False
        if "stable_for" in predicate and (not obs["fresh"] or self.now - obs["changed_at"] < predicate["stable_for"]):
            return False
        if safety:
            if not obs["fresh"]:
                return False
            for (eid, _), cap in self.caps.items():
                binding = cap.get("observation", {})
                if binding.get("entity_id") == predicate["entity_id"] and binding.get("attribute") == predicate.get("attribute"):
                    if cap["feedback_type"] != "reported" or obs["at"] <= self.invalidated_at.get(eid, -float("inf")):
                        return False
        if "state" in predicate and not _equal(value, predicate["state"]):
            return False
        if "states" in predicate and not any(_equal(value, v) for v in predicate["states"]):
            return False
        for field, compare in (("minimum", lambda x, y: x >= y), ("maximum", lambda x, y: x <= y)):
            if field in predicate:
                try:
                    if isinstance(value, bool) or not compare(float(value), predicate[field]):
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    def _predicates(self, items: list, *, safety: bool = False, allow_unavailable: bool = False) -> bool:
        return all(self._predicate(p, safety=safety, allow_unavailable=allow_unavailable) for p in items)

    def _matching_operations(self, cap: dict, value: Any) -> list[dict]:
        return [op for op in cap["operations"] if op.get("any_value") or "value" in op and _equal(op["value"], value)]

    def _operation(self, cap: dict, goal: dict) -> dict | None:
        for op in self._matching_operations(cap, goal["value"]):
            if self._predicates(op["when"], allow_unavailable=True):
                return op
        return None

    def _command_for_goal(self, key: str) -> dict | None:
        return next((c for c in self.commands.values() if c["goal_key"] == key and self._command_is_current(c)), None)

    def _lane_busy(self, eid: str) -> bool:
        return any(c["equipment_id"] == eid for c in self.commands.values())

    def _supply_allowed(self, goal: dict) -> tuple[bool, str | None]:
        cap = self.caps[(goal["equipment"], goal["capability"])]
        if "cleanup" in cap and _equal(goal["value"], cap["cleanup"]["value"]) and not self._predicates(cap["cleanup"]["guards"], safety=True):
            return False, "shutdown_unconfirmed"
        if cap["kind"] != "supply":
            return True, None
        policy = cap["supply_policy"]
        if policy == "unmanaged":
            return False, "supply_unmanaged"
        if goal["value"] is not False:
            return True, None
        if policy == "always_on" or policy == "standby_on" and self.mode != "policy":
            return False, "supply_retained"
        consumers = set(cap["consumers"])
        if any(command["equipment_id"] in consumers and command["submitted_at"] is not None for command in self.commands.values()):
            return False, "shutdown_unconfirmed"
        for other in self.goals.values():
            if other is goal or other["equipment"] not in consumers:
                continue
            other_cap = self.caps[(other["equipment"], other["capability"])]
            shutdown_goal = (other_cap["kind"] in {"power", "supply"} and other["value"] is False) or (
                "cleanup" in other_cap and _equal(other["value"], other_cap["cleanup"]["value"]))
            if not other["cleanup"] and not shutdown_goal:
                return False, "shared_supply_in_use"
            if shutdown_goal and other["status"] not in _DONE:
                return False, "shutdown_unconfirmed"
            if other_cap["kind"] == "power" and other["value"] is False and (other["status"] != "confirmed" or not other["confirmed_fresh"]):
                return False, "shutdown_unconfirmed"
        # A consumer's retained supply policy also protects it when another
        # shared upstream supply is considered for removal.
        for (eid, cid), other_cap in self.caps.items():
            if eid in consumers and (eid, cid) != (goal["equipment"], goal["capability"]) and other_cap["kind"] == "supply":
                if other_cap["supply_policy"] == "always_on" or other_cap["supply_policy"] == "standby_on" and self.mode != "policy":
                    return False, "supply_retained"
        return True, None

    def _ready_to_dispatch(self, goal: dict, op: dict) -> tuple[bool, str | None]:
        if not self._predicates(self.request_guards):
            return False, "request_guard_failed"
        if not self._predicates(goal.get("guards", []), safety=goal["cleanup"]):
            return False, "shutdown_unconfirmed" if goal["cleanup"] else "request_guard_failed"
        if not self._predicates(op["guards"], safety=True):
            return False, "shutdown_unconfirmed" if goal["value"] is False else "waiting_for_prerequisite"
        cap = self.caps[(goal["equipment"], goal["capability"])]
        supply_ok, reason = self._supply_allowed(goal)
        if not supply_ok:
            return False, reason
        if not self._predicates(cap["prerequisites"]) or not self._predicates(op["prerequisites"]):
            return False, "waiting_for_prerequisite"
        for dep in goal.get("depends_on", []):
            dependency = self.goals.get("requirement:" + dep)
            if dependency is None or dependency["status"] not in _DONE:
                return False, "waiting_for_prerequisite"
        delay_start = max([self.started] + [self.goals["requirement:" + d].get("satisfied_at") or self.started for d in goal.get("depends_on", [])])
        if self.now < delay_start + max(goal.get("min_delay", 0), op.get("min_delay", 0)):
            return False, "waiting_for_prerequisite"
        return True, None

    def _discard_reservation(self, cid: str) -> None:
        command = self.commands.get(cid)
        if command is None or command["submitted_at"] is not None:
            return
        goal = self.goals.get(command["goal_key"])
        if goal and command["generation"] == self.generation:
            goal["status"] = "pending"
        del self.commands[cid]

    def can_dispatch(self, command_id: str, now: float) -> bool:
        """Recheck guards immediately before dispatch; attempts start at submitted."""
        self.now = float(now)
        cmd = self.commands.get(command_id)
        if cmd is None or cmd["submitted_at"] is not None:
            return False
        goal = self.goals.get(cmd["goal_key"])
        allowed = not self.observer_only and not self.restart_paused and not self.guard_failed and not self.ambiguous
        allowed = allowed and cmd["generation"] == self.generation and goal is not None
        if allowed and self.preserve_devices and self.mode != "finish" and (cmd["cleanup"] or cmd["recovery"]):
            allowed = False
        if allowed:
            allowed, reason = self._ready_to_dispatch(goal, cmd["operation"])
            allowed = allowed and self._predicates(cmd["operation"]["when"], allow_unavailable=True)
            if not allowed:
                goal["reason"] = reason or "waiting_for_prerequisite"
                if reason == "request_guard_failed" and not self._predicates(self.request_guards):
                    self.guard_failed = True
        if not allowed:
            self._discard_reservation(command_id)
        return bool(allowed)

    def submitted(self, command_id: str, now: float) -> None:
        self.now = float(now)
        if not self.can_dispatch(command_id, now):
            return
        cmd = self.commands[command_id]
        cmd["submitted_at"] = self.now
        cap = self.caps[(cmd["equipment_id"], cmd["capability_id"])]
        cmd["deadline"] = self.now + cap["timeout"]
        goal = self.goals[cmd["goal_key"]]
        goal["attempts"] += 1
        goal["status"] = "inflight"
        goal["reason"] = "waiting_for_feedback"
        binding = cap.get("observation")
        if binding:
            self._expected.append({"entity_id": binding["entity_id"], "capability": (cmd["equipment_id"], cmd["capability_id"]),
                                   "value": cmd["value"], "expires": cmd["deadline"] + 5})
        self._event("command_submitted", command_id=command_id, equipment=cmd["equipment_id"], capability=cmd["capability_id"],
                    value=cmd["value"], attempt=goal["attempts"], deadline=cmd["deadline"], transport=cmd["operation"]["transport"])

    def completed(self, command_id: str, now: float, error: Any = None) -> None:
        self.now = float(now)
        cmd = self.commands.get(command_id)
        if cmd is None or cmd["submitted_at"] is None:
            return
        cmd["service_done"] = True
        # Do not persist exception text: it may contain transport credentials.
        cmd["service_error"] = bool(error)
        if error:
            error_type = type(error).__name__ if isinstance(error, BaseException) else "ActionError"
            if isinstance(error, str) and error.startswith("action_failed:"):
                candidate = error.split(":", 1)[1]
                if candidate.isidentifier() and len(candidate) <= 80:
                    error_type = candidate
            self._event("action_failed", command_id=command_id, error_type=error_type)
        self._process_commands()
        self._refresh_status()

    def _set_goal_done(self, goal: dict, confirmed: bool) -> None:
        was_done = goal["status"] in _DONE
        goal["status"] = "confirmed" if confirmed else "applied"
        goal["reason"] = None if confirmed else "command_applied_unverified"
        if not was_done or goal["satisfied_at"] is None:
            goal["satisfied_at"] = self.now
        goal["ever_done"] = True
        cap = self.caps[(goal["equipment"], goal["capability"])]
        observation = self.observations.get(cap.get("observation", {}).get("entity_id"), {})
        goal["confirmed_fresh"] = bool(confirmed and observation.get("fresh"))

    def _fail_goal(self, goal: dict, command: dict, reason: str) -> None:
        cap = self.caps[(goal["equipment"], goal["capability"])]
        retry = cap["retry_policy"]
        eligible = command["operation"]["idempotent"] and cap["feedback_type"] == "reported"
        episode_start = goal["started"] if goal["recovery"] else self.started
        if eligible and goal["attempts"] < retry["max_attempts"] and self.now - episode_start < self.config["limits"]["preparation_timeout"]:
            delays = retry["backoff"]
            delay = delays[min(goal["attempts"] - 1, len(delays) - 1)] if delays else 0
            goal["status"] = "pending"
            goal["next_try"] = self.now + delay
            goal["reason"] = reason
            self._event("command_retry_scheduled", equipment=goal["equipment"], capability=goal["capability"], reason_code=reason, next_try=goal["next_try"])
        elif goal["confirmation"] == "best_effort":
            # Best effort is an explicit user policy, not an inferred downgrade
            # of reported feedback. Exhausted/failed work stays visibly uncertain
            # while allowing other requirements to finish their preparation.
            goal.update(status="unverified", reason=reason, satisfied_at=self.now,
                        ever_done=True, confirmed_fresh=False)
            self._event("best_effort_unverified", equipment=goal["equipment"], capability=goal["capability"], reason_code=reason)
        else:
            goal["status"] = "blocked"
            goal["reason"] = reason
            self._event("command_blocked", equipment=goal["equipment"], capability=goal["capability"], reason_code=reason)

    def _process_commands(self) -> None:
        for cid, cmd in list(self.commands.items()):
            if cmd["submitted_at"] is None:
                continue
            current = self._command_is_current(cmd)
            goal = self.goals.get(cmd["goal_key"]) if current else None
            cap = self.caps[(cmd["equipment_id"], cmd["capability_id"])]
            value, valid, _ = self._read_cap(cmd["equipment_id"], cmd["capability_id"])
            binding = cap.get("observation", {})
            obs = self.observations.get(binding.get("entity_id"), {})
            confirmed = cap["feedback_type"] == "reported" and valid and obs.get("fresh") and _equal(value, cmd["value"]) and obs.get("at", -1) >= cmd["submitted_at"]
            if not cmd["service_done"]:
                # An async driver may still own its queued request. A deadline
                # does not permit another command in this equipment lane.
                if self.now >= cmd["deadline"] and goal:
                    if goal["status"] != "blocked":
                        self._event("command_deadline_exceeded_pending", command_id=cid)
                    goal["status"] = "blocked"
                    goal["reason"] = "command_still_pending"
                continue
            if cmd["service_error"]:
                if goal:
                    self._fail_goal(goal, cmd, "action_failed")
                del self.commands[cid]
            elif confirmed:
                if goal:
                    self._set_goal_done(goal, True)
                self._event("command_confirmed", command_id=cid, superseded=not current)
                del self.commands[cid]
            elif cap["feedback_type"] in {"command_only", "optimistic"}:
                if goal:
                    self._set_goal_done(goal, False)
                self._event("command_applied_unverified", command_id=cid, superseded=not current)
                del self.commands[cid]
            elif self.now >= cmd["deadline"]:
                if goal:
                    self._fail_goal(goal, cmd, "confirmation_timeout")
                self._event("command_confirmation_timeout", command_id=cid, superseded=not current)
                del self.commands[cid]

    def _detection(self) -> None:
        rules = self.config["detection_rules"]
        if not rules:
            candidates = []
            for activity_id, activity in self.activities.items():
                requirements = [r for r in activity["requirements"] if r["confirmation"] != "observe_only"]
                if not requirements:
                    continue
                matches = True
                for req in requirements:
                    cap = self.caps[(req["equipment"], req["capability"])]
                    value, valid, _ = self._read_cap(req["equipment"], req["capability"])
                    desired = self.overrides.get(f"{req['equipment']}.{req['capability']}", req["value"]) if activity_id == self.activity_id else req["value"]
                    if cap["feedback_type"] != "reported" or not valid or not _equal(value, desired):
                        matches = False
                        break
                if matches:
                    candidates.append(activity_id)
            self.observed_activity = candidates[0] if len(candidates) == 1 else "manual_routing" if len(candidates) > 1 else "unknown"
            return
        matching_rules = [r for r in rules if self._predicates(r["conditions"]) and self._predicates(r["guards"])]
        matching = {r["activity"] for r in matching_rules}
        self.observed_activity = self.activity_id if self.activity_id in matching and not self.ambiguous else next(iter(matching)) if len(matching) == 1 else "manual_routing" if len(matching) > 1 else "unknown"
        if self._detection_due is None or self.now < self._detection_due:
            return
        self._detection_due = None
        eligible_affected = {r["id"] for r in rules if r["id"] in self._detection_affected and self._predicates(r["guards"])}
        if not eligible_affected:
            self._detection_affected.clear()
            return
        affected_matching = {r["activity"] for r in matching_rules if r["id"] in eligible_affected}
        self._detection_affected.clear()
        if len(affected_matching) != 1:
            self.ambiguous = True
            for cid, cmd in list(self.commands.items()):
                if cmd["submitted_at"] is None:
                    self._discard_reservation(cid)
            self.observed_activity = "manual_routing"
            self._event("ambiguous_activity", candidates=sorted(affected_matching))
        else:
            activity = next(iter(affected_matching))
            if activity != self.activity_id or self.ambiguous or self.restart_paused or self.suspended:
                self.request(activity, self.now, origin="detected")
            elif self.mode == "activity" and not self.preserve_devices:
                # A new observed wake may retry blocked properties, but cannot
                # reset the rolling recovery budget or replay successful playback.
                for goal in self.goals.values():
                    if not goal["cleanup"] and goal["status"] == "blocked":
                        self._start_recovery(goal)
            self.observed_activity = activity
            self._last_observed_candidate = activity

    def _continuing_session(self) -> bool:
        if self.mode != "activity" or self.suspended:
            return False
        required = [g for g in self.goals.values() if not g["cleanup"] and g["confirmation"] != "observe_only"]
        if not required:
            return False
        # Require every actionable requirement to be freshly reported. A single
        # always-on supply or a saved route does not establish a continuing session.
        has_running_evidence = False
        for goal in required:
            cap = self.caps[(goal["equipment"], goal["capability"])]
            value, valid, _ = self._read_cap(goal["equipment"], goal["capability"])
            observation = self.observations.get(cap.get("observation", {}).get("entity_id"), {})
            if cap["feedback_type"] != "reported" or not valid or not observation.get("fresh") or not _equal(value, goal["value"]):
                return False
            if cap["kind"] in {"power", "playback", "availability"} and value not in (False, "off", "idle", "paused"):
                has_running_evidence = True
        return has_running_evidence

    def _activity_end(self) -> None:
        """End a positively observed source session without immediately cooling displays."""
        if self.observer_only or self.restart_paused or self.suspended or self.guard_failed:
            return
        if self.mode == "handover":
            self.observed_activity = "idle"
            if self.handover_until is not None and self.now >= self.handover_until:
                self.mode = "automatic_idle"
                self.started = self.now
                self.handover_until = None
                self._build_goals()
                self._event("handover_expired")
            return
        if self.mode == "automatic_idle":
            self.observed_activity = "idle"
            return
        if self.mode != "activity":
            return
        activity = self.activities[self.activity_id]
        conditions = activity["end_conditions"]
        if not conditions or not self._predicates(activity["end_guards"]):
            self._end_due = None
            return
        # A cached Off, initial startup, or unknown/unavailable source is not an
        # observed end. Arm only after a fresh known non-ended session.
        known = [bool((obs := self.observations.get(p["entity_id"])) and obs["fresh"]
                    and _known(obs["state"]) and _known(self._raw(p["entity_id"], p.get("attribute"))[0])
                    ) for p in conditions]
        any_mode = activity["end_condition_mode"] == "any"
        if not (any(known) if any_mode else all(known)):
            self._end_due = None
            return
        matches = [valid and self._predicate(p) for valid, p in zip(known, conditions, strict=True)]
        if not (any(matches) if any_mode else all(matches)):
            self._end_armed = True
            self._end_due = None
            return
        if not self._end_armed:
            return
        if self._end_due is None:
            self._end_due = self.now + activity["end_debounce"]
        if self.now < self._end_due:
            return
        ended = self.activity_id
        self.activity_id = "idle"
        self.mode = "handover"
        self.policy_id = None
        self.overrides.clear()
        self.request_guards = []
        self._new_request("source_ended")
        self.goals.clear()
        self.ended_activity = ended
        self.handover_until = self.now + activity["handover_timeout"]
        self.observed_activity = "idle"
        self._event("activity_ended", activity=ended, handover_until=self.handover_until)

    def _start_recovery(self, goal: dict) -> bool:
        budget_key = goal["equipment"] + "." + goal["capability"]
        window = self.config["limits"]["recovery_window"]
        times = [t for t in self.recovery_budgets.get(budget_key, []) if self.now - t < window]
        self.recovery_budgets[budget_key] = times
        if len(times) >= self.config["limits"]["recovery_episodes"]:
            goal["status"] = "blocked"
            goal["reason"] = "recovery_budget_exhausted"
            return False
        times.append(self.now)
        goal.update(status="pending", attempts=0, next_try=self.now, recovery=True, started=self.now, reason=None)
        self._event("recovery_started", equipment=goal["equipment"], capability=goal["capability"])
        return True

    def _shutdown_completed_before_supply_cut(self, goal: dict) -> bool:
        """Retain a completed shutdown step after its intentional de-energizing.

        Network readiness is still invalidated; this records historical completion
        of this policy's shutdown step, never a new assertion about device feedback.
        """
        if self.mode != "policy" or goal["status"] != "confirmed" or not goal["confirmed_fresh"]:
            return False
        cap = self.caps[(goal["equipment"], goal["capability"])]
        shutdown = cap["kind"] == "power" and goal["value"] is False or (
            "cleanup" in cap and _equal(cap["cleanup"]["value"], goal["value"]))
        if not shutdown or cap["kind"] == "supply":
            return False
        for (eid, cid), supply in self.caps.items():
            if supply["kind"] != "supply" or goal["equipment"] not in supply["consumers"]:
                continue
            supply_goal = next((g for g in self.goals.values() if g["equipment"] == eid and g["capability"] == cid and g["value"] is False), None)
            if not supply_goal or supply["feedback_type"] != "reported":
                continue
            value, valid, _ = self._read_cap(eid, cid)
            supply_observation = self.observations.get(supply.get("observation", {}).get("entity_id"), {})
            if valid and value is False and supply_observation.get("fresh") and supply_observation.get("at", -1) >= goal["satisfied_at"]:
                return True
        return False

    def tick(self, now: float) -> list[dict]:
        self.now = float(now)
        self._expected = [e for e in self._expected if e["expires"] >= self.now]
        self._process_commands()
        self._detection()
        self._activity_end()
        if self.restart_paused and self._continuing_session():
            self.restart_paused = False
            self.started = self.now
            self._event("continuing_session_confirmed")
        if not self._predicates(self.request_guards):
            if not self.guard_failed:
                self.guard_failed = True
                self._event("request_guard_failed")
            for cid, cmd in list(self.commands.items()):
                if cmd["submitted_at"] is None:
                    self._discard_reservation(cid)
        if self.observer_only or self.restart_paused or self.guard_failed or self.ambiguous or self.mode in {"none", "handover"} or self._detection_due is not None or self._end_due is not None:
            self._refresh_status()
            return []
        self._add_cleanup()
        output = []
        # Required preparation is scheduled before optional cleanup, but cleanup
        # of an unrelated device never blocks these requirements.
        for goal in sorted(self.goals.values(), key=lambda g: g["cleanup"]):
            cap = self.caps[(goal["equipment"], goal["capability"])]
            value, valid, reason = self._read_cap(goal["equipment"], goal["capability"])
            matches = valid and _equal(value, goal["value"])
            if goal["confirmation"] == "observe_only":
                goal["status"] = "observed"
                goal["reason"] = None if matches else reason or "observed_mismatch"
                continue
            if self._command_for_goal(goal["key"]):
                continue
            if self.mode in {"finish", "automatic_idle"} and goal["status"] in _DONE:
                # Finish is a one-shot explicit shutdown, not a permanent ban on
                # starting equipment manually after the shutdown has completed.
                continue
            if self._shutdown_completed_before_supply_cut(goal):
                continue
            superseded = [c for c in self.commands.values() if c["equipment_id"] == goal["equipment"] and not self._command_is_current(c)]
            if superseded:
                goal["status"] = "blocked" if any(c["deadline"] is not None and self.now >= c["deadline"] and not c["service_done"] for c in superseded) else "pending"
                goal["reason"] = "superseded_command_pending"
                continue
            if goal["status"] == "blocked" and goal["reason"] == "superseded_command_pending":
                goal["status"] = "pending"
                goal["reason"] = None
            if matches and cap["feedback_type"] == "reported":
                self._set_goal_done(goal, True)
                continue
            if goal["status"] == "confirmed":
                if self.preserve_devices and self.preparation_done:
                    goal["reason"] = "preserved_drift"
                    continue
                if not self._start_recovery(goal):
                    continue
            elif goal["status"] in {"applied", "unverified"}:
                # Optimistic, command-only and exhausted best-effort goals must
                # not create a retry loop after their bounded episode concludes.
                continue
            if goal["status"] == "blocked":
                continue
            if self.preserve_devices and self.mode != "finish" and (goal["cleanup"] or goal["recovery"]):
                continue
            if goal["cleanup"] and any(g["status"] not in _DONE for g in self.goals.values() if not g["cleanup"]):
                goal["reason"] = "waiting_for_activity"
                continue
            if self.now - (goal["started"] if goal["recovery"] else self.started) > self.config["limits"]["preparation_timeout"]:
                goal["status"] = "blocked"
                goal["reason"] = "preparation_timeout"
                continue
            if self.now < goal["next_try"]:
                continue
            if not cap["operations"]:
                if goal["confirmation"] == "best_effort":
                    self._set_goal_done(goal, False)
                else:
                    goal["reason"] = reason or "waiting_for_feedback"
                continue
            op = self._operation(cap, goal)
            if op is None:
                goal["reason"] = "waiting_for_prerequisite" if self._matching_operations(cap, goal["value"]) else "unsupported_capability"
                continue
            ready, blocking = self._ready_to_dispatch(goal, op)
            if not ready:
                goal["reason"] = blocking
                # Guard failures/supply protections are stable blockers. Readiness
                # waits may become valid later in the bounded episode.
                if blocking in {"request_guard_failed", "supply_unmanaged", "supply_retained", "shared_supply_in_use"}:
                    goal["status"] = "blocked"
                continue
            if self._lane_busy(goal["equipment"]):
                goal["reason"] = "superseded_command_pending" if any(c["equipment_id"] == goal["equipment"] and not self._command_is_current(c) for c in self.commands.values()) else "waiting_for_device_lane"
                continue
            self.sequence += 1
            cid = f"command_{self.generation}_{self.sequence}"
            data = deepcopy(op["data"])
            if op.get("value_field"):
                path = op["value_field"].split(".")
                node = data
                for part in path[:-1]:
                    node = node.setdefault(part, {})
                node[path[-1]] = deepcopy(goal["value"])
            cmd = {"id": cid, "equipment_id": goal["equipment"], "capability_id": goal["capability"],
                   "action": op["action"], "target": deepcopy(op["target"]), "data": data, "generation": self.generation,
                   "value": deepcopy(goal["value"]), "goal_key": goal["key"], "operation": deepcopy(op),
                   "resource_ids": deepcopy(op.get("resource_ids", [])),
                   "cleanup": goal["cleanup"], "recovery": goal["recovery"], "submitted_at": None,
                   "deadline": None, "service_done": False, "service_error": False}
            self.commands[cid] = cmd
            goal["status"] = "reserved"
            goal["reason"] = None
            output.append(self._public_command(cmd))
        self._refresh_status()
        return output

    def _public_command(self, cmd: dict) -> dict:
        return {key: deepcopy(cmd[key]) for key in ("id", "equipment_id", "capability_id", "action", "target", "data", "resource_ids", "generation", "value", "cleanup", "recovery")}

    def _refresh_status(self) -> None:
        preparation = [g for g in self.goals.values() if not g["cleanup"]]
        cleanup = [g for g in self.goals.values() if g["cleanup"]]
        if self.preserve_devices and self.mode != "finish":
            self.cleanup_status = "preserved"
        elif not cleanup:
            self.cleanup_status = "not_needed"
        elif any(g["status"] == "blocked" for g in cleanup):
            self.cleanup_status = "blocked"
        elif all(g["status"] in _DONE for g in cleanup):
            self.cleanup_status = "complete"
        else:
            self.cleanup_status = "pending"
        if self.observer_only:
            self.phase = "observing"
        elif self.restart_paused:
            self.phase = "recovery_paused"
        elif self.guard_failed or self.ambiguous:
            self.phase = "blocked"
        elif self.mode == "handover":
            self.phase = "handover"
        elif self.mode == "none" or self.suspended and self.mode != "policy":
            self.phase = "observing"
        elif any(g["status"] == "blocked" for g in preparation):
            self.phase = "blocked"
        elif self.mode in {"finish", "automatic_idle"}:
            self.phase = "finishing" if self.cleanup_status == "pending" else "blocked" if self.cleanup_status == "blocked" else "ready"
        elif any(g["status"] not in _DONE for g in preparation):
            self.phase = "recovering" if any(g["recovery"] for g in preparation) else "preparing"
        elif any(g["reason"] == "preserved_drift" for g in preparation):
            self.phase = "observing"
        else:
            self.preparation_done = True
            self.phase = "applied_unverified" if any(g["status"] in {"applied", "unverified"} for g in preparation) else "ready"

    def snapshot(self) -> dict:
        self._refresh_status()
        blockers = []
        if self.restart_paused:
            blockers.append({"reason": "recovery_paused_after_restart"})
        if self.guard_failed:
            blockers.append({"reason": "request_guard_failed"})
        if self.ambiguous:
            blockers.append({"reason": "ambiguous_activity"})
        for goal in self.goals.values():
            if goal["reason"] and goal["reason"] != "command_applied_unverified":
                blockers.append({"equipment": goal["equipment"], "capability": goal["capability"], "reason": goal["reason"],
                                 "cleanup": goal["cleanup"], "status": goal["status"]})
        preparation = [g for g in self.goals.values() if not g["cleanup"] and g["confirmation"] != "observe_only"]
        verified = self.mode == "activity" and bool(preparation) and self.phase == "ready" and all(g["status"] == "confirmed" and self._read_cap(g["equipment"], g["capability"])[1] and _equal(self._read_cap(g["equipment"], g["capability"])[0], g["value"]) for g in preparation)
        problem = self.phase == "blocked" or self.cleanup_status == "blocked"
        return {"activity_id": self.activity_id, "phase": self.phase, "cleanup_status": self.cleanup_status,
                "verified_ready": verified, "problem": problem, "preserve_devices": self.preserve_devices,
                "observed_activity": self.observed_activity, "request_id": self.request_id, "generation": self.generation,
                "origin": self.origin, "policy_id": self.policy_id, "blockers": deepcopy(blockers), "observer_only": self.observer_only,
                "mode": self.mode, "suspended": self.suspended,
                "handover_until": self.handover_until, "ended_activity": self.ended_activity,
                "policy_ready": self.mode == "policy" and self.phase in {"ready", "applied_unverified"},
                "events": deepcopy(list(self.events)), "capabilities": [{"equipment": eid, "capability": cid,
                    "value": self._read_cap(eid, cid)[0], "valid": self._read_cap(eid, cid)[1], "feedback_type": cap["feedback_type"]}
                    for (eid, cid), cap in self.caps.items()],
                "pending_commands": [{"id": c["id"], "equipment": c["equipment_id"], "capability": c["capability_id"],
                    "submitted": c["submitted_at"] is not None, "generation": c["generation"]} for c in self.commands.values()]}

    def export_state(self) -> dict:
        """JSON-only persistence. There is deliberately no executable queue."""
        return {"version": 1, "activity_id": self.activity_id, "generation": self.generation, "sequence": self.sequence,
                "mode": self.mode, "origin": self.origin, "policy_id": self.policy_id, "request_guards": deepcopy(self.request_guards),
                "preserve_devices": self.preserve_devices, "overrides": deepcopy(self.overrides), "suspended": self.suspended,
                "recovery_budgets": deepcopy(self.recovery_budgets), "events": deepcopy(list(self.events))}
