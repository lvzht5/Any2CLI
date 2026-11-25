"""Run OpenAdapt's real resolver and click logic against simulated OS input."""

import io
import unittest
from dataclasses import replace
from importlib.util import find_spec
from unittest.mock import Mock, patch

from macos_openadapt import AdapterError, NativeWindow, connect, operation_client


@unittest.skipUnless(find_spec("openadapt_flow"), "optional openadapt-flow dependency")
class OpenAdaptTests(unittest.TestCase):
    def setUp(self):
        from openadapt_flow.backends.macos_backend import MacCandidateSet, MacElement, QuartzMacAXClient
        from openadapt_flow.backends.remote_display import MacWindowClient, WindowInfo
        from PIL import Image

        info = WindowInfo(window_id=2, pid=8, owner="Stocks", title="Chart", bounds=(0, 0, 100, 100))
        self.element = MacElement("0/1/2", None, "button", "1 month", info.pid, info.title,
                                  (20, 20, 30, 10), supported_operations=("press",))
        self.ax = Mock(spec=QuartzMacAXClient)
        self.ax.find_candidates.return_value = MacCandidateSet((self.element,))
        self.ax.element_at_point.return_value = self.element
        self.ax.is_part_of.return_value = True
        self.client = Mock(spec=MacWindowClient)
        self.client.find_windows.return_value = [info]
        self.client.find_pinned_window.return_value = [info]
        self.client.attached_sheet.return_value = None
        self.client.input_trusted.return_value = True
        self.client.capture_trusted.return_value = True
        self.client.raise_window.return_value = True
        self.client.exact_window_focused_main.return_value = True
        self.client.frontmost_pid.return_value = self.client.focused_application_pid.return_value = info.pid
        self.client.frontmost_window_id.return_value = self.client.window_id_at_point.return_value = info.window_id
        png = io.BytesIO()
        Image.new("RGB", (100, 100)).save(png, format="PNG")
        self.client.capture.return_value = (png.getvalue(), 100, 100)
        app = Mock()
        app.localizedName.return_value = app.executableURL.return_value.lastPathComponent.return_value = info.owner
        app.processIdentifier.return_value = info.pid
        app.isTerminated.return_value = False
        self.window = NativeWindow(app, "original-window", info.title, self.ax, self.client, foreground=True)
        title_reader = patch("macos_openadapt.attribute", return_value=info.title)
        self.read_attribute = title_reader.start()
        self.addCleanup(title_reader.stop)

    def test_resolves_role_and_name_and_delivers_once(self):
        self.window.click(self.element)
        _, _, locator = self.ax.find_candidates.call_args.args
        self.assertEqual((locator.role, locator.name, locator.window_name), ("button", "1 month", "Chart"))
        self.assertEqual([call.kwargs["down"] for call in self.client.mouse.call_args_list], [True, False])
        self.ax.is_part_of.assert_called()
        self.client.activate.assert_not_called()
        self.client.raise_window.assert_not_called()

    def test_background_and_lost_focus_stop_before_pointer_input(self):
        self.window.foreground = False
        with self.assertRaises(AdapterError) as blocked:
            self.window.click(self.element)
        self.assertEqual(blocked.exception.report["status"], "foreground_required")
        self.window.foreground = True
        self.client.focused_application_pid.return_value = 99
        with self.assertRaises(AdapterError) as interrupted:
            self.window.click(self.element)
        self.assertEqual(interrupted.exception.report["status"], "focus_changed")
        self.client.focused_application_pid.return_value = self.window.pid
        self.client.frontmost_window_id.return_value = 99
        with self.assertRaises(AdapterError) as sibling:
            self.window.click(self.element)
        self.assertEqual(sibling.exception.report["status"], "focus_changed")
        self.client.mouse.assert_not_called()
        self.client.mouse_move.assert_not_called()
        self.client.activate.assert_not_called()
        self.client.raise_window.assert_not_called()
        self.client.capture.assert_not_called()

    def test_changed_member_or_occluded_point_produces_no_mouse_down(self):
        with self.assertRaises(AdapterError):
            self.window.click(replace(self.element, ax_path="0/3/4"))
        self.ax.is_part_of.return_value = False
        with self.assertRaises(AdapterError) as caught:
            self.window.click(self.element)
        self.assertEqual(caught.exception.report["action"], "not_clicked")
        self.client.mouse.assert_not_called()

    def test_permission_failure_precedes_any_capture_or_input(self):
        self.client.capture_trusted.return_value = False
        with self.assertRaises(AdapterError) as caught:
            self.window.click(self.element)
        self.assertEqual(caught.exception.report["missing"], ["screen_recording"])
        self.client.capture.assert_not_called()
        self.client.mouse.assert_not_called()

    def test_original_window_closed_before_input(self):
        self.read_attribute.return_value = None
        with self.assertRaisesRegex(AdapterError, "original window"):
            self.window.click(self.element)
        self.client.mouse.assert_not_called()

    def test_operation_observer_skips_large_data_tables_but_keeps_navigation_rows(self):
        from openadapt_flow.backends.macos_backend import QuartzMacAXClient

        notes = set()
        client = operation_client(notes)
        with patch.object(QuartzMacAXClient, "_attr", side_effect=lambda node, key: node.get(key)):
            for role, size, columns, skipped in (("AXOutline", 925, 9, True), ("AXOutline", 120, 1, False),
                                                ("AXTable", 100, 0, True), ("AXGrid", 8, 0, False),
                                                ("AXGroup", 125, 0, False)):
                children = list(range(size))
                node = {"AXRole": role, "AXChildren": children, "AXColumns": list(range(columns))}
                self.assertEqual(list(client._attr(node, "AXChildren")), [] if skipped else children)
        self.assertEqual(notes, {"large_data_table_contents_omitted"})

    @unittest.skipUnless(find_spec("CoreFoundation"), "macOS native readback")
    def test_readback_requires_the_original_ax_window_but_no_screen_capture(self):
        root = replace(self.element, native=self.window.window)
        entries = [(root, True, False)]
        self.ax.walk.return_value = (entries, False)
        self.client.capture_trusted.return_value = False
        self.assertEqual(self.window.elements(), entries)
        self.ax.walk.return_value = ([(replace(root, native="replacement-window"), True, False)], False)
        with self.assertRaises(AdapterError):
            self.window.elements()
        self.client.capture_trusted.assert_not_called()
        self.client.capture.assert_not_called()

    @unittest.skipUnless(find_spec("AppKit"), "macOS native window discovery")
    def test_unique_readable_empty_title_and_ambiguous_untitled_windows(self):
        windows = [{"AXTitle": ""}]
        self.read_attribute.side_effect = lambda element, name: windows if name == "AXWindows" else element.get(name)
        with patch("AppKit.NSRunningApplication") as apps, \
             patch("ApplicationServices.AXUIElementCreateApplication", return_value={}), \
             patch("openadapt_flow.backends.remote_display.MacWindowClient", return_value=self.client):
            apps.runningApplicationsWithBundleIdentifier_.return_value = [self.window.app]
            self.assertIs(connect("org.example.player").window, windows[0])
            windows.append({"AXTitle": "Library"})
            self.assertIs(connect("org.example.player").window, windows[1])
            self.assertIs(connect("org.example.player", "").window, windows[0])
            windows[1]["AXTitle"] = ""
            with self.assertRaises(AdapterError):
                connect("org.example.player")


if __name__ == "__main__":
    unittest.main()
