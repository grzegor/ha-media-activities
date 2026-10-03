# Public actions

All actions use `config_entry_id` to identify a system. Equipment, capability, activity and policy references use stable IDs, not display names. Actions accept an optional response and return promptly after submitting work rather than blocking through a long warmup.

| Action | Additional fields |
|---|---|
| `media_activities.request_activity` | `activity_id`; optional `origin` (default `user`) and positive `guards` |
| `media_activities.finish` | None |
| `media_activities.retry` | None |
| `media_activities.set_device_setting` | `equipment_id`, `capability_id`, typed `value` |
| `media_activities.cancel_requests` | `origin` |
| `media_activities.apply_policy` | `policy_id`; optional `origin` (default `policy`) |
| `media_activities.export_configuration` | None |
| `media_activities.import_configuration` | `configuration`: a versioned JSON object |

Configuration export and import require an administrator. The other actions are normal HA actions; their equipment and safety constraints are enforced by the coordinator.

For example:

```yaml
action: media_activities.request_activity
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  activity_id: watch_tv
  origin: dashboard
response_variable: media_request
```

Accepted requests return `accepted`, `request_id` and `observer_only`. Acceptance means the request is known to the coordinator; it does not mean the physical activity has finished.

When a request reaches a terminal result and cleanup is no longer pending, the integration fires `media_activities_request_completed` with `config_entry_id`, `request_id`, `result`, `cleanup_status` and concise `blockers`. Result names follow the system phase, such as `ready`, `applied_unverified` or `blocked`. Associate results with the returned request ID; a later activity can supersede unsent work from an earlier one.

`set_device_setting` serializes commands through the equipment's command lane. Its setting becomes an override for the current activity session. This avoids having ordinary automatic enforcement immediately undo a user's selected picture mode.

Overrides are cleared by selecting a different activity or by Finish media. Updating one setting retains unrelated completed work: changing volume or picture mode does not replay a one-shot stream-start or wake action. A matching command already submitted remains tracked and can satisfy the new request. Turning preservation Off likewise reconciles the session without replaying unrelated successful one-shot work. Explicit Retry begins a new bounded episode.

Finish media selects Idle and resets preservation Off atomically. Each completed shutdown is one-shot: later manually waking that equipment is not continuously undone by the completed Finish request. Configured retained supplies and shutdown safeguards still apply. `request_activity` with `activity_id: idle` performs Finish while retaining any supplied positive guards, allowing a guarded household automation to end a session safely.

A named policy can prepare standby supplies or finish selected equipment. Household automations decide when it applies and provide positive guards. The coordinator rechecks guards before consequential actions, including after waits; an unknown safety guard does not count as permission. Policy restoration of standby supplies does not itself resume playback.

Policies suspend previous activity enforcement. Configuring `finish_activity: true` also selects Idle, clears overrides, and resets preservation before the policy requirements run. Without that flag, the old activity remains selected but suspended. Inspect Status attributes `mode`, `policy_id`, `suspended`, and `policy_ready` to distinguish a completed policy from a ready activity. A positive `policy_ready` can include deliberately best-effort results and is not physical shutdown evidence.

An explicitly best-effort action that errors or never receives confirmation finishes unverified after its eligible bounded retries. Its failure reason remains visible, required work can continue, and Verified ready remains Off. Required failures remain blocked. A guard rejection is never reinterpreted as permission to execute the guarded action.

If a submitted HA action remains running past its deadline, the system reports **Blocked** with `command_still_pending`. The command remains tracked, its equipment lane stays reserved, and the coordinator neither pretends cancellation succeeded nor dispatches another command into that lane. Late completion and suitable feedback can resolve the block. Unsent work remains supersedable by a new activity.

Configuration import validates first and enters observer mode without issuing physical commands. Export/import require admin access even when requesting an action response. Treat configuration export as a structural backup; it is not a backup of device integration credentials or hardware state.
