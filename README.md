# Media Activities

![Media Activities](custom_components/media_activities/brand/icon.png)

Coordinate multimedia activities using equipment already available in Home Assistant. A television by itself is enough. Receivers, projectors, HDMI matrices, smart sockets and IR are optional.

Media Activities prepares the selected activity, checks available device feedback, and reports the step that needs attention. It uses existing HA integrations and ordinary entities, actions and dashboard cards. It adds no device-network drivers, separate application, cloud service or custom frontend.

**Initial target: Home Assistant Core 2026.9.4. Version 0.1.0.** This is a custom integration: test updates against your installation and keep a known-good configuration export.

## Install with HACS

[Open this repository in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=grzegor&repository=ha-media-activities&category=integration).

Alternatively, open **HACS → menu → Custom repositories**, add `https://github.com/grzegor/ha-media-activities`, select **Integration**, then download **Media Activities** and restart Home Assistant. This repository is available as a HACS custom repository; it is not included in the default HACS catalog.

After installing, open **Settings → Devices & services → Add integration → Media Activities**.

## Manual installation

1. Copy `custom_components/media_activities` into HA's `/config/custom_components/`.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → Media Activities**.
4. Name the system, add existing equipment, and create an activity. Advanced JSON configuration is available for devices needing alternate commands or feedback mappings.
5. Keep **Observer only** enabled while reviewing generated capabilities and your device observations. Turn it off only when the bindings are ready to actuate.

Release tags use the integration version, for example `v0.1.0`. Keep the system in observer mode when importing a new configuration.

## Everyday controls

Each system supplies Activity, Observed activity, Status, Verified ready, Cleanup status, Problem, Retry and Finish media controls. Use them with native HA cards or ordinary automations.

**Don't turn off devices** is the preservation switch. It defaults to Off and remembers deliberate changes.

| Control | Behavior |
|---|---|
| Preservation Off | Prepare the selected activity, clean up configured unused equipment, and correct observable drift within the recovery budget. |
| Preservation On | Prepare a newly selected activity, preserve unused equipment, then observe subsequent drift without correcting it. |
| Retry | Perform another bounded preparation pass; cleanup remains disabled while preservation is On. |
| Finish media | Select Idle and reset preservation to Off atomically; safely finish managed equipment. |
| Observer only | Observe and report without issuing device actions. |

The switch does not skip input or picture-mode preparation for a newly selected activity. Leaving a paused console running is compatible with another activity becoming ready. Cleanup failure is reported separately and does not block unrelated required equipment.

A submitted command is not necessarily a confirmed setting. Reported feedback permits **Ready**. A working IR-only device can instead show **Applied, unverified**. Missing feedback from a device configured to report it remains a confirmation problem.

After a restart, saved intent alone does not turn equipment on. Automatic continuation requires fresh observations establishing an existing session; otherwise use an activity button or Retry to resume.

## Configuration and support

- [Configuration and examples](docs/configuration.md)
- [Adapters, feedback and IR](docs/adapters.md)
- [Troubleshooting, diagnostics and rollback](docs/troubleshooting.md)
- [Public actions and completion events](docs/actions.md)
- [Development and testing](docs/development.md)
- [Version 0.1.0 verification](docs/verification.md)
- [Integration specification and implementation clarifications](spec.md)

Six importable [examples](examples/) illustrate TV only, receiver with console, audio only, IR only, mixed IR/network control and an anonymized projector installation. Every example starts in observer mode. Replace example entity IDs with your actual entities and verify the generated actions and feedback.

## Deliberate limits

Media Activities does not know who lives in a home, when they sleep or where they travel. Native home automations apply generic named policies. It does not infer a device's physical power from an unrelated Cast session, missing HDMI, or a socket reading alone. Multiple systems may not independently own the same controlled resource.

The integration cannot make an unreliable device integration report facts it does not expose. Presets are editable starting points, and their feedback must be checked on the actual equipment.

## License

MIT. See [LICENSE](LICENSE).
