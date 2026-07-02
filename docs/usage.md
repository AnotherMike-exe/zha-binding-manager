# Usage

All commands are subcommands of `zha-manager`. Examples use `uv run` (which
installs the project + its `websockets` dependency automatically); if you've
installed the package another way, drop the `uv run` prefix.

## Pipeline

```
pull → inspect/rebind or hand-edit → validate → confirm → apply
```

## Commands

### `pull` — snapshot the network
```bash
uv run zha-manager pull            # write pulls/<ts>.json + zha_bindings_edit.json
uv run zha-manager pull --edit     # ...and open the edit file in $EDITOR
```
Pull files are immutable and are the source of truth for diffing. The edit file
is overwritten on every pull.

### `inspect` — read-only view (no network)
```bash
uv run zha-manager inspect                 # list all areas + all groups
uv run zha-manager inspect "Kitchen"       # per-device binding detail for an area
uv run zha-manager inspect "Stairs" "Pantry"
```
Reads the most recent pull snapshot. Switches, lights, fan switches, and offline
devices are tagged.

### `rebind` — move switch control onto the area's light group
```bash
uv run zha-manager rebind "Kitchen"
uv run zha-manager rebind "Living Room" "Stairs"       # multiple areas
uv run zha-manager rebind "Pantry" --group "Pantry"    # force one group
uv run zha-manager rebind "Garage" --include-fans      # don't skip fan switches
uv run zha-manager rebind "Office" --exclude "closet"  # skip extra devices
```
For each non-excluded device in the area it removes the OnOff/LevelControl/
ColorControl **device** bindings, and for each switch it binds OnOff+LevelControl
to the area's light group on EP1/EP2/EP3. It then writes the edit file and prints
the resulting plan.

- **Group auto-resolution**: the target group is the group whose name is the
  longest prefix of the device name at a word boundary. A single HA area with
  several zones (e.g. `Living Room Aux` and `Living Room TV`) therefore routes
  each switch to the correct group automatically.
- **Fan switches** (name contains "fan") are skipped by default — they control a
  fan, not the lights. `--include-fans` overrides.
- Warns if a target group has no light members (a switch that would control
  nothing), and if a co-located binding will be re-bound (see selective-unbind
  note below).

### `validate` — diff the edit file against the pull
```bash
uv run zha-manager validate
uv run zha-manager validate --apply    # apply immediately if there are no errors
```
Prints the plan (errors block apply; warnings/infos don't) and saves it to
`plans/`.

### `apply` — execute the plan
```bash
uv run zha-manager apply               # prompt for confirmation
uv run zha-manager apply -y            # skip the prompt
uv run zha-manager apply --dry-run     # print every action + exact API call, change nothing
uv run zha-manager apply --resume      # continue after a partial failure
```
Actions always run in a fixed order (`remove_group_member → unbind →
create_group → add_group_member → bind`). On success a fresh post-apply snapshot
is taken automatically. On failure, the remaining actions are written to
`plans/zha_plan_resume_*.json` for `--resume`.

## Hand-editing `zha_bindings_edit.json`

| Intent | Action |
|---|---|
| Remove a binding | Delete the binding object from a device's `bindings` array |
| Add a binding | Add a binding object to the `bindings` array |
| Remove a group member | Delete the member from a group's `members` |
| Add a group member | Add a member with `"hardware_confirmed": false` |
| Create a new group | Add a new group object (created on apply) |

Bindings resolve `destination_name`→id and `cluster_name`→id automatically. If a
cluster name and id are both given and conflict, validation errors out.

## Recipe: migrate an area to group control

```bash
uv run zha-manager pull
uv run zha-manager rebind "Kitchen"     # review the printed plan
uv run zha-manager apply                 # or apply --dry-run first
```
Result: the area's wall switches drive its light group directly over Zigbee
(instant local control), individual-light and switch↔switch bindings are removed,
and metering/occupancy/mfg reporting bindings to the coordinator are preserved.

## Note on selective unbinds

`zha_toolkit.unbind_coordinator` is broken on current zigpy, so device unbinds go
through `binds_remove_all`, which removes **every** binding on an (endpoint,
cluster) regardless of destination. The diff engine detects when that would drop
a binding you intend to keep and automatically re-binds it (unbinds always run
before binds), so no group binding is lost. `rebind`/`validate` surface this as a
warning.
