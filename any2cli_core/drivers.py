"""Load only the live driver for the current desktop."""

import sys

from .catalog import CatalogError


class AdapterError(RuntimeError):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.report = {"status": status, "message": message, **details}


def open_driver(app, title=None, *, app_path=None, foreground=False):
    if sys.platform == "win32":
        from .windows import WindowsDriver

        if foreground:
            raise CatalogError("Windows uses UI Automation patterns. Omit --foreground.", status="invalid_input")
        return WindowsDriver.open(app, title, app_path=app_path)
    if sys.platform == "darwin":
        from .native import NativeDriver

        return NativeDriver.open(app, title, app_path=app_path, foreground=foreground)
    raise CatalogError("Live exploration requires a Windows or macOS desktop.")
