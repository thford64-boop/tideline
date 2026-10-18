"""Panel layouts: which panels sit where, as plain data.

A layout is a tree. A leaf is a group of tabbed panels,
``{"tabs": [panel, ...], "active": panel}``; a split puts two nodes side by
side ("h") or one above the other ("v"), the first taking ``pos`` (0..1) of
the room: ``{"split": "h", "pos": 0.4, "a": node, "b": node}``. A workspace's
state is its main tree plus panels floating in windows of their own:
``{"tree": node, "floating": [{"tree": node, "size": [w, h]}]}``. A panel in
neither is closed until it is shown again. Every function returns a new
state and leaves its input alone; nothing here touches GTK.
"""

import copy

PANELS = {"source": "Source", "program": "Program", "timeline": "Timeline", "project": "Project",
          "effect_controls": "Effect Controls", "effects": "Effects", "mixer": "Audio Mixer", "scopes": "Scopes",
          "multicam": "Multi-Camera", "history": "History", "info": "Info"}
ZONES = ("center", "left", "right", "top", "bottom")
MIN_POS, MAX_POS = 0.06, 0.94


def leaf(*tabs, active=None):
    return {"tabs": list(tabs), "active": active or (tabs[0] if tabs else None)}


def split(kind, pos, a, b):
    return {"split": kind, "pos": pos, "a": a, "b": b}


def _workspace(top, left, bottom_left, upper, lower):
    return {"tree": split("v", top,
                          split("h", left, leaf("source", "effect_controls", "mixer", "multicam", active=upper),
                                leaf("program")),
                          split("h", bottom_left, leaf("project", "effects", "scopes", "history", "info", active=lower),
                                leaf("timeline"))),
            "floating": []}


DEFAULTS = {
    "editing": _workspace(0.50, 0.42, 0.26, "source", "project"),
    "effects": _workspace(0.50, 0.36, 0.24, "effect_controls", "effects"),
    "color": _workspace(0.52, 0.40, 0.30, "effect_controls", "scopes"),
    "audio": _workspace(0.42, 0.36, 0.22, "mixer", "effects"),
}


def default(workspace):
    return copy.deepcopy(DEFAULTS.get(workspace, DEFAULTS["editing"]))


def is_leaf(node):
    return isinstance(node, dict) and "tabs" in node


# -- queries ---------------------------------------------------------------------
def panels(node):
    """Panels in a tree, in order."""
    if node is None:
        return []
    if is_leaf(node):
        return list(node["tabs"])
    return panels(node["a"]) + panels(node["b"])


def all_panels(state):
    out = panels(state.get("tree"))
    for win in state.get("floating", []):
        out += panels(win["tree"])
    return out


def leaves(node):
    if node is None:
        return []
    if is_leaf(node):
        return [node]
    return leaves(node["a"]) + leaves(node["b"])


def leaf_of(state, panel):
    """The leaf holding ``panel`` (the state's own dict), or None."""
    for root in [state.get("tree")] + [w["tree"] for w in state.get("floating", [])]:
        for lf in leaves(root):
            if panel in lf["tabs"]:
                return lf
    return None


def floating_index(state, panel):
    """Index of the floating window holding ``panel``, or None."""
    for i, win in enumerate(state.get("floating", [])):
        if panel in panels(win["tree"]):
            return i
    return None


# -- edits -------------------------------------------------------------------------
def _without(node, panel):
    """``node`` without ``panel``; empty groups disappear and a split left
    with one side becomes that side."""
    if node is None:
        return None
    if is_leaf(node):
        tabs = [t for t in node["tabs"] if t != panel]
        if not tabs:
            return None
        return {"tabs": tabs, "active": node["active"] if node["active"] in tabs else tabs[0]}
    a, b = _without(node["a"], panel), _without(node["b"], panel)
    if a is None or b is None:
        return a if b is None else b
    return dict(node, a=a, b=b)


def remove(state, panel):
    """Close ``panel`` wherever it is."""
    out = {"tree": _without(state.get("tree"), panel), "floating": []}
    for win in state.get("floating", []):
        tree = _without(win["tree"], panel)
        if tree is not None:
            out["floating"].append(dict(win, tree=tree))
    return out


def _insert(node, target, panel, zone):
    """``node`` with ``panel`` added next to the group holding ``target``."""
    if is_leaf(node):
        if target not in node["tabs"]:
            return node
        if zone == "center":
            return {"tabs": node["tabs"] + [panel], "active": panel}
        new = leaf(panel)
        kind = "h" if zone in ("left", "right") else "v"
        first = zone in ("left", "top")
        return split(kind, 0.35 if first else 0.65, new if first else node, node if first else new)
    return dict(node, a=_insert(node["a"], target, panel, zone), b=_insert(node["b"], target, panel, zone))


def dock(state, panel, target, zone="center"):
    """Move ``panel`` into the group holding ``target`` (``zone`` "center"),
    or beside it ("left", "right", "top", "bottom")."""
    if zone not in ZONES or panel == target and zone == "center":
        return copy.deepcopy(state)
    source = leaf_of(state, panel)
    if source is not None and target in source["tabs"]:
        if zone == "center" or source["tabs"] == [panel]:
            return copy.deepcopy(state)          # already there, or nothing left to split from
    out = remove(state, panel)
    if leaf_of(out, target) is None:
        return copy.deepcopy(state)
    out["tree"] = _insert(out["tree"], target, panel, zone) if out["tree"] is not None else leaf(panel)
    out["floating"] = [dict(w, tree=_insert(w["tree"], target, panel, zone)) for w in out["floating"]]
    return out


def float_panel(state, panel, size=(640, 420)):
    """Take ``panel`` out into a window of its own."""
    index = floating_index(state, panel)
    if index is not None and panels(state["floating"][index]["tree"]) == [panel]:
        return copy.deepcopy(state)              # floating alone already
    out = remove(state, panel)
    out["floating"].append({"tree": leaf(panel), "size": [int(size[0]), int(size[1])]})
    return out


def show(state, panel, workspace="editing"):
    """Bring ``panel`` to the front of its group, docking it back first if it
    is closed: beside a panel it shares a group with in the workspace's
    default layout, or else in the first group."""
    out = copy.deepcopy(state)
    lf = leaf_of(out, panel)
    if lf is not None:
        lf["active"] = panel
        return out
    if out.get("tree") is None:
        out["tree"] = leaf(panel)
        return out
    home = leaf_of(default(workspace), panel)
    partners = [p for p in (home["tabs"] if home else []) if p != panel]
    target = next((p for p in partners if leaf_of(out, p) is not None), None)
    if target is None:
        target = panels(out["tree"])[0]
    return dock(out, panel, target, "center")


def dock_back(state, index, workspace="editing"):
    """Return floating window ``index``'s panels to the main layout."""
    if not 0 <= index < len(state.get("floating", [])):
        return copy.deepcopy(state)
    moving = panels(state["floating"][index]["tree"])
    out = {"tree": copy.deepcopy(state.get("tree")),
           "floating": [copy.deepcopy(w) for i, w in enumerate(state["floating"]) if i != index]}
    for panel in moving:
        out = show(out, panel, workspace)
    return out


def set_pos(state, path, pos):
    """Set the share of the split at ``path`` ("" is the main tree's root,
    "a"/"b" steps down; "f2:ab" is inside floating window 2)."""
    out = copy.deepcopy(state)
    node = _at(out, path)
    if node is not None and not is_leaf(node):
        node["pos"] = max(MIN_POS, min(MAX_POS, float(pos)))
    return out


def _at(state, path):
    if path.startswith("f"):
        index, _, path = path[1:].partition(":")
        try:
            node = state["floating"][int(index)]["tree"]
        except (IndexError, ValueError):
            return None
    else:
        node = state.get("tree")
    for step in path:
        if node is None or is_leaf(node):
            return None
        node = node.get(step)
    return node


# -- loading -------------------------------------------------------------------
def _clean(node, seen, known):
    if not isinstance(node, dict):
        return None
    if "tabs" in node:
        tabs = []
        for t in node.get("tabs") or []:
            if isinstance(t, str) and t in known and t not in seen:
                seen.add(t)
                tabs.append(t)
        if not tabs:
            return None
        return {"tabs": tabs, "active": node.get("active") if node.get("active") in tabs else tabs[0]}
    kind = node.get("split")
    if kind not in ("h", "v"):
        return None
    a, b = _clean(node.get("a"), seen, known), _clean(node.get("b"), seen, known)
    if a is None or b is None:
        return a if b is None else b
    try:
        pos = float(node.get("pos", 0.5))
    except (TypeError, ValueError):
        pos = 0.5
    return split(kind, max(MIN_POS, min(MAX_POS, pos)), a, b)


def sanitize(state, workspace="editing", known=None):
    """A saved state made safe to build: unknown and repeated panels dropped,
    shares kept in range; the default layout if nothing usable is left."""
    known = set(known or PANELS)
    if not isinstance(state, dict):
        return default(workspace)
    seen = set()
    tree = _clean(state.get("tree"), seen, known)
    if tree is None:
        return default(workspace)
    floating = []
    for win in state.get("floating") or []:
        if not isinstance(win, dict):
            continue
        sub = _clean(win.get("tree"), seen, known)
        if sub is None:
            continue
        size = win.get("size") if isinstance(win.get("size"), (list, tuple)) and len(win["size"]) == 2 else [640, 420]
        try:
            size = [max(200, min(8000, int(size[0]))), max(150, min(8000, int(size[1])))]
        except (TypeError, ValueError):
            size = [640, 420]
        floating.append({"tree": sub, "size": size})
    return {"tree": tree, "floating": floating}
