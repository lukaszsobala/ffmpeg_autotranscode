"""Unit tests for the logic that does not shell out.

Deliberately stdlib-only (unittest, no pytest) so this runs on a bare board:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autotranscode import config, mux, pipeline
from autotranscode.grain import DENOISE_LADDER, GrainReport, flatten_table
from autotranscode.probe import MediaInfo, Stream
from autotranscode.state import Ledger, Record

TABLE = """filmgrn1
E 0 3000000 1 7391 1
\tp 3 7 0 11 0 1 128 192 256 128 192 256
\tsY 4  0 40 40 41 108 39 255 38
\tsCb 0
\tsCr 0
\tcY 1 -4 1 4
\tcCb 0
\tcCr 0
E 3000000 6000000 1 7391 1
\tp 3 7 0 11 0 1 128 192 256 128 192 256
\tsY 4  0 12 40 13 108 11 255 10
\tsCb 0
\tsCr 0
\tcY 1 -4 1 4
\tcCb 0
\tcCr 0
"""


def make_info(**overrides) -> MediaInfo:
    base = dict(
        path=Path("/library/Film (1975).mkv"), size_bytes=20_000_000_000, duration=7200.0,
        container="matroska", codec="h264", width=1920, height=1080, pix_fmt="yuv420p",
        bit_depth=8, fps=23.976, frames=172_600, video_bitrate=20_000_000,
    )
    base.update(overrides)
    return MediaInfo(**base)


class TestGrainTable(unittest.TestCase):
    def test_flatten_spans_whole_video_and_keeps_one_segment(self):
        out = flatten_table(TABLE)
        e_lines = [line for line in out.splitlines() if line.startswith("E ")]
        self.assertEqual(len(e_lines), 1)
        fields = e_lines[0].split()
        self.assertEqual(fields[1], "0")
        self.assertEqual(fields[2], "9223372036854775807")

    def test_flatten_preserves_seed_and_parameter_lines_verbatim(self):
        out = flatten_table(TABLE)
        self.assertIn("7391", out.splitlines()[1])
        self.assertIn("\tp 3 7 0 11 0 1 128 192 256 128 192 256", out)
        self.assertTrue(out.startswith("filmgrn1\n"))
        self.assertTrue(out.endswith("\n"))

    def test_flatten_picks_median_strength_segment(self):
        # Two segments: strengths 158 and 46. The median of an even count lands on
        # index len//2 == 1 after sorting, i.e. the stronger one.
        self.assertIn("sY 4  0 40", flatten_table(TABLE))

    def test_flatten_rejects_junk(self):
        for bad in ("", "nope\n", "filmgrn1\n", "filmgrn1\nE 0 1\n"):
            with self.assertRaises(ValueError):
                flatten_table(bad)

    def test_denoise_ladder_is_ordered(self):
        bounds = [b for b, _ in DENOISE_LADDER]
        self.assertEqual(bounds, sorted(bounds), "ladder bounds must ascend")


class TestConfig(unittest.TestCase):
    def test_profile_defaults_then_file_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[machine]\nprofile = "rk3588"\n[encode]\ncrf = 25\n')
            cfg = config.load(path)
            self.assertEqual(cfg.encode.preset, 8)   # from the rk3588 profile
            self.assertEqual(cfg.encode.crf, 25)     # overridden by the file
            self.assertEqual(cfg.machine.workers, 2)

    def test_unknown_keys_and_sections_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[encode]\nnonsense = 1\n")
            with self.assertRaises(config.ConfigError):
                config.load(path)
            path.write_text("[nonsense]\nx = 1\n")
            with self.assertRaises(config.ConfigError):
                config.load(path)

    def test_validation_catches_impossible_combinations(self):
        cfg = config.Config()
        cfg.encode.crf = 99
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

        cfg = config.Config()
        cfg.output.container = "webm"
        cfg.encode.concat = "mkvmerge"   # mkvmerge cannot produce webm
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestSkipRules(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config()

    def test_skips_existing_av1(self):
        info = make_info(codec="av1")
        with self.assertRaisesRegex(pipeline.SkipFile, "already av1"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_skips_dolby_vision_rather_than_silently_flattening_it(self):
        info = make_info(has_dolby_vision=True)
        with self.assertRaisesRegex(pipeline.SkipFile, "Dolby Vision"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_skips_short_clips(self):
        info = make_info(duration=12.0)
        with self.assertRaisesRegex(pipeline.SkipFile, "shorter than"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_refuses_to_overwrite_the_source(self):
        info = make_info()
        with self.assertRaisesRegex(pipeline.SkipFile, "equals source"):
            pipeline.check_skip(info, info.path, self.cfg)

    def test_accepts_an_ordinary_file(self):
        pipeline.check_skip(make_info(), Path("/out/definitely-new.mkv"), self.cfg)

    def test_output_path_defaults_next_to_source(self):
        info = make_info()
        self.assertEqual(
            pipeline.output_path(info, self.cfg),
            Path("/library/Film (1975).av1.mkv"),
        )


class TestAudioPolicy(unittest.TestCase):
    def audio_args(self, codecs_channels, mode="smart"):
        cfg = config.Config()
        cfg.audio.mode = mode
        info = make_info(audio=[
            Stream(index=i + 1, codec=c, codec_type="audio", channels=ch)
            for i, (c, ch) in enumerate(codecs_channels)
        ])
        return " ".join(mux._audio_args(info, cfg))

    def test_smart_copies_efficient_and_reencodes_wasteful(self):
        args = self.audio_args([("eac3", 6), ("truehd", 8)])
        self.assertIn("-c:a:0 copy", args)
        self.assertIn("-c:a:1 libopus", args)
        self.assertIn("-b:a:1 512k", args)  # 8 channels * 64

    def test_surround_opus_gets_mapping_family(self):
        self.assertIn("-mapping_family:a:0 1", self.audio_args([("dts", 6)]))

    def test_stereo_opus_does_not_get_mapping_family(self):
        self.assertNotIn("mapping_family", self.audio_args([("dts", 2)]))

    def test_copy_mode_never_reencodes(self):
        args = self.audio_args([("truehd", 8)], mode="copy")
        self.assertIn("-c:a:0 copy", args)
        self.assertNotIn("libopus", args)


class TestDiscovery(unittest.TestCase):
    def test_finds_media_dedupes_and_reads_txt_lists(self):
        cfg = config.Config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            wanted = [root / "a.mkv", root / "sub" / "b.mp4"]
            for f in wanted:
                f.write_bytes(b"x")
            (root / "notes.nfo").write_bytes(b"x")       # wrong extension
            listing = root / "list.txt"
            listing.write_text(f"# comment\n\n{wanted[0]}\n/does/not/exist.mkv\n")

            found = pipeline.discover([root, listing], cfg)
            self.assertEqual(sorted(p.name for p in found), ["a.mkv", "b.mp4"])

    def test_non_recursive_stays_shallow(self):
        cfg = config.Config()
        cfg.library.recursive = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "a.mkv").write_bytes(b"x")
            (root / "sub" / "b.mkv").write_bytes(b"x")
            self.assertEqual([p.name for p in pipeline.discover([root], cfg)], ["a.mkv"])


class TestLedger(unittest.TestCase):
    def test_roundtrip_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "film.mkv"
            source.write_bytes(b"0" * 100)
            state = Path(tmp) / "state.json"

            ledger = Ledger(state)
            self.assertIsNone(ledger.already_done(source))
            ledger.record(source, Record(
                source=str(source), status="done", output="/out/film.av1.mkv",
                source_bytes=100, output_bytes=40,
            ))

            reloaded = Ledger(state)
            done = reloaded.already_done(source)
            self.assertIsNotNone(done)
            self.assertAlmostEqual(done.saving_percent, 60.0)

    def test_changed_source_is_not_considered_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "film.mkv"
            source.write_bytes(b"0" * 100)
            state = Path(tmp) / "state.json"
            ledger = Ledger(state)
            ledger.record(source, Record(source=str(source), status="done"))

            source.write_bytes(b"0" * 200)   # replaced with a different file
            self.assertIsNone(Ledger(state).already_done(source))


class TestGrainReport(unittest.TestCase):
    def test_describe_mentions_the_decision(self):
        report = GrainReport(38.0, [38.0], [1.0], grainy=True, denoise_filter="hqdn3d=1.4:1.0:6:6")
        text = report.describe()
        self.assertIn("38.00", text)
        self.assertIn("grain route", text)


if __name__ == "__main__":
    unittest.main()
