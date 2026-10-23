from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import unittest

from camera_exploration_lab.performance import Availability, ClockDomain, Metric
from camera_exploration_lab.replay import (
    ReplayController,
    ReplayFormatError,
    load_builtin_replays,
    parse_replay_csv,
)

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "docs" / "research" / "canon-sx40-live-view"
DATA = RESEARCH / "results"


class PublishedReplayDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.datasets = load_builtin_replays()
        cls.by_name = {dataset.filename: dataset for dataset in cls.datasets}

    def test_six_packaged_csvs_match_manifest_hashes_rows_and_public_copies(self) -> None:
        manifest = json.loads((RESEARCH / "manifest.json").read_text(encoding="utf-8"))
        resources = ROOT / "src" / "camera_exploration_lab" / "replay_data" / "canon_live_view"
        self.assertEqual(len(self.datasets), 6)
        self.assertEqual(sum(len(dataset.rows) for dataset in self.datasets), 862)
        for entry in manifest:
            name = entry["public_file"]
            source = (DATA / name).read_bytes()
            bundled = (resources / name).read_bytes()
            self.assertEqual(source, bundled, name)
            self.assertEqual(hashlib.sha256(bundled).hexdigest(), entry["public_sha256"], name)
            self.assertEqual(len(self.by_name[name].rows), entry["rows"], name)

    def test_replay_timeline_concatenates_stage_local_elapsed_without_claiming_clock_sync(self) -> None:
        dataset = self.by_name["01_initial_fps_sweep.csv"]
        self.assertLess(dataset.rows[29].elapsed_s, dataset.rows[28].elapsed_s)
        replay_times = [row.replay_elapsed_s for row in dataset.rows]
        self.assertTrue(all(right > left for left, right in zip(replay_times, replay_times[1:])))
        self.assertEqual(dataset.rows[29].sample.clock_domain, ClockDomain.SOURCE_ELAPSED)
        self.assertGreater(dataset.duration_seconds, 150)

    def test_metrics_preserve_payload_rate_content_change_and_gui_statistic_separately(self) -> None:
        sweep = self.by_name["01_initial_fps_sweep.csv"].rows[5].sample
        self.assertEqual(sweep.get(Metric.REQUESTED_FPS).value, 30.0)
        self.assertEqual(sweep.get(Metric.CHDKPTP_GUI_REPORTED_FPS).availability, Availability.AVAILABLE)
        self.assertEqual(sweep.get(Metric.CHDKPTP_FETCH_FPS).availability, Availability.NOT_RECORDED)
        self.assertEqual(sweep.get(Metric.CHDKPTP_FETCH_DURATION_MS).availability, Availability.AVAILABLE)
        self.assertEqual(sweep.get(Metric.GUI_PREVIEW_UPDATE_FPS).availability, Availability.UNAVAILABLE)
        self.assertEqual(sweep.get(Metric.GUI_PRESENTATION_FPS).availability, Availability.UNAVAILABLE)
        self.assertEqual(sweep.get(Metric.SENSOR_EXPOSURE_FPS).availability, Availability.UNAVAILABLE)

        fresh = self.by_name["06_content_freshness.csv"].rows[5].sample
        self.assertEqual(fresh.get(Metric.CHDKPTP_FETCH_FPS).availability, Availability.AVAILABLE)
        self.assertEqual(fresh.get(Metric.CONTENT_CHANGE_RATE).kind.value, "estimated")
        self.assertNotEqual(
            fresh.get(Metric.CHDKPTP_FETCH_FPS).value,
            fresh.get(Metric.CONTENT_CHANGE_RATE).value,
        )

    def test_settling_status_is_retained_instead_of_filtered_out(self) -> None:
        dataset = self.by_name["04_device_polling_ab.csv"]
        self.assertEqual(dataset.rows[0].status, "settling")
        self.assertIn("ok", {row.status for row in dataset.rows})

    def test_dataset_one_dropped_counter_is_not_mislabeled_as_sensor_drops(self) -> None:
        row = self.by_name["01_initial_fps_sweep.csv"].rows[10].sample
        self.assertEqual(row.get(Metric.CHDKPTP_DROPPED_COUNTER).availability, Availability.AVAILABLE)
        self.assertEqual(row.get(Metric.SOFTWARE_SKIPS).availability, Availability.NOT_RECORDED)


class ReplayParserValidationTests(unittest.TestCase):
    def test_malformed_numeric_elapsed_and_status_values_are_rejected(self) -> None:
        path = DATA / "04_device_polling_ab.csv"
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        original = lines[1].split(",")
        header = lines[0].split(",")

        bad_elapsed = original.copy()
        bad_elapsed[header.index("elapsed_s")] = "nan"
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv(path.name, "\n".join([lines[0], ",".join(bad_elapsed), *lines[2:]]) + "\n")

        bad_metric = original.copy()
        bad_metric[header.index("target_fps")] = "fast"
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv(path.name, "\n".join([lines[0], ",".join(bad_metric), *lines[2:]]) + "\n")

        bad_status = original.copy()
        bad_status[header.index("status")] = ""
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv(path.name, "\n".join([lines[0], ",".join(bad_status), *lines[2:]]) + "\n")

    def test_sequence_must_have_monotonic_elapsed_within_each_stage(self) -> None:
        path = DATA / "01_initial_fps_sweep.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            fields = list(rows[0])
        rows[2]["elapsed_s"] = rows[1]["elapsed_s"]
        output = __import__("io").StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv(path.name, output.getvalue())

    def test_unknown_dataset_and_manifest_row_mismatch_are_rejected(self) -> None:
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv("unknown.csv", "")
        path = DATA / "06_content_freshness.csv"
        with self.assertRaises(ReplayFormatError):
            parse_replay_csv(path.name, path.read_text(encoding="utf-8"), expected_rows=1)

    def test_blank_measurement_is_retained_as_not_recorded_not_fabricated(self) -> None:
        path = DATA / "04_device_polling_ab.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or ())
            rows = list(reader)
        rows[4]["avg_usb_ms"] = ""
        output = __import__("io").StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        parsed = parse_replay_csv(path.name, output.getvalue())
        reading = parsed.rows[4].sample.get(Metric.CHDKPTP_FETCH_DURATION_MS)
        self.assertEqual(reading.availability, Availability.NOT_RECORDED)
        self.assertIsNone(reading.value)


class ReplayControllerTests(unittest.TestCase):
    def test_play_pause_resume_and_reset_use_injected_monotonic_timestamps(self) -> None:
        dataset = load_builtin_replays()[3]
        timeline = [row.replay_elapsed_s - dataset.rows[0].replay_elapsed_s for row in dataset.rows]
        controller = ReplayController(dataset)

        controller.play(5_000_000_000)
        self.assertTrue(controller.playing)
        controller.advance(5_000_000_000 + round(timeline[8] * 1_000_000_000))
        self.assertEqual(controller.index, 8)

        paused_at = 9_000_000_000 + round(timeline[10] * 1_000_000_000)
        controller.pause(paused_at)
        paused_index = controller.index
        self.assertFalse(controller.playing)
        controller.advance(paused_at + 20_000_000_000)
        self.assertEqual(controller.index, paused_index)

        controller.play(30_000_000_000)
        controller.advance(30_000_000_000 + 1_000_000_000)
        self.assertGreaterEqual(controller.index, paused_index)
        controller.reset()
        self.assertEqual(controller.index, 0)
        self.assertEqual(controller.position_seconds, 0.0)
        self.assertFalse(controller.playing)

    def test_replay_finishes_at_last_recorded_row(self) -> None:
        dataset = load_builtin_replays()[2]
        controller = ReplayController(dataset)
        controller.play(1_000_000_000)
        controller.advance(1_000_000_001 + round(dataset.duration_seconds * 1_000_000_000))
        self.assertEqual(controller.index, len(dataset.rows) - 1)
        self.assertFalse(controller.playing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
