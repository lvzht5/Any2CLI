"""Index and run the native algorithms advertised by an installed QGIS."""

import json
import os
import re
import subprocess
from pathlib import Path

from .applications import resolve_application
from .catalog import CatalogError, Frame, algorithm_parameters, empty_catalog, operation, remember


APP = "org.qgis.qgis3"
NATIVE_ID = re.compile(r"native:[a-zA-Z0-9_]+")
# Representative everyday operations. The installed provider decides which
# ones exist and supplies all contracts and implementations.
STARTERS = ("native:centroids", "native:reprojectlayer", "native:clip", "native:buffer",
            "native:extractbyattribute", "native:fieldcalculator", "native:polygonfromlayerextent",
            "native:dissolve", "native:creategrid", "native:hillshade", "native:distancematrix",
            "native:mergevectorlayers", "native:rasterminmax", "native:fixgeometries")


def installation(app, path):
    if app != APP:
        raise CatalogError("QGIS Processing requires an installed QGIS application.")
    identifier, path = resolve_application(path or os.environ.get("ANY2CLI_QGIS_APP") or "QGIS")
    if identifier != APP or not path:
        raise CatalogError("The selected installation does not identify itself as QGIS.")
    program = Path(path) / "Contents/MacOS/qgis_process"
    if not program.is_file() or not os.access(program, os.X_OK):
        raise CatalogError("This QGIS installation does not contain qgis_process.")
    return path, program


def profile_installation(catalog):
    """A saved path identifies the expected installation; only a local selection can launch it."""
    path, program = installation(catalog["application"], None)
    expected = catalog.get("app_path")
    if expected and Path(expected).resolve() != Path(path).resolve():
        raise CatalogError("This profile targets another QGIS installation. Select the intended local .app "
                           "with ANY2CLI_QGIS_APP or explore the local installation into a new profile.")
    return path, program


def invoke(program, command, algorithm=None, inputs=None):
    args = [str(program), "--no-python", "--skip-loading-plugins", "--json", command]
    if algorithm is not None:
        if not NATIVE_ID.fullmatch(algorithm):
            raise CatalogError("Expected a QGIS native algorithm ID.", status="invalid_input")
        args.append(algorithm)
    if inputs is not None:
        args.append("-")
    try:
        proc = subprocess.run(args, input=json.dumps({"inputs": inputs}) if inputs is not None else None,
                              capture_output=True, text=True, timeout=None if command == "run" else 45,
                              env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogError(str(exc), status="qgis_error") from exc
    if proc.returncode:
        message = (proc.stderr.strip() or proc.stdout.strip())[-2000:]
        raise CatalogError(message or "QGIS Processing failed.", status="qgis_error", exit_code=proc.returncode)
    try:
        result = json.loads(proc.stdout)
    except ValueError as exc:
        raise CatalogError("QGIS did not return JSON.", status="qgis_error") from exc
    if not isinstance(result, dict):
        raise CatalogError("Unexpected QGIS response.", status="qgis_error")
    return result


def discover(app, path):
    path, program = installation(app, path)
    listing = invoke(program, "list")
    provider = listing.get("providers", {}).get("native", {})
    algorithms = provider.get("algorithms", {}) if provider.get("is_active") is not False else {}
    frame = Frame("QGIS Processing")
    for key, info in algorithms.items():
        if NATIVE_ID.fullmatch(key) and not info.get("deprecated"):
            label = " · ".join(filter(None, (info.get("name"), info.get("group"), info.get("short_description"))))
            op = operation("algorithm", "qgis_process", key, ["Processing", "native"], help_text=label)
            op["group"] = info.get("group") or "native"
            if key in STARTERS:
                op["starter_priority"] = 45 - STARTERS.index(key)
            frame.add(op)
    if not frame.ops:
        raise CatalogError("This installation advertised no active native algorithms.", status="qgis_error")
    catalog = empty_catalog(app)
    catalog["app_path"] = path
    catalog["sources"] = ["qgis-process"]
    remember(catalog, frame)
    return catalog, {"algorithms": len(frame.ops), "qgis_version": listing.get("qgis_version")}


def prepare_parameters(catalog, op, *, discovered_path=None):
    _, program = (installation(catalog["application"], discovered_path) if discovered_path
                  else profile_installation(catalog))
    stat = program.stat()
    stamp = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
    if op.get("parameter_source") == stamp and "parameters" in op:
        return
    name = op["name"]
    help_data = invoke(program, "help", name)
    if help_data.get("algorithm_details", {}).get("id") != name or not isinstance(help_data.get("parameters"), dict):
        raise CatalogError("The installed QGIS could not confirm this algorithm's parameters.", status="qgis_error")
    properties = {}
    for key, info in help_data["parameters"].items():
        prop = {"title": info["description"], "x-qgis-type": info["type"]["id"],
                "x-qgis-optional": info.get("optional"), "x-qgis-destination": info.get("is_destination")}
        if info.get("default_value") is not None:
            prop["default"] = info["default_value"]
        properties[key] = prop
    op["parameters"] = algorithm_parameters(properties)
    op["parameter_source"] = stamp


def run(catalog, name, inputs):
    _, program = profile_installation(catalog)
    result = invoke(program, "run", name, inputs)
    if not isinstance(result.get("results"), dict):
        raise CatalogError("QGIS returned no result map.", status="unverified")
    return {"status": "executed", "mode": "qgis_process", "target": name, "outputs": result["results"]}
