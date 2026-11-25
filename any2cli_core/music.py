"""Read aggregates from the public macOS Music library without opening a window."""

from collections import Counter

from .catalog import CatalogError, Frame, empty_catalog, operation, remember


APP = "com.apple.Music"
FRAMEWORK = "/System/Library/Frameworks/iTunesLibrary.framework"
READS = {
    "item-count": ("Media", "Number of media items", ("ITLibrary.allMediaItems",)),
    "playlist-count": ("Playlists", "Number of playlists including system playlists", ("ITLibrary.allPlaylists",)),
    "album-count": ("Albums", "Distinct albums with a persistent ID",
                    ("ITLibrary.allMediaItems", "ITLibMediaItem.album", "ITLibAlbum.persistentID")),
    "duration-ms": ("Duration", "Total duration of media items in milliseconds",
                    ("ITLibrary.allMediaItems", "ITLibMediaItem.totalTime")),
    "artist-count": ("Artists", "Distinct artists with a persistent ID",
                     ("ITLibrary.allMediaItems", "ITLibMediaItem.artist", "ITLibArtist.persistentID")),
    "media-kinds": ("Media", "Media item counts by kind", ("ITLibrary.allMediaItems", "ITLibMediaItem.mediaKind")),
}
MEDIA_KINDS = {1: "unknown", 2: "song", 3: "movie", 4: "podcast", 5: "audiobook", 6: "pdf-booklet",
               7: "music-video", 8: "tv-show", 9: "interactive-booklet", 12: "home-video", 14: "ringtone",
               15: "digital-booklet", 16: "ios-application", 17: "voice-memo", 18: "itunes-u",
               19: "book", 20: "pdf-book", 21: "alert-tone"}


def framework(app):
    if app != APP:
        raise CatalogError("This library interface belongs to Apple Music.")
    try:
        import objc
        objc.loadBundle("iTunesLibrary", {}, bundle_path=FRAMEWORK, scan_classes=False)
        objc.registerMetaDataForSelector(b"ITLibrary", b"libraryWithAPIVersion:error:",
                                         {"arguments": {3: {"type_modifier": objc._C_OUT}}})
    except (ImportError, OSError, RuntimeError) as exc:
        raise CatalogError("The public Music library framework and PyObjC are required.") from exc
    return objc


def discover(app, path=None):
    objc = framework(app)
    frame = Frame("Music Library")
    for index, (name, (group, help_text, selectors)) in enumerate(READS.items()):
        for selector in selectors:
            cls, attr = selector.split(".")
            if not hasattr(objc.lookUpClass(cls), attr):
                break
        else:
            op = operation("library-read", "ITLibrary", name, ["Music Library"], help_text=help_text)
            op.update(group=group, starter_priority=28 - index * 2)
            frame.add(op)
    if not frame.ops:
        raise CatalogError("No supported Music library selectors were found.")
    catalog = empty_catalog(app)
    catalog["sources"] = ["music-library"]
    if path:
        catalog["app_path"] = path
    remember(catalog, frame)
    return catalog, {"library_operations": len(frame.ops)}


def aggregate(lib, name):
    values = lib.allPlaylists() if name == "playlist-count" else lib.allMediaItems()
    if values is None:
        raise CatalogError("The Music library did not return a collection.", status="unverified")
    if name in {"item-count", "playlist-count"}:
        return len(values)
    if name == "duration-ms":
        return sum(int(item.totalTime()) for item in values)
    if name == "media-kinds":
        counts = Counter(int(item.mediaKind()) for item in values)
        return {MEDIA_KINDS.get(k, f"other-{k}"): n for k, n in sorted(counts.items())}
    if name in {"album-count", "artist-count"}:
        entities = (item.album() if name == "album-count" else item.artist() for item in values)
        return len({int(entity.persistentID()) for entity in entities if entity and entity.persistentID()})
    raise CatalogError("Unknown Music library operation.", status="not_found")


def run(catalog, name):
    if name not in READS:
        raise CatalogError("Unknown Music library operation.", status="not_found")
    objc = framework(catalog["application"])
    lib, error = objc.lookUpClass("ITLibrary").libraryWithAPIVersion_error_("1.0", None)
    if lib is None:
        raise CatalogError("Apple Music could not open the default library.", status="unavailable",
                           system_error=int(error.code()) if error else None)
    return {"status": "read", "mode": "music_library", "target": name, "value": aggregate(lib, name)}
