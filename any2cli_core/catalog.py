"""Semantic UI states, observed transitions and generated command definitions."""

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field


KINDS = {"press", "menu", "field", "choice", "toggle", "expand", "read", "algorithm", "library-read"}
SERVICE_KINDS = {"algorithm", "library-read"}
DISCRETE = {"choice", "toggle", "expand"}
OUTCOMES = ("unverified", "observed", "verified")
FORMAT = "any2cli.profile.v1"
WINDOW_MODES = {"切换至迷你模式": "切换至完整模式", "切换至完整模式": "切换至迷你模式",
                "switch to mini player": "switch to full player", "switch to full player": "switch to mini player"}


def inverse_name(name):
    name = name.casefold()
    if name in WINDOW_MODES:
        return WINDOW_MODES[name]
    for first, second in (("show ", "hide "), ("expand ", "collapse "), ("显示", "隐藏"),
                          ("顯示", "隱藏"), ("展开", "收起"), ("展開", "收起")):
        for prefix, replacement in ((first, second), (second, first)):
            if name.startswith(prefix) and len(name) > len(prefix):
                return replacement + name[len(prefix):]
    return None


class CatalogError(ValueError):
    def __init__(self, message, *, status="unavailable", **details):
        super().__init__(message)
        self.report = {"status": status, "message": message, **details}


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def slug(text):
    clean = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", "-", clean).strip("-_") or "operation"


def operation(kind, role, name, scope=(), *, options=(), help_text=""):
    if kind not in KINDS or not isinstance(role, str) or not role or not isinstance(name, str) or not name.strip():
        raise CatalogError("Expected a supported operation kind, role and nonempty control name.")
    if not isinstance(scope, (tuple, list)) or not all(isinstance(s, str) and s for s in scope):
        raise CatalogError("Expected an ordered list of named ancestor controls.")
    if kind == "menu" and not scope:
        raise CatalogError("A menu operation needs its parent menu path.")
    name = name.strip()
    key = "op-" + digest([kind, role, name, scope])
    op = {"id": key, "kind": kind, "role": role, "name": name, "scope": list(scope)}
    if kind == "choice":
        if not isinstance(options, (list, tuple)) or not options or not all(isinstance(v, str) and v for v in options):
            raise CatalogError("Choice operations need named options.")
        op["options"] = list(dict.fromkeys(options))
    if help_text:
        op["help"] = str(help_text)
    return op


@dataclass
class Frame:
    title: str
    ops: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    refs: dict = field(default_factory=dict)
    ambiguous: dict = field(default_factory=dict)
    probes: list = field(default_factory=list)
    conditional: set = field(default_factory=set)

    def add(self, op, value=None, ref=None):
        key = op["id"]
        if key in self.ops or key in self.ambiguous:
            self.ambiguous[key] = op
            self.ops.pop(key, None)
            self.values.pop(key, None)
            self.refs.pop(key, None)
            return key
        self.ops[key] = op
        self.refs[key] = ref
        if value is not None:
            self.values[key] = value
        return key

    def state(self):
        # Free text and business data stay in memory. Only selector states
        # participate in navigation identity, so editing a field creates no page.
        selected = {k: v for k, v in self.values.items()
                    if self.ops[k]["kind"] in DISCRETE or (self.ops[k]["kind"] == "menu" and v is True)}
        choices = {k: sorted(op["options"]) for k, op in self.ops.items() if op["kind"] == "choice"}
        state = {"title": self.title, "operations": sorted(self.ops), "selected": selected, "choices": choices}
        if self.conditional:
            state["conditional_operations"] = sorted(self.conditional)
        return state


def navigation_id(state):
    conditional = set(state.get("conditional_operations", ()))
    if conditional:
        state = {"title": state["title"], "operations": sorted(set(state["operations"]) - conditional),
                 "selected": {k: v for k, v in state["selected"].items() if k not in conditional},
                 "choices": {k: v for k, v in state["choices"].items() if k not in conditional}}
    return "s-" + digest(state)


def merge_state(states, key, state):
    # Conditional OS menu items belong to the inventory. Keyboard focus may
    # enable them without changing the app page; live dispatch rechecks them.
    older = states.get(key, {}).get("conditional_operations", ())
    if older:
        conditional = sorted(set(older).union(state.get("conditional_operations", ())))
        state = {**state, "conditional_operations": conditional,
                 "operations": sorted(set(state["operations"]).union(conditional))}
    states[key] = state


def empty_catalog(app):
    return {"format": FORMAT, "application": app, "root": None, "states": {}, "operations": {},
            "transitions": [], "commands": {}, "sources": [], "projections": [], "recordings": {}}


def merge_op(operations, op):
    key = op["id"]
    if key not in operations:
        operations[key] = dict(op)
    elif op["kind"] == "choice":
        operations[key]["options"] = sorted(set(operations[key]["options"]) | set(op["options"]))
    elif op["kind"] == "algorithm" and op.get("group"):
        operations[key]["group"] = op["group"]
    if op.get("help"):
        operations[key]["help"] = op["help"]
    if "starter_priority" in op:
        operations[key]["starter_priority"] = op["starter_priority"]


def remember(catalog, frame):
    state = frame.state()
    key = navigation_id(state)
    merge_state(catalog["states"], key, state)
    for op in frame.ops.values():
        merge_op(catalog["operations"], op)
    if catalog["root"] is None:
        catalog["root"] = key
    return key


def transition_outcome(edge):
    # Older profiles mixed successful and failed live observations. Only
    # recorded evidence or verified navigation proves an unlabelled outcome.
    if "outcome" in edge:
        return edge["outcome"]
    return "verified" if edge["navigation"] or edge.get("evidence") or edge.get("recording") else "unverified"


def transition(catalog, source, target, key, value=None, *, navigation=False, evidence=None, label=None, recording=None,
               outcome=None):
    edge = {"from": source, "to": target, "operation": key, "value": value, "navigation": bool(navigation)}
    if label:
        edge["label"] = label
    if evidence:
        edge["evidence"] = evidence
    if recording:
        edge["recording"] = recording
    if outcome is not None:
        if outcome not in OUTCOMES:
            raise CatalogError("Unknown transition outcome.")
        edge["outcome"] = outcome
    for previous in catalog["transitions"]:
        if all(previous.get(k) == edge.get(k) for k in ("from", "to", "operation", "value", "label", "evidence")):
            if previous.get("recording") and recording and previous["recording"] != recording:
                continue
            if outcome is not None or "outcome" in previous:
                previous["outcome"] = max(transition_outcome(previous), transition_outcome(edge), key=OUTCOMES.index)
            previous["navigation"] |= edge["navigation"]
            if recording:
                previous["recording"] = recording
            return
    catalog["transitions"].append(edge)


def merge_catalog(existing, incoming):
    if existing["application"] != incoming["application"]:
        raise CatalogError("This profile belongs to a different application.")
    if incoming.get("app_path"):
        if existing.get("app_path", incoming["app_path"]) != incoming["app_path"]:
            raise CatalogError("This profile is bound to a different installed .app path.")
        existing["app_path"] = incoming["app_path"]
    services = {"qgis-process", "music-library"}
    if not existing["root"] or (set(existing["sources"]) <= services and set(incoming["sources"]) - services):
        existing["root"] = incoming["root"]
    for key, state in incoming["states"].items():
        merge_state(existing["states"], key, state)
    for op in incoming["operations"].values():
        merge_op(existing["operations"], op)
    for e in incoming["transitions"]:
        transition(existing, e["from"], e["to"], e["operation"], e["value"],
                   navigation=e["navigation"], evidence=e.get("evidence"), label=e.get("label"),
                   recording=e.get("recording"), outcome=e.get("outcome"))
    existing["sources"] = sorted(set(existing["sources"] + incoming["sources"]))
    existing["projections"] = sorted(set(existing.get("projections", [])) | set(incoming.get("projections", [])))
    existing.setdefault("recordings", {}).update(incoming.get("recordings", {}))


def algorithm_parameters(properties):
    required = [name for name, spec in properties.items()
                if spec.get("x-qgis-optional") is False and "default" not in spec]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def schema(op):
    kind = op["kind"]
    if kind == "algorithm":
        contract = op.get("parameters", {"type": "object"})
        if "properties" in contract and "required" not in contract:
            return algorithm_parameters(contract["properties"])
        return contract
    if kind == "choice":
        return {"type": "string", "enum": op["options"]}
    if kind in {"toggle", "expand"}:
        return {"type": "boolean"}
    return {"type": "string"} if kind == "field" else None


def command_name(catalog, key):
    op = catalog["operations"][key]
    name = slug(op["name"])
    # Make collisions deterministic even for two uncompiled, same-named controls.
    twins = [other for other in catalog["operations"].values() if slug(other["name"]) == name]
    if len(twins) > 1 or catalog["commands"].get(name, key) != key:
        name += "-" + key[3:11]
    return name


def register(catalog, key):
    existing = next((name for name, target in catalog["commands"].items() if target == key), None)
    name = existing or command_name(catalog, key)
    catalog["commands"][name] = key
    return name


def describe(catalog, key):
    op = catalog["operations"][key]
    cmd = next((name for name, target in catalog["commands"].items() if target == key), None)
    edges = [e for e in catalog["transitions"] if e["operation"] == key]
    values = {e["value"] for e in edges if e["value"] is not None and transition_outcome(e) == "verified" and
              catalog["states"][e["to"]]["selected"].get(key) == e["value"]}
    public = {k: v for k, v in op.items() if k not in {"parameters", "parameter_source", "starter_priority"}}
    return {**public, "command": cmd, "input": schema(op),
            "evidence": {"states": sum(key in s["operations"] for s in catalog["states"].values()),
                         "recorded_transitions": len(edges), "confirmed_values": sorted(values)}}


def initial_commands(catalog, count=None):
    size = len(catalog["operations"])
    if count is None:
        count = max(4, min(12, math.isqrt(max(size - 1, 0)) + 1))
    observed = defaultdict(set)
    from_root = set()
    for edge in catalog["transitions"]:
        if edge["from"] != edge["to"] and edge["operation"] and transition_outcome(edge) == "verified":
            observed[edge["operation"]].add(edge["to"])
            if edge["from"] == catalog["root"]:
                from_root.add(edge["operation"])
    root = set(catalog["states"][catalog["root"]]["operations"])
    base = {"choice": 42, "field": 30, "toggle": 22, "expand": 20, "read": 18, "menu": 12, "press": 9,
            "algorithm": 1, "library-read": 18}

    def rank(op):
        score = base[op["kind"]] + 16 * min(len(observed[op["id"]]), 3) + 12 * (op["id"] in root)
        score += 20 * (op["id"] in from_root)
        score += op.get("starter_priority", 0)
        if op["kind"] == "menu":
            scopes = [s.casefold() for s in op["scope"]]
            name = op["name"].casefold()
            if scopes[0] in {"help", "window", "account", "edit", "帮助", "窗口", "账户", "编辑", "編輯", "帳號", "輔助說明"}:
                score -= 16
            if any(s in {"services", "服务", "服務"} for s in scopes):
                score -= 80
            if scopes[0] in {"controls", "playback", "控制", "播放"}:
                score += 14
                if name in {"play", "pause", "播放", "暂停", "暫停"}:
                    score += 10
            if name.startswith(("about ", "关于", "關於")) or name in {"feedback", "意见反馈", "意見反饋"}:
                score -= 30
            if name.startswith(("hide ", "quit ", "隐藏", "退出", "隱藏")) or name in {"delete", "删除", "刪除"}:
                score -= 44
            if name in WINDOW_MODES:
                score += 38
            elif inverse_name(name):
                score += 8
        if op["name"].isdigit():
            score -= 15
        return -score, len(op["scope"]), op["name"].casefold(), op["id"]

    # Discount further members of the same group without discarding strong
    # evidence for multiple useful operations on the same surface.
    group_sizes = defaultdict(int)
    ranked = []
    for op in sorted(catalog["operations"].values(), key=rank):
        group = (op["kind"], op.get("group"), tuple(op["scope"]))
        score, depth, name, key = rank(op)
        ranked.append((score + 8 * group_sizes[group], depth, name, key))
        group_sizes[group] += 1
    return [register(catalog, entry[-1]) for entry in sorted(ranked)[:count]]


def terms(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    words = set(re.findall(r"[a-z0-9]+", text))
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        words.add(block)
        words.update(block[i:i+2] for i in range(len(block) - 1))
    return words


def search(catalog, query):
    q = query.strip().casefold()
    if not q:
        raise CatalogError("Enter a control name, operation ID or search terms.")
    if q in catalog["operations"]:
        return [q]
    if re.fullmatch(r"[a-z0-9_]+:[a-z0-9_]+", q):
        # A provider prefix is shared by the entire Processing catalog.
        prefix = q.split(":", 1)[0] + ":"
        algorithms = [op for op in catalog["operations"].values() if op["kind"] == "algorithm"
                      and op["name"].startswith(prefix)]
        exact = [op["id"] for op in algorithms if op["name"] == q]
        if algorithms:
            return exact or [op["id"] for op in sorted(algorithms, key=lambda op: op["name"])
                             if op["name"].startswith(q)]
    qt = terms(q)
    results = []
    for key, op in catalog["operations"].items():
        name = op["name"].casefold()
        path = " / ".join([*op["scope"], op["name"]]).casefold()
        hay = " ".join([path, op.get("help", ""), *op.get("options", [])]).casefold()
        if q in (key, name, path) or catalog["commands"].get(q) == key:
            score = 1000
        else:
            score = (80 if q in name else 0) + (30 if q in hay else 0) + 10 * len(qt & terms(hay))
        if score:
            results.append((score, key))
    return [key for _, key in sorted(results, key=lambda row: (-row[0], row[1]))]


def resolve(catalog, query):
    if query in catalog["commands"]:
        return catalog["commands"][query]
    if query in catalog["operations"]:
        return query
    matches = search(catalog, query)
    exact = [key for key in matches if query.strip().casefold() in
             (catalog["operations"][key]["name"].casefold(),
              " / ".join([*catalog["operations"][key]["scope"], catalog["operations"][key]["name"]]).casefold())]
    # A literal control name inside a request can disambiguate help-text hits.
    # Independent names in one request still require the caller to select an ID.
    mentioned = {}
    if not exact:
        text = query.casefold()
        for key in matches:
            name = catalog["operations"][key]["name"].casefold()
            if len(name) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", text):
                mentioned[key] = name
    specific = [key for key, name in mentioned.items()
                if not any(name != other and name in other for other in mentioned.values())]
    candidates = exact or specific or matches
    if len(candidates) != 1:
        raise CatalogError("Choose a unique operation by ID." if candidates else "No matching operation in this tree.",
                           status="ambiguous" if candidates else "not_found",
                           candidates=[describe(catalog, key) for key in candidates[:12]])
    return candidates[0]


def route(catalog, start, key, value=None):
    if start not in catalog["states"]:
        raise CatalogError("Unknown starting state.", state=start)
    outgoing = defaultdict(lambda: defaultdict(dict))
    for edge in catalog["transitions"]:
        if edge["operation"]:
            destinations = outgoing[edge["from"]][edge["operation"], edge["value"]]
            if edge["to"] not in destinations or edge["navigation"]:
                destinations[edge["to"]] = edge
    queue = deque([(start, [])])
    seen = {start}
    while queue:
        state_id, path = queue.popleft()
        state = catalog["states"][state_id]
        if key in state["operations"] and (key not in state["choices"] or value in state["choices"][key]):
            return path
        for outcomes in outgoing[state_id].values():
            if len(outcomes) != 1:
                continue
            edge = next(iter(outcomes.values()))
            if edge["navigation"] and edge["to"] not in seen:
                seen.add(edge["to"])
                queue.append((edge["to"], path + [edge]))
    raise CatalogError("The operation is known but no observed navigation path reaches it from this state.",
                       status="no_route", state=start, operation=key)
