# Changelog

All notable changes to ZHA Binding Manager.

## [0.2.0]

### Added
- `inspect` command — read-only view of the latest pull (areas + groups, or
  per-area device/binding detail), no network calls.
- `rebind` command — one-shot generator that wipes device control-cluster
  bindings and binds switches' OnOff/LevelControl onto each area's light group on
  EP1/EP2/EP3. Auto-resolves the target group per switch by longest group-name
  prefix (handles multi-zone areas), auto-excludes fan switches.
- `apply -y/--yes` to skip the confirmation prompt.
- Project packaging: `pyproject.toml`, `zha-manager` entry point, `python -m
  zha_binding_manager`, and a `pytest` suite over the diff/resolve engine.
- Documentation set: README, `docs/ARCHITECTURE.md`, `docs/usage.md`, and
  `docs/CLAUDE.md`. (The original REST-based design spec is kept out of git as
  historical reference in `_resources/Notes/original-spec.md`.)

### Changed
- **Restructured** from a single root script into a `src/zha_binding_manager/`
  package. Runtime data (`config.json`, `pulls/`, `plans/`, edit file) now anchors
  at the project root via marker discovery, so it stays put regardless of launch
  location.
- Diff engine now **self-heals** the destination-blind `binds_remove_all` unbind:
  any co-located binding that would be collateral-removed is re-emitted as a
  `bind` (ordered after unbinds).
- Pull progress only animates on a TTY (clean piped output).

### Fixed
- zha_toolkit calls now use the correct `cluster`/`endpoint` service-data keys
  (the `*_id` variants were silently ignored → all-cluster bind/unbind).
- Device unbinds use `binds_remove_all` (with the coordinator IEEE) because
  `unbind_coordinator` is broken on current zigpy.
- Success is judged from the `errors` list, not the unreliable `success` flag.
- Transient mesh timeouts retried via `tries=3` on toolkit calls.

## [0.1.0]

### Added
- Initial `pull → validate → confirm → apply` pipeline built to the design spec,
  migrated from the spec's (non-existent) REST endpoints to the Home Assistant
  WebSocket API + `zha_toolkit` services after verifying the live network.
- Live migrations completed for Kitchen, Pantry, Downstairs Bathroom, Living
  Room, Stairs, and Laundry Room (switches rebound to area light groups).
