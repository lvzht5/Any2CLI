"""macOS observation and actions for generic profiles. Native imports are lazy."""

import re
import time

from macos_openadapt import AdapterError, attribute, connect

from .catalog import CatalogError, Frame, WINDOW_MODES, inverse_name, navigation_id, operation
from .surfaces import Node, extract
from . import visual


VIEW_MODES = (
    {"basic", "scientific", "programmer", "基础", "科学", "程序员", "基本", "工程"},
    {"icons", "as icons", "list", "as list", "columns", "as columns", "gallery", "as gallery", "grid",
     "图标", "列表", "分栏", "画廊", "网格", "圖像", "列表顯示", "直欄", "圖庫"},
)
VIEW_GROUPS = {"layout", "view as", "display mode", "view mode", "布局", "显示模式", "视图模式"}
VIEW_BARS = {"show status bar", "show sidebar", "show toolbar", "显示状态栏", "显示边栏", "显示工具栏",
             "顯示狀態列", "顯示側邊欄", "顯示工具列"}
EDIT_COMMANDS = {"undo", "redo", "cut", "copy", "paste", "delete", "select all",
                 "撤销", "重做", "剪贴", "剪切", "拷贝", "复制", "粘贴", "删除", "全选"}


def conditional_menu(op):
    if op["kind"] != "menu":
        return False
    top = op["scope"][0].casefold()
    name = op["name"].casefold()
    if top in {"编辑", "edit"}:
        return name in EDIT_COMMANDS
    return top in {"窗口", "window"} and (
        any(s.casefold() in {"移动与调整大小", "全屏幕平铺", "move & resize", "full screen tile"}
            for s in op["scope"][1:]) or name.startswith(("移到“", "全部移到", "move to ", "move all to ")))


def settable(native, name="AXValue"):
    from ApplicationServices import AXUIElementIsAttributeSettable

    error, result = AXUIElementIsAttributeSettable(native, name, None)
    return error == 0 and bool(result)


def menu_nodes(native):
    return list(attribute(native, "AXChildren") or ()) if native is not None else []


def sidebar_label(row):
    """Single-column outlines often put the row's label inside its cell."""
    for key in ("AXTitle", "AXValue"):
        value = attribute(row, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    children = menu_nodes(row)
    cells = [child for child in children if attribute(child, "AXRole") == "AXCell"]
    if len(cells) == 1:
        children = menu_nodes(cells[0])
    labels = {str(value).strip() for child in children if attribute(child, "AXRole") == "AXStaticText"
              for value in [attribute(child, "AXValue")] if isinstance(value, str) and value.strip()}
    return labels.pop() if len(labels) == 1 else ""


def menus(frame, bar, notes):
    """AX menu paths also disambiguate identically named items in other menus."""
    budget = 3500

    def visit(menu, scope):
        nonlocal budget
        sections = [[]]
        for item in menu_nodes(menu):
            budget -= 1
            if budget < 0:
                notes.add("menu_node_limit")
                return
            name = str(attribute(item, "AXTitle") or "").strip()
            if not name:
                sections.append([])
                continue
            enabled = attribute(item, "AXEnabled")
            if enabled is not None and not bool(enabled):
                continue
            sub = [c for c in menu_nodes(item) if attribute(c, "AXRole") == "AXMenu"]
            if sub:
                if len(scope) < 12:
                    visit(sub[0], (*scope, name))
                continue
            if attribute(item, "AXRole") != "AXMenuItem":
                continue
            marked = bool(str(attribute(item, "AXMenuItemMarkChar") or "").strip())
            op = operation("menu", "menuitem", name, scope)
            key = frame.add(op, True if marked else None, item)
            sections[-1].append((key, marked, name.casefold()))
        # A marked mode in a View-menu section gives a concrete return action.
        if scope[0].casefold() in {"view", "display", "显示", "视图", "顯示", "表示"}:
            present = {name for section in sections for _, _, name in section}
            for section in sections:
                for key, _, name in section:
                    inverse = inverse_name(name)
                    if inverse not in present and (name in VIEW_BARS or inverse in VIEW_BARS):
                        frame.probes.append((key, None, None, None))
                selected = [(key, name) for key, marked, name in section if marked]
                if len(selected) == 1:
                    back, old = selected[0]
                    for key, marked, name in section:
                        peers = scope[-1].casefold() in VIEW_GROUPS or any({old, name} <= group for group in VIEW_MODES)
                        if not marked and peers:
                            frame.probes.append((key, None, back, None))

    for top in menu_nodes(bar):
        name = str(attribute(top, "AXTitle") or "")
        if not name or name in {"Apple", "苹果", ""}:
            continue
        for menu in menu_nodes(top):
            if attribute(menu, "AXRole") == "AXMenu":
                visit(menu, (name,))


def press_menu(window, application, op):
    from ApplicationServices import AXUIElementPerformAction

    def send(item, action):
        window.require_foreground(application)
        return AXUIElementPerformAction(item, action)

    def named(parent, name, role):
        matches = [item for item in menu_nodes(parent)
                   if attribute(item, "AXRole") == role and attribute(item, "AXTitle") == name]
        if len(matches) != 1:
            raise CatalogError("The menu path no longer resolves uniquely.", control=name)
        return matches[0]

    def submenu(parent):
        deadline = time.monotonic() + 0.8
        while True:
            matches = [item for item in menu_nodes(parent) if attribute(item, "AXRole") == "AXMenu"]
            if len(matches) == 1 and menu_nodes(matches[0]):
                return matches[0]
            if time.monotonic() >= deadline:
                raise CatalogError("The requested submenu did not open.")
            time.sleep(0.05)

    bar = attribute(application, "AXMenuBar")
    top = named(bar, op["scope"][0], "AXMenuBarItem")
    branch = top
    if not attribute(top, "AXSelected") and send(top, "AXPress") != 0:
        raise CatalogError("Could not open the application's menu.")
    try:
        for part in op["scope"][1:]:
            branch = named(submenu(branch), part, "AXMenuItem")
            if send(branch, "AXPress") != 0:
                raise CatalogError("Could not open the requested submenu.", control=part)
        leaf = named(submenu(branch), op["name"], "AXMenuItem")
        enabled = attribute(leaf, "AXEnabled")
        if enabled is not None and not bool(enabled):
            raise CatalogError("The requested menu item is disabled.")
        code = send(leaf, "AXPress")
        if code != 0:
            raise CatalogError("macOS did not confirm delivery of the menu action.", status="delivery_unconfirmed",
                               ax_error=int(code))
    except AdapterError:
        raise
    except (CatalogError, OSError, RuntimeError):
        send(top, "AXCancel")
        raise


class NativeDriver:
    def __init__(self, window):
        from ApplicationServices import AXUIElementCreateApplication

        self.window = window
        self.app_element = AXUIElementCreateApplication(window.pid)
        self.notes = set()
        self.pending_window_mode = None

    @classmethod
    def open(cls, app, title=None, *, app_path=None, foreground=False):
        return cls(connect(app, title, app_path=app_path, foreground=foreground))

    def check_route(self, prerequisites, target):
        # Check the whole route before a background call changes a first page
        # and encounters a later step that needs the physical foreground.
        if self.window.foreground:
            return
        if target["kind"] == "read":
            raise CatalogError("This read is on another page.", status="navigation_required")
        for op in [*prerequisites, target]:
            if op["kind"] == "menu" or op["role"] in {"visual-sidebar", "visual-tabs"}:
                self.window.require_foreground()

    def observe(self):
        if self.pending_window_mode:
            from CoreFoundation import CFEqual
            # Some apps replace the entire main AX window on a mode change.
            # Rebind only to a unique main window of this PID once the same
            # menu path has replaced the original item with its inverse.
            windows = attribute(self.app_element, "AXWindows") or ()
            if len(windows) == 1 and not CFEqual(windows[0], self.window.window):
                main = attribute(self.app_element, "AXMainWindow")
                if main is not None and CFEqual(main, windows[0]):
                    snapshot = Frame("")
                    menus(snapshot, attribute(self.app_element, "AXMenuBar"), self.notes)
                    old = self.pending_window_mode
                    inverse = inverse_name(old["name"])
                    opposite = [op for op in snapshot.ops.values()
                                if op["scope"] == old["scope"] and op["name"].casefold() == inverse]
                    if old["id"] not in snapshot.ops and len(opposite) == 1:
                        self.window.window = windows[0]
                        self.window.title = str(attribute(windows[0], "AXTitle") or "")
                        self.pending_window_mode = None
        # A window may change its title during navigation. Keep its original AX
        # reference and PID; NativeWindow.elements verifies the same window.
        title = attribute(self.window.window, "AXTitle")
        if title:
            self.window.title = str(title)
        nodes = []
        list_paths = set()
        try:
            entries = self.window.elements(operations=self.notes)
        except AdapterError as exc:
            if self.pending_window_mode and exc.report.get("reason") == "window_replaced":
                raise AdapterError("unavailable", "Waiting for the new main window and its inverse menu item.",
                                   reason="window_mode_transition") from exc
            raise
        for element, enabled, _ in entries:
            role = element.role
            native = element.native
            path = tuple(element.ax_path.split("/"))
            if role in {"AXList", "AXOutline"}:
                list_paths.add(path)
            if role == "textbox" and ("AXSecureTextField" in
                    (attribute(native, "AXRole"), attribute(native, "AXSubrole"))):
                continue
            value = None
            if role in {"textbox", "text", "checkbox", "radio", "tab", "AXGroup", "AXScrollArea", "AXTabGroup", "AXRadioGroup"}:
                value = attribute(native, "AXValue")
                if value is not None:
                    value = str(value)
            if role == "AXDisclosureTriangle":
                value = attribute(native, "AXExpanded")
            selected = role in {"button", "tab", "radio", "row", "cell"} and bool(attribute(native, "AXSelected"))
            selected = selected or (role in {"tab", "radio"} and value == "1")
            name = element.name or ""
            if role in {"row", "cell"} and path[:-1] in list_paths:
                name = sidebar_label(native) or name
            if role == "textbox" and not name:
                name = str(attribute(native, "AXPlaceholderValue") or "")
            if role == "text":
                # Document text stays in memory. Identify read commands only by
                # a separate accessibility description or widget identifier.
                name = str(attribute(native, "AXDescription") or "")
            if not name and role in {"button", "textbox", "tab", "checkbox", "text"} and element.accessible_id:
                if re.fullmatch(r"[\w.-]{1,180}", element.accessible_id):
                    name = element.accessible_id
            help_text = str(attribute(native, "AXHelp") or "") if name and role in {"button", "textbox", "tab"} else ""
            nodes.append(Node(path, role, name, value, enabled, selected, "invoke" in element.supported_operations,
                              element, help_text, role != "textbox" or settable(native)))
        frame, _ = extract(self.window.title, nodes)
        opaque = not frame.ops
        if opaque:
            visual.observe(self.window, frame, self.notes)
        bar = attribute(self.app_element, "AXMenuBar")
        if bar is not None:
            menus(frame, bar, self.notes)
        if opaque and (any(op["role"] == "visual-sidebar" or op["name"].casefold() in WINDOW_MODES
                           for op in frame.ops.values())):
            frame.conditional.update(key for key, op in frame.ops.items() if conditional_menu(op))
        return frame

    def perform(self, frame, key, value):
        from ApplicationServices import (AXUIElementCopyActionNames, AXUIElementPerformAction,
                                         AXUIElementSetAttributeValue)

        fresh = self.observe()
        if (navigation_id(fresh.state()) != navigation_id(frame.state()) or key not in fresh.ops
                or fresh.ambiguous.keys() != frame.ambiguous.keys()):
            raise CatalogError("The UI changed before delivery.", status="state_changed")
        op = fresh.ops[key]
        ref = fresh.refs[key]
        if self.window.foreground:
            self.window.require_foreground(self.app_element)
        if op["kind"] == "choice" and op["role"] in {"visual-sidebar", "visual-tabs"}:
            visual.select(self.window, ref, value, op)
            return
        if op["kind"] == "menu":
            press_menu(self.window, self.app_element, op)
            self.pending_window_mode = op if op["name"].casefold() in WINDOW_MODES else None
            return
        elif op["kind"] == "field":
            if not settable(ref.native):
                raise CatalogError("The field no longer accepts AXValue.")
            code = AXUIElementSetAttributeValue(ref.native, "AXValue", value)
        elif op["kind"] == "read":
            raise CatalogError("A read operation cannot dispatch input.")
        else:
            target = ref[value] if op["kind"] == "choice" else ref
            if (op["kind"] == "choice" and target.role in {"row", "cell"}
                    and settable(target.native, "AXSelected")):
                code = AXUIElementSetAttributeValue(target.native, "AXSelected", True)
            else:
                error, actions = AXUIElementCopyActionNames(target.native, None)
                if error == 0 and "AXPress" in (actions or ()):
                    code = AXUIElementPerformAction(target.native, "AXPress")
                else:
                    try:
                        self.window.click(target)
                    except AdapterError:
                        raise
                    except (OSError, RuntimeError) as exc:
                        raise CatalogError(str(exc), status="delivery_unconfirmed") from exc
                    return
        if code != 0:
            raise CatalogError("macOS did not confirm the requested AX action.", status="delivery_unconfirmed",
                               ax_error=int(code))
