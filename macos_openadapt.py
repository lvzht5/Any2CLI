"""A single macOS window driven by OpenAdapt's native structural backend."""

import subprocess
import sys
import time
from pathlib import Path


class AdapterError(RuntimeError):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.report = {"status": status, "message": message, **details}


def missing_permission(name):
    return AdapterError("permission_required", "Check the launching app in System Settings > Privacy & Security.",
                        missing=[name])


def connect(bundle_id, window_title=None, *, launch=False, app_path=None, foreground=False):
    if sys.platform != "darwin":
        raise AdapterError("unavailable", "Native desktop execution requires macOS.")
    try:
        from AppKit import NSRunningApplication
        from ApplicationServices import AXUIElementCreateApplication
        from openadapt_flow.backends.macos_backend import QuartzMacAXClient
        from openadapt_flow.backends.remote_display import MacWindowClient
    except ImportError as exc:
        raise AdapterError("unavailable", "Install Any2CLI with the 'macos' extra using Python 3.10–3.12.") from exc

    client = MacWindowClient()
    if not client.input_trusted():
        raise missing_permission("accessibility")

    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    deadline = time.monotonic() + 6 if launch else 0
    was_running = bool(apps)

    def open_application():
        target = [str(app_path)] if app_path else ["-b", bundle_id]
        opened = subprocess.run(["/usr/bin/open", *target], capture_output=True, text=True)
        if opened.returncode:
            raise AdapterError("unavailable", "Could not open the installed application.", application=bundle_id)

    if launch and not apps:
        open_application()
        while not apps and time.monotonic() < deadline:
            time.sleep(0.15)
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if len(apps) != 1:
        raise AdapterError("unavailable", "Open one instance of the application and display its window.")
    app = apps[0]
    if app_path and Path(str(app.bundleURL().path())).resolve() != Path(app_path).resolve():
        raise AdapterError("unavailable", "The running application has the requested ID but a different .app path.")
    pid = int(app.processIdentifier())
    app_element = AXUIElementCreateApplication(pid)
    ax_windows = attribute(app_element, "AXWindows") or []
    if launch and was_running and not ax_windows:
        open_application()
    while launch and not ax_windows and time.monotonic() < deadline:
        time.sleep(0.15)
        ax_windows = attribute(app_element, "AXWindows") or []
    windows = [(window, attribute(window, "AXTitle")) for window in ax_windows]
    windows = [(window, str(title)) for window, title in windows if title is not None]
    if window_title is None:
        # An untitled main window is common in custom-drawn apps. Preserve
        # the existing preference for a named window when both are present.
        windows = [pair for pair in windows if pair[1]] or windows
    else:
        windows = [pair for pair in windows if pair[1] == window_title]
    if len(windows) != 1:
        raise AdapterError("unavailable", "Expected one matching window. Select its exact title with --window.",
                           matching_windows=len(windows))
    window, title = windows[0]
    return NativeWindow(app, window, title, QuartzMacAXClient(), client, foreground=foreground)


def attribute(element, name):
    from ApplicationServices import AXUIElementCopyAttributeValue

    error, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


class BoundClient:
    """Pin the window and turn OpenAdapt focus requests into read-only checks."""

    def __init__(self, client, window):
        self.client = client
        self.window = window

    def find_windows(self, owner, title):
        return [w for w in self.client.find_windows(owner, title)
                if (w.pid, w.window_id) == (self.window.pid, self.window.window_id)]

    def activate(self, pid):
        if pid != self.window.pid or self.client.focused_application_pid() != pid:
            raise AdapterError("focus_changed", "The target lost focus. Input stopped.", action="not_clicked")

    def raise_window(self, window):
        if (window.window_id != self.window.window_id or self.client.frontmost_window_id() != window.window_id
                or not self.client.exact_window_focused_main(window)):
            raise AdapterError("focus_changed", "The target window is no longer frontmost.", action="not_clicked")
        return True

    def __getattr__(self, name):
        return getattr(self.client, name)


def operation_client(notes):
    from openadapt_flow.backends.macos_backend import QuartzMacAXClient

    class OperationClient(QuartzMacAXClient):
        def _attr(self, element, key):
            value = super()._attr(element, key)
            if key == "AXChildren" and value is not None and len(value) > 96:
                role = super()._attr(element, "AXRole")
                tabular = role in {"AXTable", "AXGrid"} or (
                    role == "AXOutline" and len(super()._attr(element, "AXColumns") or ()) > 1)
                if tabular:
                    # Skip the entire data subtree. All remaining AX paths keep
                    # their original indices, including the toolbar after it.
                    notes.add("large_data_table_contents_omitted")
                    return ()
            return value

    return OperationClient()


class NativeWindow:
    def __init__(self, app, window, title, ax, client, *, foreground=False):
        self.app = app
        self.ax = ax
        self.window = window
        self.title = title
        self.client = client
        self.pid = int(app.processIdentifier())
        self.foreground = foreground
        self._operation_client = None

    def require_foreground(self, application=None):
        if not self.foreground:
            raise AdapterError("foreground_required", "This operation needs --foreground on the active target window.",
                               action="not_clicked")
        if self.client.focused_application_pid() != self.pid:
            raise AdapterError("focus_changed", "The target lost focus. Input stopped.", action="not_clicked")
        if application is not None:
            from CoreFoundation import CFEqual

            focused = attribute(application, "AXFocusedWindow")
            if focused is None or not CFEqual(focused, self.window):
                raise AdapterError("focus_changed", "The target window lost focus.", action="not_clicked")

    def elements(self, *, operations=None):
        from CoreFoundation import CFEqual

        if self.app.isTerminated():
            raise AdapterError("unavailable", "The selected window is no longer available.")
        observer = self.ax
        if operations is not None:
            if self._operation_client is None:
                self._operation_client = operation_client(operations)
            observer = self._operation_client
        entries, truncated = observer.walk(self.pid, self.title, limit=4000)
        if truncated or not entries:
            raise AdapterError("unavailable", "Could not read a complete tree for the selected window.",
                               reason="tree_incomplete")
        if not CFEqual(entries[0][0].native, self.window):
            raise AdapterError("unavailable", "The observed tree belongs to a different window.",
                               reason="window_replaced")
        return entries

    @staticmethod
    def value(element):
        value = attribute(element.native, "AXValue")
        return str(value).strip() if value is not None else ""

    @staticmethod
    def point_label(element):
        return str(attribute(element.native, "AXDescription") or element.name or "").strip()

    def click(self, element):
        from openadapt_flow.backend import StructuralResolutionRefused
        from openadapt_flow.backends.macos_backend import MacOSBackend, _fingerprint
        from openadapt_flow.ir import StructuralLocator

        self.require_foreground()
        if not self.client.capture_trusted():
            raise missing_permission("screen_recording")
        owners = {str(self.app.localizedName()), str(self.app.executableURL().lastPathComponent())}
        matches = {w.window_id: w for owner in owners for w in self.client.find_windows(owner, self.title)
                   if w.pid == self.pid and w.title == self.title}
        if len(matches) != 1:
            raise AdapterError("unavailable", "Could not bind the AX window to one window-server ID.")
        window = next(iter(matches.values()))

        backend = MacOSBackend(BoundClient(self.client, window), app=window.owner,
                               window_title=self.title, ax_client=self.ax)
        locator = StructuralLocator(role=element.role, name=element.name, window_name=self.title,
                                    automation_id=element.accessible_id if not element.name else None)
        try:
            backend.ensure_foreground()
            try:
                handle = backend.locate_structural(locator)
            except StructuralResolutionRefused:
                if not element.name or not element.accessible_id:
                    raise
                locator = locator.model_copy(update={"automation_id": element.accessible_id})
                handle = backend.locate_structural(locator)
        except AdapterError:
            raise
        except (OSError, RuntimeError) as exc:
            raise AdapterError("unavailable", str(exc), action="not_clicked") from exc
        # OpenAdapt 3.4.0 exposes this fingerprint on the handle, but its
        # constructor is private. Compare the *observed* member of the group
        # before handing the same handle to OpenAdapt's final hit test.
        if attribute(self.window, "AXTitle") != self.title:
            raise AdapterError("unavailable", "The original window closed or changed title before input.")
        if handle is None or handle.target_fingerprint != _fingerprint(element):
            raise AdapterError("unavailable", "The discovered control moved or disappeared before input.")
        try:
            backend.act_structural(locator, handle)
        except StructuralResolutionRefused as exc:
            # Upstream distinguishes a refusal before mouse-down from a failure
            # after delivery began, where the resulting state is unknown.
            raise AdapterError("unavailable", str(exc), action="not_clicked") from exc
