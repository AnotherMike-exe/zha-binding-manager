# CLAUDE.md — ZHA Binding Manager

> **Purpose**: Project memory for Claude Code. Rules, workflows, and conventions Claude should follow when working on this codebase.

## Project Overview

**ZHA Binding Manager** is a single-purpose Python CLI that queries, edits, validates, and applies Zigbee **binding** and **group** configurations against a Home Assistant / ZHA network. It exists to make bulk binding changes (e.g. "make every wall switch in an area drive its light group directly") safe, reviewable, and repeatable instead of hand-editing bindings device by device in the HA UI.

### Key Features
- **Pull** full ZHA state (devices, groups, areas, per-device binding tables) into an immutable snapshot + an editable working file.
- **Inspect** the latest snapshot read-only (areas, groups, per-device bindings) with no network calls.
- **Rebind** helper: wipe device→device control bindings and bind switches' OnOff/LevelControl onto each area's light group, with automatic group resolution and fan-switch exclusion.
- **Validate → confirm → apply** pipeline with a typed action plan, dry-run, partial-failure resume, and an automatic post-apply snapshot.

### Project Context
- **Stage**: Working tool, actively used against a live network.
- **Team Size**: Solo.
- **Priority Focus**: Safety and correctness of live network mutations.

---

## Technology Stack

- **Language/Runtime**: Python ≥ 3.10 (single package, stdlib + one dep).
- **Dependency**: `websockets` — the sole transport to Home Assistant.
- **External systems**: Home Assistant WebSocket API (`zha/devices`, `zha/groups`, `zha/group/*`, registries) and the **`zha_toolkit`** custom integration (`binds_get`, `bind_group`, `binds_remove_all`, …) called over WS with `return_response`.
- **Packaging**: `pyproject.toml` (hatchling), console entry point `zha-manager`.
- **Tests**: `pytest` (unit tests over the diff/resolve engine).

There is **no REST API, no database, no web frontend, and no Docker** — ignore those sections of the house templates for this project.

---

## Project Structure

```
├── src/zha_binding_manager/
│   ├── __init__.py            # exposes main() + __version__
│   ├── __main__.py            # `python -m zha_binding_manager`
│   └── manager.py             # the entire tool (CLI, WS client, diff, apply)
├── docs/                      # all docs (this file is symlinked to root CLAUDE.md)
│   ├── ARCHITECTURE.md        # design + the ZHA/zha_toolkit findings
│   ├── usage.md               # command + recipe reference
│   ├── DEV-SETUP.md · QUICK-REFERENCE.md
├── tests/unit/test_diff.py    # diff/resolve engine tests
├── config.example.json        # copy to config.json (ha_url + token)
├── pulls/  plans/             # runtime snapshots + plans (gitignored)
└── zha_bindings_edit.json     # working edit file (gitignored, regenerated on pull)
```

`config.json`, `pulls/`, `plans/`, and the edit file are resolved at the **project root** (found by walking up for `pyproject.toml`/`config.json`), not next to the module — so runtime data lives at the repo top level regardless of how the tool is launched.

---

## Naming Convention

**Python `snake_case`** for modules, functions, and variables; `UPPER_SNAKE_CASE` for constants. (This deviates from the house PascalCase standard because PascalCase Python fights imports, PEP 8, and tooling — per the new-project skill's ecosystem rule.)

---

## Development Workflow

- **Run**: `uv run zha-manager <cmd>` (websockets is a declared dep). Also `python -m zha_binding_manager <cmd>`.
- **Test**: `uv run --extra dev pytest`.
- **Git**: `git pull --rebase` for linear history; atomic, well-described commits.
- The tool mutates a **live Zigbee network**. Prefer `apply --dry-run` for anything new, and rely on `pull → rebind → validate → apply` so every change is reviewed as a typed plan first.

---

## Core Architecture

`manager.py` is organised as a pipeline (details in [ARCHITECTURE.md](ARCHITECTURE.md)):
1. **WsClient** — HA WebSocket client (auth, `command`, `call_service` with `return_response`, `toolkit` helper).
2. **Pull** — `zha/devices` + `zha/groups` + area registry + per-device `binds_get`; normalises the binding table (addrmode 1 = group, addrmode 3 = device with little-endian IEEE).
3. **Diff/validate** — compares edit vs pull → typed `Plan` (bind / unbind / *_group_member / create_group) with errors/warnings/infos. Transport-independent; this is what the tests cover.
4. **Apply** — fixed execution order, per-action progress, `binds_remove_all`/`bind_group` via zha_toolkit, resume on partial failure, auto post-apply pull.

---

## Known Issues & Gotchas (READ before touching apply)

1. **zha_toolkit service_data keys are `cluster` and `endpoint`** (user-facing), NOT `cluster_id`/`endpoint_id` — the latter are silently ignored, which makes bind/unbind hit *all* clusters. Pass `command`, `command_data`, `cluster`, `endpoint`, `tries`.
2. **`unbind_coordinator` is broken** on current zigpy (`data = app.ieee` no longer exists). Device unbinds go through **`binds_remove_all`** with the coordinator IEEE as `command_data`.
3. **`binds_remove_all` is destination-blind** in the installed toolkit version: it removes *every* binding on an (endpoint, cluster), not just the one to the given destination. The diff **self-heals** this by re-emitting any co-located binding as a `bind` (unbinds always run before binds). Don't "optimise" that away.
4. **`unbind_group` has no per-cluster filter** — it's deliberately blocked in `execute_action` (would clobber all group binds).
5. **Response success signal** = the `errors` list. The `success` boolean is unreliable (binds_remove_all leaves it false even on success).
6. Fan switches (`name` contains "fan") are excluded from `rebind` by default — they control fans, not the light group.

---

## Common Tasks

- **Migrate an area's switches to its light group**: `pull` → `rebind "<Area>"` → review the plan → `apply`. Multi-zone areas (e.g. Living Room Aux vs TV) auto-resolve per switch by longest group-name prefix.
- **Look before editing**: `inspect` (areas+groups) or `inspect "<Area>"` (per-device bindings).
- **Recover a half-applied plan**: `apply --resume`.

---

## Quick Reference

```bash
uv run zha-manager pull                 # snapshot + edit file
uv run zha-manager inspect "Kitchen"    # read-only detail
uv run zha-manager rebind "Kitchen"     # generate the plan
uv run zha-manager apply --dry-run      # show exact API calls, change nothing
uv run zha-manager apply -y             # apply without the prompt
uv run --extra dev pytest               # run tests
```

- Config: `config.json` (repo root) — `ha_url`, `token`, `editor`.
- Snapshots: `pulls/` · Plans + resume files: `plans/` · Working edit: `zha_bindings_edit.json`.
- Docs: `docs/` (this file symlinked to root `CLAUDE.md`). Reference material: `_resources/` (not in git).
