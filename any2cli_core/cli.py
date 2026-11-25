"""Agent-facing commands and per-application CLI generation."""

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from macos_openadapt import AdapterError

from . import music, qgis
from .applications import resolve_application
from .catalog import (CatalogError, SERVICE_KINDS, describe, empty_catalog, initial_commands, merge_catalog,
                      register, resolve, search)
from .engine import execute, explore_live, input_value
from .importers import import_graph, import_trace
from .native import NativeDriver
from .store import Store, live_session, profile_path


SUCCESS = {"explored", "converted", "planned", "replayed", "verified_ui", "observed_ui_change", "executed", "read", "ok"}


def integer(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return value


def seconds(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("expected a finite nonnegative number")
    return value


def run_flags(parser, op=None):
    kind = op["kind"] if op else None
    named = {}
    if kind == "algorithm":
        for key, spec in op.get("parameters", {}).get("properties", {}).items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
                continue
            flag = "--" + key.lower().replace("_", "-")
            while flag in {"--value", "--help", "--dry-run", "--replay"} or flag in named.values():
                flag = "--param-" + flag[2:]
            named[key] = flag
            parser.add_argument(flag, dest="param_" + key, action="append", metavar=key,
                                help=(spec.get("title") or key).replace("%", "%%"))
    parser.set_defaults(named_parameters=list(named))
    if kind is None or kind in {"field", "choice", "expand", "toggle", "algorithm"}:
        choices = op.get("options") if op else None
        if kind in {"expand", "toggle"}:
            choices = ["true", "false"]
        help_text = "JSON object of QGIS Processing inputs" if kind == "algorithm" else "Operation argument"
        if kind == "algorithm":
            if op.get("parameters"):
                help_text += " (" + ", ".join(op["parameters"]["properties"]) + ")"
            else:
                help_text += ". Use any2cli convert to cache the named parameters."
        parser.add_argument("--value", required=op is not None and kind != "algorithm",
                            choices=choices, help=help_text.replace("%", "%%"))
    parser.set_defaults(value=None)
    modes = parser.add_mutually_exclusive_group()
    plan_help = "Plan the call without opening the provider" if kind in SERVICE_KINDS else "Print the saved route without opening the app"
    modes.add_argument("--dry-run", action="store_true", help=plan_help)
    modes.add_argument("--replay", action="store_true", help="Use only transitions in the recording")
    if kind in SERVICE_KINDS:
        parser.set_defaults(from_state=None, window=None, settle=None)
        return
    parser.add_argument("--from-state", help="Starting state for a plan or recorded replay")
    parser.add_argument("--window", help="Exact native window title")
    modes.add_argument("--foreground", action="store_true",
                       help="Allow menus and physical clicks while the target window already has focus")
    parser.add_argument("--settle", type=seconds, default=5.0, help="Seconds to wait for a UI postcondition")


def add_commands(commands):
    p = commands.add_parser("explore", help="Explore an installed macOS app or import an exploration recording")
    p.add_argument("app", help="Bundle ID, installed name or .app directory")
    source = p.add_mutually_exclusive_group()
    source.add_argument("--graph", type=Path, help="GUIrilla graph/data.json")
    source.add_argument("--trace", type=Path, help="Observed any2cli.trace.v1 JSON")
    source.add_argument("--processing", action="store_true", help="Index QGIS native algorithms without opening a window")
    source.add_argument("--library", action="store_true", help="Index the public Apple Music library operations")
    p.add_argument("--profile", type=Path, help="Destination profile (default .any2cli/<bundle>.json)")
    p.add_argument("--primary", type=integer, help="Initial command count (automatic by default, normally 4–12)")
    p.add_argument("--steps", type=integer, help="Maximum forward probes (default: observe only, or one with --follow or --foreground)")
    p.add_argument("--foreground", action="store_true",
                   help="Allow foreground-only probes while the target window already has focus")
    p.add_argument("--depth", type=integer, default=2, help="Maximum nested navigation depth")
    p.add_argument("--follow", action="append", default=[], help="Probe a button, menu, readable switch or selector=value (repeatable)")
    p.add_argument("--settle", type=seconds, default=5.0)
    p.add_argument("--window", help="Exact native window title")
    for name, help_text in [("tree", "Inspect the observed operation tree"), ("commands", "List generated commands"),
                            ("find", "Search the whole operation tree"), ("convert", "Compile an operation on demand"),
                            ("run", "Resolve, compile and run an operation"), ("app", "Open a generated app CLI")]:
        p = commands.add_parser(name, help=help_text)
        p.add_argument("profile", help="Profile path or bundle ID")
        if name in {"find", "convert", "run"}:
            p.add_argument("query", help="Operation ID, name, full path or search text")
        if name == "find":
            p.add_argument("--limit", type=integer, default=12)
        if name == "tree":
            p.add_argument("--json", action="store_true", help="Print the complete machine-readable graph")
        if name == "run":
            run_flags(p)


def wrapper_text(interpreter, module, profile_name):
    return ("#!/bin/sh\n# Generated by Any2CLI\ninterpreter=" + shlex.quote(interpreter) +
            "\nprofile_name=" + shlex.quote(profile_name) + '\nexec "${ANY2CLI_PYTHON:-$interpreter}" ' +
            shlex.quote(module) + ' app "$(dirname "$0")/$profile_name" "$@"\n')


def generated_wrapper(content):
    try:
        words = shlex.split(content, comments=True)
    except ValueError:
        return False
    return (len(words) == 8 and content == wrapper_text(words[0].removeprefix("interpreter="), words[4],
                                                         words[1].removeprefix("profile_name=")))


def wrapper(path):
    import any2cli

    if os.name == "nt":
        return None
    path = path.absolute()
    target = path.with_name(path.name + ".cli") if path.suffix != ".json" else path.with_suffix(".cli")
    body = wrapper_text(sys.executable, str(Path(any2cli.__file__).resolve()), path.name)
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        tmp = Path(folder) / "launcher"
        tmp.write_text(body, encoding="utf-8")
        tmp.chmod(0o700)
        try:
            os.link(tmp, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                return None
            with target.open("rb") as stream:
                existing = stream.read(65537)
                previous = os.fstat(stream.fileno())
            if existing == body.encode() and os.access(target, os.X_OK):
                return str(target)
            try:
                managed = len(existing) <= 65536 and generated_wrapper(existing.decode("utf-8"))
            except UnicodeDecodeError:
                managed = False
            if not managed or not os.path.samestat(previous, target.lstat()):
                return None
            os.replace(tmp, target)
    return str(target)


def text_tree(cat):
    out = [cat["application"]]
    compiled = {key: name for name, key in cat["commands"].items()}
    edges = defaultdict(dict)
    for e in cat["transitions"]:
        op = cat["operations"].get(e["operation"], {})
        label = " / ".join([*op.get("scope", []), op.get("name") or e.get("label") or "observed action"])
        if e["value"] is not None:
            label += " = " + str(e["value"])
        edges[e["from"]][label, e["to"]] = e
    seen = set()
    # Iterative traversal also handles cycles and recordings with long paths.
    for root in dict.fromkeys([cat["root"], *cat["states"]]):
        if root in seen:
            continue
        stack = [(root, "", "")]
        while stack:
            key, indent, via = stack.pop()
            label = cat["states"][key]["title"]
            prefix = indent + ("└─ " + via + " → " if via else "")
            if key in seen:
                out.append(f"{prefix}↩ {label} [{key}]")
                continue
            seen.add(key)
            out.append(f"{prefix}{label} [{key}]")
            groups = defaultdict(list)
            for oid in cat["states"][key]["operations"]:
                op = cat["operations"][oid]
                qualified = " / ".join([*op["scope"], op["name"]]).replace("\n", " ")
                cli = f", CLI {compiled[oid]}" if oid in compiled else ""
                if op["kind"] == "algorithm":
                    groups[op.get("group", "native")].append(f"{op['name']} ({oid}{cli})")
                else:
                    out.append(f"{indent}   · {qualified} ({op['kind']}, {oid}{cli})")
            for group, entries in sorted(groups.items()):
                out.append(f"{indent}   {group}")
                out.extend(f"{indent}      · {entry}" for entry in sorted(entries))
            for name, child in reversed(sorted(edges[key])):
                stack.append((child, indent + "   ", name.replace("\n", " ")))
    return "\n".join(out)


def explore_installed(app, path, args):
    if args.processing:
        return qgis.discover(app, path)
    if getattr(args, "library", False):
        return music.discover(app, path)
    provider = {music.APP: music, qgis.APP: qgis}.get(app)
    try:
        with live_session(app):
            foreground = getattr(args, "foreground", False)
            driver = NativeDriver.open(app, args.window, app_path=path, foreground=foreground)
            steps = args.steps if args.steps is not None else int(bool(foreground or args.follow))
            catalog, report = explore_live(app, driver, steps=steps, depth=args.depth, timeout=args.settle,
                                           follow=args.follow)
    except AdapterError as exc:
        if provider is None or exc.report["status"] not in {"permission_required", "unavailable"}:
            raise
        try:
            catalog, report = provider.discover(app, path)
        except CatalogError:
            raise exc
        report["issues"] = [exc.report]
        return catalog, report
    if provider is not None:
        try:
            service, count = provider.discover(app, path)
        except CatalogError as exc:
            report["issues"].append(exc.report)
        else:
            merge_catalog(catalog, service)
            report.update(count)
    return catalog, report


def explore(args):
    app, exact_path = resolve_application(args.app)
    store = Store(args.profile or profile_path(app))
    previous = store.read() if store.path.exists() else {}
    if previous and previous["application"] != app:
        raise CatalogError("This profile belongs to a different application.")
    if exact_path and previous.get("app_path", exact_path) != exact_path:
        raise CatalogError("This profile is bound to a different installed .app path.")
    if args.follow and (args.graph or args.trace or args.processing or args.library):
        raise CatalogError("--follow requires live GUI exploration.")
    if args.graph or args.trace:
        file = args.graph or args.trace
        payload = file.read_bytes()
        source_id = hashlib.sha256(payload).hexdigest()
        raw = json.loads(payload)
        del payload
        incoming, report = import_graph(app, raw) if args.graph else import_trace(app, raw)
        incoming["recordings"][source_id] = {"file": file.name, "format": incoming["sources"][0]}
        for edge in incoming["transitions"]:
            edge["recording"] = source_id
    else:
        path = exact_path or previous.get("app_path")
        if app == qgis.APP and path and not exact_path:
            path, _ = qgis.profile_installation(previous)
        incoming, report = explore_installed(app, path, args)
    if exact_path:
        incoming["app_path"] = exact_path

    def save(cat):
        merge_catalog(cat, incoming)
        primary = initial_commands(cat, args.primary)
        if "qgis-process" in incoming["sources"]:
            for name in primary:
                op = cat["operations"][cat["commands"][name]]
                if op["kind"] == "algorithm":
                    try:
                        qgis.prepare_parameters(cat, op, discovered_path=incoming["app_path"])
                    except CatalogError as exc:
                        report.setdefault("issues", []).append({**exc.report, "command": name})
        return {"states": len(cat["states"]), "transitions": len(cat["transitions"]),
                "operations": len(cat["operations"]), "commands": len(cat["commands"]),
                "primary_commands": primary, "root": cat["root"]}

    totals = store.update(save, initial=empty_catalog(app))
    return {"status": "partial" if report.get("issues") or report.get("restored") is False else "explored",
            "application": app, "profile": str(store.path.absolute()), "cli": wrapper(store.path),
            **totals, "exploration": report}


def compile_query(store, query, *, validate=None, load_parameters=False):
    def convert(cat):
        key = resolve(cat, query)
        op = cat["operations"][key]
        if load_parameters and op["kind"] == "algorithm":
            qgis.prepare_parameters(cat, op)
        if validate:
            validate(op)
        created = key not in cat["commands"].values()
        register(cat, key)
        return describe(cat, key), created

    return store.update(convert)


def run_command(store, args):
    offline = args.dry_run or args.replay
    named = {}
    for key in getattr(args, "named_parameters", []):
        values = getattr(args, "param_" + key, None)
        if values is not None:
            named[key] = values[0] if len(values) == 1 else values
    if named and args.value is not None:
        raise CatalogError("Choose either named parameters or --value JSON for a call.", status="invalid_input")
    if getattr(args, "named_parameters", []) and args.value is None:
        args.value = json.dumps(named, ensure_ascii=False)
    if args.from_state and not offline:
        raise CatalogError("--from-state is available for recorded replay and dry runs.")

    def validate(item):
        if item["kind"] in SERVICE_KINDS and (args.window or args.from_state):
            raise CatalogError("This operation runs independently of GUI state.", status="invalid_input")
        if args.dry_run and item["kind"] == "algorithm" and "parameters" not in item:
            raise CatalogError("Cache the installed parameter contract with any2cli convert before planning.",
                               status="parameters_unavailable")
        input_value(item, args.value)

    op, created = compile_query(store, args.query, validate=validate, load_parameters=not offline)
    cat = store.read()
    if op["kind"] in SERVICE_KINDS:
        if offline:
            result = {"target": op["name"], "mode": "plan" if args.dry_run else "recording",
                      "status": "planned" if args.dry_run else "not_recorded", "route": [], "backend": op["role"]}
        elif op["kind"] == "library-read":
            result = music.run(cat, op["name"])
        else:
            result = qgis.run(cat, op["name"], input_value(op, args.value))
        result["operation"] = op["id"]
    elif offline:
        result = execute(cat, op["id"], args.value, start=args.from_state, dry_run=args.dry_run)
    else:
        with live_session(cat["application"]):
            driver = NativeDriver.open(cat["application"], args.window, app_path=cat.get("app_path"),
                                       foreground=args.foreground)
            try:
                result = execute(cat, op["id"], args.value, driver=driver, timeout=args.settle)
            finally:
                store.update(lambda current: merge_catalog(current, cat))
    return {**result, "command": op["command"], "newly_compiled": created}


def dispatch(args):
    if args.command == "explore":
        return explore(args)
    store = Store(profile_path(args.profile))
    if args.command == "run":
        return run_command(store, args)
    if args.command == "convert":
        op, created = compile_query(store, args.query, load_parameters=True)
        return {"status": "converted", "newly_compiled": created, **op}
    cat = store.read()
    if args.command == "tree":
        return cat if args.json else text_tree(cat)
    if args.command == "find":
        keys = search(cat, args.query)
        return {"status": "ok", "matches": len(keys), "operations": [describe(cat, k) for k in keys[:args.limit]]}
    return {"status": "ok", "application": cat["application"],
            "commands": [describe(cat, k) for _, k in sorted(cat["commands"].items())]}


def app_parser(path, cat, query, selected):
    parser = argparse.ArgumentParser(prog=path.stem, description=f"Commands for {cat['application']}")
    subs = parser.add_subparsers(dest="query", required=True)
    for name, key in sorted(cat["commands"].items()):
        op = cat["operations"][key]
        description = op.get("help") or " / ".join([*op["scope"], op["name"]])
        sub = subs.add_parser(name, help=description.replace("%", "%%"))
        run_flags(sub, op)
    if selected and query not in cat["commands"]:
        sub = subs.add_parser(query, help="Resolve an operation from the tree")
        run_flags(sub, selected)
    return parser


def app_cli(profile, argv):
    store = Store(profile_path(profile))
    cat = store.read()
    query = argv[0] if argv and not argv[0].startswith("-") else None
    selected = cat["operations"][resolve(cat, query)] if query else None
    parser = app_parser(store.path, cat, query, selected)
    if not argv:
        return parser.format_help().rstrip()
    # argparse handles help entirely from the profile. Only a live call using
    # uncached provider-specific flags needs a second pass.
    args, extra = parser.parse_known_args(argv)
    if extra and selected and selected["kind"] == "algorithm" and "parameters" not in selected:
        if args.dry_run or args.replay:
            raise CatalogError("Use any2cli convert to cache named parameters for offline calls.",
                               status="parameters_unavailable")
        def load_signature(current):
            op = current["operations"][selected["id"]]
            qgis.prepare_parameters(current, op)
            return op
        selected = store.update(load_signature)
        cat["operations"][selected["id"]] = selected
        parser = app_parser(store.path, cat, query, selected)
    if extra:
        args = parser.parse_args(argv)
    return run_command(store, args)


def output(fn, *args):
    try:
        result = fn(*args)
    except (CatalogError, AdapterError) as exc:
        result = exc.report
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        result = {"status": "unavailable", "message": str(exc)}
    if isinstance(result, str):
        print(result)
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status", "ok") in SUCCESS:
        return 0
    return 1 if result.get("status") in {"unverified", "partial", "not_recorded", "route_changed"} else 2
