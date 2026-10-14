from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from camera_exploration_lab.analysis import load_analysis
from camera_exploration_lab.db import Database
from camera_exploration_lab.importer import CSVHistoryImporter


class CSVHistoryImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = Database(self.root / "history.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path.read_bytes()

    def test_import_preserves_rows_maps_metrics_and_is_idempotent(self) -> None:
        source = self.root / "camera-recordings/performance/ps3eye/synthetic.csv"
        fields = [
            "mode", "timestamp", "requested_fps", "reported_fps", "measured_fps",
            "frame_number", "elapsed_s", "interval_ms", "read_ms", "note",
        ]
        original_bytes = self.write_csv(
            source,
            fields,
            [
                {
                    "mode": "240p_125", "timestamp": "2026-10-13T02:00:00", "requested_fps": "125",
                    "reported_fps": "125.0", "measured_fps": "124.9", "frame_number": "1",
                    "elapsed_s": "0.008", "interval_ms": "0", "read_ms": "7.9", "note": "first",
                },
                {
                    "mode": "240p_125", "timestamp": "2026-10-13T02:00:01", "requested_fps": "125",
                    "reported_fps": "125.0", "measured_fps": "124.9", "frame_number": "2",
                    "elapsed_s": "0.016", "interval_ms": "8.0", "read_ms": "7.8", "note": "second",
                },
            ],
        )
        importer = CSVHistoryImporter(self.database)

        result = importer.import_file(source)
        duplicate = importer.import_file(source)

        self.assertEqual(result.row_count, 2)
        self.assertEqual(result.measurement_count, 8)
        self.assertFalse(result.already_imported)
        self.assertTrue(duplicate.already_imported)
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(self.database.count("raw_records"), 2)
        self.assertEqual(self.database.count("import_sources"), 1)
        self.assertEqual(self.database.count("measurements"), 8)

        with self.database.connect() as connection:
            raw = connection.execute("SELECT raw_json FROM raw_records ORDER BY row_number").fetchall()
            fps = connection.execute(
                "SELECT metric, value, stream_scope FROM measurements WHERE metric='capture_fps'"
            ).fetchall()
            read_time = connection.execute(
                "SELECT method FROM measurements WHERE metric='capture_read_duration_ms' LIMIT 1"
            ).fetchone()[0]
            setting = connection.execute(
                "SELECT requested_value_json, reported_value_json, verified_value_json, verification_status "
                "FROM setting_snapshots WHERE setting_key='frame_rate' LIMIT 1"
            ).fetchone()

        self.assertEqual(json.loads(raw[0]["raw_json"])["note"], "first")
        self.assertEqual((fps[0]["metric"], fps[0]["stream_scope"], fps[0]["value"]), ("capture_fps", "capture", 124.9))
        self.assertIn("not sensor latency", read_time)
        self.assertEqual(setting["requested_value_json"], "125")
        self.assertEqual(setting["reported_value_json"], "125")
        self.assertIsNone(setting["verified_value_json"])
        self.assertEqual(setting["verification_status"], "reported")

    def test_first_legacy_zero_interval_is_preserved_and_excluded_as_boundary(self) -> None:
        source = self.root / "synthetic-avermedia.csv"
        fields = ["mode", "timestamp", "frame_number", "elapsed_s", "interval_ms"]
        original = self.write_csv(source, fields, [
            {"mode": "720p_mjpeg", "timestamp": "2026-10-14T10:00:00", "frame_number": "1",
             "elapsed_s": "0.025", "interval_ms": "0.000"},
            {"mode": "720p_mjpeg", "timestamp": "2026-10-14T10:00:00.016", "frame_number": "2",
             "elapsed_s": "0.041", "interval_ms": "16.000"},
            {"mode": "720p_mjpeg", "timestamp": "2026-10-14T10:00:00.033", "frame_number": "3",
             "elapsed_s": "0.058", "interval_ms": "17.000"},
        ])
        imported = CSVHistoryImporter(self.database).import_file(source)
        with self.database.connect_readonly() as connection:
            benchmark_id = connection.execute(
                "SELECT id FROM benchmark_runs WHERE session_id=?", (imported.session_id,)
            ).fetchone()[0]
            first = connection.execute(
                """SELECT m.value, m.interval_ns, m.method, rr.raw_json
                   FROM measurements m JOIN raw_records rr ON rr.id=m.raw_record_id
                   WHERE m.benchmark_id=? AND m.metric='capture_frame_interval_ms'
                   ORDER BY rr.row_number LIMIT 1""",
                (benchmark_id,),
            ).fetchone()

        analysis = load_analysis(self.database, benchmark_id, imported.session_id)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(first["value"], 0.0)
        self.assertEqual(first["interval_ns"], 0)
        self.assertEqual(json.loads(first["raw_json"])["interval_ms"], "0.000")
        self.assertEqual(first["method"], "legacy_csv:interval_ms")
        self.assertEqual(analysis.interval_boundary_samples_excluded, 1)
        self.assertEqual(analysis.interval_stats.count, 2)
        self.assertEqual(analysis.interval_stats.minimum, 16.0)
        self.assertEqual(analysis.interval_stats.maximum, 17.0)
        self.assertEqual(analysis.interval_histogram.zero_sample_count, 0)
        self.assertAlmostEqual(analysis.capture_fps_stats.mean or 0.0, 1000 / 16.5)

    def test_canon_live_view_fps_is_separate_from_capture_fps(self) -> None:
        source = self.root / "camera-recordings/performance/canon/live.csv"
        self.write_csv(
            source,
            ["timestamp", "actual_fps", "usb_ms", "phase", "event", "note"],
            [{
                "timestamp": "2026-10-12T03:11:44", "actual_fps": "7.298", "usb_ms": "19.048",
                "phase": "zoom35", "event": "action_end", "note": "35",
            }],
        )
        result = CSVHistoryImporter(self.database).import_file(source)
        self.assertEqual(result.measurement_count, 2)
        with self.database.connect() as connection:
            fps = connection.execute("SELECT metric, stream_scope FROM measurements WHERE metric LIKE '%_fps'").fetchone()
            zoom = connection.execute(
                "SELECT requested_value_json, reported_value_json, verified_value_json, verification_status "
                "FROM setting_snapshots WHERE setting_key='optical_zoom_step'"
            ).fetchone()
        self.assertEqual((fps["metric"], fps["stream_scope"]), ("preview_fps", "preview"))
        self.assertEqual((zoom["requested_value_json"], zoom["reported_value_json"]), ("35", "35"))
        self.assertIsNone(zoom["verified_value_json"])
        self.assertEqual(zoom["verification_status"], "reported")

    def test_avermedia_exposure_is_imported_as_a_request(self) -> None:
        source = self.root / "camera-recordings/performance/webcam/exposure/summary.csv"
        self.write_csv(
            source,
            ["mode", "exposure_value", "measured_fps"],
            [{"mode": "manual_5ms", "exposure_value": "50", "measured_fps": "23.9"}],
        )
        importer = CSVHistoryImporter(self.database)
        result = importer.import_file(source)
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM setting_snapshots WHERE session_id=?", (result.session_id,)
            )
        self.assertTrue(importer.import_file(source).already_imported)
        self.assertEqual(self.database.count("raw_records"), 1)
        with self.database.connect() as connection:
            settings = connection.execute(
                "SELECT setting_key, requested_value_json, reported_value_json, verification_status "
                "FROM setting_snapshots ORDER BY setting_key"
            ).fetchall()
        self.assertEqual(
            [(row["setting_key"], row["requested_value_json"], row["verification_status"]) for row in settings],
            [("exposure_mode", '"manual"', "requested_only"), ("exposure_time_absolute", "50", "requested_only")],
        )

    def test_unknown_camera_keeps_unclassified_fps_scope(self) -> None:
        source = self.root / "other-device/results.csv"
        self.write_csv(source, ["measured_fps"], [{"measured_fps": "14.5"}])
        CSVHistoryImporter(self.database).import_file(source)
        with self.database.connect() as connection:
            row = connection.execute("SELECT metric, stream_scope FROM measurements").fetchone()
        self.assertEqual((row["metric"], row["stream_scope"]), ("measured_fps", "unknown"))

    def test_summary_without_row_timestamps_uses_filename_run_stamp(self) -> None:
        source = self.root / "webcam/webcam_summary_20261013_003542.csv"
        self.write_csv(source, ["mode", "measured_fps"], [{"mode": "640x480", "measured_fps": "23.9"}])
        importer = CSVHistoryImporter(self.database)
        result = importer.import_file(source)
        with self.database.connect() as connection:
            # Simulate an earlier import that retained rows but lacked filename-time enrichment.
            connection.execute("UPDATE sessions SET started_at=NULL, metadata_json='{}' WHERE id=?", (result.session_id,))
        duplicate = importer.import_file(source)
        self.assertTrue(duplicate.already_imported)
        self.assertEqual(self.database.count("raw_records"), 1)
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT started_at, metadata_json FROM sessions WHERE id=?", (result.session_id,)
            ).fetchone()
        self.assertEqual(session["started_at"], "2026-10-13T00:35:42")
        self.assertEqual(json.loads(session["metadata_json"])["start_time_source"], "csv_filename")

    def test_row_timestamps_take_precedence_over_filename_stamp_on_reimport(self) -> None:
        source = self.root / "run_20261013_020000.csv"
        self.write_csv(
            source,
            ["timestamp", "measured_fps"],
            [{"timestamp": "2026-10-13T02:00:17", "measured_fps": "20"}],
        )
        importer = CSVHistoryImporter(self.database)
        result = importer.import_file(source)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE sessions SET metadata_json='{}' WHERE id=?", (result.session_id,)
            )
        self.assertTrue(importer.import_file(source).already_imported)
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT started_at, metadata_json FROM sessions WHERE id=?", (result.session_id,)
            ).fetchone()
        self.assertEqual(session["started_at"], "2026-10-13T02:00:17")
        self.assertEqual(json.loads(session["metadata_json"])["start_time_source"], "row_timestamp")

    def test_rejects_headerless_input_without_partial_database_records(self) -> None:
        source = self.root / "empty.csv"
        source.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            CSVHistoryImporter(self.database).import_file(source)
        self.assertEqual(self.database.count("sessions"), 0)
        self.assertEqual(self.database.count("import_sources"), 0)

    def test_preserves_csv_overflow_fields_in_raw_record(self) -> None:
        source = self.root / "overflow.csv"
        source.write_text("measured_fps\n23.5,extra-value\n", encoding="utf-8")
        CSVHistoryImporter(self.database).import_file(source)
        with self.database.connect() as connection:
            raw_json = connection.execute("SELECT raw_json FROM raw_records").fetchone()[0]
        self.assertEqual(json.loads(raw_json)["_extra_fields"], ["extra-value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
