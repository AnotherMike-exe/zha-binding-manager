"""Unit tests for the transport-independent diff / resolve engine.

These cover the logic that decides what bind/unbind/group actions a plan
contains — the part that must stay correct regardless of the ZHA/WS layer.
Run with:  uv run --extra dev pytest   (or: PYTHONPATH=src pytest)
"""

from zha_binding_manager import manager as m


# --- small helpers -----------------------------------------------------------
def _pull(devices=None, groups=None):
    return {"_meta": {}, "devices": devices or [], "groups": groups or []}


def _edit(devices=None, groups=None, pull_file="pulls/x.json"):
    return {"_meta": {"pull_file": pull_file}, "devices": devices or [], "groups": groups or []}


def _dev(ieee, name, bindings, **kw):
    d = {"ieee": ieee, "name": name, "available": True, "model": "", "bindings": bindings}
    d.update(kw)
    return d


def _bind(ep, cid, dtype, did, name="", cname=None):
    return {"endpoint": ep, "cluster_id": cid, "cluster_name": cname,
            "destination_type": dtype, "destination_id": did, "destination_name": name}


# --- id / cluster helpers ----------------------------------------------------
def test_parse_int_forms():
    assert m.parse_int(6) == 6
    assert m.parse_int("6") == 6
    assert m.parse_int("0x0006") == 6
    assert m.parse_int(None) is None
    assert m.parse_int(True) is None  # bools are not ints here


def test_ieee_bytes_little_endian():
    raw = [214, 135, 27, 254, 255, 39, 135, 4]
    assert m.ieee_bytes_to_str(raw) == "04:87:27:ff:fe:1b:87:d6"


# --- binding add / remove ----------------------------------------------------
def test_bind_and_unbind_detected():
    pull = _pull([_dev("aa", "Sw", [_bind(1, 6, "group", "0x0001", "K")])],
                 [{"group_id": "0x0001", "name": "K", "members": []},
                  {"group_id": "0x0002", "name": "K2", "members": []}])
    edit = _edit([_dev("aa", "Sw", [_bind(1, 8, "group", "0x0002", "K2")])],
                 pull["groups"])
    plan = m.diff(pull, edit)
    acts = {(a["action"], a["cluster_id"]) for a in plan.actions}
    assert ("unbind", 6) in acts   # old OnOff→K removed
    assert ("bind", 8) in acts     # new Level→K2 added
    assert not plan.errors


def test_unchanged_binding_is_noop():
    b = [_bind(1, 6, "group", "0x0001", "K")]
    pull = _pull([_dev("aa", "Sw", list(b))], [{"group_id": "0x0001", "name": "K", "members": []}])
    edit = _edit([_dev("aa", "Sw", list(b))], pull["groups"])
    plan = m.diff(pull, edit)
    assert plan.actions == []


# --- validation errors -------------------------------------------------------
def test_unknown_cluster_errors_with_hint():
    pull = _pull([_dev("aa", "Sw", [])], [{"group_id": "0x0001", "name": "K", "members": []}])
    edit = _edit([_dev("aa", "Sw", [{"endpoint": 1, "cluster_name": "LevelCtrl",
                                     "destination_type": "group", "destination_name": "K"}])],
                 pull["groups"])
    plan = m.diff(pull, edit)
    assert any("not recognized" in e and "LevelControl" in e for e in plan.errors)


def test_cluster_name_id_conflict_errors():
    pull = _pull([_dev("aa", "Sw", [])], [{"group_id": "0x0001", "name": "K", "members": []}])
    edit = _edit([_dev("aa", "Sw", [{"endpoint": 1, "cluster_id": 6, "cluster_name": "LevelControl",
                                     "destination_type": "group", "destination_name": "K"}])],
                 pull["groups"])
    plan = m.diff(pull, edit)
    assert any("conflict" in e.lower() for e in plan.errors)


def test_missing_ieee_errors():
    pull = _pull([_dev("aa", "Sw", [])])
    edit = _edit([_dev("bb", "Ghost", [])])
    plan = m.diff(pull, edit)
    assert any("not found" in e for e in plan.errors)


def test_destination_name_resolves_to_group_id():
    pull = _pull([_dev("aa", "Sw", [])], [{"group_id": "0x000B", "name": "Kitchen", "members": []}])
    edit = _edit([_dev("aa", "Sw", [{"endpoint": 1, "cluster_name": "OnOff",
                                     "destination_type": "group", "destination_name": "Kitchen"}])],
                 pull["groups"])
    plan = m.diff(pull, edit)
    binds = [a for a in plan.actions if a["action"] == "bind"]
    assert len(binds) == 1 and binds[0]["destination_id"] == "0x000B"


# --- destination-blind self-heal --------------------------------------------
def test_self_heal_rebinds_colocated_group_binding():
    # EP2 OnOff has both a coordinator (device) binding and a group binding.
    # Removing the device one would (via binds_remove_all) drop the group one too,
    # so the diff must re-emit the group binding as a bind, ordered after unbinds.
    pull = _pull([_dev("aa", "Sw", [
        _bind(2, 6, "device", "co", "Coordinator"),
        _bind(2, 6, "group", "0x000B", "Kitchen"),
    ])], [{"group_id": "0x000B", "name": "Kitchen", "members": []}])
    edit = _edit([_dev("aa", "Sw", [_bind(2, 6, "group", "0x000B", "Kitchen")])], pull["groups"])
    plan = m.diff(pull, edit)
    ordered = m.order_actions(plan.actions)
    kinds = [a["action"] for a in ordered]
    assert kinds == ["unbind", "bind"]           # unbind first, then re-bind
    assert ordered[1]["destination_id"] == "0x000B"
    assert any("destination-blind" in w for w in plan.warnings)


# --- rebind group auto-resolution -------------------------------------------
def test_resolve_switch_group_longest_prefix():
    groups = [{"group_id": "0x000C", "name": "Living Room Aux"},
              {"group_id": "0x0004", "name": "Living Room TV"},
              {"group_id": "0x0009", "name": "Stairs"}]
    aux = {"name": "Living Room Aux - Light Switch - A"}
    tv = {"name": "Living Room TV - Light Switch"}
    assert m.resolve_switch_group(aux, groups, None) == ("0x000C", "Living Room Aux")
    assert m.resolve_switch_group(tv, groups, None) == ("0x0004", "Living Room TV")


def test_resolve_switch_group_requires_word_boundary():
    groups = [{"group_id": "0x0001", "name": "Kitchen"}]
    assert m.resolve_switch_group({"name": "Kitchenette - Switch"}, groups, None) is None
    assert m.resolve_switch_group({"name": "Kitchen - Switch"}, groups, None) == ("0x0001", "Kitchen")


def test_is_switch_and_is_fan():
    assert m.is_switch({"model": "VZM31-SN", "name": "X"})
    assert m.is_switch({"model": "", "name": "Foo Light Switch"})
    assert not m.is_switch({"model": "AE 270 T", "name": "Innr CCT"})
    assert m.is_fan({"name": "Living Room - Fan Switch"})
    assert not m.is_fan({"name": "Living Room - Light Switch"})
