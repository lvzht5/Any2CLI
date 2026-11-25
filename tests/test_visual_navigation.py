import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from unittest.mock import Mock, patch

from any2cli_core import visual
from any2cli import main
from any2cli_core.catalog import CatalogError, Frame, empty_catalog, merge_catalog, operation, remember
from any2cli_core.engine import state_id, window_mode_changed
from any2cli_core.native import NativeDriver, conditional_menu
from macos_openadapt import AdapterError, NativeWindow


class NavigationStateTests(unittest.TestCase):
    def test_default_explore_observes_once_without_opening_or_moving_the_window(self):
        page = Frame("")
        key = page.add(operation("choice", "visual-sidebar", "侧栏", options=["概览", "详情"]), "概览")
        page.probes.append((key, "详情", key, "概览"))
        driver = Mock(notes=set())
        driver.observe.return_value = page
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()), \
             patch("any2cli_core.cli.open_driver", return_value=driver) as opened:
            self.assertEqual(main(["explore", "test.workspace", "--profile", str(Path(folder) / "ui.json")]), 0)
            opened.assert_called_once_with("test.workspace", None, app_path=None, foreground=False)
        driver.observe.assert_called_once_with()
        driver.perform.assert_not_called()
        with patch("any2cli_core.native.connect") as connect, patch.object(NativeDriver, "__init__", return_value=None):
            NativeDriver.open("test.workspace")
        connect.assert_called_once_with("test.workspace", None, app_path=None, foreground=False)

    def test_background_visual_and_indirect_reads_stop_before_navigation(self):
        app = Mock()
        app.processIdentifier.return_value = 92
        window = NativeWindow(app, "window", "", Mock(), Mock())
        with patch.object(NativeDriver, "__init__", return_value=None):
            driver = NativeDriver(window)
        driver.window = window
        row = operation("choice", "visual-sidebar", "侧栏", options=["概览", "详情"])
        read = operation("read", "visual-selection", "当前页签")
        with self.assertRaises(AdapterError) as blocked:
            driver.check_route([row], row)
        self.assertEqual(blocked.exception.report["status"], "foreground_required")
        with self.assertRaises(CatalogError) as off_page:
            driver.check_route([row], read)
        self.assertEqual(off_page.exception.report["status"], "navigation_required")
        if find_spec("openadapt_flow"):
            with patch.object(visual, "window_info") as locate, self.assertRaises(AdapterError):
                visual.select(window, None, "详情", row)
            locate.assert_not_called()
        self.assertEqual(window.client.mock_calls, [])

    def test_conditional_host_items_do_not_split_the_same_page(self):
        menu = operation("menu", "menuitem", "拷贝", ["编辑"])
        side = operation("choice", "visual-sidebar", "侧栏", options=["概览", "详情", "面板"])

        def page(name, enabled):
            frame = Frame("")
            frame.add(side, name)
            if enabled:
                key = frame.add(menu)
                self.assertTrue(conditional_menu(menu))
                frame.conditional.add(key)
            return frame

        minimal = page("概览", False)
        expanded = page("概览", True)
        self.assertEqual(state_id(minimal), state_id(expanded))
        self.assertNotEqual(state_id(expanded), state_id(page("详情", True)))
        first, second = empty_catalog("example.app"), empty_catalog("example.app")
        key = remember(first, expanded)
        remember(second, minimal)
        merge_catalog(first, second)
        self.assertIn(menu["id"], first["states"][key]["operations"])
        self.assertIn(menu["id"], first["states"][key]["conditional_operations"])

    def test_window_mode_requires_matching_inverse_in_the_same_menu(self):
        old = operation("menu", "menuitem", "显示工具栏", ["窗口"])
        f = Frame("")
        f.add(operation("menu", "menuitem", "隐藏工具栏", ["其他"]))
        self.assertFalse(window_mode_changed(old, f))
        f.add(operation("menu", "menuitem", "隐藏工具栏", ["窗口"]))
        self.assertTrue(window_mode_changed(old, f))
        f.add(old)
        self.assertFalse(window_mode_changed(old, f))

    def test_changed_window_cannot_receive_a_visual_selection(self):
        if not find_spec("openadapt_flow"):
            self.skipTest("optional desktop backend")
        window = Mock()
        earlier = visual.ChoiceGroup({}, "概览", 8)
        with patch.object(visual, "window_info", return_value=Mock(window_id=9)):
            with self.assertRaisesRegex(ValueError, "window changed"):
                visual.select(window, earlier, "面板", {"role":"visual-sidebar"})
        window.client.activate.assert_not_called()


@unittest.skipUnless(find_spec("PIL"), "optional desktop imaging dependency")
class VisualDetectionTests(unittest.TestCase):
    def frame(self, rectangles):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1000, 700), "white")
        draw = ImageDraw.Draw(image)
        for bounds in rectangles:
            draw.rectangle(bounds, fill="#d64542")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()

    def test_sidebar_needs_one_highlight_and_recovers_labels_with_icons(self):
        labels = [visual.Label(s, 40, 95, 100+i*40, 20)
                  for i,s in enumerate(["概览", "n 详情", "（（o））面板", "∞ 属性", "• 其他●"])]
        with patch.object(visual, "recognize", return_value=labels):
            self.assertIsNone(visual.detect(self.frame([]), 9))
            second = (12,126,164,154)
            group = visual.detect(self.frame([second]), 9)
            self.assertEqual(group.selected, "详情")
            self.assertEqual(list(group.labels), ["概览", "详情", "面板", "属性", "其他"])
            # Two highlighted destinations cannot establish a selected row.
            both = self.frame([second, (12,166,164,194)])
            candidate = visual.detect(both, 9)
            self.assertIsNone(candidate)

    def test_page_tabs_require_unique_underline_and_retain_window_coordinates(self):
        labels = [visual.Label(s, 15+i*80, 65+i*80, 28, 20)
                  for i,s in enumerate(["详情", "属性", "设置", "选项"])]
        x0, y0 = 210, 72
        def bar(n):
            return (x0+30+n*80, y0+42, x0+51+n*80, y0+45)

        with patch.object(visual, "recognize", return_value=labels):
            self.assertIsNone(visual.detect_tabs(self.frame([]), 4))
            group = visual.detect_tabs(self.frame([bar(2)]), 4)
            self.assertEqual(group.selected, "设置")
            self.assertEqual(group.labels["设置"].left, x0+175)
            self.assertIsNone(visual.detect_tabs(self.frame([bar(1),bar(2)]), 4))
