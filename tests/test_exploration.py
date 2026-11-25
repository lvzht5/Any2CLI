import json
import unittest
from unittest.mock import Mock

from any2cli_core.catalog import CatalogError, Frame, empty_catalog, merge_catalog, operation, remember, resolve, transition
from any2cli_core.engine import execute, explore_live, return_control, state_id
from any2cli_core.surfaces import Node, extract


class DemoApp:
    """An interactive state machine; the crawler is given only the current UI."""

    def __init__(self, link="Details"):
        self.tab = "Overview"
        self.panel = False
        self.text = ""
        self.link = link
        self.back = True
        self.drift = False
        self.calls = []

    def observe(self):
        frame = Frame("Demo")
        if self.panel:
            frame.add(operation("field", "textbox", "Query"), self.text)
            frame.add(operation("read", "text", "Result"), self.text.upper())
            if self.back:
                frame.add(operation("press", "button", "Back"))
        else:
            options = ["Overview", "Reports"]
            key = frame.add(operation("choice", "AXTabGroup", "Tabs", options=options), self.tab)
            frame.probes.extend((key, v, key, self.tab) for v in options if v != self.tab)
            if self.tab == "Reports":
                frame.add(operation("press", "button", self.link))
        return frame

    def perform(self, frame, key, value):
        name = frame.ops[key]["name"]
        self.calls.append((name, value))
        if name == "Tabs":
            if not self.drift:
                self.tab = value
        elif name == self.link:
            self.panel = True
        elif name == "Back":
            self.panel = False
        elif name == "Query":
            self.text = value
        else:
            raise AssertionError(name)


class ExplorationTests(unittest.TestCase):
    def test_scroll_area_buttons_remain_explicit_actions(self):
        frame, _ = extract("Reports", [Node((0,), "AXScrollArea", "Reports"),
                           Node((0, 0), "button", "Overview", selected=True), Node((0, 1), "button", "Export report")])
        driver = Mock(notes=set())
        driver.observe.return_value = frame
        cat, report = explore_live("org.example.reports", driver, steps=1, depth=1, timeout=0)
        self.assertEqual(cat["operations"][resolve(cat, "Export report")]["kind"], "press")
        self.assertEqual(report["probes"], 0)
        driver.perform.assert_not_called()
        explicit, _ = extract("Reports", [Node((0,), "AXScrollArea", "Period", value="Month"),
                              Node((0, 0), "button", "Month"), Node((0, 1), "button", "Year")])
        self.assertEqual(next(iter(explicit.ops.values()))["kind"], "choice")
        self.assertFalse(explicit.probes)

    def test_wrong_destination_cannot_validate_a_retry_or_recorded_replay(self):
        cat = empty_catalog("org.example.details")
        page, correct, wrong = Frame("Start"), Frame("Details"), Frame("Offline")
        key = page.add(operation("press", "button", "Open"))
        src, dst = remember(cat, page), remember(cat, correct)
        transition(cat, src, dst, key, evidence="#/transitions/0")
        # A v1 profile could also contain an old, unlabelled failed live observation.
        transition(cat, src, remember(cat, wrong), key)
        for _ in range(2):
            driver = Mock()
            driver.observe.side_effect = [page, wrong]
            self.assertEqual(execute(cat, key, None, driver=driver, timeout=0)["status"], "unverified")
            reopened = empty_catalog(cat["application"])
            merge_catalog(reopened, cat)
            cat = reopened
        self.assertEqual(execute(cat, key, None)["destination"], dst)
        self.assertEqual(next(e for e in cat["transitions"] if e["to"] == state_id(wrong))["outcome"], "unverified")

    def test_observed_change_can_be_replayed_without_becoming_its_own_live_proof(self):
        cat = empty_catalog("org.example.reports")
        page, after = Frame("Start"), Frame("Report")
        key = page.add(operation("press", "button", "Open"))
        remember(cat, page)
        for _ in range(2):
            driver = Mock()
            driver.observe.side_effect = [page, after]
            self.assertEqual(execute(cat, key, None, driver=driver, timeout=0)["status"], "observed_ui_change")
            reopened = empty_catalog(cat["application"])
            merge_catalog(reopened, cat)
            cat = reopened
            replay = execute(cat, key, None)
            self.assertEqual((replay["status"], replay["destination"]), ("replayed", state_id(after)))

    def test_duplicate_full_or_partial_scope_never_binds_another_button(self):
        for outer in ([], ["Panel"]):
            with self.subTest(scope=outer):
                cat = empty_catalog("org.example.controls")
                recorded = Frame("Start")
                key = recorded.add(operation("press", "button", "Details"))
                remember(cat, recorded)
                live = Frame("Start")
                for scope in (outer, outer, ["Toolbar"]):
                    live.add(operation("press", "button", "Details", scope))
                driver = Mock()
                driver.observe.return_value = live
                with self.assertRaises(CatalogError) as error:
                    execute(cat, key, None, driver=driver, timeout=0)
                self.assertEqual(error.exception.report["status"], "ambiguous")
                driver.perform.assert_not_called()

    def test_document_title_can_change_only_for_a_unique_complete_structure(self):
        class Document(DemoApp):
            title = "Project A"
            def observe(self):
                frame = super().observe()
                frame.title = self.title
                return frame

        app = Document()
        cat, _ = explore_live("org.example.docs", app, steps=5, depth=3, timeout=0)
        for title in ("Project B", "Project C"):
            app = Document()
            app.title = title
            self.assertEqual(execute(cat, resolve(cat, "Query"), "test", driver=app, timeout=0)["status"], "verified_ui")
        other = Document()
        other.title = "Another recorded page"
        remember(cat, other.observe())
        other.title = "Project D"
        with self.assertRaises(CatalogError) as error:
            execute(cat, resolve(cat, "Query"), "test", driver=other, timeout=0)
        self.assertEqual(error.exception.report["status"], "no_route")
        self.assertFalse(other.calls)

    def test_exploration_refuses_a_return_with_duplicated_back_buttons(self):
        frame = Frame("Details")
        for scope in (["Panel"], ["Panel"], ["Toolbar"]):
            frame.add(operation("press", "button", "Back", scope))
        self.assertIsNone(return_control(frame, operation("press", "button", "Details")))

    def test_autonomous_exploration_then_live_parameterized_nested_command(self):
        app = DemoApp()
        cat, coverage = explore_live("org.example.demo", app, steps=5, depth=3, timeout=0)
        self.assertEqual(coverage["issues"], [])
        self.assertTrue(coverage["restored"])
        self.assertEqual((coverage["probes"], coverage["return_actions"]), (2, 2))
        self.assertEqual((len(cat["states"]), len(cat["transitions"])), (3, 4))
        self.assertEqual((app.tab, app.panel), ("Overview", False))
        app.calls.clear()
        key = resolve(cat, "Query")
        result = execute(cat, key, "private sample", driver=app, timeout=0)
        self.assertEqual(result["status"], "verified_ui")
        self.assertEqual(app.calls, [("Tabs", "Reports"), ("Details", None), ("Query", "private sample")])
        self.assertNotIn("private sample", json.dumps(cat))
        self.assertEqual(execute(cat, key, "private sample", driver=app)["already_selected"], True)
        readback = execute(cat, resolve(cat, "Result"), None, driver=app)
        self.assertEqual(readback["value"], "PRIVATE SAMPLE")

    def test_changed_prerequisite_stops_before_the_final_action(self):
        app = DemoApp()
        cat, _ = explore_live("org.example.demo", app, steps=5, depth=3, timeout=0)
        app.calls.clear()
        app.drift = True
        result = execute(cat, resolve(cat, "Query"), "x", driver=app, timeout=0)
        self.assertEqual(result["status"], "route_changed")
        self.assertEqual(app.calls, [("Tabs", "Reports")])

    def test_budgets_and_explicit_agent_directed_navigation(self):
        app = DemoApp(link="Inspector")
        cat, report = explore_live("org.example.demo", app, steps=1, depth=3, timeout=0)
        self.assertTrue(report["step_limit_reached"])
        self.assertEqual(len(cat["states"]), 2)
        cat, report = explore_live("org.example.demo", app, steps=5, depth=3, timeout=0, follow=["Inspector"])
        self.assertEqual(len(cat["states"]), 3)
        self.assertEqual(report["issues"], [])
        self.assertTrue(report["restored"])
        shallow, _ = explore_live("org.example.demo", app, steps=50, depth=0, timeout=0)
        self.assertEqual(len(shallow["states"]), 1)

    def test_follow_parameterized_choice_checks_both_legs(self):
        app = DemoApp()
        cat, report = explore_live("org.example.demo", app, steps=1, depth=1, timeout=0, follow=["Tabs=Reports"])
        self.assertEqual(app.calls, [("Tabs", "Reports"), ("Tabs", "Overview")])
        self.assertTrue(report["restored"])
        self.assertEqual(sum(e["navigation"] for e in cat["transitions"]), 2)

    def test_failed_return_preserves_the_observed_partial_tree(self):
        app = DemoApp()
        app.back = False
        cat, report = explore_live("org.example.demo", app, steps=3, depth=3, timeout=0)
        self.assertFalse(report["restored"])
        self.assertTrue(report["issues"])
        self.assertIn(resolve(cat, "Query"), cat["states"][state_id(app.observe())]["operations"])
        self.assertFalse(any(e["navigation"] for e in cat["transitions"]))

    def test_interrupt_retains_already_observed_state(self):
        class Interrupted(DemoApp):
            def perform(self, frame, key, value):
                raise KeyboardInterrupt

        cat, report = explore_live("org.example.demo", Interrupted(), steps=3, depth=2, timeout=0)
        self.assertEqual(report["issues"][0]["status"], "interrupted")
        self.assertTrue(report["restored"])
        self.assertTrue(cat["root"])

    def test_interrupt_in_a_nested_probe_cancels_pending_return_actions(self):
        class Interrupted(DemoApp):
            def perform(self, frame, key, value):
                if self.tab == "Reports":
                    raise KeyboardInterrupt
                super().perform(frame, key, value)

        app = Interrupted()
        cat, report = explore_live("org.example.demo", app, steps=2, depth=2, timeout=0)
        self.assertEqual(app.calls, [("Tabs", "Reports")])
        self.assertFalse(report["restored"])
        self.assertEqual(report["return_actions"], 0)
        self.assertEqual(report["issues"][0]["status"], "interrupted")
        self.assertFalse(any(edge["navigation"] for edge in cat["transitions"]))

    def test_duplicate_nonnative_controls_and_selected_tabs(self):
        nodes = [Node((0,), "AXWindow", "Demo"), Node((0, 1), "AXTabGroup", ""),
                 Node((0, 1, 0), "tab", "A", selected=True), Node((0, 1, 1), "tab", "B"),
                 Node((0, 2), "button", "Apply"), Node((0, 3), "button", "Apply")]
        frame, _ = extract("Demo", nodes)
        self.assertEqual(len(frame.ambiguous), 1)
        self.assertEqual([op["name"] for op in frame.ops.values()], ["Tabs"])
        self.assertEqual(len(frame.probes), 1)


if __name__ == "__main__":
    unittest.main()
