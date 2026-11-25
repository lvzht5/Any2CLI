"""Discover CLI parameters in GUIrilla graphs and run supported desktop adapters."""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


__version__ = "0.5.0"

# StructuralLocator role names used by OpenAdapt's macOS backend.
CHOICE_ROLES = {"AXButton": "button", "AXRadioButton": "radio", "AXMenuItem": "menuitem"}


def caption(element):
    for key in ("description", "name"):
        value = element.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith("empty-name-"):
            return value.strip()
    return ""


def identity(element):
    role, identifier = element.get("role"), element.get("id")
    if not isinstance(role, str) or (identifier is not None and not isinstance(identifier, str)):
        raise ValueError("Expected a GUIrilla element role and an optional string ID")
    return role, caption(element), identifier


def walk_elements(root):
    pending = [root]
    while pending:
        element = pending.pop()
        if not isinstance(element, dict) or not isinstance(element.get("role"), str):
            raise ValueError("Expected an accessibility element with a role")
        children = element.get("children", [])
        if not isinstance(children, list):
            raise ValueError("Expected an accessibility element's children to be a list")
        yield element
        pending.extend(reversed(children))


@dataclass
class ChoiceGroup:
    current: str
    options: dict[str, str]
    targets: set[tuple]


def read_groups(root):
    elements = list(walk_elements(root))
    group_names = Counter((e["role"], caption(e)) for e in elements)
    targets = Counter(identity(e) for e in elements)
    groups = {}
    for element in elements:
        key = (element["role"], caption(element))
        current = element.get("value")
        if not key[1] or group_names[key] != 1 or not isinstance(current, str):
            continue
        members = [identity(c) for c in element.get("children", []) if c["role"] in CHOICE_ROLES]
        options = {label: role for role, label, _ in members}
        if len(options) < 2 or len(options) != len(members) or "" in options or current not in options:
            continue
        unambiguous = {ref for ref in members if ref[2] and targets[ref] == 1}
        groups[key] = ChoiceGroup(current, options, unambiguous)
    return groups


def read_state(node, pointer):
    if not isinstance(node, dict) or not isinstance(node.get("edges"), list) or "element" not in node:
        raise ValueError(f"{pointer}: expected a GUIrilla graph state with 'element' and 'edges'")
    return read_groups(node["element"])


def recorded_press(action):
    names = action["names"]
    if "AXPress" not in names or "MoveCursor" in names:
        return False
    representation = action.get("represent")
    if isinstance(representation, dict) and representation.get("string"):
        event = str(representation["string"]).casefold().split("(", 1)[0].strip()
        return event in {"click", "left click"}
    return True


def discover(graph):
    pending = [(graph, "#", read_state(graph, "#"))]
    candidates = []
    state_count = transition_count = 0
    while pending:
        node, state_path, before = pending.pop()
        state_count += 1
        observations = defaultdict(list)
        for index, edge in enumerate(node["edges"]):
            transition_count += 1
            path = f"{state_path}/edges/{index}"
            if not isinstance(edge, dict) or not isinstance(edge.get("action"), dict):
                raise ValueError(f"{path}: expected an edge containing an action")
            target = edge["action"].get("element")
            if not isinstance(target, dict):
                raise ValueError(f"{path}: expected a recorded target element")
            child = edge.get("out_vertice")
            after = read_state(child, path + "/out_vertice")
            pending.append((child, path + "/out_vertice", after))
            action_names = edge["action"].get("names")
            if not isinstance(action_names, list):
                raise ValueError(f"{path}: expected the recorded action's names to be a list")
            if not recorded_press(edge["action"]):
                continue
            # Compare the same named surface. Element IDs only identify the target
            # in the source snapshot; they are never persisted as replay locators.
            if identity(node["element"])[:2] != identity(child["element"])[:2]:
                continue
            target_ref = identity(target)
            label = target_ref[1]
            for key, group in before.items():
                if target_ref not in group.targets or group.current == label:
                    continue
                result = after.get(key)
                if result and result.current == label and result.options == group.options:
                    observations[key].append({"from": group.current, "to": label, "edge": path,
                                              "locator": {"role": CHOICE_ROLES[target_ref[0]], "name": label}})

        for (role, label), evidence in observations.items():
            observed = list(dict.fromkeys(item["to"] for item in evidence))
            if len(observed) >= 2:
                candidates.append({
                    "status": "candidate",
                    "control_role": role,
                    "source_state": state_path,
                    "parameter": {"title": label, "type": "string", "enum": observed},
                    "visible_options": list(before[role, label].options),
                    "evidence": evidence,
                })
    return {"states": state_count, "transitions": transition_count, "candidates": candidates}


def bind_choice(discovery, title, value):
    matches = [c for c in discovery["candidates"]
               if c["parameter"]["title"] == title and value in c["parameter"]["enum"]]
    if len(matches) != 1:
        raise ValueError(f"Expected one observed candidate for {title!r} = {value!r}, found {len(matches)}")
    candidate = matches[0]
    evidence = [e for e in candidate["evidence"] if e["to"] == value]
    return {"control": {"role": candidate["control_role"], "name": title},
            "target": evidence[0]["locator"], "evidence": [e["edge"] for e in evidence]}


def timeout_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("expected a finite, non-negative number of seconds")
    return seconds


def main(argv=None):
    from any2cli_core import cli

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "app" and not argv[1].startswith("-"):
        return cli.output(cli.app_cli, argv[1], argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"Any2CLI {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    cli.add_commands(commands)
    scan = commands.add_parser("discover", help="Read a GUIrilla graph and print candidate parameters as JSON")
    scan.add_argument("graph", type=Path, help="Path to GUIrilla's graph/data.json")
    bind = commands.add_parser("bind", help="Export one observed choice as a reusable structural locator")
    bind.add_argument("graph", type=Path)
    bind.add_argument("--control", required=True, help="Exact parameter title from discover")
    bind.add_argument("--value", required=True, help="An observed enum value")
    stocks = commands.add_parser("stocks", help="Use the currently displayed Apple Stocks chart")
    stocks.set_defaults(bundle_id="com.apple.stocks")
    operations = stocks.add_subparsers(dest="operation", required=True)
    period = operations.add_parser("period", help="Select a chart period and verify its date samples")
    period.add_argument("value", choices=("1m", "1 month"), help="One calendar month")
    period.add_argument("--window", help="Exact title when Stocks has multiple windows")
    period.add_argument("--check-only", action="store_true", help="Read the current chart once without input")
    period.add_argument("--foreground", action="store_true", help="Allow a physical click in the active Stocks window")
    period.add_argument("--timeout", type=timeout_seconds, default=8.0, metavar="SECONDS",
                        help="How long to wait for the chart to refresh after selection (default: 8)")
    args = parser.parse_args(argv)
    if args.command in ("discover", "bind"):
        try:
            with args.graph.open(encoding="utf-8") as stream:
                result = discover(json.load(stream))
            if args.command == "bind":
                result = bind_choice(result, args.control, args.value)
        except (OSError, ValueError) as exc:
            print(f"any2cli: {exc}", file=sys.stderr)
            return 1
        exit_code = 0
    elif args.command == "stocks":
        from macos_openadapt import AdapterError, connect

        try:
            from stocks_adapter import run

            window = connect(args.bundle_id, args.window, foreground=args.foreground)
            result = run(window, check_only=args.check_only, timeout=args.timeout)
        except AdapterError as exc:
            result = exc.report
        except (OSError, RuntimeError) as exc:
            result = {"status": "unavailable", "message": str(exc)}
        result = {"application": args.bundle_id, "period": "1m", **result}
        exit_code = {"verified": 0, "unverified": 1}.get(result["status"], 2)
    else:
        return cli.output(cli.dispatch, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
