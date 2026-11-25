"""Observe and drive a single Windows window using UI Automation patterns."""

import ctypes
import ntpath
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache

from .catalog import CatalogError, Frame, navigation_id, operation


@lru_cache(maxsize=1)
def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def process_image(pid):
    api = _kernel()
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        api.CloseHandle(handle)
    return None


def select_window(windows, image_for, app, title, exact_path, errors):
    matches = []
    images = {}
    for window in windows:
        try:
            pid, hwnd = window.ProcessId, window.NativeWindowHandle
            if not hwnd or window.IsOffscreen:
                continue
            if pid not in images:
                images[pid] = image_for(pid)
            path = images[pid]
            if not path or ntpath.basename(path).casefold() != app.casefold():
                continue
            if exact_path and ntpath.normcase(path) != ntpath.normcase(exact_path):
                continue
            if title is None or window.Name == title:
                matches.append(window)
        except errors:
            continue  # A top-level window can disappear during enumeration.
    if not matches:
        raise CatalogError("No visible window matches the executable and title in this desktop session.",
                           status="not_found")
    if len(matches) != 1:
        raise CatalogError("Several windows match. Specify a unique exact --window title.", status="ambiguous",
                           candidates=[w.Name for w in matches])
    return matches[0]


@dataclass
class Binding:
    pattern: object
    runtime_id: tuple


class WindowsDriver:
    def __init__(self, api, root, errors=(OSError,)):
        self.api = api
        self.pid = root.ProcessId
        self.hwnd = root.NativeWindowHandle
        self.root_id = tuple(root.GetRuntimeId())
        self.errors = errors
        self.notes = set()

    @classmethod
    def open(cls, app, title=None, *, app_path=None):
        try:
            import uiautomation as api
            from comtypes import COMError
        except ImportError as exc:
            raise CatalogError("Install Any2CLI with the 'windows' extra for live UI Automation.") from exc
        errors = (COMError, OSError)
        try:
            window = select_window(api.GetRootControl().GetChildren(), process_image, app, title, app_path, errors)
            return cls(api, window, errors)
        except errors as exc:
            raise CatalogError("Windows could not read the target window in this desktop session.") from exc

    def _root(self):
        window = self.api.ControlFromHandle(self.hwnd)
        if (window is None or window.ProcessId != self.pid or window.NativeWindowHandle != self.hwnd
                or tuple(window.GetRuntimeId()) != self.root_id or window.IsOffscreen):
            raise CatalogError("The original window is no longer available.", status="state_changed")
        return window

    def _pattern(self, control, name):
        return control.GetPattern(getattr(self.api.PatternId, name + "Pattern"))

    def _visible(self, control):
        return (control.ProcessId == self.pid and not control.IsPassword and not control.IsOffscreen
                and control.IsEnabled)

    def _tabs(self, frame, name, scope, children):
        options = {}
        selected = []
        for item in children:
            if item.ControlTypeName != "TabItemControl":
                continue
            label = (item.Name or item.AutomationId or "").strip()
            pattern = self._pattern(item, "SelectionItem")
            if not label or label in options or pattern is None:
                return
            options[label] = Binding(pattern, tuple(item.GetRuntimeId()))
            if pattern.IsSelected:
                selected.append(label)
        if len(options) < 2 or len(selected) != 1:
            return
        key = frame.add(operation("choice", "tab-group", name or "Tabs", scope, options=list(options)),
                        selected[0], options)
        frame.probes.extend((key, other, key, selected[0]) for other in options if other != selected[0])

    def _control(self, frame, control, role, scope):
        identifier = (control.AutomationId or "").strip()
        name = (control.Name or "").strip()
        label = name or identifier
        if role == "Text":
            if identifier:
                frame.add(operation("read", "text", identifier, scope), name)
            return
        if not label:
            return

        def add(kind, pattern, value=None, op_label=label):
            return frame.add(operation(kind, role.casefold(), op_label, scope), value,
                             Binding(pattern, tuple(control.GetRuntimeId())))

        if role in {"Edit", "ComboBox", "Spinner"}:
            pattern = self._pattern(control, "Value")
            if pattern is not None:
                value = pattern.Value
                field = identifier or (name if name != value else "")
                if field:
                    if not pattern.IsReadOnly:
                        add("field", pattern, value, field)
                    add("read", pattern, value, "Read " + field if not pattern.IsReadOnly else field)
        if role in {"Button", "CheckBox", "MenuItem"}:
            pattern = self._pattern(control, "Toggle")
            if pattern is not None:
                state = pattern.ToggleState
                if state in (0, 1):
                    add("toggle", pattern, bool(state))
                return
        if role in {"ComboBox", "TreeItem", "MenuItem", "SplitButton", "Button", "Group"}:
            pattern = self._pattern(control, "ExpandCollapse")
            if pattern is not None:
                state = pattern.ExpandCollapseState
                if state in (0, 1):
                    key = add("expand", pattern, bool(state))
                    if state == 0:
                        frame.probes.append((key, True, key, False))
                    return
        if role in {"Button", "Hyperlink", "MenuItem", "SplitButton"}:
            pattern = self._pattern(control, "Invoke")
            if pattern is not None:
                add("press", pattern)

    def observe(self):
        try:
            root = self._root()
            frame = Frame(root.Name or "")
            stack = [(root, (), 0)]
            visited = 0
            while stack:
                control, scope, depth = stack.pop()
                visited += 1
                if visited > 2000 or depth > 64:
                    raise CatalogError("Window exceeds the UI Automation traversal limit.", status="tree_incomplete")
                role = control.ControlTypeName.removesuffix("Control")
                if role in {"DataGrid", "Table", "Document", "List", "TitleBar", "ToolTip"}:
                    continue
                if depth:
                    self._control(frame, control, role, scope)
                if role in {"Text", "Edit", "Button", "CheckBox", "Hyperlink", "RadioButton"}:
                    continue
                children = [child for child in control.GetChildren() if self._visible(child)]
                label = control.AutomationId or control.Name or ""
                if role == "Tab":
                    self._tabs(frame, label, scope, children)
                if depth and label and role in {"Pane", "Group", "Tab", "TabItem", "ToolBar", "MenuBar", "MenuItem"}:
                    scope = (*scope, label)
                stack.extend((child, scope, depth + 1) for child in reversed(children))
            if not frame.ops:
                self.notes.add("no_supported_uia_controls")
            return frame
        except self.errors as exc:
            raise CatalogError("A UI Automation element became inaccessible.", status="state_changed") from exc

    def check_route(self, prerequisites, target):
        if prerequisites and target["kind"] == "read":
            raise CatalogError("This read is on another page.", status="navigation_required")

    def perform(self, frame, key, value):
        fresh = self.observe()
        if (navigation_id(fresh.state()) != navigation_id(frame.state()) or key not in fresh.ops
                or fresh.ambiguous.keys() != frame.ambiguous.keys()):
            raise CatalogError("The UI changed before delivery.", status="state_changed")
        kind = fresh.ops[key]["kind"]
        previous, binding = frame.refs[key], fresh.refs[key]
        if kind == "choice":
            previous, binding = previous[value], binding[value]
        if kind == "read" or binding.runtime_id != previous.runtime_id:
            raise CatalogError("The target cannot receive this action.", status="state_changed")
        pattern = binding.pattern
        try:
            if kind == "choice":
                ok = pattern.Select(0)
            elif kind == "toggle":
                state = pattern.ToggleState
                if state not in (0, 1) or bool(state) != fresh.values[key]:
                    raise CatalogError("The switch changed before delivery.", status="state_changed")
                ok = bool(state) == value or pattern.Toggle(0)
            elif kind == "expand":
                ok = pattern.Expand(0) if value else pattern.Collapse(0)
            elif kind == "field":
                ok = pattern.SetValue(value, 0)
            else:
                ok = pattern.Invoke(0)
        except self.errors as exc:
            raise CatalogError("Windows could not deliver the requested action.", status="delivery_unconfirmed") from exc
        if not ok:
            raise CatalogError("The UI Automation provider did not confirm delivery.", status="delivery_unconfirmed")
