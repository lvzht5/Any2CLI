"""Bounded exploration, path planning and lazy execution."""

import json
import time

from macos_openadapt import AdapterError

from .catalog import (CatalogError, DISCRETE, WINDOW_MODES, empty_catalog, inverse_name, navigation_id,
                      remember, route, schema, transition, transition_outcome)


def state_id(frame):
    return navigation_id(frame.state())


def window_mode_changed(op, frame):
    inverse = WINDOW_MODES.get(op["name"].casefold())
    return op["id"] not in frame.ops and any(other["kind"] == "menu" and other["scope"] == op["scope"]
            and other["name"].casefold() == inverse for other in frame.ops.values())


def wait_for(driver, predicate, timeout, *, stable=False):
    deadline = time.monotonic() + timeout
    previous = None
    first_match = None
    while True:
        try:
            frame = driver.observe()
        except AdapterError as exc:
            remaining = deadline - time.monotonic()
            if exc.report.get("reason") not in {"tree_incomplete", "window_mode_transition"} or remaining <= 0:
                raise
            time.sleep(min(remaining, 0.15))
            continue
        now = time.monotonic()
        remaining = deadline - now
        matched = predicate(frame)
        if matched and stable and timeout:
            signature = state_id(frame)
            if previous != signature:
                previous, first_match = signature, now
            matched = now - first_match >= .45
            if not matched and remaining <= 0:
                raise CatalogError("The selected state did not stay stable before the timeout.", status="unverified")
        elif not matched:
            previous = None
        if matched or remaining <= 0:
            return frame
        time.sleep(min(remaining, 0.15))


BACK = {"back", "go back", "previous page", "返回", "后退", "上一页", "返回上一级"}
PANELS = {"details", "show details", "filters", "show filters", "advanced options", "more options",
          "详细信息", "显示详细信息", "筛选", "筛选条件", "高级选项", "更多选项"}


def return_control(frame, origin):
    controls = [*frame.ops.values(), *frame.ambiguous.values()]
    inverse = inverse_name(origin["name"])
    if inverse:
        matches = [op["id"] for op in controls if op["name"].casefold() == inverse
                   and (op["kind"], op["scope"]) == (origin["kind"], origin["scope"])]
        if matches or origin["kind"] == "menu":
            if origin["id"] in frame.ops or origin["id"] in frame.ambiguous:
                return None
            return matches[0] if len(matches) == 1 and matches[0] in frame.ops else None
    matches = [op["id"] for op in controls if op["kind"] in {"press", "menu"} and op["name"].casefold() in BACK]
    return matches[0] if len(matches) == 1 and matches[0] in frame.ops else None


def explore_live(app, driver, *, steps, depth, timeout, follow=()):
    cat = empty_catalog(app)
    cat["sources"] = ["native"]
    initial = driver.observe()
    start = remember(cat, initial)
    tried = set()
    visited = {}
    requested = {s.strip().casefold() for s in follow}
    found = set()
    report = {"probes": 0, "return_actions": 0, "no_change": 0, "restored": True, "issues": []}
    if steps == 0 and not requested:
        report.update(notes=sorted(getattr(driver, "notes", set())), step_limit_reached=True)
        return cat, report

    def visit(frame, level):
        source = remember(cat, frame)
        if level >= depth or visited.get(source, depth + 1) <= level:
            return
        visited[source] = level
        directed = []
        panels = []
        structural = {p[0] for p in frame.probes}
        for key, op in frame.ambiguous.items():
            labels = {key, op["name"].casefold(), " / ".join([*op["scope"], op["name"]]).casefold()}
            for query in requested:
                if query in labels or (op["kind"] == "choice" and query.partition("=")[0] in labels):
                    raise CatalogError("The requested exploration control is ambiguous.", status="ambiguous",
                                       query=query, candidates=[key])
        for key, op in frame.ops.items():
            labels = {key, op["name"].casefold(), " / ".join([*op["scope"], op["name"]]).casefold()}
            for query in sorted(requested):
                selector, delimiter, wanted = query.partition("=")
                qualified = op["kind"] == "choice" and delimiter and selector in labels
                if query not in labels and not qualified:
                    continue
                control = selector if qualified else query
                if control != key and sum(control in {o["name"].casefold(),
                        " / ".join([*o["scope"], o["name"]]).casefold()}
                        for o in (*frame.ops.values(), *frame.ambiguous.values())) != 1:
                    raise CatalogError("The requested exploration control is ambiguous.", status="ambiguous", query=query)
                if qualified:
                    matches = [v for v in op["options"] if v.casefold() == wanted]
                    current = frame.values.get(key)
                    if len(matches) != 1 or current not in op["options"]:
                        raise CatalogError("Cannot uniquely identify the current or requested choice.", operation=key)
                    if matches[0] == current:
                        found.add(query)
                        continue
                    probe = (key, matches[0], key, current)
                elif op["kind"] in {"toggle", "expand"}:
                    current = frame.values.get(key)
                    if not isinstance(current, bool):
                        raise CatalogError("Cannot probe a switch whose current value is unreadable.", operation=key)
                    probe = (key, not current, key, current)
                elif op["kind"] in {"press", "menu"}:
                    probe = next((p for p in frame.probes if p[0] == key), (key, None, None, None))
                else:
                    raise CatalogError("--follow accepts a button, menu, readable switch or selector=value.", operation=key)
                found.add(query)
                directed.append(probe)
            if op["kind"] == "press" and op["name"].casefold() in PANELS and key not in structural:
                panels.append((key, None, None, None))
        for key, value, back, old in [*directed, *frame.probes, *panels]:
            attempt = (source, key, value)
            if report["probes"] >= steps:
                return
            if attempt in tried or key not in frame.ops or (back is not None and back not in frame.ops):
                continue
            tried.add(attempt)
            before = driver.observe()
            if state_id(before) != source or before.ambiguous.keys() != frame.ambiguous.keys():
                raise CatalogError("The window changed outside the exploration path.", status="state_changed")
            report["probes"] += 1
            try:
                driver.perform(before, key, value)
            except AdapterError as exc:
                if exc.report["status"] == "foreground_required":
                    report["probes"] -= 1
                    if (key, value, back, old) in directed:
                        raise
                    skipped = report.setdefault("foreground_operations_skipped", [])
                    if key not in skipped:
                        skipped.append(key)
                    continue
                if exc.report.get("missing") != ["screen_recording"]:
                    raise
                # A different control may support AXPress or AXSelected.
                if exc.report not in report["issues"]:
                    report["issues"].append(exc.report)
                continue
            checks_value = frame.ops[key]["kind"] in DISCRETE
            visual = frame.ops[key]["role"] in {"visual-sidebar", "visual-tabs"}
            after = wait_for(driver, lambda f: f.values.get(key) == value if checks_value
                             else state_id(f) != source, timeout, stable=visual)
            dest = remember(cat, after)
            if dest == source:
                report["no_change"] += 1
                transition(cat, source, dest, key, value, outcome="unverified")
                continue
            # Record the observed edge even if the return route cannot be verified.
            if checks_value and after.values.get(key) != value:
                transition(cat, source, dest, key, value, outcome="unverified")
                raise CatalogError("The probed selector did not report the requested value.", status="unverified",
                                   operation=key)
            transition(cat, source, dest, key, value, outcome="verified" if checks_value else "observed")
            if back is None:
                back = return_control(after, frame.ops[key])
            if back is None:
                raise CatalogError("Observed a new state without a unique return control.", status="restore_failed",
                                   state=dest)
            # The return leg will be observed below; avoid probing it twice.
            tried.add((dest, back, old))
            # Only start the return after the nested probe finished normally.
            # An interruption or loss of focus cancels the remaining input.
            visit(after, level + 1)
            current = driver.observe()
            current_id = remember(cat, current)
            same_choice = (key == back and frame.ops[key]["kind"] == "choice" and key in current.ops
                           and current.values.get(key) == value and old in current.ops[key]["options"])
            if (current_id == dest or same_choice) and back in current.ops:
                report["return_actions"] += 1
                driver.perform(current, back, old)
                restored = wait_for(driver, lambda f: state_id(f) == source, timeout, stable=visual)
                returned = remember(cat, restored)
                transition(cat, current_id, returned, back, old, navigation=returned == source,
                           outcome="verified" if returned == source else "unverified")
                if returned != source:
                    raise CatalogError("The return action reached a different state.", status="restore_failed")
                if current_id == dest:
                    transition(cat, source, dest, key, value, navigation=True)
            elif current_id != source:
                raise CatalogError("The original state could not be restored.", status="restore_failed")

    try:
        visit(initial, 0)
    except KeyboardInterrupt:
        report["issues"].append({"status": "interrupted", "message": "Exploration was interrupted."})
    except (CatalogError, AdapterError, RuntimeError, OSError) as exc:
        report["issues"].append(getattr(exc, "report", {"message": str(exc)}))
    try:
        report["restored"] = remember(cat, driver.observe()) == start
    except (CatalogError, RuntimeError, OSError):
        report["restored"] = False
    report["notes"] = sorted(getattr(driver, "notes", set()))
    if requested - found:
        report["issues"].append({"status": "not_found", "follow": sorted(requested - found)})
    report["step_limit_reached"] = report["probes"] >= steps
    return cat, report


def input_value(op, value):
    kind = op["kind"]
    if kind == "algorithm":
        try:
            inputs = {} if value is None else json.loads(value)
        except (TypeError, ValueError):
            inputs = None
        if not isinstance(inputs, dict):
            raise CatalogError("Supply --value as a JSON object of QGIS parameters.", status="invalid_input")
        if "parameters" in op:
            contract = schema(op)
            unknown = sorted(inputs.keys() - contract["properties"].keys())
            if unknown:
                raise CatalogError("Unknown QGIS parameter names.", status="invalid_input", parameters=unknown)
            missing = [name for name in contract["required"] if inputs.get(name) is None]
            if missing:
                raise CatalogError("Missing mandatory QGIS parameters.", status="invalid_input", parameters=missing)
        return inputs
    if kind in {"choice", "field"}:
        if not isinstance(value, str) or (kind == "choice" and value not in op["options"]):
            raise CatalogError("Supply --value with an allowed parameter.", status="invalid_input",
                               options=op.get("options"))
        return value
    if kind in {"toggle", "expand"}:
        if value not in ("true", "false"):
            raise CatalogError("Supply --value true or --value false.", status="invalid_input")
        return value == "true"
    if value is not None:
        raise CatalogError("This operation takes no value.", status="invalid_input")
    return None


def outcomes(cat, state, key, value, *, accepted=("verified",)):
    return {e["to"] for e in cat["transitions"] if
            (e["from"], e["operation"], e["value"]) == (state, key, value)
            and (accepted is None or transition_outcome(e) in accepted)}


def menu_effect_present(cat, state, op):
    """Only skip a menu action at the destination of its verified round trip."""
    inverse = inverse_name(op["name"])
    key = op["id"]
    here = cat["states"][state]["operations"]
    if not inverse or key in here:
        return False
    for back in cat["transitions"]:
        if back["from"] != state or not back["navigation"] or back["operation"] not in here:
            continue
        peer = cat["operations"][back["operation"]]
        if peer["kind"] != "menu" or peer["scope"] != op["scope"] or peer["name"].casefold() != inverse:
            continue
        source = back["to"]
        before = cat["states"][source]["operations"]
        if (key in before and peer["id"] not in before
                and outcomes(cat, state, peer["id"], None, accepted=None) == {source}
                and outcomes(cat, source, key, None, accepted=None) == {state}
                and any(e["from"] == source and e["operation"] == key and e["navigation"]
                        for e in cat["transitions"])):
            return True
    return False


def live_key(op, frame, *, strict=False):
    if op["id"] in frame.ops:
        return op["id"]
    # Agent traces may omit unnamed or outer containers. A named scope must
    # still match the end of the live scope and the control must be unique.
    scope = op["scope"]

    def matches(live):
        if op["kind"] == "menu":
            return op["id"] == live["id"]
        return ((op["kind"], op["role"], op["name"]) == (live["kind"], live["role"], live["name"])
                and (not scope or live["scope"][-len(scope):] == scope))

    duplicates = [key for key, item in frame.ambiguous.items() if matches(item)]
    candidates = [key for key, item in frame.ops.items() if matches(item)]
    if duplicates or len(candidates) > 1:
        if strict:
            raise CatalogError("Several live controls match this operation.", status="ambiguous",
                               operation=op["id"], candidates=sorted(duplicates + candidates))
        return None
    return candidates[0] if candidates else None


def renamed_state(cat, frame):
    """Recognize a different document only by a unique, complete page structure."""
    if not frame.ops or frame.ambiguous:
        return None
    surface = frame.state()
    titles = {state["title"] for state in cat["states"].values()} - {frame.title}
    candidates = [navigation_id({**surface, "title": title}) for title in titles]
    matches = [key for key in candidates if key in cat["states"]]
    return matches[0] if len(matches) == 1 else None


def projected_state(cat, frame):
    compatible = {}
    for sid in cat.get("projections", []):
        state = cat["states"][sid]
        if state["title"] != frame.title or not state["operations"]:
            continue
        bindings = {k: live_key(cat["operations"][k], frame) for k in state["operations"]}
        if None in bindings.values() or len(set(bindings.values())) != len(bindings):
            continue
        if any(frame.values.get(bindings[k]) != v for k, v in state["selected"].items()):
            continue
        if any(not set(opts).issubset(frame.ops[bindings[k]].get("options", [])) for k, opts in state["choices"].items()):
            continue
        features = {(k,) for k in bindings} | set(state["selected"].items())
        features.update((k, "choice", v) for k, opts in state["choices"].items() for v in opts)
        compatible[sid] = features
    # A base page can be a subset of a more specific panel. Two incomparable
    # matches are ambiguous even when one happens to have more controls.
    winners = [k for k, features in compatible.items()
               if all(k == other or features > required for other, required in compatible.items())]
    return winners[0] if len(winners) == 1 else None


def execute(cat, key, value, *, driver=None, start=None, dry_run=False, timeout=1.5):
    op = cat["operations"][key]
    value = input_value(op, value)
    frame = driver.observe() if driver else None
    if driver and op["role"] in {"visual-sidebar", "visual-tabs", "visual-selection"} and not any(
            o["role"] == "visual-sidebar" for o in frame.ops.values()):
        frame = wait_for(driver, lambda f: any(o["role"] == "visual-sidebar" for o in f.ops.values()), min(2, timeout))
    live_title = frame.title if frame is not None else None
    recorded_title = None
    if frame is not None and state_id(frame) not in cat["states"] and not projected_state(cat, frame):
        anchor = renamed_state(cat, frame)
        if anchor:
            recorded_title = cat["states"][anchor]["title"]

    def document_state(observed):
        if recorded_title is not None and observed.title == live_title:
            candidate = renamed_state(cat, observed)
            if candidate and cat["states"][candidate]["title"] == recorded_title:
                return candidate
        return None

    def remember_live(observed):
        return document_state(observed) or remember(cat, observed)

    start = remember_live(frame) if frame is not None else (start or cat["root"])
    direct = live_key(op, frame, strict=True) if frame is not None else None
    visible = direct and (op["kind"] != "choice" or value in frame.ops[direct]["options"])
    if (op["kind"] == "menu" and not visible and start in cat["states"]
            and (frame is None or key not in frame.ambiguous)):
        anchors = [start]
        if frame is not None:
            anchor = projected_state(cat, frame)
            if anchor and anchor != start:
                anchors.append(anchor)
        if any(menu_effect_present(cat, sid, op) for sid in anchors):
            mode = "plan" if dry_run else "live" if frame is not None else "recording"
            return {"start": start, "operation": key, "target": op["name"], "source": start, "route": [],
                    "mode": mode, "status": {"plan": "planned", "recording": "replayed", "live": "verified_ui"}[mode],
                    "destination": start, "already_selected": True}
    if visible:
        path = []
    else:
        try:
            path = route(cat, start, key, value)
        except CatalogError as exc:
            anchor = projected_state(cat, frame) if frame is not None else None
            if frame is not None and not anchor and recorded_title is None:
                anchor = renamed_state(cat, frame)
                if anchor:
                    recorded_title = cat["states"][anchor]["title"]
            if exc.report["status"] != "no_route" or not anchor or anchor == start:
                if driver and op["role"] in {"visual-sidebar", "visual-tabs", "visual-selection"}:
                    exc.report["observation_notes"] = sorted(getattr(driver, "notes", set()))
                raise
            start = anchor
            path = route(cat, start, key, value)
    terminal = path[-1]["to"] if path else start
    result = {"start": start, "operation": key, "target": op["name"],
              "route": [{**e, "name": cat["operations"][e["operation"]]["name"]} for e in path], "source": terminal}
    if dry_run:
        return {**result, "status": "planned", "mode": "plan"}
    if driver is None:
        result["mode"] = "recording"
        if op["kind"] in DISCRETE and cat["states"][terminal]["selected"].get(key) == value:
            return {**result, "status": "replayed", "destination": terminal, "already_selected": True}
        targets = outcomes(cat, terminal, key, value, accepted=("observed", "verified"))
        if op["kind"] in {"field", "read"} or len(targets) != 1:
            return {**result, "status": "not_recorded" if len(targets) <= 1 else "ambiguous_outcome"}
        dest = next(iter(targets))
        if op["kind"] in DISCRETE and cat["states"][dest]["selected"].get(key) != value:
            return {**result, "status": "unverified", "destination": dest}
        return {**result, "status": "replayed", "destination": dest, "state_changed": terminal != dest}

    result["mode"] = "live"
    if path and hasattr(driver, "check_route"):
        driver.check_route([cat["operations"][edge["operation"]] for edge in path], op)

    def at(expected, observed):
        return (state_id(observed) == expected or document_state(observed) == expected
                or (expected in cat.get("projections", []) and projected_state(cat, observed) == expected))

    for edge in path:
        nav_key = live_key(cat["operations"][edge["operation"]], frame, strict=True)
        if not nav_key or not at(edge["from"], frame):
            return {**result, "status": "route_changed", "at": state_id(frame)}
        previous = remember_live(frame)
        driver.perform(frame, nav_key, edge["value"])
        try:
            frame = wait_for(driver, lambda f: at(edge["to"], f), timeout,
                             stable=cat["operations"][edge["operation"]]["role"] in {"visual-sidebar", "visual-tabs"})
        except (CatalogError, RuntimeError, OSError) as exc:
            return {**result, "status": "unverified", "phase": "navigation", "message": str(exc)}
        actual = remember_live(frame)
        success = at(edge["to"], frame)
        transition(cat, previous, actual, nav_key, edge["value"], navigation=success,
                   outcome="verified" if success else "unverified")
        if not success:
            return {**result, "status": "route_changed", "at": actual}
    bound = live_key(op, frame, strict=True)
    if not bound or (op["kind"] == "choice" and value not in frame.ops[bound]["options"]):
        return {**result, "status": "route_changed", "at": state_id(frame)}
    if op["kind"] == "read":
        if frame.values.get(bound) is None:
            return {**result, "status": "unverified"}
        return {**result, "status": "read", "value": frame.values[bound]}
    checks_value = op["kind"] in DISCRETE or op["kind"] == "field"
    if op["kind"] in DISCRETE and bound not in frame.values:
        raise CatalogError("The current selector value is unreadable.")
    if checks_value and frame.values.get(bound) == value:
        return {**result, "status": "verified_ui", "already_selected": True, "destination": terminal}
    native_source = remember_live(frame)
    anchor = projected_state(cat, frame)
    expected = outcomes(cat, native_source, bound, value) - {native_source}
    if anchor:
        expected |= outcomes(cat, anchor, key, value) - {anchor}
    driver.perform(frame, bound, value)

    def matches(after):
        if checks_value:
            return after.values.get(live_key(op, after)) == value
        if op["kind"] == "menu" and op["name"].casefold() in WINDOW_MODES:
            return window_mode_changed(op, after)
        return any(at(s, after) for s in expected) if expected else state_id(after) != state_id(frame)

    try:
        observed = wait_for(driver, matches, timeout, stable=op["role"] in {"visual-sidebar", "visual-tabs"})
    except (CatalogError, RuntimeError, OSError) as exc:
        return {**result, "status": "unverified", "phase": "postcondition", "message": str(exc)}
    dest = remember_live(observed)
    # Never persist free-form text arguments or field readbacks.
    stored_value = value if op["kind"] != "field" else None
    if not matches(observed):
        status, outcome = "unverified", "unverified"
    elif checks_value or expected or (op["kind"] == "menu" and op["name"].casefold() in WINDOW_MODES):
        status, outcome = "verified_ui", "verified"
    else:
        status, outcome = "observed_ui_change", "observed"
    transition(cat, native_source, dest, bound, stored_value, outcome=outcome)
    return {**result, "status": status, "destination": dest, "state_changed": native_source != dest}
