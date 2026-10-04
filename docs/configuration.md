# Configuration

One HA config entry describes a media system. Equipment and activities have stable IDs; names are editable labels. Start with native entity selection, then use advanced JSON to describe interfaces requiring additional observations, alternate commands or dependencies.

Exact entity-registry renames update their configured references automatically. If a device action is already submitted, new dispatch pauses while that action settles; consecutive renames are combined before reloading. Removing an entity produces a repair issue and never substitutes a similarly named entity.

An equipment item represents one physical unit and all its interfaces. A receiver's native media-player entity and its Music Assistant session can be capabilities of the same equipment. The session should not become a second source of physical-power truth.

## Shape

A minimal source-waking TV needs only an input capability:

```json
{
  "version": 1,
  "name": "Living room",
  "observer_only": true,
  "equipment": [{
    "id": "tv", "name": "Television",
    "capabilities": [{
      "id": "input", "kind": "input", "value_type": "enum",
      "allowed_values": ["Live TV", "HDMI 1"],
      "feedback_type": "reported",
      "observation": {
        "entity_id": "media_player.television", "attribute": "source",
        "valid_when": [{
          "entity_id": "media_player.television",
          "states": ["on", "idle", "playing", "paused", "buffering"]
        }]
      },
      "operations": [{
        "any_value": true, "action": "media_player.select_source",
        "target": {"entity_id": "media_player.television"},
        "data": {}, "value_field": "source", "idempotent": true
      }]
    }]
  }],
  "activities": [{
    "id": "watch_tv", "name": "Watch TV",
    "requirements": [{
      "id": "input", "equipment": "tv", "capability": "input",
      "value": "Live TV", "confirmation": "required"
    }]
  }]
}
```

If input selection does not wake this television, add its supported power capability and an explicit activity requirement. Other equipment categories are never mandatory.

## Capabilities and requirements

A capability defines a property and its available actions. Supported value types are `boolean`, `number`, `string` and `enum`. Values are typed: use JSON `true`, not the string `"true"`, for logical power.

An observation reads an entity's state or an attribute. `value_map` normalizes integration-specific values; for example `{"on": true, "off": false}`. Unavailable states do not become a false Off reading. Optional `max_age` is measured in seconds; an HA timestamp does not itself establish a new physical measurement.

Optional observation `valid_when` predicates establish when a remembered attribute is meaningful. Native media-player source and sound-mode bindings require an active physical state, so a cached HDMI value while a TV is Off cannot falsely satisfy an activity that wakes the TV by selecting that input.

`valid_when` controls whether feedback can confirm a goal; it does not prevent the operation that makes the feedback valid. Put conditions that must hold before sending an operation in `prerequisites`. For example, selecting a source may wake a television whose old source attribute is currently invalid.

An operation has either `value` for a particular desired value or `any_value: true` with `value_field` identifying the action data field into which the requested value is inserted. Other `data` values are fixed JSON. There is no arbitrary code or Jinja execution in the configuration. Mark an action idempotent only when repeating the same set operation is safe; toggles and one-shot playback should not be retried automatically.

Every operation must identify its controlled resources with `target.entity_id` or explicit `resource_ids`. For a direct script action with no target, declare the script entity in `resource_ids`. Do not hide an additional controlled device inside a script without declaring that resource: ownership checks and serialized command lanes rely on the declaration.

Version 1 accepts fixed entity targets, including lists of entity IDs. It rejects area/device targets, addressing hidden in `data.entity_id`/`device_id`/`area_id`, and a `value_field` that changes an action target. Targetless or opaque actions must declare their resources explicitly; these are existing HA entity IDs, not arbitrary labels:

```json
{
  "value": true,
  "action": "script.wake_projector",
  "target": {},
  "resource_ids": ["script.wake_projector", "media_player.projector"],
  "data": {},
  "idempotent": false
}
```

The declaration above belongs to the same equipment item as the projector's other controls. Other systems or equipment items cannot independently claim the same controlled resources. Declaring a resource establishes ownership; it does not create physical-state feedback.

A requirement references an equipment and capability and supplies its desired value. `depends_on` contains prerequisite requirement IDs in the same activity. Dependencies must be acyclic. Independent equipment can prepare concurrently; commands mutating one physical item are serialized. Optional `min_delay`, `guards` and declared capability/operation prerequisites describe readiness without imposing an outlet-first sequence on every installation.

Confirmation is `required` for suitable reported feedback, `best_effort` when limited feedback is deliberate, or `observe_only` for a diagnostic condition that must not actuate. An unsupported requirement is a configuration error.

With explicit `best_effort`, an action error or missing confirmation terminates as unverified after any eligible bounded retries. The actual reason, such as `action_failed` or `confirmation_timeout`, remains visible; the integration does not claim that a failed action was applied. Other required steps may proceed, and the activity reports **Applied, unverified**, with Verified ready Off. The capability remains classified as reported if that is how it was configured. Required confirmation still blocks on failure, and best effort never bypasses request guards or supply-removal safeguards.

A reported capability with no operations can be a `required` observation-only readiness requirement: it waits for its condition within the preparation deadline without inventing a command. A deliberately unobservable capability with no operations can use `best_effort` to make its limitation explicit. `observe_only` is diagnostic and does not gate dependent work; use `required` when readiness must hold.

Request/policy guards are positive permission conditions. Losing a request guard invalidates the current episode; a later True value does not silently restart it. Capability/operation prerequisites and operation safety guards are bounded readiness waits, rechecked immediately before dispatch. Safety conditions require suitable fresh evidence; unknown or optimistic shutdown state cannot authorize a protected cut.

## Cleanup and supplies

An optional capability `cleanup: {"value": false}` declares logical shutdown when its equipment is unused. Add explicit guards when needed. Cleanup is independent of the selected activity's preparation.

The native physical media-player factory adds logical Off cleanup when the entity advertises `TURN_OFF`. A player supporting only On receives no invented cleanup action. This logical standby default does not authorize cutting a socket.

A supply is an optional Boolean capability of kind `supply`, with `supply_policy` set to `unmanaged`, `always_on`, `standby_on` or `managed`. New supply bindings default to `standby_on`. Factory-generated supplies include no Off operation: removing power requires deliberate configuration of the command and its safety prerequisites.

A supply may list `consumers` by equipment ID. All consumers and their declared shutdown requirements must permit removal. A shared source kept always on prevents the common supply being removed. Low watts or no HDMI is not a substitute for a configured OS-shutdown or cooldown acknowledgement.

Supply loss invalidates cached network readiness. A policy that first confirms shutdown and then intentionally removes supply retains that completed shutdown step; it does not try to contact the now-unpowered device to shut it down again. This historical completion does not establish readiness for a later activity.

## Named policies

A policy has `id`, optional `name`, positive `guards`, and `requirements` using the activity requirement shape. Version 1 policies suspend activity enforcement (`suspend_activity: true`). They do not run concurrently with an old activity or restart playback when restoring standby supplies.

Optional `finish_activity: true` atomically selects Idle, clears session overrides, and resets preservation Off before preparing the policy's own requirements. Use it for a policy that ends the current media session. The default is False: the prior activity selection is retained as suspended intent. Only the policy's declared requirements are performed; the flag does not automatically add every device's cleanup operation.

The Status sensor exposes `mode`, `policy_id`, `suspended`, and `policy_ready`. A completed policy does not set the activity's Verified ready sensor On. `policy_ready` permits explicitly configured best-effort results, so it must not substitute for a fresh physical safety condition before removing power.

## External activity detection

Detection rules have an `id`, `activity`, `conditions`, optional `guards`, `trigger_entities` or `trigger_bindings`, and `debounce` in seconds. Conditions support `state` equality, `states` membership, `minimum`/`maximum` numeric bounds, an optional `attribute`, and `max_age`. The default debounce is two seconds.

Use a relevant source/input change or explicitly qualified wake signal. By default, triggers watch the fields referenced by a rule's conditions; an unrelated volume attribute update is not a source-selection change. Advanced `trigger_bindings` select exact fields and optionally restrict the transition with scalar `from` and `to` values:

```json
{
  "id": "player_wake",
  "activity": "watch_tv",
  "conditions": [{"entity_id": "binary_sensor.player_video", "state": "on"}],
  "guards": [{"entity_id": "input_boolean.media_detection_allowed", "state": "on"}],
  "trigger_bindings": [{
    "entity_id": "binary_sensor.player_video", "from": "off", "to": "on"
  }],
  "debounce": 2
}
```

This rule treats a qualified video signal rising as intent, while its disappearance does not request an activity. Omit `from`/`to` for an input selector whose known value changes should all be considered. Directional filters use raw entity state/attribute values, not a capability's `value_map`.

Only known, fresh previous values changing to known, fresh values create external intent. Startup cache refreshes and transitions out of `unknown`/`unavailable` establish a baseline; they do not wake devices. Rules whose guards are disabled do not create ambiguity, including when disabled during debounce.

During debounce the coordinator waits before enforcing the previous activity. It evaluates the matching rules affected by the actual changes, so a still-running unused source does not compete with the newly selected source. A state change matching an outstanding command is its acknowledgement, not a new request. Conflicting simultaneous changes—or an unmapped selector value—report manual routing and suspend enforcement until clear intent arrives.

## Portable examples

| File | Demonstrates |
|---|---|
| [tv_only.json](../examples/tv_only.json) | One TV; source selection wakes it; no separate power action. |
| [avr_console.json](../examples/avr_console.json) | Independent receiver/display preparation and best-effort console wake. |
| [audio_only.json](../examples/audio_only.json) | Music Assistant playback with no display or socket requirements. |
| [ir_only.json](../examples/ir_only.json) | Useful command-only operation with unverified success. |
| [mixed_ir_network.json](../examples/mixed_ir_network.json) | IR bootstrap while network is unavailable; network confirms power later. |
| [projector_matrix.json](../examples/projector_matrix.json) | Projector, receiver, router and sources using anonymized, editable bindings. |

Examples are structural templates, not assurances that an arbitrary device implements those actions. For example, the console in the projector example deliberately requires a functioning console integration; a smart socket alone cannot implement it.

Export configuration before editing. Both export and import actions require an administrator. Import validates the configuration and returns the system to observer mode without actuating equipment.

## Source shutdown and handover

Activities may declare positive `end_conditions`, optional `end_guards`, `end_debounce` (default two seconds), and `handover_timeout` (default 180 seconds). All end conditions must match by default; `end_condition_mode: "any"` accepts any fresh known matching condition. Unknown alternatives never count as matched. Bind these to suitable source feedback, not merely to an outlet that remains On while the source sleeps:

```json
{
  "id": "play_console",
  "name": "Play console",
  "requirements": [],
  "end_conditions": [{"entity_id": "media_player.console", "states": ["off", "standby"]}],
  "end_debounce": 2,
  "handover_timeout": 180
}
```

Replace the empty requirements with your equipment requirements. A source session must first have fresh known observations that do not match the end conditions. Initial cached Off and unknown/unavailable do not end a session. After confirmed source end, requested and observed activity become Idle, Status shows Waiting for next activity, and `handover_until` identifies the cleanup deadline. Old enforcement and unsent work stop immediately; already-submitted device commands remain tracked. No shutdown is sent during the handover window.

Selecting or detecting the next activity cancels the window and prepares it without first cooling a still-running display. Otherwise, one bounded cleanup pass follows expiry. Automatic Idle preserves the Don't turn off devices switch; On prevents cleanup. Explicit Finish bypasses the window and resets preservation Off. A restart during the window pauses recovery and never replays a saved timer to shut equipment down.

A new detected wake of a blocked current activity may restart recovery of blocked requirements in normal mode, preserving successful one-shot playback and the rolling recovery budget. Unchanged reports do not restart recovery.

Predicates may also specify `stable_for` in seconds. This requires a fresh observation that has stayed unchanged for that duration. For a device that briefly reports standby before accepting wake, use a stable Off condition for the On operation, and configure spaced, finite retries. This reduces transition races; it cannot guarantee that a device will never reject a command. A reported state timestamp is not proof of a physical read, and unknown/optimistic state cannot authorize a protected mains cut.
