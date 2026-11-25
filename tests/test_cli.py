"""Exercise the installed command surface using a temporary, synthetic observation."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = "test.workspace"


def call_cli(*args, launcher=None, cwd=None):
    args = [str(arg) for arg in args]
    if launcher is None:
        command = [sys.executable, str(ROOT / "any2cli.py"), *args]
    elif os.name == "nt":
        # /s drops the outer pair. Preserve the inner quotes around paths and values.
        inner = subprocess.list2cmdline([str(launcher), *args])
        command = f'cmd.exe /d /s /c "{inner}"'
    else:
        command = [str(launcher), *args]
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=45)


class GeneratedCLITests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.profile = self.folder / "operator's 空间.json"
        self.wrapper = self.profile.with_suffix(".cmd" if os.name == "nt" else ".cli")
        self.trace = self.folder / "input.json"
        names = ["General", "Details", "Advanced"]
        selector = {"name": "Tabs", "kind": "choice", "role": "tab-group", "options": names}
        states = [{"id": name, "title": "Panel", "controls": [
            {**selector, "selected": name},
            {"kind": "field", "role": "edit", "name": "Filter"},
            {"kind": "toggle", "role": "switch", "name": "Pinned", "selected": False},
            {"kind": "read", "role": "text", "name": "Status"},
            *([{"kind": "read", "role": "text", "name": "Summary"}] if name == "Details" else [])
        ]} for name in names[:2]]
        edges = [{"from": before, "to": after, "control": {"kind": "choice", "name": "Tabs"},
                  "value": after, "navigation": True} for before, after in [names[:2], names[1::-1]]]
        self.trace.write_text(json.dumps({"format": "any2cli.trace.v1", "application": APP,
                                         "root": "General", "states": states, "transitions": edges}), encoding="utf-8")

    def invoke(self, *args, generated=False, expected=0):
        result = call_cli(*args, launcher=self.wrapper if generated else None, cwd=self.folder)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def explore(self):
        return self.invoke("explore", APP, "--trace", self.trace, "--profile", self.profile, "--primary", "4")

    def test_multiple_commands_and_on_demand_route(self):
        initial = self.explore()
        self.assertEqual((initial["states"], initial["transitions"], initial["commands"]), (2, 2, 4))
        self.assertEqual(self.invoke("tabs", "--value", "Details", "--replay", generated=True)["status"], "replayed")
        match = self.invoke("find", self.profile, "Summary")["operations"][0]
        self.assertIsNone(match["command"])
        first = self.invoke("Summary", "--dry-run", generated=True)
        self.assertEqual([(edge["name"], edge["value"]) for edge in first["route"]], [("Tabs", "Details")])
        self.assertTrue(first["newly_compiled"])
        self.assertFalse(self.invoke("summary", "--dry-run", generated=True)["newly_compiled"])
        self.assertEqual(len(self.invoke("commands", self.profile)["commands"]), 5)

    def test_unrecorded_selection_and_concurrent_registry(self):
        self.explore()
        result = self.invoke("tabs", "--value", "Advanced", "--replay", generated=True, expected=1)
        self.assertEqual(result["status"], "not_recorded")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda name: self.invoke("convert", self.profile, name), ["Summary", "Status"]))
        self.assertEqual(self.explore()["commands"], 5)
        self.assertIn("summary", {item["command"] for item in self.invoke("commands", self.profile)["commands"]})

    def test_user_launcher_and_existing_profile_survive_failed_import(self):
        self.wrapper.write_text("user contents", encoding="utf-8")
        self.assertIsNone(self.explore()["cli"])
        self.assertEqual(self.wrapper.read_text(encoding="utf-8"), "user contents")
        original = self.profile.read_bytes()
        self.invoke("explore", "test.other", "--profile", self.profile, "--trace", self.trace, expected=2)
        self.assertEqual(self.profile.read_bytes(), original)
