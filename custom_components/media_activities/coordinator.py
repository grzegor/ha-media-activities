"""Home Assistant transport and lifecycle for the deterministic coordinator."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
import logging
import time
from typing import Any

from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .engine import Engine
from .schema import required_entities, validate_config

_LOGGER = logging.getLogger(__name__)
TERMINAL_PHASES = {"ready", "applied_unverified", "blocked", "recovery_paused"}
SERVICE_ACTIONS = (
    "request_activity", "finish", "retry", "set_device_setting",
    "cancel_requests", "apply_policy", "export_configuration", "import_configuration",
)


def replace_entity(value: Any, old: str, new: str) -> Any:
    """Replace exact entity references only; do not rewrite arbitrary strings."""
    if isinstance(value, dict):
        return {key: replace_entity(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_entity(item, old, new) for item in value]
    return new if value == old else value


class MediaCoordinator:
    """Own HA I/O without embedding household or device protocols."""

    def __init__(self, hass: HomeAssistant, entry: Any, config: dict) -> None:
        self.hass = hass
        self.entry = entry
        self.config = validate_config(config)
        self.engine: Engine | None = None
        self._listeners: set[Callable] = set()
        self._unsubscribers: list[Callable] = []
        self._timer: Callable | None = None
        self._closed = False
        self._reconfiguring = False
        self._configuration_error: str | None = None
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._queued: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_snapshot: dict = {}
        self._last_completion: tuple | None = None
        self._registry_bindings: dict[str, str] = {}
        self._registry_watch_entities: set[str] = set()
        self._pending_registry_config: dict | None = None
        self._registry_rename_task: asyncio.Task | None = None
        self._context = Context()
        self._last_refresh: dict[str, float] = {}
        self._entities = required_entities(self.config)

    @property
    def snapshot(self) -> dict:
        snapshot = self.engine.snapshot() if self.engine else {
            "phase": "observing", "observer_only": True, "activity_id": "idle",
            "preserve_devices": False, "verified_ready": False, "problem": False,
            "cleanup_status": "not_needed", "observed_activity": "unknown",
        }
        if self._configuration_error:
            snapshot.update(phase="blocked", problem=True, verified_ready=False, observer_only=True)
            snapshot["blockers"] = [*snapshot.get("blockers", []), {"reason": "invalid_configuration"}]
        return snapshot

    async def async_suspend_configuration(self, error: str) -> None:
        """Stop unsent work while preserving the outcome of submitted commands."""
        self._configuration_error = error
        self._queued.clear()
        if self.engine is not None:
            self.engine.set_observer(True, time.time())
        self._publish()
        self._update_issues()

    def async_add_listener(self, callback_: Callable) -> Callable:
        self._listeners.add(callback_)
        return lambda: self._listeners.discard(callback_)

    async def async_start(self) -> None:
        saved = await self._store.async_load()
        self.engine = Engine(self.config, time.time(), saved)
        registry = er.async_get(self.hass)
        self._registry_bindings = {
            entity_id: entry.id
            for entity_id in self._entities
            if (entry := registry.async_get(entity_id))
        }
        self._registry_watch_entities = set(self._entities)
        # Initial cached HA states are useful observations, but are not new
        # reports proving that a session continued across a restart.
        for entity_id in self._entities:
            self._read_state(entity_id, fresh=False)
        self._unsubscribers.extend([
            async_track_state_change_event(self.hass, self._entities, self._state_changed),
            async_track_state_report_event(self.hass, self._entities, self._state_reported),
            async_track_entity_registry_updated_event(
                self.hass, self._entities, self._registry_changed
            ),
        ])
        self._step()
        self._schedule_tick()

    def _read_state(self, entity_id: str, fresh: bool) -> None:
        state = self.hass.states.get(entity_id)
        assert self.engine is not None
        self.engine.observe(
            entity_id,
            state.state if state else "unavailable",
            dict(state.attributes) if state else {},
            time.time(),
            fresh=fresh and bool(state) and not state.attributes.get("restored", False),
        )

    @callback
    def _state_changed(self, event: Event) -> None:
        if self._closed:
            return
        self._read_state(event.data["entity_id"], fresh=True)
        self._step()

    @callback
    def _state_reported(self, event: Event) -> None:
        if self._closed:
            return
        self._read_state(event.data["entity_id"], fresh=True)
        self._step()

    @callback
    def _registry_changed(self, event: Event) -> None:
        if self._closed:
            return
        data = event.data
        old = data.get("old_entity_id") or data.get("changes", {}).get("entity_id")
        new = data.get("entity_id")
        current = self._pending_registry_config or self.config
        if data.get("action") == "update" and old and new and old in required_entities(current):
            self._pending_registry_config = replace_entity(current, old, new)
            self._queued.clear()
            if self.engine is not None:
                self.engine.set_observer(True, time.time())
            if old in self._registry_bindings:
                self._registry_bindings[new] = self._registry_bindings.pop(old)
            # The original indexed listener still watches old IDs. Follow a
            # second rename of the new ID while a slow device action settles.
            if new not in self._registry_watch_entities:
                self._registry_watch_entities.add(new)
                self._unsubscribers.append(async_track_entity_registry_updated_event(
                    self.hass, [new], self._registry_changed
                ))
            if self._registry_rename_task is None or self._registry_rename_task.done():
                self._registry_rename_task = self.hass.async_create_background_task(
                    self._apply_registry_renames(), f"{DOMAIN} deferred entity rename"
                )
            self._publish()
        elif data.get("action") == "remove":
            self._update_issues()

    async def _apply_registry_renames(self) -> None:
        """Coalesce exact registry changes without interrupting device drivers."""
        try:
            while not self._closed and self._pending_registry_config is not None:
                active = {task for task in self._tasks if not task.done()}
                if active:
                    # Unlike gather, cancelling this waiter does not propagate
                    # cancellation into the already-submitted device actions.
                    await asyncio.wait(active)
                    continue
                self._tasks.difference_update(task for task in tuple(self._tasks) if task.done())
                candidate = self._pending_registry_config
                await self.async_import(candidate, keep_observer=True)
                self.config = validate_config(candidate)
                self._entities = required_entities(self.config)
                self._pending_registry_config = None
        except (HomeAssistantError, ValueError, KeyError, TypeError) as exc:
            await self.async_suspend_configuration(str(exc))
        finally:
            self._registry_rename_task = None

    def _schedule_tick(self) -> None:
        if self._closed:
            return
        self._timer = async_call_later(self.hass, 2, self._timed_step)

    @callback
    def _timed_step(self, _now: Any) -> None:
        self._timer = None
        self._step()
        self._schedule_tick()

    @callback
    def _step(self) -> None:
        if self._closed or self.engine is None:
            return
        if self._pending_registry_config is not None:
            self._publish()
            self._update_issues()
            return
        if self._configuration_error:
            self._publish()
            self._update_issues()
            return
        for command in self.engine.tick(time.time()):
            self._queued[command["id"]] = command
        shared = self.hass.data[DOMAIN].setdefault("in_flight", {})
        for command_id, command in list(self._queued.items()):
            if not self.engine.can_dispatch(command_id, time.time()):
                self._queued.pop(command_id, None)
                continue
            resources = self._command_resources(command)
            # A previous integration incarnation may still have an action in
            # another driver's queue. Wait; unloading did not cancel hardware.
            if any(key in shared and not shared[key].done() for key in resources):
                continue
            self._queued.pop(command_id, None)
            self.engine.submitted(command_id, time.time())
            task = self.hass.async_create_background_task(
                self._execute(command),
                f"{DOMAIN} {self.entry.entry_id} {command_id}",
            )
            self._tasks.add(task)
            for key in resources:
                shared[key] = task
            task.add_done_callback(self._tasks.discard)
        self._publish()
        self._update_issues()

    def _command_resources(self, command: dict) -> set[str]:
        targets = command.get("target", {}).get("entity_id", [])
        if isinstance(targets, str):
            targets = [targets]
        # Use the full equipment lane as well as target entities, including
        # across coordinator reloads.
        return {str(x) for x in targets} | set(command.get("resource_ids", [])) | {
            "equipment:" + self.entry.entry_id + ":" + str(command["equipment_id"])
        }

    async def _execute(self, command: dict) -> None:
        assert self.engine is not None
        error = None
        try:
            # Submission is serialized by the pure engine. HA action completion
            # still is not sufficient proof of the requested physical state.
            domain, service = command["action"].split(".", 1)
            if not self.hass.services.has_service(domain, service):
                raise HomeAssistantError("Action is not registered")
            await self.hass.services.async_call(
                domain, service,
                deepcopy(command.get("data", {})),
                target=deepcopy(command.get("target", {})),
                blocking=True, context=self._context,
            )
        except asyncio.CancelledError:
            error = "action_outcome_unknown"
            raise
        except Exception as exc:  # integration boundary: redact exception payload
            error = f"action_failed:{type(exc).__name__}"
            _LOGGER.warning(
                "Media command %s for %s failed (%s)",
                command["action"], command["equipment_id"], type(exc).__name__,
            )
        finally:
            self.engine.completed(command["id"], time.time(), error)
            shared = self.hass.data.get(DOMAIN, {}).get("in_flight", {})
            current = asyncio.current_task()
            for key in self._command_resources(command):
                if shared.get(key) is current:
                    shared.pop(key, None)
            if not self._closed:
                self._step()

    def _publish(self) -> None:
        snap = self.snapshot
        if snap != self._last_snapshot:
            self._last_snapshot = deepcopy(snap)
            for listener in tuple(self._listeners):
                listener()
            self._store.async_delay_save(self.engine.export_state, 5)
        result = (snap.get("request_id"), snap.get("phase"), snap.get("cleanup_status"))
        if (
            result[0] and result[1] in TERMINAL_PHASES
            and result[2] != "pending" and result != self._last_completion
        ):
            self._last_completion = result
            self.hass.bus.async_fire(
                f"{DOMAIN}_request_completed",
                {
                    "config_entry_id": self.entry.entry_id,
                    "request_id": result[0],
                    "result": result[1],
                    "cleanup_status": result[2],
                    "blockers": [
                        {"reason": item.get("reason"), "equipment": item.get("equipment")}
                        for item in snap.get("blockers", [])
                        if isinstance(item, dict)
                    ],
                },
                context=self._context,
            )

    def _update_issues(self) -> None:
        from .repairs import async_update_issues

        missing = sorted(
            entity_id for entity_id in required_entities(self._pending_registry_config or self.config)
            if self.hass.states.get(entity_id) is None
        )
        async_update_issues(self.hass, self.entry, missing, configuration_error=self._configuration_error)

    def _invoke(self, method: str, *args: Any, **kwargs: Any) -> dict:
        if self._closed or self.engine is None:
            raise HomeAssistantError("Media system is not available")
        if self._configuration_error:
            raise HomeAssistantError("Media configuration needs repair before control can resume")
        if self._pending_registry_config is not None:
            raise HomeAssistantError("Entity renames are waiting for submitted media actions")
        try:
            request_id = getattr(self.engine, method)(*args, time.time(), **kwargs)
        except (ValueError, KeyError) as exc:
            raise HomeAssistantError(str(exc)) from exc
        self._step()
        return {
            "accepted": True, "request_id": request_id,
            "observer_only": self.snapshot.get("observer_only", True),
        }

    async def async_request(
        self, activity_id: str, origin: str = "user", guards: list | None = None
    ) -> dict:
        return self._invoke("request", activity_id, origin=origin, guards=guards)

    async def async_finish(self) -> dict:
        return self._invoke("finish")

    async def async_retry(self) -> dict:
        return self._invoke("retry")

    async def async_set_preserve(self, value: bool) -> dict:
        return self._invoke("set_preserve", value)

    async def async_set_observer(self, value: bool) -> dict:
        result = self._invoke("set_observer", value)
        from .config_flow import store_config

        self.config["observer_only"] = value
        self._reconfiguring = True
        try:
            store_config(self.hass, self.entry, self.config)
        finally:
            # Scheduled update listeners run after this function returns.
            self.hass.loop.call_soon(self._finish_reconfiguration)
        return result

    @callback
    def _finish_reconfiguration(self) -> None:
        self._reconfiguring = False

    async def async_set_setting(self, equipment: str, capability: str, value: Any) -> dict:
        return self._invoke("set_override", equipment, capability, value)

    async def async_apply_policy(self, policy_id: str, origin: str = "policy") -> dict:
        return self._invoke("apply_policy", policy_id, origin=origin)

    async def async_cancel_origin(self, origin: str) -> dict:
        return self._invoke("cancel_origin", origin)

    async def async_export(self) -> dict:
        return {"configuration": deepcopy(self.config)}

    async def async_diagnostics(self) -> dict:
        return {
            "configuration": deepcopy(self.config),
            "snapshot": self.snapshot,
            "runtime": {
                "pending_action_count": len(self._tasks),
                "waiting_dispatch_count": len(self._queued),
                "registry_bindings": self._registry_bindings,
                "entity_rename_pending": self._pending_registry_config is not None,
                "closed": self._closed,
            },
        }

    async def async_import(self, config: dict, keep_observer: bool = False) -> dict:
        from . import validate_ownership
        from .config_flow import store_config

        candidate = validate_config(config)
        if not keep_observer:
            candidate["observer_only"] = True
        validate_ownership(self.hass, candidate, self.entry.entry_id)
        if self._tasks or (
            self._pending_registry_config is not None
            and asyncio.current_task() is not self._registry_rename_task
        ):
            raise HomeAssistantError("Wait for submitted media actions before reconfiguring")
        self._reconfiguring = True
        try:
            self.engine.set_observer(True, time.time())
            store_config(self.hass, self.entry, candidate)
        finally:
            self._reconfiguring = False
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
        return {"accepted": True, "observer_only": candidate["observer_only"]}

    async def async_stop(self) -> None:
        self._closed = True
        self._queued.clear()
        if self._registry_rename_task is not None:
            self._registry_rename_task.cancel()
            await asyncio.gather(self._registry_rename_task, return_exceptions=True)
            self._registry_rename_task = None
        if self._pending_registry_config is not None:
            # Persist exact bindings on unload without activating a new engine.
            # A replacement coordinator still respects the shared action ledger.
            from .config_flow import store_config

            self._reconfiguring = True
            store_config(self.hass, self.entry, self._pending_registry_config)
            self._pending_registry_config = None
        if self._timer:
            self._timer()
            self._timer = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._listeners.clear()
        if self.engine:
            await self._store.async_save(self.engine.export_state())
        # Do not cancel already-submitted device-driver calls. The shared ledger
        # prevents a replacement coordinator from overlapping their resources.
