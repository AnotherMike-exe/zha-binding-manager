# ZHA Binding Manager

> Query, edit, validate, and apply Zigbee binding & group configs against Home Assistant / ZHA.

Bulk Zigbee binding changes are tedious and risky in the Home Assistant UI. This
CLI pulls the whole ZHA binding/group state into reviewable files, lets you (or
its `rebind` helper) express a change, and applies it as a typed, dry-runnable,
resumable plan — so "make every switch in an area drive its light group" is one
reviewed command instead of dozens of manual edits.

---

## Quick Start

```bash
cp config.example.json config.json      # then fill in ha_url + a long-lived token

uv run zha-manager pull                 # snapshot the network
uv run zha-manager rebind "Kitchen"     # generate a plan (review it)
uv run zha-manager apply --dry-run      # see exact API calls, change nothing
uv run zha-manager apply                # do it (prompts; -y to skip)
```

Prerequisites: Python ≥ 3.10, [`uv`](https://docs.astral.sh/uv/), and the
**`zha_toolkit`** custom integration installed in Home Assistant. Generate the
token in HA under Profile → Security → Long-lived access tokens.

Without `uv`: `pip install -e .` then run `zha-manager …`.

## Documentation

- [Usage](docs/usage.md) — every command, flags, and recipes
- [Architecture](docs/ARCHITECTURE.md) — design, data flow, and the ZHA/zha_toolkit findings
- [CLAUDE.md](CLAUDE.md) — Claude Code project memory
- [Dev Setup](docs/DEV-SETUP.md) · [Quick Reference](docs/QUICK-REFERENCE.md) — house standards

## Tech Stack

- **Language/Runtime**: Python ≥ 3.10
- **Dependency**: `websockets` (sole transport to Home Assistant)
- **Interfaces**: HA WebSocket API + the `zha_toolkit` integration
- **Data**: local JSON snapshots/plans (no database, no server, no Docker)

## Tests

```bash
uv run --extra dev pytest
```

## License

MIT

---

**Repository**: https://github.com/AnotherMike-exe/zha-binding-manager · **Maintainer**: Plum Solutions
