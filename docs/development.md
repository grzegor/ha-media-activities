# Development

The initial compatibility target is HA Core 2026.9.4, using Python 3.14. The integration has no separate runtime or external database. Existing HA integrations perform device communications.

Run the suite in the isolated Linux container from the repository root:

```sh
docker build -f Dockerfile.test -t media-activities-test:2026.9.4 .
docker run --rm --network none -v "$PWD:/workspace" media-activities-test:2026.9.4 python -m pytest --cov --cov-branch --cov-report=term-missing
docker run --rm --network none -v "$PWD:/workspace" media-activities-test:2026.9.4 python -m ruff check .
```

The image pins HA and development packages. Building needs package-network access; test runs have networking disabled. The test container uses HA's Linux environment. No device controls are needed to run this suite.

The adapter factories are pure functions generating JSON-compatible configuration. They never inspect HA state or call an action. The schema validator returns a detached normalized configuration. The state engine should be exercised with synthetic observations and time so recovery paths do not depend on real equipment.

Tests should prove user behavior and failure boundaries: no accidental supply requirement, no physical-power inference from playback, honest IR-only operation, late acknowledgements, preservation during transitions, conflicting manual routes, stale feedback, shutdown guards and reload/restart recovery. Include configuration-flow, runtime and native entity tests in the same suite. Aim for at least 95% integration coverage, but coverage is a measure to review rather than evidence that real devices are commissioned.

Keep HA-specific lifecycle and dispatch work separate from device-agnostic decisions. Use documented entity/state/action interfaces. Do not reach into private objects belonging to Epson, HDFury, Sony or other integrations.

Presets generate editable configuration. Add a preset only for demonstrated device semantics; do not turn each household preference into a new adapter. A new activity using existing capabilities should need configuration alone.

Version configuration migrations deliberately. Before a release, validate all six example configurations, run the complete suite and lint checks, and exercise the new version in observer mode against a representative installation. Keep a known-good export and release available for rollback.
