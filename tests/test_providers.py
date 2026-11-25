"""Check provider contracts without opening an application or a personal library."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from any2cli_core import music, qgis
from any2cli_core.catalog import CatalogError, Frame, empty_catalog, operation, register, remember
from any2cli_core.cli import app_cli
from any2cli_core.engine import input_value


class ProviderTests(unittest.TestCase):
    def test_music_reads_count_entities_once_and_keep_unknown_kinds(self):
        lib = Mock()
        def item(album, kind, duration):
            entity = None if album is None else SimpleNamespace(persistentID=lambda: album)
            return SimpleNamespace(album=lambda: entity, mediaKind=lambda: kind, totalTime=lambda: duration)
        lib.allMediaItems.return_value = [item(9, 2, 1000), item(9, 2, 900), item(None, 99, 0)]
        self.assertEqual(music.aggregate(lib, "item-count"), 3)
        self.assertEqual(music.aggregate(lib, "album-count"), 1)
        self.assertEqual(music.aggregate(lib, "duration-ms"), 1900)
        self.assertEqual(music.aggregate(lib, "media-kinds"), {"song": 2, "other-99": 1})
        lib.allMediaItems.return_value = None
        with self.assertRaisesRegex(ValueError, "did not return"):
            music.aggregate(lib, "item-count")

    def test_offline_help_and_plans_never_start_an_uncached_provider(self):
        cat = empty_catalog(qgis.APP)
        cat["app_path"] = "/moved/QGIS.app"
        frame = Frame("Processing")
        op = operation("algorithm", "qgis_process", "native:centroids", ["Processing"])
        key = frame.add(op)
        remember(cat, frame)
        name = register(cat, key)
        with tempfile.TemporaryDirectory() as folder, patch.object(qgis, "installation") as provider:
            profile = Path(folder) / "gis.json"
            original = json.dumps(cat)
            profile.write_text(original)
            for flag in ("--help", "-h"):
                with self.assertRaises(SystemExit) as exit_code, redirect_stdout(io.StringIO()):
                    app_cli(profile, [name, flag])
                self.assertEqual(exit_code.exception.code, 0)
            with self.assertRaises(CatalogError) as missing:
                app_cli(profile, [name, "--dry-run", "--value", "{}"])
            self.assertEqual(missing.exception.report["status"], "parameters_unavailable")
            self.assertEqual(profile.read_text(), original)
            provider.assert_not_called()
            provider.return_value = ("/Applications/QGIS.app", Path("/Applications/QGIS.app/Contents/MacOS/qgis_process"))
            with patch.object(qgis, "invoke") as invoke, self.assertRaises(CatalogError):
                qgis.run(cat, op["name"], {})
            provider.assert_called_once_with(qgis.APP, None)
            invoke.assert_not_called()

    def test_required_parameters_include_falsey_defaults_and_legacy_contracts(self):
        op = {"kind": "algorithm", "parameters": {"properties": {
            "INPUT": {"x-qgis-optional": False}, "OUTPUT": {"x-qgis-optional": False},
            "KEEP": {"x-qgis-optional": False, "default": False}, "OPTION": {"x-qgis-optional": True}}}}
        with self.assertRaises(CatalogError) as invalid:
            input_value(op, '{"INPUT":null}')
        self.assertEqual(invalid.exception.report["parameters"], ["INPUT", "OUTPUT"])
        self.assertEqual(input_value(op, '{"INPUT":"a","OUTPUT":"b"}'), {"INPUT":"a", "OUTPUT":"b"})

    def test_processing_uses_the_installed_parameter_contract_and_refuses_missing_results(self):
        description = {"algorithm_details": {"id": "native:centroids"},
                       "parameters": {"INPUT": {"description": "Input", "type": {"id": "source"}}}}
        cat = {"application": qgis.APP}
        op = {"name": "native:centroids"}
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "qgis_process"
            binary.touch()
            with patch.object(qgis, "installation", return_value=(root, binary)), \
                 patch.object(qgis, "invoke", return_value=description) as call:
                qgis.prepare_parameters(cat, op)
                qgis.prepare_parameters(cat, op)
                call.assert_called_once_with(binary, "help", op["name"])
                binary.write_bytes(b"new build")
                qgis.prepare_parameters(cat, op)
                self.assertEqual(call.call_count, 2)
                call.return_value = {"progress": "finished"}
                with self.assertRaises(CatalogError) as caught:
                    qgis.run(cat, op["name"], {})
                self.assertEqual(caught.exception.report["status"], "unverified")
