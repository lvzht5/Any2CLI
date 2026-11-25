"""Import GUIrilla state graphs or named snapshots gathered by an external agent."""

from collections import defaultdict

from any2cli import caption, identity, recorded_press

from .catalog import CatalogError, Frame, empty_catalog, operation, remember, transition
from .surfaces import Node, ROLES, extract


def graph_frame(state):
    if not isinstance(state, dict):
        raise CatalogError("Expected a GUIrilla state object.")
    root = state.get("element")
    edges = state.get("edges")
    actions = state.get("actions", [])
    if not isinstance(root, dict) or not isinstance(edges, list) or not isinstance(actions, list):
        raise CatalogError("Expected a GUIrilla state with element and edges.")
    pressed = set()
    if any(not isinstance(e, dict) or not isinstance(e.get("out_vertice"), dict) for e in edges):
        raise CatalogError("Expected a GUIrilla edge with a destination state.")
    for action in [*actions, *(edge.get("action") for edge in edges)]:
        if not isinstance(action, dict) or not isinstance(action.get("names"), list) or not isinstance(action.get("element"), dict):
            raise CatalogError("Expected a GUIrilla action with target and names.")
        if recorded_press(action):
            pressed.add(identity(action["element"]))
    nodes = []
    paths = defaultdict(list)
    stack = [(root, (0,))]
    while stack:
        e, path = stack.pop()
        if not isinstance(e, dict):
            raise CatalogError("Expected an AX element object.")
        role, name, identifier = identity(e)
        if role == "AXSecureTextField" or e.get("subrole") == "AXSecureTextField":
            continue
        ref = (role, name, identifier)
        if identifier:
            paths[ref].append(path)
        nodes.append(Node(path, ROLES.get(role, role), name, e.get("value"), e.get("enabled") is not False,
                          selected=e.get("selected") is True, pressable=ref in pressed and role != "AXStaticText"))
        children = e.get("children", [])
        if not isinstance(children, list):
            raise CatalogError("Expected a list of AX children.")
        stack.extend((children[i], (*path, i)) for i in reversed(range(len(children))))
    frame, targets = extract(caption(root) or root["role"], nodes)
    identified = {ref: targets[ps[0]] for ref, ps in paths.items() if len(ps) == 1 and ps[0] in targets}
    return frame, identified


def import_graph(app, graph):
    cat = empty_catalog(app)
    cat["sources"] = ["guirilla"]
    todo = [(graph, "#", None)]
    states = edges = 0
    while todo:
        raw, pointer, incoming = todo.pop()
        frame, identified = graph_frame(raw)
        state = remember(cat, frame)
        states += 1
        if incoming:
            parent, key, value, label, evidence = incoming
            op = cat["operations"].get(key, {})
            nav = (op.get("kind") == "choice" and frame.values.get(key) == value
                   and cat["states"][parent]["selected"].get(key) != value
                   and op["role"] in {"AXTabGroup", "AXScrollArea", "AXList", "AXOutline"})
            transition(cat, parent, state, key, value, navigation=nav, evidence=evidence, label=label)
        for i in reversed(range(len(raw["edges"]))):
            edge = raw["edges"][i]
            action = edge["action"]
            ref = identity(action["element"])
            key, value = identified.get(ref, (None, None))
            if (key not in frame.ops or frame.ops[key]["kind"] not in {"press", "choice"}
                    or not recorded_press(action)):
                key, value = None, None
            ep = f"{pointer}/edges/{i}"
            todo.append((edge["out_vertice"], ep + "/out_vertice", (state, key, value, ref[1] or ref[0], ep)))
            edges += 1
    return cat, {"observations": states, "recorded_actions": edges}


def import_trace(app, trace):
    if not isinstance(trace, dict) or trace.get("format") != "any2cli.trace.v1" or trace.get("application") != app:
        raise CatalogError("Expected an any2cli.trace.v1 for the selected application.")
    if (not isinstance(trace.get("states"), list) or not isinstance(trace.get("transitions"), list)
            or not isinstance(trace.get("common", []), list)):
        raise CatalogError("Trace states, common controls and transitions must be lists.")
    cat = empty_catalog(app)
    cat["sources"] = ["agent-trace"]
    labels = {}
    frames = {}
    for state in trace["states"]:
        if (not isinstance(state, dict) or not isinstance(state.get("id"), str) or not state["id"]
                or not isinstance(state.get("title"), str) or not isinstance(state.get("controls"), list)):
            raise CatalogError("Expected a state ID, window title and list of controls.")
        label = state["id"]
        if label in labels:
            raise CatalogError("Duplicate trace state ID.")
        frame = Frame(state["title"])
        for item in [*trace.get("common", []), *state["controls"]]:
            if not isinstance(item, dict):
                raise CatalogError("Expected a trace control object.")
            if item.get("kind") in {"algorithm", "library-read"}:
                raise CatalogError("Service operations must be discovered from the installed provider.")
            op = operation(item["kind"], item["role"], item["name"], item.get("scope", []),
                           options=item.get("options", []), help_text=item.get("help", ""))
            selected = item.get("selected")
            if selected is not None:
                if op["kind"] == "choice" and selected not in op["options"]:
                    raise CatalogError("The selected trace value is not one of the visible choices.")
                if op["kind"] in {"toggle", "expand", "menu"} and not isinstance(selected, bool):
                    raise CatalogError("Boolean selector state expected.")
            frame.add(op, selected if op["kind"] in {"menu", "choice", "toggle", "expand"} else None)
        labels[label] = remember(cat, frame)
        frames[label] = frame
    if trace["root"] not in labels:
        raise CatalogError("Root state missing from trace.")
    cat["root"] = labels[trace["root"]]
    cat["projections"] = sorted(set(labels.values()))
    switch_routes = set()
    for i, edge in enumerate(trace["transitions"]):
        if not isinstance(edge, dict) or not isinstance(edge.get("control"), dict):
            raise CatalogError(f"Expected a control selector in trace transition {i}.")
        source, target = edge["from"], edge["to"]
        if source not in labels or target not in labels:
            raise CatalogError(f"Unknown source or destination in trace transition {i}.")
        selector = edge["control"]
        wanted = (selector["kind"], selector["name"], selector.get("scope", []))
        matches = [op for op in frames[source].ops.values() if (op["kind"], op["name"], op["scope"]) == wanted]
        if len(matches) != 1:
            raise CatalogError(f"Trace transition {i} has no unique source control.")
        op = matches[0]
        value = edge.get("value")
        if op["kind"] == "choice" and value not in op["options"]:
            raise CatalogError(f"Invalid choice in trace transition {i}.")
        if op["kind"] in {"expand", "toggle"} and not isinstance(value, bool):
            raise CatalogError(f"Expected a boolean in trace transition {i}.")
        if op["kind"] in {"menu", "press", "read"} and value is not None:
            raise CatalogError(f"Unexpected argument in trace transition {i}.")
        if op["kind"] == "toggle" and edge.get("navigation") is True:
            old = frames[source].values.get(op["id"])
            new = frames[target].values.get(op["id"])
            if old is (not value) and new is value:
                switch_routes.add((labels[source], labels[target], op["id"], value))
        can_navigate = op["kind"] in {"menu", "choice", "expand", "press"}
        transition(cat, labels[source], labels[target], op["id"], None if op["kind"] == "field" else value,
                   navigation=can_navigate and edge.get("navigation") is True, evidence=f"#/transitions/{i}")
    # A switch is eligible as an incidental navigation step only when the
    # recording opts in and observes both changes, including the return.
    for edge in cat["transitions"]:
        src, dst, key, val = (edge[k] for k in ("from", "to", "operation", "value"))
        if (src, dst, key, val) in switch_routes and (dst, src, key, not val) in switch_routes:
            edge["navigation"] = True
    return cat, {"observations": len(labels), "recorded_actions": len(trace["transitions"])}
