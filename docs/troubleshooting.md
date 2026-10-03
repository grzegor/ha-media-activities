# Troubleshooting and rollback

Read the system's **Status**, **Cleanup status** and **Problem** entities first. Preparation and cleanup are separate: a blocked console shutdown need not stop television playback.

| Symptom | What to check |
|---|---|
| Observing; no actions | Observer only is enabled. Review bindings before disabling it. |
| Recovery paused after restart | Fresh observations have not established a continuing session. Choose an activity or press Retry when ready. |
| Applied, unverified | One or more deliberate best-effort/command-only requirements lack confirmation. This is not automatically a fault. |
| Waiting for reported state | Inspect the bound state/attribute in Developer tools. Check the value map, device readiness and the underlying integration. |
| Cleanup blocked | Read the specific cleanup guard or shutdown prerequisite. Do not substitute a socket reading for required device feedback. |
| Manual routing / ambiguous activity | Multiple detection rules disagree or routing is unmapped. Select the intended activity. |
| Devices remain on | Preservation may be enabled, cleanup may be unconfigured, or a retained-supply policy may apply. |
| A source change gets corrected | Preservation is Off and it was not a recognized new activity or session override. Use a configured detection rule or explicit activity selection. |
| Cannot add another system | A controlled resource is already owned. Configure all interfaces of one physical device in one equipment item. |

## Diagnostics

Download diagnostics from **Settings → Devices & services → Media Activities → integration menu → Download diagnostics**. The bounded event history records requested and reported states, attempted commands, transport, deadlines and blocker reasons. Keep full event history out of dashboard entity attributes.

Diagnostics are intended to redact sensitive fields, but example or user-provided action data can contain private values. Review an export before publishing it. No credentials should be required for ordinary entity-based configuration.

Use the integration's native debug logging only while reproducing a problem. There is no need to enable debug logging for every device integration unless the evidence points there. The decisive question is whether the expected command was submitted and whether the configured observation subsequently confirmed it.

Retry creates a new bounded pass. It does not promise to cancel a device's internal command queue or overcome missing physical feedback. Recovery budgets prevent repeated wake commands or infinite corrections; a persistent failure needs attention to its specific cause.

## Commissioning

Start in observer mode and inspect actual feedback while using the device's ordinary controls. Exercise one capability at a time before testing whole activities. Check cold boot, warmup, shutdown and manual routing; then test rapid activity changes and temporary network loss. Preserve normal standby supply until the chosen wake path and safe shutdown have been established.

For a device lacking suitable shutdown feedback, omit destructive supply removal. Logical Off may still be supported. Ordinary equipment with no socket should work without supply diagnostics or waiting for an imaginary outlet.

## Disable or roll back

1. Enable Observer only to stop new automatic actuation. Already submitted physical commands may still complete.
2. Export the current configuration and retain relevant diagnostics.
3. Disable the integration if needed. Underlying device integrations and their native controls remain available.
4. Restore a known-good integration version and configuration, then restart HA if replacing code.
5. Resume in observer mode and verify current device state before enabling actuation.

Do not edit HA's `.storage` files while HA is running. Remove the integration through its native UI before uninstalling its files. Retired household automations should be re-enabled only deliberately; running two coordinators against the same equipment recreates conflicting control.
