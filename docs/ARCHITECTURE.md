# Architecture Overview

A living reference for how ZHA Binding Manager is built. Update as the code evolves.

## 1. Project Structure

```
zha-binding-manager/
├── src/zha_binding_manager/
│   ├── __init__.py           # main() + __version__
│   ├── __main__.py           # python -m zha_binding_manager
│   └── manager.py            # the whole tool (one module by design)
├── docs/                     # ARCHITECTURE.md, usage.md, CLAUDE.md, guides
├── tests/unit/test_diff.py   # diff/resolve engine tests (transport-independent)
├── config.example.json       # template for config.json (ha_url + token)
├── pyproject.toml            # packaging, entry point (zha-manager), pytest config
├── pulls/                    # immutable timestamped snapshots (runtime, gitignored)
├── plans/                    # saved plans + *_resume_* files (runtime, gitignored)
└── zha_bindings_edit.json    # working edit file (runtime, gitignored)
```

`manager.py` is intentionally a **single module**: the tool is small, the stages
are tightly coupled through a couple of plain-dict shapes (the snapshot and the
plan), and one file keeps the whole pipeline greppable. Splitting into a package
of modules would add import ceremony without reducing real complexity.

## 2. High-Level System Diagram

```
                        ┌─────────────────────────── Home Assistant ───────────────────────────┐
 [ CLI: zha-manager ]   │                                                                       │
        │               │   WebSocket API                         zha_toolkit integration       │
        ▼               │   • zha/devices, zha/groups             • binds_get   (read table)    │
 pull ─ inspect ─ rebind│   • config/*_registry/list              • bind_group / binds_remove_  │
   │      │       │      │   • zha/group/members/* (add/remove)      all / bind_ieee (mutations) │
   │      │       │      │            ▲                                     ▲                     │
   ▼      ▼       ▼      │            │  ws (auth + json commands,          │  call_service       │
 validate ─ apply ───────┼────────────┴─ return_response) ─────────────────┘  return_response    │
   │                     └───────────────────────────────────────────────────────────────────────┘
   ▼
 pulls/  plans/  zha_bindings_edit.json      (local files; source of truth for diffing)
```

The tool never touches a REST API — modern HA does not expose ZHA over REST. A
single `WsClient` is the only transport; `zha_toolkit` services are invoked over
that same socket via `call_service` with `return_response: true`.

## 3. Core Components (all in `manager.py`)

### 3.1 `WsClient`
HA WebSocket client. Handles the auth handshake, `command()` (send a typed
command, await the matching `result`), `call_service(..., return_response=True)`,
and a `toolkit(ieee, command, **params)` convenience for zha_toolkit calls.

### 3.2 Pull (`pull_snapshot`, `parse_binds_response`)
Reads `zha/devices`, `zha/groups`, and the area registry, then calls
`binds_get` per available device. Normalises each raw binding-table entry:
`DstAddress.addrmode == 1` → group binding (`group_id` = `.nwk`);
`addrmode == 3` → device binding (`.ieee` is a **little-endian** byte list →
reversed into a colon IEEE). Writes an immutable `pulls/…json` snapshot and the
editable `zha_bindings_edit.json` (with `_meta.pull_file` back-pointer).

### 3.3 Diff / Validate (`diff`, `resolve_cluster`, `resolve_destination`, `Plan`)
Compares the edit file against the pull it references and produces a typed
`Plan` of actions (`bind`, `unbind`, `add/remove_group_member`, `create_group`)
plus `errors` / `warnings` / `infos`. Resolves `cluster_name`↔`cluster_id`
(conflict = error), resolves `destination_name`→id, and applies the
**self-heal** for the destination-blind unbind (§ Gotchas). This layer is pure
data-in/data-out and is what `tests/unit/test_diff.py` exercises.

### 3.4 Confirm (`print_plan`, `render_action`)
Renders the plan for human review — errors block apply; warnings/infos don't.

### 3.5 Apply (`run_apply`, `_run_apply_ws`, `execute_action`)
Executes actions in a **fixed order** regardless of edit-file order:
`remove_group_member → unbind → create_group → add_group_member → bind`.
Per-action progress, `tries=3` on toolkit calls to ride out transient mesh
timeouts, resume-file on partial failure, and an automatic post-apply `pull`.

### 3.6 Rebind helper (`cmd_rebind`, `resolve_switch_group`)
Generates an edit file that wipes device control-cluster bindings and binds each
switch's OnOff/LevelControl onto its area light group. Target group is resolved
per switch by the **longest group name that prefixes the device name at a word
boundary**, so multi-zone areas route correctly with no config. Fan switches are
excluded by default.

## 4. Data Stores

Plain JSON files on disk — no database.

- **`pulls/zha_bindings_<ts>.json`** — immutable network snapshots; the source of
  truth for diffing. Never modified after write.
- **`plans/zha_plan_<ts>.json`** and **`plans/zha_plan_resume_<ts>.json`** — saved
  action plans and partial-failure resume points.
- **`zha_bindings_edit.json`** — the working copy the user (or `rebind`) edits;
  overwritten on every fresh pull.

## 5. External Integrations

- **Home Assistant WebSocket API** — inventory, group registry, area/entity
  registries, group membership mutations. Auth via a long-lived access token.
- **`zha_toolkit`** custom integration — the per-device binding table and all
  bind/unbind primitives. Called over the same WS via `call_service`.

## 6. Deployment & Infrastructure

None. It is a local CLI run against a HA instance on the LAN. Distributed as a
Python package (`pyproject.toml`, entry point `zha-manager`); typically run with
`uv run zha-manager …`.

## 7. Security Considerations

- **Auth**: a HA long-lived access token in `config.json` (gitignored). The token
  grants broad HA access — treat the file as a secret.
- No data leaves the LAN; the tool talks only to the configured `ha_url`.

## 8. Development & Testing

- **Setup**: `cp config.example.json config.json` + fill `ha_url`/`token`.
- **Tests**: `uv run --extra dev pytest` — unit tests over the diff/resolve
  engine (bind/unbind detection, cluster/destination resolution, validation
  errors, self-heal ordering, rebind group resolution).
- No integration tests hit a live network (that would require a real mesh); the
  live behaviours are documented in § 9 and CLAUDE.md.

## 9. Gotchas / Known Constraints (verified against zha_toolkit v1.1.38 + zigpy 1.5.1)

- **Service-data keys are `cluster` / `endpoint`**, not `cluster_id`/`endpoint_id`
  (silently ignored → would hit all clusters).
- **`unbind_coordinator` is broken** (`data = app.ieee`); device unbinds use
  `binds_remove_all` with the coordinator IEEE as `command_data`.
- **`binds_remove_all` is destination-blind** — removes every binding on an
  (endpoint, cluster). The diff self-heals by re-binding co-located survivors;
  unbinds always run before binds so the re-bind lands last.
- **`unbind_group`** has no per-cluster filter and is intentionally blocked.
- Success is judged by the `errors` list, not the unreliable `success` flag.

## 10. Project Identification

- **Project Name**: ZHA Binding Manager
- **Primary Contact/Team**: Plum Solutions
- **Repository URL**: https://github.com/AnotherMike-exe/zha-binding-manager

## 11. Glossary

- **ZHA** — Zigbee Home Automation, Home Assistant's built-in Zigbee integration.
- **Binding** — a Zigbee device-level link so a source endpoint/cluster sends
  commands directly to a destination (device or group) without routing through HA.
- **Group** — a Zigbee multicast group; group members react to group commands.
- **zha_toolkit** — HA custom integration exposing low-level ZDO operations
  (read/modify the binding table, bind to groups, etc.).
- **IEEE** — a device's 64-bit address (`xx:xx:xx:xx:xx:xx:xx:xx`).
- **Endpoint (EP)** — a logical sub-unit of a device; Inovelli switches use EP1
  (main paddle) plus EP2/EP3.
- **addrmode** — zigpy binding destination mode: 1 = group, 3 = device/IEEE.
