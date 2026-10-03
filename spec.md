# Media Activities — reusable Home Assistant integration specification

**Document:** `spec.md`  
**Status:** Public integration specification for version 0.1.0. Implementation details are documented below and in the project documentation.  
**Initial compatibility target:** Home Assistant Core 2026.9.4. Later releases require compatibility testing.

## 1. Purpose and design boundaries

Build a reusable Home Assistant integration that coordinates multimedia activities using devices already exposed to HA.

It must work with:

- A television using its own speakers and applications.
- A television, receiver and console.
- A projector and separate HDMI matrix.
- Audio-only equipment.
- IR-controlled equipment.
- Devices with or without smart outlets.
- Different combinations of network control, IR commands and feedback.

**No television, receiver, HDMI switch, smart outlet, power meter, IR transmitter or particular manufacturer is mandatory.**

The integration describes desired states, issues appropriate commands, checks available feedback and performs bounded recovery. It must remain useful when some states cannot be verified.

Use domain `media_activities` and display name **Media Activities**.

### Architecture decision

Implement a custom integration inside HA. Retain existing device integrations and native dashboard cards.

Do not introduce:

- A separate app/add-on or runtime.
- New device-network protocol implementations.
- Custom frontend cards.
- An external database.
- Cloud dependencies.
- A general scripting or automation language.
- Autonomous LLM modification of running configuration.

Manufacturer-specific behavior belongs in optional adapters or presets. Household preferences belong in configuration and external automations. Example installations must not determine the product architecture.

## 2. User behavior

### Activities

An activity is a collection of desired device capabilities and optional dependencies.

For example:

- Watch television: television On, television source Live TV.
- Play console: television source HDMI 2, receiver source Game, console On.
- Listen to music: speaker ready, selected media playing.
- Watch projector: source awake, matrix routed, receiver ready, projector On with a picture mode.

An activity does not require a fixed sequence of equipment categories.

Selecting an activity must:

1. Immediately publish the requested activity.
2. Prepare the configured requirements.
3. Report confirmation or its absence honestly.
4. Handle unused equipment according to policy.
5. Continue bounded enforcement when enabled.

### Preservation mode

Expose a switch with internal key `preserve_devices` and English name **Don’t turn off devices**.

Default: **Off**. Persist deliberate changes across restarts.

| Behavior | Off: normal operation | On: manual flexibility |
|---|---|---|
| New activity | Prepare required equipment and clean up unused equipment | Prepare required equipment and preserve unused equipment |
| Recognized external activity change | Adopt and prepare it, including cleanup | Adopt and prepare it, preserving unused equipment |
| Later state drift | Bounded automatic correction where observable and controllable | Observe and report only |
| Explicit Retry | Reconcile activity and cleanup | Perform one bounded preparation pass without cleanup |
| Finish media | Finish all managed equipment safely | Finish all managed equipment safely |

Turning preservation On cancels unsent cleanup and automatic recovery. Track any command already submitted.

Turning it Off reconciles the current activity again. Do not guess an activity when routing or activity identification is ambiguous.

### Finish media

Finish media must atomically:

- Set the requested activity to Idle.
- Reset preservation to its default, Off.
- Supersede obsolete unsent work.
- Stop or shut down managed equipment according to configured device policies.
- Report blocked cleanup independently.

It must not temporarily re-enforce the previous activity while resetting the switch.

Always-on devices and supplies remain protected.

### Restart recovery

Persisted intent alone must not turn equipment on after an HA restart.

Resume automatically only when fresh observations establish a continuing session. Otherwise publish **Recovery paused after restart**.

An explicit activity request or Retry resumes coordination.

For installations without enough feedback to establish a continuing session, wait for explicit input.

## 3. Generalized equipment model

### Systems and equipment

A **system** is an independently coordinated media installation, normally one room.

An **equipment item** represents one logical piece of equipment. It can combine several HA entities, such as:

- A receiver’s network entity.
- Its Cast playback entity.
- Its optional outlet.
- Its optional IR wake action.

These are interfaces to the same equipment, not competing copies of its physical state.

Equipment roles—display, audio, source, router or accessory—are optional presentation metadata. They must not impose execution requirements.

Multiple systems are supported when their controlled resources do not overlap. Shared controlled equipment across systems is outside v1; reject conflicting ownership with a clear configuration error.

### Optional capabilities

An equipment item can expose any subset of:

- Logical power.
- Availability/readiness.
- Input or application selection.
- Routing channels.
- Picture mode.
- Sound mode.
- Playback.
- Volume and mute.
- Other explicitly named scalar or enumerated settings.
- Optional supply control.

A missing capability means **not applicable**, not failed or unavailable.

Examples:

- No outlet configured: perform no supply operation and display no missing-outlet warning.
- No external router: use the TV or receiver’s input capability.
- No display: an audio activity never waits for a display.
- Source selection wakes the TV: use that operation without demanding a separate power command.
- No independent logical power control: coordinate the supported capabilities instead.

### Capability definitions

Each capability contains:

| Field | Meaning |
|---|---|
| `id` | Stable identifier within its equipment |
| `kind` | Power, input, route, playback, setting, availability or supply |
| `value_type` | Boolean, number, string or enum |
| `allowed_values` | Optional validated choices/range |
| `observation` | Optional entity state or attribute binding |
| `feedback_type` | Reported, optimistic or command-only |
| `operations` | Supported desired-value-to-action mappings |
| `prerequisites` | Explicit readiness requirements |
| `timeout` | Confirmation deadline |
| `retry_policy` | Idempotence and retry eligibility |
| `cleanup` | Optional unused-equipment behavior |

Capabilities may be observation-only or command-only.

Automatic recovery requires both a suitable observation and a supported corrective action. It must not infer an error from the absence of feedback.

### Observation bindings

Support:

- Entity state.
- A named entity attribute.
- Explicit mapping from reported values to normalized values.
- Expected values and simple membership/range checks.
- Freshness requirements where needed.
- A separate indication that the source is unavailable.

Keep predicates finite and declarative. Do not require Jinja or arbitrary Python.

An HA timestamp alone does not prove that an integration performed a new physical read. Adapters must document their feedback guarantees.

### Feedback classes

| Class | Meaning | Permitted behavior |
|---|---|---|
| Reported | Suitable device feedback establishes a property | Confirm success and recover observed mismatches |
| Optimistic | State primarily reflects commands submitted | Show limited confirmation; do not use for destructive safety decisions |
| Command-only | No usable feedback exists | Dispatch once and report unverified application |

An ordinary IR television must remain usable. It should report **Applied, unverified**, not permanently fail or receive repeated power commands.

Conversely, missing feedback from a capability configured as verifiable is a failure to confirm; do not silently downgrade it to command-only.

## 4. Actions, adapters and dependencies

### Generic adapters required in v1

1. **Native media player**
   - Uses supported HA power, source, sound-mode, playback, volume and mute actions.
   - Requires the user or preset to distinguish physical equipment from playback-session entities.
   - Does not assume every media player supports every action.

2. **Native select, switch, number and button**
   - Maps existing entities to a capability.
   - Button actions require independent feedback or command-only classification.

3. **HA action with optional observation**
   - Binds a registered HA action, fixed target and data to an operation.
   - Allows insertion of a requested typed value into explicitly configured data fields.
   - Supports vendor actions, ESPHome actions and existing user-owned scripts.
   - No arbitrary executable expressions, loops or branching inside the integration configuration.

4. **Optional device-specific extensions**
   - Handle behavior that generic mappings cannot express clearly: queued requests, unusual readiness or unreliable state semantics.
   - Use existing HA actions and observations.
   - Do not access private objects belonging to other integrations.

Standard HA media-player actions already cover common power, source, playback and sound controls; use those before requiring advanced mappings. [HA media-player interface](https://www.home-assistant.io/integrations/media_player/)

### Presets

Provide optional presets for the initial deployment’s Epson, Sony, Apple TV, HDFury and Music Assistant interfaces.

Presets generate editable bindings. They must not:

- Be required dependencies.
- Assume entity IDs.
- Assume HDMI port numbers.
- Force an outlet or power meter.
- Install another integration automatically.
- Store household-specific preferences.

A TV-only installation must load and operate without importing any optional manufacturer adapter.

### Dependencies

An activity’s requirements may depend on other requirements.

Examples:

- Select receiver input after receiver readiness.
- Start playback after speaker readiness.
- Set picture mode while a particular projector is warming up, if supported.
- Send wake IR after supply restoration and a minimum boot delay.

Do not universally impose “outlet → power → input.” That sequence is appropriate only when declared.

Validate dependency graphs and reject cycles before activation.

Independent equipment may prepare concurrently.

## 5. Configuration and native user interface

### Setup experience

Offer a short setup path:

1. Name the system and optionally select an area.
2. Add existing HA equipment entities.
3. Choose supported activity inputs or settings.
4. Review the generated configuration.
5. Start in observer-only commissioning mode.

Expose advanced settings progressively:

- Alternate command paths.
- IR startup.
- Readiness dependencies.
- Safe supply removal.
- Manual activity-detection rules.
- Feedback mappings and recovery limits.

Do not show outlet or router configuration as mandatory steps.

### Persistence

Use one native HA config entry per system and native subentries for equipment and activities.

Support:

- Reconfiguration.
- Stable internal IDs.
- Versioned migrations.
- Validated JSON export/import.
- No actuation during import.
- Entity-registry rename tracking.

Configuration entries and subentries provide HA-managed persistence and lifecycle. [HA configuration entries](https://developers.home-assistant.io/docs/config_entries_index/)

Missing entities must produce an actionable issue. Never silently substitute a similarly named device.

### Activity schema

Each activity contains:

- Stable `id`.
- Name, icon and display order.
- A list of requirements, each with a stable ID.
- Equipment/capability reference.
- Desired value.
- Confirmation policy.
- Optional prerequisite requirement IDs.
- Optional playback specification.

Supported confirmation policies:

- `required`: a suitable observation must confirm success.
- `best_effort`: send the configured action; record whether confirmation exists.
- `observe_only`: include the condition in diagnostics without actuating it.

Unsupported requirements must be flagged during configuration. The user may deliberately choose best-effort behavior; the integration must not do so automatically.

### Example: television only

Conceptual configuration:

```yaml
equipment:
  - id: television
    adapter: media_player
    entity_id: media_player.television

activities:
  - id: watch_tv
    name: Watch TV
    requirements:
      - id: television_input
        equipment: television
        capability: input
        value: Live TV
        confirmation: required
```

The adapter/configuration specifies whether input selection implicitly wakes this television. A separate power requirement is added only if necessary.

There is no supply, receiver, router, IR or power-meter requirement.

### User entities

For each system expose:

- Activity select: requested activity.
- Preservation switch.
- Observed activity sensor.
- Status sensor.
- Verified-ready binary sensor.
- Cleanup status sensor.
- Problem binary sensor.
- Retry button.
- Finish media button.

Entity names derive from the system and standard entity naming conventions.

Use English strings and provide Polish translations initially. User-defined activity names remain editable and are not forced into English for other installations.

Expose concise progress and blocker attributes. Keep the full diagnostic history out of entity attributes.

### Public actions

Use `config_entry_id` to address a system and stable IDs for equipment/activities.

| Action | Purpose |
|---|---|
| `media_activities.request_activity` | Select an activity; return request ID and acceptance |
| `media_activities.finish` | Finish equipment and reset preservation |
| `media_activities.retry` | Explicit bounded reconciliation |
| `media_activities.set_device_setting` | Apply a supported setting through the equipment command lane |
| `media_activities.cancel_requests` | Invalidate pending requests by origin |
| `media_activities.apply_policy` | Apply a configured operating/standby policy |
| `media_activities.export_configuration` | Return versioned configuration |
| `media_activities.import_configuration` | Validate and import without actuation |

Action submission must not block through a multi-minute warmup.

Expose documented completion events with request ID, result and reason codes.

A manual setting made through `set_device_setting` becomes an activity-session override. Clear it on selection of a different activity or Finish media.

## 6. Coordinator and state semantics

### Separate state dimensions

Maintain:

- Requested activity.
- Observed activity.
- Operational phase.
- Cleanup status.
- Per-capability verification.

Operational phases:

`observing`, `preparing`, `ready`, `applied_unverified`, `recovering`, `finishing`, `blocked`, `recovery_paused`.

Cleanup states:

`not_needed`, `preserved`, `pending`, `complete`, `blocked`.

Examples:

> Watch TV · Ready

> Watch TV · Applied, unverified  
> Television power has no feedback.

> Watch Apple TV · Ready  
> Xbox cleanup blocked: shutdown not confirmed.

`verified_ready` is true only when all required confirmations are satisfied. Best-effort command-only requirements can produce `applied_unverified` without a Problem alert.

### Execution rules

- One current request generation per system.
- One serialized mutating-command lane per physical equipment item.
- Track shared HA control resources to prevent duplicate/conflicting dispatch.
- New requests supersede unsent work.
- Already-submitted work remains tracked.
- Never queue entire obsolete activities for later execution.
- Never claim canceling an outer task cleared an underlying device queue.

Each command records its desired value, generation, transport, submission time, confirmation predicate, deadline, attempts and eventual outcome.

Service completion means submission unless the adapter explicitly documents a stronger guarantee.

### Idempotence

Classify operations as:

- Idempotent set operations.
- Non-idempotent toggles/relative operations.
- One-shot playback/start actions.

Do not automatically repeat toggles when current state is uncertain.

Do not repeatedly restart a radio stream or repeat correct input selections.

### Recovery

Normal mode corrects observable mismatches within its budget.

Preservation mode performs preparation for a new explicit activity but stops ongoing enforcement after that preparation concludes.

Cleanup failure must not prevent unrelated required equipment from becoming ready.

An unavailable optional diagnostic sensor must not block ordinary activity use. An unavailable safety prerequisite must block only its dependent destructive operation.

## 7. Generic activity detection

Manual activity detection is optional.

Detection rules may observe:

- TV source/application.
- Receiver input.
- External matrix output.
- Playback source.
- Another explicitly configured entity state or attribute.

A system with no detection rules remains fully usable through explicit activity selection.

Each rule specifies:

- Observation bindings.
- A simple matching predicate.
- Target activity ID.
- Triggering changes.
- Optional additional evidence.
- Debounce interval.

Do not infer an activity merely because a device is powered.

### Distinguish observation from intent

Maintain a ledger of expected state changes before sending commands.

An observed change matching pending work is an acknowledgement, not a fresh user request. HA context is supplementary evidence; it may be lost during polling.

Default debounce: two seconds.

If simultaneous external changes indicate conflicting activities, publish `manual_routing` and suspend enforcement until a clear request.

Unknown or unmapped source values must not create an invented activity.

Do not select a new activity merely because unused equipment remains active.

## 8. Optional supplies and safe shutdown

### Supply control is independent

No configured supply means:

- No supply commands.
- No supply-related waits.
- No power-meter requirement.
- No missing-supply warning.
- No assertion about whether mains is physically present.

Logical power control must work without electrical measurements.

### Configurable supply policies

Support:

- `unmanaged`: never control this supply.
- `always_on`: preserve supply.
- `standby_on`: keep supply during normal operation; named policies may permit removal.
- `managed`: supply may follow equipment use, subject to declared safety prerequisites.

Supply removal must be explicitly enabled. Default newly added supplies to `standby_on`.

Power meters are optional. Use them only when a configured safety policy requires them.

### Shared supplies

One supply may serve multiple equipment items.

Before removing it:

- Every configured consumer must permit removal.
- Every required shutdown/cooldown condition must hold.
- No consumer may require retained standby or always-on power.
- No active request may depend on that supply.

A supply-off observation invalidates cached readiness for all consumers.

### Shutdown evidence

A television’s logical Off action does not universally require a power meter.

A computer’s mains removal may require an OS acknowledgement.

A projector may require completed cooldown and stable standby.

These are configured capability/safety contracts, not assumptions inferred from equipment labels.

Unknown, unavailable, optimistic Off, low watts or missing HDMI alone must not satisfy a safety condition that requires verified shutdown.

## 9. Optional IR and alternate command paths

IR is one possible HA-exposed transport. ESPHome is supported through configured actions, but not required.

A capability may use different command paths for normal operation and startup.

Example:

1. Restore supply, if controlled.
2. Wait the configured minimum boot delay.
3. Send discrete IR wake.
4. Wait for network availability.
5. Confirm reported power.
6. Continue dependent settings.

Without a controlled outlet, begin at the appropriate remaining step.

Do not simultaneously spam network and IR paths for one unresolved operation.

For IR-only equipment:

- Send a command once.
- Report it as unverified if there is no feedback.
- Disable automatic correction for unobservable properties.
- Keep other observable capabilities usable.

Toggle-only wake is not eligible for automatic retry under uncertain state.

Commissioning IR must not automatically enable more aggressive supply-cut policies.

## 10. Household policies remain outside the core

The reusable integration must not contain knowledge of:

- Guest mode.
- A specific person or phone.
- Sleep schedules.
- Travel destinations.
- Heating.
- Thread border routers.
- A particular home’s privacy rules.

Instead provide generic primitives:

- Named equipment/standby policy profiles.
- Request origins.
- Cancellation by origin.
- Positive guard conditions attached to policy operations.
- Suspension of conflicting automatic enforcement.

Native home automations decide when to submit a policy request.

The coordinator rechecks attached guards immediately before each consequential action, including after long waits. Use explicit positive conditions, such as a configured `vacancy_allowed` entity being On; unknown guard state fails closed.

Restoring standby supplies must not implicitly resume a suspended activity.

This allows one home to protect its Apple TV’s supply and another to power down the same type of device without changing integration code.

## 11. Retry, time and restart defaults

### General defaults

- Ordinary command confirmation: 30 seconds.
- Device startup: 300 seconds.
- Projector warmup/cooldown preset: 600 seconds.
- Overall preparation episode: 900 seconds.
- Three attempts maximum for retry-safe operations.
- Backoff: two and five seconds.
- Three automatic recovery episodes per equipment/property per rolling hour.

All timeouts are configurable per applicable capability. A command-only capability does not wait for nonexistent confirmation.

No retry while an earlier queued command remains genuinely pending.

Network flapping and repeated telemetry do not reset budgets. Explicit requests may start a new bounded episode.

Exhaustion creates a stable blocked reason. Continue observing without issuing more commands on every update.

No automatic plug cycling, integration reload or HA restart in v1.

### Persistence

Persist:

- Requested activity and session overrides.
- Preservation mode.
- Policy suspension.
- Recovery-budget state.
- Bounded significant diagnostic events.

Do not persist an executable command queue.

After startup, gather fresh evidence before resuming a continuing session or performing destructive cleanup.

Physical power loss invalidates cached logical readiness and prior shutdown confirmations.

## 12. Diagnostics and quality requirements

Maintain a ring of 500 significant events per system with debounced persistence.

Record:

- Timestamp and request generation.
- Equipment/capability.
- Desired and observed value.
- Decision and reason.
- Command transport.
- Attempts and deadline.
- Outcome or blocker.

Do not record every unchanged poll.

Provide downloadable, redacted HA diagnostics. Custom coordinator code does not automatically receive native script traces; equivalent troubleshooting visibility is required. [HA diagnostics](https://developers.home-assistant.io/docs/core/integration/diagnostics/)

Use stable reason codes, including:

- `waiting_for_feedback`
- `waiting_for_network`
- `waiting_for_prerequisite`
- `command_applied_unverified`
- `shutdown_unconfirmed`
- `entity_missing`
- `unsupported_capability`
- `ambiguous_activity`
- `recovery_budget_exhausted`
- `recovery_paused_after_restart`
- `request_guard_failed`
- `superseded_command_pending`

Use Repairs for persistent configuration problems. Ordinary startup waits and intentionally command-only equipment are not Repairs issues.

The engine must be independently testable with an injected clock. HA lifecycle tests must verify unload, listener cleanup, migrations, entity renames and service registration.

Ship installation, configuration, troubleshooting, upgrade and rollback documentation. Use a standard custom-integration layout with optional HACS installation metadata.

## 13. Reference configurations and acceptance tests

Provide documented examples and automated fixtures for all of these topologies:

| Topology | Required demonstration |
|---|---|
| TV only | One media player; source/application activities; no sockets, receiver or router |
| TV + receiver + console | Input selection across TV and receiver; no external HDMI matrix |
| Audio only | Speaker and media selection; no display-related requirements |
| IR only | Commands without feedback; useful unverified status and no retry storm |
| Mixed network/IR | IR startup followed by network confirmation |
| Projector + HDMI matrix | Matrix routing, queued display settings, audio readiness, optional supplies and retained standby |

Required behavioral tests:

1. A television with no outlet completes an activity without supply checks.
2. Input selection that implicitly wakes equipment does not require an unsupported power command.
3. Audio-only activity never waits for display capabilities.
4. Missing optional telemetry affects diagnostics only.
5. Command-only equipment receives one action and reports unverified application.
6. Optimistic feedback cannot authorize a protected mains cut.
7. Activity selection preserves unused equipment when preservation is On.
8. Preservation Off performs bounded enforcement and cleanup.
9. Cleanup failure does not block preparation of unrelated equipment.
10. Finish resets preservation and supersedes the old activity atomically.
11. External source changes and explicit requests follow the same coordination path.
12. Coordinator-generated updates do not create request loops.
13. Ambiguous detection suspends enforcement rather than guessing.
14. New requests during warmup supersede unsent work and account for late queued commands.
15. IR startup waits for configured network confirmation.
16. Repeated failures and network flapping exhaust finite budgets.
17. Restart does not wake equipment based solely on saved intent.
18. Guard invalidation during cooldown prevents the later supply cut.
19. Shared supply remains on while any consumer needs it.
20. Entity rename preserves bindings; deletion produces a useful issue.
21. Repeated reconciliation does not restart playback.
22. Observer-only mode performs zero mutating HA actions.
23. Unload/reload does not leave duplicate listeners or controllers.
24. Import/export preserves stable IDs and never actuates equipment.
25. A native-only installation works with none of the optional vendor integrations installed.

Target at least 95% automated coverage, with full configuration-flow coverage and emphasis on failure/recovery behavior.

## 14. Commissioning and migration

Deployment configuration belongs to each installation and is not part of this public specification. The example JSON files use fictional entity IDs and reserved example URLs.

- Establish which entity is the authority for each physical capability. A playback-session entity may not describe the power state of its underlying equipment.
- Verify source names, routing channels, warmup/cooldown behavior and whether commands are queued. An accepted queued command is not confirmed application.
- Classify missing or optimistic feedback explicitly. Retain standby supplies until reliable wake and safe shutdown contracts have been commissioned.
- Validate alternate command paths on the actual hardware before enabling them.
- Preserve any existing script or exported-control identities that other frontends depend on by using compatibility wrappers.
- Replace competing command writers during cutover. A legacy helper can be a one-way projection of the new activity select; do not create bidirectional loops.
- External policy cancellation must invalidate integration requests by origin. Stopping a wrapper does not cancel previously submitted work.
- Keep unrelated home policies in native automations and verify their positive guards before consequential actions.

Capability gaps must remain visible in status and diagnostics. Do not infer physical readiness from a supply being On or invent feedback to make an activity appear ready.

## 15. Delivery and rollout

1. Implement the reusable engine, native HA interface and generic adapters first.
2. Prove the simple TV-only and audio-only fixtures before adding device-specific extensions.
3. Add optional presets and representative example configurations.
4. Run all automated tests.
5. Commission in observer-only mode.
6. Validate real device capabilities before enabling their dependent operations.
7. Cut over with competing writers disabled.
8. Run separately authorized physical tests.
9. Preserve a scoped rollback and document commissioned capabilities and remaining limitations.

Rollback must unload the new coordinator before restoring old command writers.

**Definition of done:** another user can configure a straightforward TV or speaker installation without knowledge of the original installation, while the same engine can coordinate complex equipment through configuration and optional adapters. Missing hardware and unsupported capabilities remain explicit, optional and safely isolated.

## 16. Implementation clarifications

Current implementation behavior is documented in [configuration](docs/configuration.md) and [public actions](docs/actions.md). These clarifications describe the v1 interfaces.

- Observation `valid_when` predicates distinguish meaningful feedback from retained attributes, such as a TV's remembered input while Off. They gate confirmation, while capability/operation `prerequisites` gate dispatch.
- Actions use fixed `target.entity_id` values. Targetless/opaque actions declare existing HA `resource_ids`; hidden or dynamic addressing and area/device targets are rejected. Resource declarations establish ownership, not feedback.
- Detection `trigger_bindings` select an entity state or attribute and optionally scalar `from`/`to` values. Only known fresh-to-fresh changes express intent. Startup cache refreshes/reconnections establish a baseline; disabled rules do not create ambiguity. Debounce considers the rules affected by actual changes, rather than every unused source still active.
- Named policies suspend old activity enforcement. Optional `finish_activity: true` atomically selects Idle, clears overrides and resets preservation before the declared policy requirements run. Status exposes `mode`, `policy_id`, `suspended` and `policy_ready`; completed policy work does not imply a ready playback activity.
- Explicit best-effort action/confirmation failure becomes terminal unverified after bounded eligible retries, with the actual failure reason retained. Required work can continue without claiming verified readiness. Guard and supply protections remain enforced.
- Finish is one-shot per completed shutdown. Session overrides and preservation changes retain unrelated completed one-shot actions; matching submitted work stays tracked across supersession.
- A driver call still running after its deadline reports blocked while retaining its command lane. It is not cancelled or blindly retried; late completion and feedback can resolve it. Intentional supply removal preserves already completed policy shutdown steps while invalidating network readiness for later activity requests.
- Configuration export and import require administrator access. Import validates and enters observer mode without actuating equipment.
