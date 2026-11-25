"""Set and independently verify the one-month period on the current Stocks chart."""

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from macos_openadapt import AdapterError


BINDING = json.loads((Path(__file__).with_name("adapters") / "stocks-1m.json").read_text(encoding="utf-8"))
GROUP_NAMES = {BINDING["control"]["name"].casefold(), "报告期"}
ONE_MONTH = {BINDING["target"]["name"].casefold(), "1个月"}
POINT_ROLES = {"AXGenericElement", "AXUnknown", "AXDataPoint"}
MONTHS = "january february march april may june july august september october november december".split()
MONTH_NUMBER = {key: i for i, month in enumerate(MONTHS, 1) for key in (month, month[:3])}


@dataclass
class Chart:
    selected: str
    button: object
    samples: list[str]


def observe(window):
    entries = window.elements()
    groups = [e for e, _, _ in entries
              if e.role == BINDING["control"]["role"] and (e.name or "").casefold() in GROUP_NAMES]
    if len(groups) != 1:
        raise AdapterError("unavailable", "Expected one Reporting period control in the chart.",
                           matching_groups=len(groups))
    group = groups[0]
    panel = group.ax_path.rpartition("/")[0]
    if not any(e.ax_path == panel and e.role == "AXGroup" for e, _, _ in entries):
        raise AdapterError("unavailable", "Could not identify the chart panel containing Reporting period.")

    buttons = [(e, enabled) for e, enabled, _ in entries
               if e.role == BINDING["target"]["role"] and (e.name or "").casefold() in ONE_MONTH]
    if len(buttons) != 1:
        raise AdapterError("unavailable", "Expected one one-month button in the window.",
                           matching_buttons=len(buttons))
    button, enabled = buttons[0]
    if not enabled or button.ax_path.rpartition("/")[0] != group.ax_path:
        raise AdapterError("unavailable", "The one-month button is disabled or outside Reporting period.")
    selected = window.value(group)
    if not selected:
        raise AdapterError("unavailable", "The current reporting period is unreadable.")

    # Stocks exposes each sampled price as a sibling of the selector. Its AX
    # description contains the date or time, and its separate AX value is numeric.
    samples = [window.point_label(e) for e, _, _ in entries
               if e.role in POINT_ROLES and e.ax_path.rpartition("/")[0] == panel
               and re.fullmatch(r"[+−-]?\d[\d.,\s]*", window.value(e))]
    return Chart(selected, button, samples)


def parse_date(text):
    # Require an explicit year. Inferring it from today's date would also make
    # a screenshot or a stalled chart from the previous year look current.
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    try:
        if match:
            return date(*map(int, match.groups()))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        match = re.fullmatch(r"([a-z]+) (\d{1,2}), (\d{4})", text.casefold())
        if match and match[1] in MONTH_NUMBER:
            return date(int(match[3]), MONTH_NUMBER[match[1]], int(match[2]))
    except ValueError:
        pass
    return None


def check_chart(samples, as_of):
    previous = as_of.replace(day=1) - timedelta(days=1)
    expected_start = previous.replace(day=min(as_of.day, previous.day))
    parsed = [parse_date(label) for label in samples]
    dates = [d for d in parsed if d is not None]
    evidence = {"as_of": as_of.isoformat(), "expected_start": expected_start.isoformat(),
                "sample_count": len(samples), "dates": [d.isoformat() for d in dates]}
    if len(dates) != len(samples) or len(set(dates)) < 3:
        reason = "insufficient_dated_samples"
    elif dates != sorted(dates) and dates != sorted(dates, reverse=True):
        reason = "unordered_samples"
    else:
        start, end = min(dates), max(dates)
        span = (end - start).days
        evidence["span_days"] = span
        # The app samples the series and omits non-trading days. Both ends
        # must be close to the requested calendar month, in addition to span.
        matches = (21 <= span <= 34 and abs((start - expected_start).days) <= 7
                   and 0 <= (as_of - end).days <= 7)
        reason = "month_covered" if matches else "outside_one_month_window"
    return {**evidence, "matches": reason == "month_covered", "reason": reason}


def run(window, *, check_only=False, timeout=8.0, as_of=None):
    as_of = as_of or date.today()
    snapshot = observe(window)
    before = snapshot.selected
    action = "check_only" if check_only else "already_selected"
    if not check_only and before.casefold() not in ONE_MONTH:
        try:
            window.click(snapshot.button)
        except AdapterError:
            raise
        except (RuntimeError, OSError) as exc:
            raise AdapterError("unverified", str(exc), action="delivery_unconfirmed", before=before) from exc
        action = "clicked"
        snapshot = None  # Only a fresh, post-delivery observation can establish success.

    deadline = time.monotonic() + timeout
    while True:
        try:
            if snapshot is None:
                snapshot = observe(window)
            selected = snapshot.selected.casefold() in ONE_MONTH
            chart = check_chart(snapshot.samples, as_of)
            report = {"status": "verified" if selected and chart["matches"] else "unverified",
                      "action": action, "before": before, "selected": snapshot.selected,
                      "selection_matches": selected, "chart": chart}
        except (OSError, RuntimeError) as exc:
            # A refresh can temporarily replace the panel. Only re-observe it.
            report = {"status": "unverified", "action": action, "before": before, "message": str(exc)}
        remaining = deadline - time.monotonic()
        if report["status"] == "verified" or check_only or remaining <= 0:
            return report
        time.sleep(min(0.25, remaining))
        snapshot = None
