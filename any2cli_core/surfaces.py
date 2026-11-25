"""The same semantic controls are extracted from GUIrilla and live AX windows."""

from collections import defaultdict
from dataclasses import dataclass

from .catalog import Frame, operation


ROLES = {"AXButton": "button", "AXMenuButton": "button", "AXTabButton": "tab",
         "AXRadioButton": "radio", "AXTextField": "textbox", "AXTextArea": "textbox",
         "AXCheckBox": "checkbox", "AXStaticText": "text", "AXMenuItem": "menuitem",
         "AXLink": "link", "AXPopUpButton": "combobox", "AXComboBox": "combobox",
         "AXRow": "row", "AXCell": "cell"}
CONTAINERS = {"AXGroup", "AXScrollArea", "AXToolbar", "AXTabGroup", "AXRadioGroup",
              "AXList", "AXOutline", "AXSplitGroup", "AXSheet", "AXTable", "AXPopover"}


@dataclass
class Node:
    path: tuple
    role: str
    name: str
    value: object = None
    enabled: bool = True
    selected: bool = False
    pressable: bool = False
    ref: object = None
    help: str = ""
    writable: bool = True


def boolean(value):
    if value is True or str(value).lower() in {"true", "1", "yes"}:
        return True
    if value is False or str(value).lower() in {"false", "0", "no"}:
        return False
    return None


def extract(title, nodes):
    frame = Frame(title)
    children = defaultdict(list)
    scopes = {}
    for node in nodes:
        children[node.path[:-1]].append(node)
    grouped = set()
    targets = {}
    for node in nodes:
        parent_scope = scopes.get(node.path[:-1], ())
        scopes[node.path] = parent_scope
        if node.role in CONTAINERS and node.name:
            scopes[node.path] = (*parent_scope, node.name)
        if node.path in grouped or not node.enabled:
            continue
        member_roles = {"row", "cell"} if node.role in {"AXList", "AXOutline"} else {"tab", "radio"}
        children_here = [n for n in children[node.path] if n.enabled and n.name]
        members = [n for n in children_here if n.role in member_roles]
        # Some selection groups expose button members
        # and an explicit group value. Keep these choices explicitly callable.
        if not members and isinstance(node.value, str) and node.role in CONTAINERS:
            buttons = [n for n in children_here if n.role == "button"]
            if any(n.name == node.value for n in buttons):
                members = buttons
        current = node.value
        marked = [n.name for n in members if n.selected]
        options = [n.name for n in members]
        if current not in options and len(marked) == 1:
            current = marked[0]
        group_name = node.name or {"AXTabGroup": "Tabs", "AXRadioGroup": "Options",
                                   "AXList": "List", "AXOutline": "Outline"}.get(node.role)
        if (node.role in CONTAINERS and group_name and len(options) >= 2
                and len(set(options)) == len(options) and current in options and (not marked or marked == [current])):
            op = operation("choice", node.role, group_name, parent_scope, options=options)
            refs = {n.name: n.ref for n in members}
            key = frame.add(op, current, refs)
            for n in members:
                grouped.add(n.path)
                targets[n.path] = (key, n.name)
                if (n.role in member_roles and node.role in {"AXTabGroup", "AXScrollArea", "AXList", "AXOutline"}
                        and n.name != current):
                    frame.probes.append((key, n.name, key, current))
            continue
        if not node.name:
            continue
        if node.role == "textbox":
            kind = "field" if node.writable else "read"
        elif node.role in {"checkbox", "AXDisclosureTriangle"}:
            kind = "toggle" if node.role == "checkbox" else "expand"
        elif node.role in {"button", "radio", "tab", "menuitem", "link", "combobox"} or node.pressable:
            kind = "press"
        elif node.role == "text" and node.value is not None and node.name != str(node.value):
            kind = "read"
        else:
            continue
        op = operation(kind, node.role, node.name, parent_scope, help_text=node.help)
        value = boolean(node.value) if kind in {"toggle", "expand"} else node.value
        key = frame.add(op, value, node.ref)
        targets[node.path] = (key, None)
        if kind == "expand" and value is False:
            frame.probes.append((key, True, key, False))
    return frame, targets
