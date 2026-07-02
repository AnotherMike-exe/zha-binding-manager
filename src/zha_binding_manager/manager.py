#!/usr/bin/env python3
"""ZHA Binding Manager.

A single script for querying, editing, validating, and applying Zigbee binding
and group configurations against a Home Assistant / ZHA network.

Pipeline:  pull -> review/edit -> validate -> confirm -> apply

Transport note: Home Assistant no longer exposes ZHA over REST. Inventory and
group management go over the WebSocket API (``zha/devices``, ``zha/groups``,
``zha/group/...``) and binding/hardware-table operations go through the
``zha_toolkit`` integration's services (called over WS with return_response).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover - surfaced at runtime
    websockets = None


def _find_project_root() -> str:
    """Anchor runtime data (config.json, pulls/, plans/, edit file) at the
    project root, not next to this module. Walk up from here looking for a
    project marker; fall back to the current working directory."""
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(d, "pyproject.toml")) or os.path.exists(os.path.join(d, "config.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


# HERE = project root; every runtime path and display-relative path anchors here.
HERE = _find_project_root()
CONFIG_PATH = os.path.join(HERE, "config.json")

# ---------------------------------------------------------------------------
# Built-in cluster map. Users may use either the name or the hex/int ID in the
# edit file. Names are matched case-insensitively.
# ---------------------------------------------------------------------------
CLUSTERS: dict[str, int] = {
    "OnOff": 0x0006,
    "LevelControl": 0x0008,
    "ColorControl": 0x0300,
    "Scenes": 0x0005,
    "Groups": 0x0004,
    "Identify": 0x0003,
}
CLUSTER_BY_ID: dict[int, str] = {v: k for k, v in CLUSTERS.items()}
CLUSTER_BY_LOWER: dict[str, str] = {k.lower(): k for k in CLUSTERS}

# Execution order for apply. Actions always run in this sequence regardless of
# the order they appear in the edit file.
ACTION_ORDER = [
    "remove_group_member",
    "unbind",
    "create_group",
    "add_group_member",
    "bind",
]

# zha_toolkit retries each ZDO request this many times before giving up — guards
# against transient mesh timeouts (a busy router not answering an unbind/bind).
TOOLKIT_TRIES = 3


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------
class C:
    """ANSI colour codes (disabled when output is not a TTY)."""

    _on = sys.stdout.isatty()
    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    CYAN = "\033[36m" if _on else ""
    GREY = "\033[90m" if _on else ""


def die(msg: str, code: int = 1) -> None:
    print(f"{C.RED}error:{C.RESET} {msg}", file=sys.stderr)
    sys.exit(code)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Cluster / id helpers
# ---------------------------------------------------------------------------
def parse_int(value: Any) -> int | None:
    """Parse an int that may be given as 6, "6", or "0x0006"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s, 16) if s.lower().startswith("0x") else int(s, 10)
        except ValueError:
            return None
    return None


def hex_id(value: Any, width: int = 4) -> str:
    """Normalise a group/network id to a 0x-prefixed hex string."""
    n = parse_int(value)
    if n is None:
        return str(value)
    return f"0x{n:0{width}X}"


def ieee_bytes_to_str(raw: Any) -> str:
    """Convert a zigpy little-endian IEEE byte list into 'xx:xx:..:xx'."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)) and len(raw) == 8:
        return ":".join(f"{b & 0xFF:02x}" for b in reversed(raw))
    return str(raw)


def suggest_cluster(name: str) -> str | None:
    """Best-effort 'did you mean' for an unrecognised cluster name."""
    low = name.lower()
    for known_low, known in CLUSTER_BY_LOWER.items():
        if known_low.startswith(low[:5]) or low.startswith(known_low[:5]):
            return known
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "ha_url": "http://192.168.x.x:8123",
    "token": "your_long_lived_token",
    "editor": "nano",
    "pulls_dir": "pulls",
    "plans_dir": "plans",
    "edit_file": "zha_bindings_edit.json",
}


def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        die(
            f"config.json not found at {CONFIG_PATH}\n"
            "Copy config.example.json to config.json and fill in your HA url + token."
        )
    with open(CONFIG_PATH) as fh:
        cfg = json.load(fh)
    for key, default in DEFAULT_CONFIG.items():
        cfg.setdefault(key, default)
    cfg["pulls_dir"] = os.path.join(HERE, cfg["pulls_dir"])
    cfg["plans_dir"] = os.path.join(HERE, cfg["plans_dir"])
    cfg["edit_file"] = os.path.join(HERE, cfg["edit_file"])
    os.makedirs(cfg["pulls_dir"], exist_ok=True)
    os.makedirs(cfg["plans_dir"], exist_ok=True)
    if "x.x" in cfg["ha_url"] or cfg["token"] == "your_long_lived_token":
        die("config.json still has placeholder ha_url/token — fill them in first.")
    return cfg


def ws_url(ha_url: str) -> str:
    base = ha_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/api/websocket"
    return "ws://" + base + "/api/websocket"


# ---------------------------------------------------------------------------
# WebSocket client (async) — the single transport for everything
# ---------------------------------------------------------------------------
class WsClient:
    """Minimal Home Assistant WebSocket API client."""

    def __init__(self, cfg: dict[str, Any]):
        if websockets is None:
            die("The 'websockets' package is required. Install with: pip install websockets")
        self.url = ws_url(cfg["ha_url"])
        self.token = cfg["token"]
        self._id = 0
        self._conn = None

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def __aenter__(self):
        self._conn = await websockets.connect(self.url, max_size=None)
        msg = json.loads(await self._conn.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"unexpected first ws message: {msg}")
        await self._conn.send(json.dumps({"type": "auth", "access_token": self.token}))
        result = json.loads(await self._conn.recv())
        if result.get("type") != "auth_ok":
            raise RuntimeError(f"ws auth failed: {result}")
        return self

    async def __aexit__(self, *exc):
        if self._conn is not None:
            await self._conn.close()

    async def command(self, payload: dict[str, Any]) -> Any:
        """Send a command and wait for the matching result message."""
        cmd_id = self._next_id()
        payload = {**payload, "id": cmd_id}
        await self._conn.send(json.dumps(payload))
        while True:
            msg = json.loads(await self._conn.recv())
            if msg.get("id") == cmd_id and msg.get("type") == "result":
                if not msg.get("success", False):
                    raise RuntimeError(_fmt_ws_error(msg.get("error"), payload))
                return msg.get("result")

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any], return_response: bool = True
    ) -> Any:
        """Call a HA service over WS. With return_response, returns the response dict."""
        payload = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "service_data": data,
        }
        if return_response:
            payload["return_response"] = True
        result = await self.command(payload)
        if isinstance(result, dict) and "response" in result:
            return result["response"]
        return result

    async def toolkit(self, ieee: str, command: str, **extra: Any) -> dict | None:
        """Call a zha_toolkit service and return its response payload.

        zha_toolkit returns results synchronously via service return_response;
        the legacy zha_toolkit.executed / event_done event paths are not used.
        """
        data = {"ieee": ieee, "command": command, **extra}
        resp = await self.call_service("zha_toolkit", command, data)
        return resp if isinstance(resp, dict) else None


def _fmt_ws_error(error: Any, payload: dict[str, Any]) -> str:
    code = (error or {}).get("code") if isinstance(error, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else error
    ctx = payload.get("type")
    if payload.get("type") == "call_service":
        ctx = f"{payload.get('domain')}.{payload.get('service')}"
    return f"{ctx}: {message} ({code})" if code else f"{ctx}: {message}"


# ---------------------------------------------------------------------------
# Stage 1 — Pull
# ---------------------------------------------------------------------------
def parse_binds_response(resp: dict | None) -> list[dict[str, Any]]:
    """Normalise a zha_toolkit binds_get response into binding objects.

    The device's binding table arrives as resp['replies'][*][-1] — a list of
    {SrcEndpoint, ClusterId, DstAddress} entries. DstAddress.addrmode 1 is a
    group binding (group id in .nwk); addrmode 3 is a device binding (.ieee is
    a little-endian byte list).
    """
    out: list[dict[str, Any]] = []
    if not resp:
        return out
    for reply in resp.get("replies", []) or []:
        if not (isinstance(reply, list) and reply and isinstance(reply[-1], list)):
            continue
        for entry in reply[-1]:
            if not isinstance(entry, dict):
                continue
            cluster_id = entry.get("ClusterId")
            endpoint = entry.get("SrcEndpoint", 1)
            dst = entry.get("DstAddress", {}) or {}
            addrmode = dst.get("addrmode")
            if addrmode == 1:  # group
                dtype, did = "group", hex_id(dst.get("nwk"))
            elif addrmode == 3:  # device / ieee
                dtype, did = "device", ieee_bytes_to_str(dst.get("ieee"))
            else:
                continue
            out.append(
                {
                    "endpoint": endpoint,
                    "cluster_id": cluster_id,
                    "cluster_name": CLUSTER_BY_ID.get(cluster_id),
                    "destination_type": dtype,
                    "destination_id": did,
                    "destination_name": "",
                }
            )
    return out


async def pull_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    """Pull the full ZHA state over a single WebSocket session."""
    async with WsClient(cfg) as ws:
        zha_devices = await ws.command({"type": "zha/devices"})
        zha_groups = await ws.command({"type": "zha/groups"})
        areas = await ws.command({"type": "config/area_registry/list"})
        area_name = {a["area_id"]: a["name"] for a in areas}

        # Build name/id lookups for resolving binding destinations.
        name_by_ieee = {
            d["ieee"].lower(): (d.get("user_given_name") or d.get("name", "")) for d in zha_devices
        }
        name_by_group = {hex_id(g.get("group_id")): g.get("name", "") for g in zha_groups}

        print(
            f"  {len(zha_devices)} devices, {len(zha_groups)} groups. "
            f"Reading per-device binding tables ..."
        )

        device_objs: list[dict[str, Any]] = []
        for idx, d in enumerate(zha_devices, 1):
            ieee = d["ieee"]
            available = d.get("available", False)
            bindings: list[dict[str, Any]] = []
            if available and not d.get("active_coordinator"):
                if C._on:  # animate only on a TTY; keep piped output clean
                    print(f"\r    [{idx}/{len(zha_devices)}] {d.get('name', ieee)[:40]:<40}", end="", flush=True)
                try:
                    resp = await ws.toolkit(ieee, "binds_get")
                    bindings = parse_binds_response(resp)
                except Exception:  # noqa: BLE001 - offline/unreachable device
                    bindings = []
            # Fill in destination names from the lookups.
            for b in bindings:
                if b["destination_type"] == "group":
                    b["destination_name"] = name_by_group.get(b["destination_id"], "")
                else:
                    b["destination_name"] = name_by_ieee.get(b["destination_id"].lower(), "")
            device_objs.append(
                {
                    "ieee": ieee,
                    "name": d.get("user_given_name") or d.get("name", ""),
                    "area": area_name.get(d.get("area_id"), ""),
                    "model": d.get("model", ""),
                    "manufacturer": d.get("manufacturer", ""),
                    "nwk": hex_id(d.get("nwk")),
                    "device_type": d.get("device_type", ""),
                    "available": available,
                    "bindings": bindings,
                }
            )
        if C._on:
            print()  # end the live progress line

    group_objs: list[dict[str, Any]] = []
    for g in zha_groups:
        members = []
        for m in g.get("members", []):
            dev = m.get("device", m)
            members.append(
                {
                    "ieee": dev.get("ieee", m.get("ieee", "")),
                    "name": dev.get("user_given_name") or dev.get("name", m.get("name", "")),
                    "hardware_confirmed": True,
                }
            )
        group_objs.append(
            {"group_id": hex_id(g.get("group_id")), "name": g.get("name", ""), "members": members}
        )

    return {
        "_meta": {
            "pulled_at": now_iso(),
            "ha_url": cfg["ha_url"],
            "device_count": len(device_objs),
            "group_count": len(group_objs),
        },
        "devices": device_objs,
        "groups": group_objs,
    }


def cmd_pull(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    print(f"{C.CYAN}Pulling ZHA state from {cfg['ha_url']} ...{C.RESET}")
    try:
        snapshot = asyncio.run(pull_snapshot(cfg))
    except Exception as exc:  # noqa: BLE001
        die(f"pull failed: {exc}")

    stamp = now_stamp()
    pull_path = os.path.join(cfg["pulls_dir"], f"zha_bindings_{stamp}.json")
    with open(pull_path, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    pull_rel = os.path.relpath(pull_path, HERE)

    edit_doc = {
        "_meta": {"pull_file": pull_rel, "pulled_at": snapshot["_meta"]["pulled_at"]},
        "devices": snapshot["devices"],
        "groups": snapshot["groups"],
    }
    with open(cfg["edit_file"], "w") as fh:
        json.dump(edit_doc, fh, indent=2)

    nbind = sum(len(d["bindings"]) for d in snapshot["devices"])
    print(f"{C.GREEN}✓{C.RESET} Snapshot: {pull_rel}  ({nbind} bindings across {snapshot['_meta']['device_count']} devices)")
    print(f"{C.GREEN}✓{C.RESET} Edit file: {os.path.relpath(cfg['edit_file'], HERE)}")

    if getattr(args, "edit", False):
        open_editor(cfg)
    return 0


def open_editor(cfg: dict[str, Any]) -> None:
    editor = cfg.get("editor") or os.environ.get("EDITOR", "nano")
    try:
        subprocess.call([editor, cfg["edit_file"]])
    except FileNotFoundError:
        die(f"editor '{editor}' not found — set 'editor' in config.json")


# ---------------------------------------------------------------------------
# Stage 3 — Diff & Validate
# ---------------------------------------------------------------------------
class Plan:
    def __init__(self, source_pull: str):
        self.source_pull = source_pull
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.actions: list[dict[str, Any]] = []

    def to_dict(self, dry_run: bool = False) -> dict[str, Any]:
        return {
            "generated_at": now_iso(),
            "source_pull": self.source_pull,
            "dry_run": dry_run,
            "errors": self.errors,
            "warnings": self.warnings,
            "infos": self.infos,
            "actions": self.actions,
        }


def resolve_cluster(binding: dict[str, Any], device_name: str, plan: Plan) -> tuple[int | None, str | None]:
    """Resolve (cluster_id, cluster_name) applying the spec's precedence rules."""
    raw_id = parse_int(binding.get("cluster_id"))
    raw_name = binding.get("cluster_name")

    name_id = None
    if raw_name is not None:
        canonical = CLUSTER_BY_LOWER.get(str(raw_name).lower())
        if canonical is None and raw_id is None:
            hint = suggest_cluster(str(raw_name))
            msg = f"[{device_name}] Cluster '{raw_name}' not recognized"
            if hint:
                msg += f"\n     Did you mean '{hint}'?"
            plan.errors.append(msg)
            return None, raw_name
        name_id = CLUSTERS.get(canonical) if canonical else None

    if raw_id is not None and name_id is not None and raw_id != name_id:
        plan.errors.append(
            f"[{device_name}] Cluster name/ID conflict: "
            f"'{raw_name}' (0x{name_id:04X}) vs 0x{raw_id:04X}"
        )
        return raw_id, raw_name

    cluster_id = raw_id if raw_id is not None else name_id
    cluster_name = CLUSTER_BY_ID.get(cluster_id, raw_name if isinstance(raw_name, str) else None)
    return cluster_id, cluster_name


def binding_key(b: dict[str, Any]) -> tuple:
    return (
        b.get("endpoint"),
        parse_int(b.get("cluster_id")),
        b.get("destination_type"),
        str(b.get("destination_id", "")).lower(),
    )


def build_resolvers(pull: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (group_name->id, device_name->ieee) lookups for resolution."""
    group_id_by_name = {g["name"].lower(): g["group_id"] for g in pull.get("groups", [])}
    ieee_by_name = {d["name"].lower(): d["ieee"] for d in pull.get("devices", [])}
    return group_id_by_name, ieee_by_name


def resolve_destination(
    binding: dict[str, Any],
    group_id_by_name: dict[str, str],
    ieee_by_name: dict[str, str],
    device_name: str,
    plan: Plan,
) -> tuple[str | None, str, str]:
    """Resolve (destination_id, destination_type, destination_name)."""
    dtype = binding.get("destination_type", "group")
    did = binding.get("destination_id")
    dname = binding.get("destination_name", "")

    if did:
        if dtype == "group":
            did = hex_id(did)
        return did, dtype, dname

    if dname:
        if dtype == "group" and dname.lower() in group_id_by_name:
            return group_id_by_name[dname.lower()], dtype, dname
        if dtype == "device" and dname.lower() in ieee_by_name:
            return ieee_by_name[dname.lower()], dtype, dname

    plan.errors.append(f"[{device_name}] Destination '{dname or did}' could not be resolved")
    return None, dtype, dname


def diff(pull: dict[str, Any], edit: dict[str, Any]) -> Plan:
    plan = Plan(source_pull=edit.get("_meta", {}).get("pull_file", "?"))
    group_id_by_name, ieee_by_name = build_resolvers(pull)

    pull_devices = {d["ieee"].lower(): d for d in pull.get("devices", [])}
    pull_groups = {g["group_id"].lower(): g for g in pull.get("groups", [])}
    pull_group_ids = set(pull_groups.keys())

    for edev in edit.get("devices", []):
        ieee = edev.get("ieee", "")
        name = edev.get("name", ieee)
        pdev = pull_devices.get(ieee.lower())
        if pdev is None:
            plan.errors.append(f"[{name}] IEEE {ieee} not found in pull snapshot")
            continue
        if not edev.get("available", True) or not pdev.get("available", True):
            plan.warnings.append(f"[{name}] device was offline during pull")

        old = {binding_key(b): b for b in pdev.get("bindings", [])}
        new_resolved: dict[tuple, dict[str, Any]] = {}
        for b in edev.get("bindings", []):
            cid, cname = resolve_cluster(b, name, plan)
            did, dtype, dname = resolve_destination(b, group_id_by_name, ieee_by_name, name, plan)
            if cid is None or did is None:
                continue
            resolved = {
                "endpoint": b.get("endpoint", 1),
                "cluster_id": cid,
                "cluster_name": cname,
                "destination_type": dtype,
                "destination_id": did,
                "destination_name": dname or _name_for_dest(dtype, did, pull),
            }
            new_resolved[binding_key(resolved)] = resolved

        for key, b in new_resolved.items():
            if key in old:
                continue  # unchanged binding — present in both pull and edit, no action
            plan.actions.append(
                {
                    "action": "bind",
                    "device_name": name,
                    "ieee": ieee,
                    **{k: b[k] for k in (
                        "endpoint", "cluster_id", "cluster_name",
                        "destination_type", "destination_id", "destination_name")},
                }
            )
        unbound_keys = [k for k in old if k not in new_resolved]
        unbound_ep_cluster = {(k[0], k[1]) for k in unbound_keys}
        # Kept bindings (present in both pull and edit) that share an
        # (endpoint, cluster) with an unbind. zha_toolkit's binds_remove_all —
        # the only working device-unbind primitive on zigpy>=1.x — is
        # destination-blind: it drops EVERY binding on (endpoint, cluster), not
        # just the one to this destination. So these co-located survivors would
        # be collateral-removed. We self-heal by re-emitting them as bind actions
        # (execution order runs all unbinds before all binds, so they're restored).
        for key, b in new_resolved.items():
            if key not in old:
                continue  # newly added — already handled above
            if (key[0], key[1]) not in unbound_ep_cluster:
                continue  # not threatened
            plan.warnings.append(
                f"[{name}] EP{key[0]} {b.get('cluster_name') or hex_id(key[1])} → "
                f"{b.get('destination_name') or b.get('destination_id')} sits on the same "
                f"endpoint+cluster as an unbind; toolkit unbind is destination-blind, so it "
                f"will be re-bound after the unbind to preserve it"
            )
            plan.actions.append(
                {
                    "action": "bind",
                    "device_name": name,
                    "ieee": ieee,
                    **{k: b[k] for k in (
                        "endpoint", "cluster_id", "cluster_name",
                        "destination_type", "destination_id", "destination_name")},
                }
            )
        for key in unbound_keys:
            b = old[key]
            plan.actions.append(
                {
                    "action": "unbind",
                    "device_name": name,
                    "ieee": ieee,
                    "endpoint": b.get("endpoint", 1),
                    "cluster_id": parse_int(b.get("cluster_id")),
                    "cluster_name": b.get("cluster_name"),
                    "destination_type": b.get("destination_type", "group"),
                    "destination_id": b.get("destination_id"),
                    "destination_name": b.get("destination_name", ""),
                }
            )

    for egroup in edit.get("groups", []):
        gid = hex_id(egroup.get("group_id")) if egroup.get("group_id") else None
        gname = egroup.get("name", "")
        is_new = gid is None or gid.lower() not in pull_group_ids
        if is_new:
            plan.infos.append(f"new group '{gname}' will be created")
            plan.actions.append({"action": "create_group", "group_id": gid, "group_name": gname})
            old_members: dict[str, Any] = {}
        else:
            old_members = {m["ieee"].lower(): m for m in pull_groups[gid.lower()].get("members", [])}

        new_members = {m["ieee"].lower(): m for m in egroup.get("members", [])}

        for ieee_l, m in new_members.items():
            if ieee_l in old_members:
                continue
            if not m.get("hardware_confirmed", False):
                plan.warnings.append(
                    f"[{m.get('name', ieee_l)}] will be pushed to group {gid or gname} hardware table"
                )
            plan.actions.append(
                {
                    "action": "add_group_member",
                    "group_id": gid,
                    "group_name": gname,
                    "ieee": m.get("ieee"),
                    "device_name": m.get("name", ""),
                    "hardware_confirmed": m.get("hardware_confirmed", False),
                }
            )
        if not is_new:
            for ieee_l, m in old_members.items():
                if ieee_l in new_members:
                    continue
                plan.actions.append(
                    {
                        "action": "remove_group_member",
                        "group_id": gid,
                        "group_name": gname,
                        "ieee": m.get("ieee"),
                        "device_name": m.get("name", ""),
                    }
                )

    return plan


def _name_for_dest(dtype: str, did: str, pull: dict[str, Any]) -> str:
    if dtype == "group":
        for g in pull.get("groups", []):
            if g["group_id"].lower() == str(did).lower():
                return g["name"]
    else:
        for d in pull.get("devices", []):
            if d["ieee"].lower() == str(did).lower():
                return d["name"]
    return ""


# ---------------------------------------------------------------------------
# Stage 4 — Confirm / console rendering
# ---------------------------------------------------------------------------
ACTION_LABEL = {
    "unbind": ("✗", "UNBIND", C.RED),
    "bind": ("+", "BIND", C.GREEN),
    "create_group": ("+", "CREATE GROUP", C.GREEN),
    "add_group_member": ("+", "ADD MEMBER", C.GREEN),
    "remove_group_member": ("✗", "REM MEMBER", C.RED),
}


def render_action(a: dict[str, Any]) -> str:
    act = a["action"]
    if act in ("bind", "unbind"):
        cluster = a.get("cluster_name") or hex_id(a.get("cluster_id"))
        dest = a.get("destination_name") or a.get("destination_id")
        dtype = a.get("destination_type", "group").upper()
        return f"{a['device_name']}  EP{a.get('endpoint', 1)} {cluster} → {dtype} {dest}"
    if act == "create_group":
        return f"{a.get('group_name')} ({a.get('group_id') or 'auto-id'})"
    if act == "add_group_member":
        tail = " (push to hardware)" if not a.get("hardware_confirmed", False) else ""
        return f"{a.get('device_name')} → {a.get('group_name')}{tail}"
    if act == "remove_group_member":
        return f"{a.get('device_name')} → {a.get('group_name')}"
    return json.dumps(a)


def print_plan(plan: Plan) -> None:
    bar = "═" * 51
    print(f"\n{C.BOLD}{bar}{C.RESET}")
    print(f"{C.BOLD}  ZHA BINDING PLAN — {datetime.now().strftime('%Y-%m-%d %H:%M')}{C.RESET}")
    print(f"{C.BOLD}{bar}{C.RESET}\n")

    if plan.errors:
        print(f"  {C.RED}{C.BOLD}ERRORS (must fix before apply):{C.RESET}")
        for e in plan.errors:
            print(f"  {C.RED}✗{C.RESET}  {e}")
        print()

    if plan.warnings:
        print(f"  {C.YELLOW}{C.BOLD}WARNINGS:{C.RESET}")
        for w in plan.warnings:
            print(f"  {C.YELLOW}⚠{C.RESET}  {w}")
        print()

    if plan.infos:
        print(f"  {C.CYAN}{C.BOLD}INFO:{C.RESET}")
        for i in plan.infos:
            print(f"  {C.CYAN}ℹ{C.RESET}  {i}")
        print()

    ordered = order_actions(plan.actions)
    print(f"  {C.BOLD}ACTIONS ({len(ordered)} total):{C.RESET}")
    for a in ordered:
        glyph, label, colour = ACTION_LABEL.get(a["action"], ("·", a["action"].upper(), ""))
        print(f"  {colour}{glyph}{C.RESET}  {label:<12} {render_action(a)}")
    print()

    if plan.errors:
        print(f"  {C.RED}Errors present — cannot apply.{C.RESET}")
        print("  Edit zha_bindings_edit.json and re-run: zha-manager validate")
    elif not ordered:
        print(f"  {C.GREY}No changes — edit file matches the pull snapshot.{C.RESET}")


def order_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        actions,
        key=lambda a: ACTION_ORDER.index(a["action"]) if a["action"] in ACTION_ORDER else 99,
    )


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------
def save_plan(cfg: dict[str, Any], plan_dict: dict[str, Any], resume: bool = False) -> str:
    stamp = now_stamp()
    prefix = "zha_plan_resume_" if resume else "zha_plan_"
    path = os.path.join(cfg["plans_dir"], f"{prefix}{stamp}.json")
    with open(path, "w") as fh:
        json.dump(plan_dict, fh, indent=2)
    return path


def latest_file(dir_path: str, prefix: str) -> str | None:
    candidates = [
        os.path.join(dir_path, f)
        for f in os.listdir(dir_path)
        if f.startswith(prefix) and f.endswith(".json")
    ]
    return max(candidates, key=os.path.getmtime) if candidates else None


def load_pull_for_edit(cfg: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    rel = edit.get("_meta", {}).get("pull_file")
    if not rel:
        die("edit file has no _meta.pull_file — run 'pull' first")
    pull_path = rel if os.path.isabs(rel) else os.path.join(HERE, rel)
    if not os.path.exists(pull_path):
        die(f"pull snapshot referenced by edit file not found: {pull_path}")
    with open(pull_path) as fh:
        return json.load(fh)


def load_edit(cfg: dict[str, Any]) -> dict[str, Any]:
    if not os.path.exists(cfg["edit_file"]):
        die(f"edit file not found: {cfg['edit_file']} — run 'pull' first")
    with open(cfg["edit_file"]) as fh:
        return json.load(fh)


def build_plan(cfg: dict[str, Any]) -> Plan:
    edit = load_edit(cfg)
    pull = load_pull_for_edit(cfg, edit)
    return diff(pull, edit)


def cmd_validate(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    plan = build_plan(cfg)
    print_plan(plan)
    plan_dict = plan.to_dict()
    plan_dict["actions"] = order_actions(plan_dict["actions"])
    path = save_plan(cfg, plan_dict)
    print(f"\n  {C.GREY}Plan saved: {os.path.relpath(path, HERE)}{C.RESET}")

    if plan.errors:
        return 2
    if args.apply:
        print()
        return run_apply(cfg, plan_dict, dry_run=False)

    if plan_dict["actions"]:
        print(f"\n  {len(plan_dict['actions'])} actions ready.")
        print("  Run: zha-manager apply")
    return 0


# ---------------------------------------------------------------------------
# Stage 5 — Apply  (WebSocket: zha_toolkit services + zha/group commands)
# ---------------------------------------------------------------------------
def _toolkit_failed(resp: Any) -> str | None:
    """Return an error string if a zha_toolkit response reports failure.

    The explicit error channel is the `errors` list. The `success` boolean is
    NOT reliable across commands — e.g. binds_remove_all leaves it false even
    on a clean run (including the idempotent "nothing matched" case), so we do
    not treat success=false as a failure.
    """
    if not isinstance(resp, dict):
        return None
    errors = resp.get("errors")
    if errors:
        return "; ".join(str(e) for e in errors)
    return None


async def execute_action(
    ws: WsClient, a: dict[str, Any], created_groups: dict[str, int], coord_ieee: str | None = None
) -> None:
    act = a["action"]

    # NOTE: zha_toolkit service_data uses the user-facing keys 'cluster' and
    # 'endpoint' (extractParams maps them to cluster_id / endpoint_id). Passing
    # 'cluster_id' is silently ignored, which would unbind/bind ALL clusters.
    if act in ("bind", "unbind"):
        endpoint = a.get("endpoint", 1)
        cluster_id = parse_int(a.get("cluster_id"))
        dtype = a.get("destination_type")
        if act == "bind" and dtype == "group":
            resp = await ws.toolkit(
                a["ieee"], "bind_group",
                command_data=parse_int(a.get("destination_id")),
                cluster=cluster_id, endpoint=endpoint, tries=TOOLKIT_TRIES,
            )
        elif act == "bind":  # device → device
            resp = await ws.toolkit(
                a["ieee"], "bind_ieee",
                command_data=a.get("destination_id"),
                cluster=cluster_id, endpoint=endpoint, tries=TOOLKIT_TRIES,
            )
        elif act == "unbind" and dtype == "group":
            # unbind_group ignores cluster/endpoint filters — it removes ALL
            # bindable out-clusters from the group. Guard against silent overreach.
            raise RuntimeError(
                "group unbind via zha_toolkit removes ALL bindable clusters at once "
                "(no per-cluster filter) — not executing to avoid clobbering other bindings"
            )
        else:  # unbind, device destination
            dest = str(a.get("destination_id", "")).lower()
            # zha_toolkit.unbind_coordinator is broken on zigpy>=1.x (it does
            # `data = app.ieee`, which no longer exists). binds_remove_all does
            # the same job and is destination-selective: it removes only bindings
            # whose dst IEEE + endpoint + cluster match. Supply the coordinator
            # IEEE as command_data ourselves to keep group binds untouched.
            target = a.get("destination_id")
            if not target and coord_ieee:
                target = coord_ieee
            if not target:
                raise RuntimeError("device unbind needs a destination IEEE to target")
            resp = await ws.toolkit(
                a["ieee"], "binds_remove_all",
                command_data=target, cluster=cluster_id, endpoint=endpoint, tries=TOOLKIT_TRIES,
            )
        err = _toolkit_failed(resp)
        if err:
            raise RuntimeError(err)

    elif act == "create_group":
        payload: dict[str, Any] = {"group_name": a.get("group_name")}
        gid = parse_int(a.get("group_id"))
        if gid is not None:
            payload["group_id"] = gid
        result = await ws.command({"type": "zha/group/add", **payload})
        # Remember the assigned id so member adds in this run can resolve it.
        new_id = result.get("group_id") if isinstance(result, dict) else None
        if new_id is not None and a.get("group_name"):
            created_groups[a["group_name"].lower()] = new_id

    elif act == "add_group_member":
        gid = _resolve_group_id(a, created_groups)
        await ws.command(
            {"type": "zha/group/members/add", "group_id": gid,
             "members": [{"ieee": a["ieee"], "endpoint_id": a.get("endpoint", 1)}]}
        )
        # hardware_confirmed:false → also push the group into the device's table.
        if not a.get("hardware_confirmed", False):
            resp = await ws.toolkit(a["ieee"], "add_to_group", command_data=gid, endpoint=a.get("endpoint", 1))
            err = _toolkit_failed(resp)
            if err:
                raise RuntimeError(err)

    elif act == "remove_group_member":
        gid = _resolve_group_id(a, created_groups)
        await ws.command(
            {"type": "zha/group/members/remove", "group_id": gid,
             "members": [{"ieee": a["ieee"], "endpoint_id": a.get("endpoint", 1)}]}
        )
    else:
        raise RuntimeError(f"unknown action: {act}")


def _resolve_group_id(a: dict[str, Any], created_groups: dict[str, int]) -> int:
    gid = parse_int(a.get("group_id"))
    if gid is None:
        gid = created_groups.get(str(a.get("group_name", "")).lower())
    if gid is None:
        raise RuntimeError(f"could not resolve group id for {a.get('group_name')}")
    return gid


def describe_api_call(a: dict[str, Any]) -> str:
    act = a["action"]
    ep, cid = a.get("endpoint", 1), parse_int(a.get("cluster_id"))
    chex = hex_id(cid) if cid is not None else "?"
    if act == "bind" and a.get("destination_type") == "group":
        return f"service zha_toolkit.bind_group ieee={a['ieee']} command_data={a.get('destination_id')} cluster={chex} endpoint={ep}"
    if act == "unbind" and a.get("destination_type") == "group":
        return "UNSUPPORTED: group unbind has no per-cluster filter (would clobber all group binds)"
    if act == "bind":
        return f"service zha_toolkit.bind_ieee ieee={a['ieee']} command_data={a.get('destination_id')} cluster={chex} endpoint={ep}"
    if act == "unbind":
        return f"service zha_toolkit.binds_remove_all ieee={a['ieee']} command_data={a.get('destination_id')} cluster={chex} endpoint={ep}  (dest={a.get('destination_name') or a.get('destination_id')})"
    if act == "create_group":
        return f"ws zha/group/add group_name={a.get('group_name')!r} group_id={a.get('group_id')}"
    if act == "add_group_member":
        extra = " + zha_toolkit.add_to_group (hw push)" if not a.get("hardware_confirmed", False) else ""
        return f"ws zha/group/members/add group_id={a.get('group_id')} ieee={a['ieee']}{extra}"
    if act == "remove_group_member":
        return f"ws zha/group/members/remove group_id={a.get('group_id')} ieee={a['ieee']}"
    return "?"


async def _run_apply_ws(cfg: dict[str, Any], actions: list[dict[str, Any]], plan_dict: dict[str, Any]) -> int:
    total = len(actions)
    created_groups: dict[str, int] = {}
    async with WsClient(cfg) as ws:
        # Identify the coordinator so device→coordinator unbinds can use
        # unbind_coordinator (the only selective device-unbind primitive).
        coord_ieee = None
        try:
            for d in await ws.command({"type": "zha/devices"}):
                if d.get("active_coordinator"):
                    coord_ieee = d["ieee"]
                    break
        except Exception:  # noqa: BLE001
            coord_ieee = None

        print(f"  Applying {total} actions...\n")
        for i, a in enumerate(actions, 1):
            _, label, _ = ACTION_LABEL.get(a["action"], ("·", a["action"].upper(), ""))
            print(f"  [{i}/{total}] {label:<11} {render_action(a)} ... ", end="", flush=True)
            try:
                await execute_action(ws, a, created_groups, coord_ieee)
            except Exception as exc:  # noqa: BLE001
                print(f"{C.RED}✗{C.RESET}")
                print(f"\n  {C.RED}Action failed:{C.RESET} {exc}")
                remaining = actions[i - 1:]
                resume_dict = {
                    "generated_at": now_iso(),
                    "source_pull": plan_dict.get("source_pull"),
                    "resumed_from_failure": True,
                    "failed_action": a,
                    "errors": [], "warnings": [], "infos": [],
                    "actions": remaining,
                }
                path = save_plan(cfg, resume_dict, resume=True)
                print(f"  {len(remaining)} action(s) not executed. Resume file: {os.path.relpath(path, HERE)}")
                print("  Fix the issue, then run: zha-manager apply --resume")
                return 1
            print(f"{C.GREEN}✓{C.RESET}")
    return 0


def run_apply(cfg: dict[str, Any], plan_dict: dict[str, Any], dry_run: bool) -> int:
    actions = order_actions(plan_dict.get("actions", []))
    if plan_dict.get("errors"):
        die("plan has errors — cannot apply. Run validate and fix them first.")
    if not actions:
        print(f"  {C.GREY}Nothing to apply.{C.RESET}")
        return 0

    total = len(actions)
    if dry_run:
        print(f"  {C.CYAN}DRY RUN — {total} actions (no API calls will be made):{C.RESET}\n")
        for i, a in enumerate(actions, 1):
            _, label, _ = ACTION_LABEL.get(a["action"], ("·", a["action"].upper(), ""))
            print(f"  [{i}/{total}] {label:<12} {render_action(a)}")
            print(f"          {C.GREY}{describe_api_call(a)}{C.RESET}")
        out = dict(plan_dict)
        out["dry_run"] = True
        path = save_plan(cfg, out)
        print(f"\n  {C.GREY}Dry-run plan saved: {os.path.relpath(path, HERE)}{C.RESET}")
        return 0

    try:
        rc = asyncio.run(_run_apply_ws(cfg, actions, plan_dict))
    except Exception as exc:  # noqa: BLE001
        die(f"apply failed to connect: {exc}")
    if rc != 0:
        return rc

    print(f"\n  {C.GREEN}Done.{C.RESET} Running post-apply snapshot ...")
    try:
        cmd_pull(cfg, argparse.Namespace(edit=False))
    except SystemExit:
        print(f"  {C.YELLOW}⚠ post-apply pull failed — apply itself succeeded.{C.RESET}")
    return 0


def cmd_apply(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    if args.resume:
        path = latest_file(cfg["plans_dir"], "zha_plan_resume_")
        if not path:
            die("no resume plan found in plans/")
        with open(path) as fh:
            plan_dict = json.load(fh)
        print(f"  Resuming from {os.path.relpath(path, HERE)}")
        return run_apply(cfg, plan_dict, dry_run=args.dry_run)

    plan = build_plan(cfg)
    print_plan(plan)
    plan_dict = plan.to_dict(dry_run=args.dry_run)
    plan_dict["actions"] = order_actions(plan_dict["actions"])

    if plan.errors:
        save_plan(cfg, plan_dict)
        return 2
    if not plan_dict["actions"]:
        return 0
    if args.dry_run:
        print()
        return run_apply(cfg, plan_dict, dry_run=True)

    if getattr(args, "yes", False):
        save_plan(cfg, plan_dict)
        print()
        return run_apply(cfg, plan_dict, dry_run=False)

    print()
    choice = input("  Apply? [y/N] / save plan and apply later [s]: ").strip().lower()
    if choice == "s":
        path = save_plan(cfg, plan_dict)
        print(f"  Plan saved: {os.path.relpath(path, HERE)}")
        print("  Apply later with: zha-manager apply")
        return 0
    if choice != "y":
        print("  Aborted.")
        return 0

    save_plan(cfg, plan_dict)
    print()
    return run_apply(cfg, plan_dict, dry_run=False)


# ---------------------------------------------------------------------------
# Device-role helpers + latest-pull loading (shared by inspect / rebind)
# ---------------------------------------------------------------------------
# The rebind helper wipes these device-bound clusters and re-binds OnOff+Level
# to the area's light group. Kept as named constants so the behaviour is obvious.
WIPE_CLUSTERS = {0x0006, 0x0008, 0x0300}          # OnOff, LevelControl, ColorControl
REBIND_CLUSTERS = [(0x0006, "OnOff"), (0x0008, "LevelControl")]
REBIND_ENDPOINTS = (1, 2, 3)


def is_switch(dev: dict[str, Any]) -> bool:
    return (dev.get("model") or "").startswith("VZM") or "Light Switch" in (dev.get("name") or "")


def is_fan(dev: dict[str, Any]) -> bool:
    return "fan" in (dev.get("name") or "").lower()


def latest_pull_path(cfg: dict[str, Any]) -> str | None:
    return latest_file(cfg["pulls_dir"], "zha_bindings_")


def load_latest_pull(cfg: dict[str, Any]) -> dict[str, Any]:
    path = latest_pull_path(cfg)
    if not path:
        die("no pull snapshot found — run 'pull' first")
    with open(path) as fh:
        doc = json.load(fh)
    doc["_meta"]["_path"] = path
    return doc


def resolve_switch_group(
    dev: dict[str, Any], groups: list[dict[str, Any]], forced: str | None
) -> tuple[str, str] | None:
    """Pick a switch's target light group: forced name, else the group whose
    name is the longest prefix of the device name at a word boundary."""
    if forced:
        for g in groups:
            if g["name"].strip().lower() == forced.strip().lower():
                return g["group_id"], g["name"]
        die(f"--group {forced!r} not found among ZHA groups")
    name_l = (dev.get("name") or "").lower()
    best: tuple[str, str] | None = None
    best_len = -1
    for g in groups:
        gl = g["name"].strip().lower()
        if not gl or not name_l.startswith(gl):
            continue
        nxt = name_l[len(gl): len(gl) + 1]
        if nxt and nxt.isalnum():  # require a boundary so 'Kitchen' != 'Kitchenette'
            continue
        if len(gl) > best_len:
            best, best_len = (g["group_id"], g["name"]), len(gl)
    return best


# ---------------------------------------------------------------------------
# inspect — read-only view of the latest pull (no network)
# ---------------------------------------------------------------------------
def cmd_inspect(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    pull = load_latest_pull(cfg)
    devices, groups = pull["devices"], pull["groups"]
    print(f"{C.GREY}from {os.path.relpath(pull['_meta']['_path'], HERE)} "
          f"(pulled {pull['_meta'].get('pulled_at', '?')}){C.RESET}\n")

    if not args.areas:
        by_area: dict[str, int] = {}
        for d in devices:
            by_area[d.get("area") or "(no area)"] = by_area.get(d.get("area") or "(no area)", 0) + 1
        print(f"{C.BOLD}Areas ({len(by_area)}):{C.RESET}")
        for area in sorted(by_area):
            print(f"  {area:<26} {by_area[area]:>2} devices")
        print(f"\n{C.BOLD}Groups ({len(groups)}):{C.RESET}")
        for g in sorted(groups, key=lambda x: x["group_id"]):
            print(f"  {g['group_id']}  {g['name']:<26} {len(g.get('members', []))} members")
        print(f"\n{C.GREY}Detail for an area:  zha-manager inspect \"Kitchen\"{C.RESET}")
        return 0

    for area in args.areas:
        devs = [d for d in devices if d.get("area") == area]
        if not devs:
            print(f"{C.YELLOW}No devices in area {area!r}.{C.RESET}")
            continue
        print(f"{C.BOLD}═ {area}  ({len(devs)} devices) ═{C.RESET}")
        for d in devs:
            role = "SWITCH" if is_switch(d) else "light "
            tag = f" {C.YELLOW}<fan>{C.RESET}" if is_fan(d) else ""
            off = "" if d.get("available", True) else f" {C.RED}(offline){C.RESET}"
            nb = len(d["bindings"])
            print(f"\n  {C.CYAN}[{role}]{C.RESET}{tag} {d['name']}  {C.GREY}{d['model']} · {d['ieee']}{C.RESET}{off}  — {nb} bindings")
            for b in d["bindings"]:
                cl = b["cluster_name"] or hex_id(b["cluster_id"])
                dt = b["destination_type"]
                print(f"       EP{b['endpoint']} {cl:<13} → {dt:<6} {b['destination_name'] or b['destination_id']}")
        print()
    return 0


# ---------------------------------------------------------------------------
# rebind — generate an edit file that moves switch control onto the light group
# ---------------------------------------------------------------------------
def cmd_rebind(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    edit = load_edit(cfg)
    groups = edit["groups"]
    excludes = [] if args.include_fans else ["fan"]
    excludes += [x.lower() for x in (args.exclude or [])]

    known_areas = {d.get("area") for d in edit["devices"]}
    summary: list[str] = []
    removed = added = kept = 0

    for area in args.areas:
        if area not in known_areas:
            die(f"unknown area {area!r} — see: zha-manager inspect")
        for d in edit["devices"]:
            if d.get("area") != area:
                continue
            if any(x in (d.get("name") or "").lower() for x in excludes):
                continue  # excluded (e.g. fan switch) — left untouched

            before = len(d["bindings"])
            d["bindings"] = [b for b in d["bindings"]
                             if not (b.get("destination_type") == "device" and b.get("cluster_id") in WIPE_CLUSTERS)]
            removed += before - len(d["bindings"])

            if not is_switch(d):
                continue
            tg = resolve_switch_group(d, groups, args.group)
            if tg is None:
                die(f"no light group matches switch {d['name']!r} — pass --group NAME")
            gid, gname = tg
            # sanity: warn if the target group has no light (non-switch) members
            grp = next((g for g in groups if g["group_id"].lower() == gid.lower()), None)
            if grp and not any(not is_switch(m) for m in grp.get("members", [])):
                print(f"  {C.YELLOW}⚠ group {gname!r} has no light members — switch would control nothing{C.RESET}")

            existing = {(b.get("endpoint"), b.get("cluster_id"), b.get("destination_type"),
                         str(b.get("destination_id", "")).lower()) for b in d["bindings"]}
            for ep in REBIND_ENDPOINTS:
                for cid, cname in REBIND_CLUSTERS:
                    if (ep, cid, "group", gid.lower()) in existing:
                        kept += 1
                        continue
                    d["bindings"].append({
                        "endpoint": ep, "cluster_id": cid, "cluster_name": cname,
                        "destination_type": "group", "destination_id": gid, "destination_name": gname,
                    })
                    added += 1
            summary.append(f"  {d['name']} → {gname}")

    with open(cfg["edit_file"], "w") as fh:
        json.dump(edit, fh, indent=2)

    print(f"\n{C.BOLD}Rebind planned for: {', '.join(args.areas)}{C.RESET}")
    for line in summary:
        print(line)
    print(f"\n  device control-cluster bindings removed: {removed}")
    print(f"  new group bindings added:                {added}")
    print(f"  existing group bindings kept:            {kept}")

    # Show the resulting plan straight away so the user can review before applying.
    plan = build_plan(cfg)
    print_plan(plan)
    if not plan.errors and plan.actions:
        print(f"\n  Review above, then: {C.BOLD}zha-manager apply{C.RESET}"
              f"  (or 'apply --dry-run' first)")
    return 2 if plan.errors else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zha-manager",
        description="Query, edit, validate, and apply ZHA binding/group config.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pull = sub.add_parser("pull", help="Pull current state from ZHA")
    pull.add_argument("--edit", action="store_true", help="Open the edit file after pulling")

    insp = sub.add_parser("inspect", help="Show areas/groups (no args) or per-area device+binding detail")
    insp.add_argument("areas", nargs="*", help="Area name(s) to detail; omit to list all areas + groups")

    reb = sub.add_parser("rebind", help="Move switch OnOff/Level control onto each area's light group")
    reb.add_argument("areas", nargs="+", help="Area name(s) to rebind")
    reb.add_argument("--group", help="Force all switches in the area(s) to this group (else auto by name prefix)")
    reb.add_argument("--exclude", action="append", metavar="SUBSTR",
                     help="Also skip devices whose name contains SUBSTR (repeatable)")
    reb.add_argument("--include-fans", action="store_true", help="Do not auto-exclude fan switches")

    val = sub.add_parser("validate", help="Validate edit file against last pull, show plan")
    val.add_argument("--apply", action="store_true", help="Apply immediately if no errors")

    ap = sub.add_parser("apply", help="Apply the validated plan")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without making API calls")
    ap.add_argument("--resume", action="store_true", help="Resume from a partial-failure plan")
    ap.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if args.command == "pull":
        return cmd_pull(cfg, args)
    if args.command == "inspect":
        return cmd_inspect(cfg, args)
    if args.command == "rebind":
        return cmd_rebind(cfg, args)
    if args.command == "validate":
        return cmd_validate(cfg, args)
    if args.command == "apply":
        return cmd_apply(cfg, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
