"""Run the public CLI in separate processes against the shipped navigation trace."""

import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "examples" / "netease.zh.trace.json"
APP = "com.netease.163music"


class GeneratedCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.profile = Path(directory.name) / "player's commands.json"
        self.wrapper = self.profile.with_suffix(".cli")

    def invoke(self, *args, generated=False, expected=0):
        program = [self.wrapper] if generated else [sys.executable, ROOT / "any2cli.py"]
        result = subprocess.run([*program, *args], cwd=self.profile.parent, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def explore(self):
        return self.invoke("explore", APP, "--trace", TRACE, "--profile", self.profile)

    def test_multiple_starters_route_and_on_demand_conversion(self):
        initial = self.explore()
        self.assertEqual((initial["states"], initial["transitions"]), (4, 6))
        self.assertGreaterEqual(initial["commands"], 4)
        chart = self.invoke("精选页签", "--value", "排行榜", "--replay", generated=True)
        self.assertEqual((chart["mode"], chart["status"]), ("recording", "replayed"))
        self.assertEqual([(edge["name"], edge["value"]) for edge in chart["route"]], [("侧栏", "精选")])

        name = "当前精选页签"
        found = self.invoke("find", self.profile, name, "--limit", "1")
        self.assertIsNone(found["operations"][0]["command"])
        first = self.invoke(name, "--dry-run", generated=True)
        second = self.invoke(name, "--dry-run", generated=True)
        self.assertTrue(first["newly_compiled"])
        self.assertFalse(second["newly_compiled"])
        names = [op["command"] for op in self.invoke("commands", self.profile)["commands"]]
        self.assertIn(name, names)
        self.assertEqual(len(names), initial["commands"] + 1)

    def test_visible_but_untried_choice_has_no_recorded_outcome(self):
        self.explore()
        result = self.invoke("精选页签", "--value", "歌手", "--replay", generated=True, expected=1)
        self.assertEqual(result["status"], "not_recorded")

    def test_parallel_registration_survives_reexploration(self):
        self.explore()
        names = ("当前精选页签", "当前页面")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda name: self.invoke("convert", self.profile, name), names))
        self.explore()
        commands = {op["command"] for op in self.invoke("commands", self.profile)["commands"]}
        self.assertTrue(set(names) <= commands)

    def test_preserves_an_existing_user_executable_and_rejects_a_mismatched_trace(self):
        self.wrapper.write_text("user contents")
        self.assertIsNone(self.explore()["cli"])
        self.assertEqual(self.wrapper.read_text(), "user contents")
        before = self.profile.read_bytes()
        self.invoke("explore", "org.example.other", "--profile", self.profile, "--trace", TRACE, expected=2)
        self.assertEqual(self.profile.read_bytes(), before)
