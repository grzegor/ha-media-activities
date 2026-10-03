# Adapters and feedback

Adapters generate editable capability bindings. Device protocols remain with existing HA integrations. A preset is never required to use Media Activities, and none of the presets creates another integration or assumes a smart socket.

## Generic adapters

The media-player adapter reads the entity's `supported_features`. It offers only advertised power, source, sound-mode, playback, volume, mute and play-media actions. With no advertised actions it provides an observation-only state. A feature bit establishes that an action is offered, not that a particular integration's feedback is reliable.

Advertised logical Off becomes the default cleanup action for physical equipment. Source and sound-mode readback are valid only while its media-player state is active; remembered settings in standby cannot report an input-only activity ready prematurely.

Use role `physical` (or an appropriate equipment presentation role) for an entity representing the device. Use `playback` for Cast or another playback-session entity: its idle/off states cannot establish the physical equipment's power.

Select, switch, number and button adapters use their corresponding native actions. Buttons are command-only unless independent feedback is deliberately added. Number bounds and select options can be generated from the entity attributes.

Advanced action bindings support any registered HA action with fixed target and JSON data, optionally inserting the requested value into one data field. This includes ESPHome actions and existing user-owned scripts. A long-running script remains a submitted command until its action call completes; it does not exempt the resulting state from confirmation.

## Optional presets

| Preset | Behavior and limitation |
|---|---|
| Epson Projector Link | Logical power and observed `color_mode`, plus raw power-state diagnostics. Warmup/cooldown remain distinct from confirmed On/Off. Mode actions may queue inside the Epson integration; canceling an activity cannot flush that queue. |
| Sony Songpal | Physical power and supported inputs from the native Sony entity. Input readiness must be declared for the actual equipment. Cast and MA playback belong in separate capabilities of this equipment. |
| Apple TV | Native remote `wakeup` and `suspend` actions. The remote entity's state is not power feedback. Power is command-only unless the user supplies a suitable independent observation. |
| HDFury | Parameterized, independent output-route selects. No input numbering or source mapping is assumed. Native integration polling can delay readback; a source may not be visible on every unselected physical input. |
| Music Assistant | A playback session with a one-shot `play_media` operation. Starting a stream is not repeatedly retried, and session state is never physical-power proof. |

Preset functions accept explicit entity IDs; each supply is supplied separately and defaults to retained standby. No preset grants power-cut permission automatically.

## IR bootstrap with network confirmation

IR is an alternate command transport. A network-controlled device can remain configured with `reported` feedback while using IR to get through cold boot:

1. Restore an optional supply and wait the device's minimum boot interval.
2. When network control is unavailable, send the configured discrete IR wake command.
3. Wait for network feedback confirming power and readiness.
4. Continue dependent settings; do not restart the whole activity.

The mixed example selects its IR path with an explicit unavailable-state predicate. The ordinary network action applies when its interface is available. Only one eligible path is dispatched for an unresolved operation. IR transmission alone does not convert reported feedback to success; the network observation does.

For IR-only equipment, use `command_only` and `best_effort`. The integration sends once and reports Applied, unverified. A toggle without reliable state is not eligible for automatic repeated correction. Commissioning IR does not implicitly enable more aggressive socket policies.

## Freshness and manual control

HA's state timestamps may represent a cached integration update. Confirm each device's actual reporting behavior during commissioning. Missing or stale reported feedback blocks confirmation; it must not silently turn into an optimistic success.

Preservation On prepares a new requested activity and then relinquishes ongoing correction. Preservation Off allows bounded correction. Commands issued through `set_device_setting` become session overrides so that the activity does not immediately undo them. Select a different activity or Finish media to clear those overrides.

The controller tracks commands already submitted even if a new request supersedes the activity. Late feedback from an old command can then be reconciled with the newest target. It never treats canceling a Python task as proof that an underlying driver or IR receiver canceled work.
