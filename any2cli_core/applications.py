"""Resolve native application identities without starting a process."""

import plistlib
import sys
import unicodedata
from pathlib import Path
from xml.parsers.expat import ExpatError

from .catalog import CatalogError
from .store import bundle_id


def _plist(path):
    try:
        with path.open("rb") as stream:
            data = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
        return {}
    return data if isinstance(data, dict) else {}


def _name(value):
    return unicodedata.normalize("NFC", value).casefold() if isinstance(value, str) else ""


def _names(info):
    return {_name(info.get(key)) for key in ("CFBundleName", "CFBundleDisplayName")}


def _localized_match(app, wanted):
    resources = app / "Contents/Resources"
    table = _plist(resources / "InfoPlist.loctable")
    if any(wanted in _names(entry) for entry in table.values() if isinstance(entry, dict)):
        return True
    for file in resources.glob("*.lproj/InfoPlist.strings"):
        info = _plist(file)
        if not info:
            # Traditional .strings files can use the OpenStep text syntax.
            try:
                from Foundation import NSDictionary
            except ImportError:
                continue
            info = NSDictionary.dictionaryWithContentsOfFile_(str(file)) or {}
        if wanted in _names(info):
            return True
    return False


def resolve_application(subject, roots=None):
    """Return an identity and, when supplied, an exact installation path."""
    path = Path(subject).expanduser()
    if sys.platform == "win32" and roots is None:
        if path.drive or path.parent != Path(".") or "/" in subject or "\\" in subject:
            if path.suffix.casefold() != ".exe" or not path.is_file():
                raise CatalogError("Expected a path to an installed .exe.", path=str(path))
            return bundle_id(path.name), str(path.resolve())
        return bundle_id(subject), None
    if path.name == subject and subject.casefold().endswith(".exe"):
        return bundle_id(subject), None
    if path.is_dir() or "/" in subject:
        info = _plist(path / "Contents/Info.plist")
        if path.suffix.lower() != ".app" or not isinstance(info.get("CFBundleIdentifier"), str):
            raise CatalogError("Expected an installed .app with a bundle identifier.", path=str(path))
        return bundle_id(info["CFBundleIdentifier"]), str(path.resolve())
    if roots is None:
        roots = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")
    folders = [folder for root in roots for folder in (root, root / "Utilities")]
    if (not subject.lower().endswith(".app") and
            not any((folder / (subject + ".app")).is_dir() for folder in folders)):
        try:
            return bundle_id(subject), None
        except CatalogError:
            pass
    wanted = _name(path.stem if path.suffix.lower() == ".app" else path.name)
    if not wanted:
        raise CatalogError("Enter an installed application name or bundle identifier.")
    apps = {p.resolve() for folder in folders for p in folder.glob("*.app") if p.is_dir()}
    matches = []
    for app in sorted(apps):
        info = _plist(app / "Contents/Info.plist")
        identifier = info.get("CFBundleIdentifier")
        if not isinstance(identifier, str):
            continue
        if wanted in {_name(app.stem), *_names(info)} or _localized_match(app, wanted):
            matches.append((bundle_id(identifier), str(app)))
    if len(matches) > 1:
        raise CatalogError("Several installations have this name. Select the exact .app path.",
                           status="ambiguous", candidates=[p for _, p in matches])
    if matches:
        return matches[0]
    # Some reverse-DNS identifiers themselves end in ".app".
    if subject.lower().endswith(".app") and "." in path.stem:
        return bundle_id(subject), None
    raise CatalogError("No installed application matches this name.", status="not_found", application=subject)
