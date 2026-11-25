"""Locate selected navigation groups in an otherwise opaque macOS window."""

from dataclasses import dataclass
from functools import lru_cache
import io
import math
import re
from statistics import median

from macos_openadapt import AdapterError, BoundClient, attribute
from .catalog import CatalogError, operation


# Only recognizable navigation destinations participate in automatic probing.
# Other choices in the same confirmed group remain explicitly addressable.
NAVIGATION = {"home", "browse", "discover", "featured", "podcasts", "overview", "library", "recommended", "charts",
              "playlists", "首页", "浏览", "发现", "精选", "播客", "概览", "资料库", "推荐", "歌单广场",
              "排行榜", "歌单", "歌手", "首頁", "瀏覽", "精選", "推薦"}


@dataclass(frozen=True)
class Label:
    name: str
    left: float
    right: float
    y: float
    height: float


@dataclass
class ChoiceGroup:
    labels: dict
    selected: str
    window_id: int


def window_info(window):
    """Bind the original AX window to one CG window, even when titles are empty."""
    from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType

    pos, size = attribute(window.window, "AXPosition"), attribute(window.window, "AXSize")
    if pos is None or size is None:
        return None
    position_ok, point = AXValueGetValue(pos, kAXValueCGPointType, None)
    size_ok, dimensions = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (position_ok and size_ok):
        return None
    bounds = (point.x, point.y, dimensions.width, dimensions.height)
    owners = {str(window.app.localizedName()), str(window.app.executableURL().lastPathComponent())}
    matches = {item.window_id: item for owner in owners for item in window.client.find_windows(owner, window.title)
               if item.pid == window.pid and item.title == window.title
               and all(abs(x - y) < 1.1 for x, y in zip(item.bounds, bounds))}
    return next(iter(matches.values())) if len(matches) == 1 else None


@lru_cache(maxsize=1)
def vision_classes():
    import objc

    objc.loadBundle("Vision", {}, bundle_path="/System/Library/Frameworks/Vision.framework", scan_classes=False)
    objc.registerMetaDataForSelector(b"VNImageRequestHandler", b"performRequests:error:",
                                     {"arguments": {3: {"type_modifier": objc._C_OUT}}})
    return objc.lookUpClass("VNRecognizeTextRequest"), objc.lookUpClass("VNImageRequestHandler")


def recognize(image):
    """Apple Vision OCR on the supplied crop. Coordinates are in crop pixels."""
    from Foundation import NSData, NSDictionary

    text_request, image_handler = vision_classes()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = NSData.dataWithBytes_length_(buf.getvalue(), buf.tell())
    request = text_request.alloc().init()
    request.setRecognitionLanguages_(["zh-Hans", "en-US"])
    handler = image_handler.alloc().initWithData_options_(data, NSDictionary.dictionary())
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        raise AdapterError("unavailable", "Apple Vision could not read this window.", reason="ocr_unavailable")
    for observation in request.results() or ():
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        rect = observation.boundingBox()
        x, y, w, h = rect.origin.x, rect.origin.y, rect.size.width, rect.size.height
        yield Label(str(candidates[0].string()).strip(), x * image.width, (x + w) * image.width,
                    (1 - y - h / 2) * image.height, h * image.height)


def navigation_name(raw):
    # OCR can merge a sidebar icon and its label into one line.
    raw = raw.strip()
    match = re.search(r"[\u3400-\u9fff]", raw)
    if match and not re.search(r"[a-zA-Z]{2,}", raw[:match.start()]):
        raw = raw[match.start():]
    return re.sub(r"^[^\w]+|[^\w]+$", "", raw).strip()


def selected_row(image, group):
    """Find a single filled row across several patches beyond all text boxes."""
    from PIL import ImageStat

    height = median(r.height for r in group)
    right = max(r.right for r in group)
    if right + 1.5 * height >= image.width:
        return None
    colors = []
    for label in group:
        samples = []
        for gap in (.45, .95, 1.45):
            x, y = round(right + height * gap), round(label.y)
            patch = image.crop((x - 2, y - 2, x + 3, y + 3))
            samples.append(ImageStat.Stat(patch).median[:3])
        if max(math.dist(c, samples[0]) for c in samples) > 25:
            return None
        colors.append(tuple(median(channel) for channel in zip(*samples)))
    background = tuple(median(channel) for channel in zip(*colors))
    contrasts = [math.dist(c, background) for c in colors]
    candidates = [row for row, contrast in zip(group, contrasts) if contrast >= 48]
    return candidates[0].name if len(candidates) == 1 else None


def detect(png, window_id):
    from PIL import Image

    with Image.open(io.BytesIO(png)) as frame:
        # Only this strip is sent to on-device OCR during sidebar discovery.
        image = frame.crop((0, 0, round(frame.width * .24), frame.height)).convert("RGB")
    rows = []
    for item in recognize(image):
        name = navigation_name(item.name)
        if (1 < len(name) <= 24 and item.left < image.width * .62 and item.right < image.width * .98
                and .045 * image.height < item.y < .93 * image.height):
            rows.append(Label(name, item.left, item.right, item.y, item.height))
    rows.sort(key=lambda r: r.y)
    groups = []
    for index in range(len(rows) - 2):
        first, second = rows[index:index + 2]
        gap = second.y - first.y
        if not 1.65 * first.height < gap < 4.1 * first.height:
            continue
        group = [first, second]
        for row in rows[index + 2:]:
            if (abs((row.y - group[-1].y) / gap - 1) > .24 or abs(row.left - first.left) > 2.6 * first.height
                    or row.height / first.height < .5 or row.height / first.height > 1.9):
                break
            group.append(row)
        if len(group) < 3 or len({r.name for r in group}) != len(group):
            continue
        groups.append(group)
    maximal = [group for group in groups if not any(set(group) < set(other) for other in groups)]
    found = [(group, selected_row(image, group)) for group in maximal]
    found = [(group, selected) for group, selected in found if selected]
    if len(found) != 1:
        return None
    group, selected = found[0]
    return ChoiceGroup({r.name: r for r in group}, selected, window_id)


def underlined(image, label):
    """Look for a solid centered bar separated from the glyphs underneath."""
    rgb = image.load()
    left = max(2, round(label.left))
    right = min(image.width - 3, round(label.right))
    if right - left < 8:
        return False
    bottom = label.y + label.height / 2
    consecutive = 0
    for y in range(max(0, round(bottom + .04 * label.height)), min(image.height, round(bottom + .66 * label.height))):
        background = rgb[left - 2, y]
        if math.dist(background, rgb[right + 2, y]) > 28:
            consecutive = 0
            continue
        xs = [x for x in range(left, right) if math.dist(rgb[x, y], background) >= 75]
        if (xs and .18 < len(xs) / (right - left) < .9 and len(xs) >= .9 * (xs[-1] - xs[0] + 1)
                and abs((xs[0] + xs[-1] - left - right) / 2) < .19 * (right - left)):
            consecutive += 1
            if consecutive >= max(2, round(label.height * .065)):
                return True
        else:
            consecutive = 0
    return False


def detect_tabs(png, window_id):
    from PIL import Image

    with Image.open(io.BytesIO(png)) as frame:
        width, height = frame.size
        x0, y0 = round(width * .21), round(height * .103)
        image = frame.crop((x0, y0, round(width * .88), round(height * .198))).convert("RGB")
    lines = [r for r in recognize(image) if 1 < len(r.name) <= 20 and r.height >= image.height * .12]
    for first in sorted(lines, key=lambda r: r.left):
        row = sorted((r for r in lines if abs(r.y - first.y) <= .38 * first.height), key=lambda r: r.left)
        if (len(row) < 3 or len(set(r.name for r in row)) != len(row)
                or any(not 0 < b.left - a.right < 3 * first.height for a, b in zip(row, row[1:]))):
            continue
        active = [r.name for r in row if underlined(image, r)]
        if len(active) == 1:
            labels = {r.name: Label(r.name, x0 + r.left, x0 + r.right, y0 + r.y, r.height) for r in row}
            return ChoiceGroup(labels, active[0], window_id)
    return None


def observe(window, frame, notes):
    if not window.client.capture_trusted():
        notes.add("visual_sidebar_requires_screen_recording")
        return
    info = window_info(window)
    if info is None:
        notes.add("visual_window_unmatched")
        return
    if info.bounds[2] < 420 or info.bounds[3] < 240:
        return
    try:
        png, _, _ = window.client.capture(info.window_id)
        sidebar = detect(png, info.window_id)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        notes.add(getattr(exc, "report", {}).get("reason", "visual_sidebar_unavailable"))
        return
    if sidebar is None:
        notes.add("visual_sidebar_not_detected")
        return
    notes.add("visual_sidebar")
    try:
        tabs = detect_tabs(png, info.window_id)
    except (ImportError, OSError, RuntimeError, ValueError):
        tabs = None
    if tabs:
        notes.add("visual_tabs")
        title = sidebar.selected + "页签"
        key = frame.add(operation("choice", "visual-tabs", title, [sidebar.selected], options=list(tabs.labels)),
                        tabs.selected, tabs)
        frame.add(operation("read", "visual-selection", "当前" + title, [sidebar.selected]), tabs.selected)
        for name in tabs.labels:
            if name != tabs.selected and name.casefold() in NAVIGATION:
                frame.probes.append((key, name, key, tabs.selected))
    key = frame.add(operation("choice", "visual-sidebar", "侧栏", options=list(sidebar.labels)), sidebar.selected, sidebar)
    frame.add(operation("read", "visual-selection", "当前页面"), sidebar.selected)
    for name in sidebar.labels:
        if name != sidebar.selected and name.casefold() in NAVIGATION:
            frame.probes.append((key, name, key, sidebar.selected))


def select(window, previous, target, op):
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend

    window.require_foreground()
    info = window_info(window)
    if info is None or info.window_id != previous.window_id:
        raise CatalogError("The window changed before sidebar selection.", status="state_changed")
    backend = RemoteDisplayBackend(client=BoundClient(window.client, info), owner_substr=info.owner,
                                   title_substr=info.title, max_frame_age_s=3, activate_before_input=False)
    png = backend.screenshot()
    current = detect(png, info.window_id)
    if op["role"] == "visual-tabs":
        if not current or [current.selected] != op["scope"]:
            raise CatalogError("The tab's parent page changed.", status="state_changed")
        current = detect_tabs(png, info.window_id)
    if (not current or current.selected != previous.selected or current.labels.keys() != previous.labels.keys()
            or target not in current.labels):
        raise CatalogError("The visual navigation target changed.", status="state_changed")
    label = current.labels[target]
    backend.click(round((label.left + label.right) / 2), round(label.y))
