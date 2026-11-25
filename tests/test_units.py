"""Unit tests for the logic that does not shell out.

Deliberately stdlib-only (unittest, no pytest) so this runs on a bare board:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import itertools
import json
import locale
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from autotranscode import (
    __version__,
    clean,
    cli,
    config,
    cuts,
    encode,
    essential,
    estimate,
    grain,
    mux,
    pipeline,
    probe,
    progress,
    regrain,
    resume,
    sample,
    scenegrain,
    scenes,
    tools,
    update,
    util,
    verify,
    versions,
    watch,
)
from autotranscode.grain import DENOISE_LADDER, GrainReport, flatten_table
from autotranscode.probe import ContentLight, DolbyVision, MasteringDisplay, MediaInfo, Stream
from autotranscode.state import Guess, Ledger, Record
from autotranscode.tools import Toolchain
from autotranscode.util import MIB, CommandError


class _CurrentStderr(logging.StreamHandler):
    """Writes each record to whatever sys.stderr is at that moment: under a
    buffered run, the test's own buffer."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


# Many tests take the failure paths on purpose, and their warnings and errors
# looked like real ones in the output. The package's log goes to the current
# stderr (not to the handler cli.main sets on the root logger, which keeps the
# stderr of the test that made it), and load_tests buffers each test's output:
# a passing test shows nothing, a failing one shows its log with the failure.
util.log.addHandler(_CurrentStderr())
util.log.setLevel(logging.INFO)
util.log.propagate = False


class _BufferedSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        result.buffer = True
        return super().run(result, debug)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None):
    return _BufferedSuite(tests)


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


# Correct HDR10 signalling, which a DV base layer must carry to be believed.
HDR10_TAGS = dict(
    color_primaries="bt2020", color_transfer="smpte2084", color_space="bt2020nc",
)


T = TypeVar("T")


def present(value: T | None) -> T:
    """Fail the test on None, and tell the type checker it cannot be None after.

    assertIsNotNone does the first but not the second, so a test that goes on
    to use the value reads as an error in the editor.
    """
    if value is None:
        raise AssertionError("expected a value, got None")
    return value


def make_info(**overrides: Any) -> MediaInfo:
    base: dict[str, Any] = dict(
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
            self.assertEqual(cfg.encode.preset, 4)   # from the rk3588 profile
            self.assertEqual(cfg.encode.crf, 25)     # overridden by the file
            self.assertEqual(cfg.machine.workers, 1)

    def test_the_profile_flag_takes_the_files_profile_and_leaves_its_settings(self):
        # Order: defaults, profile, file, flags. --profile used to be applied after
        # the file, so its preset 4 and crf 28 overwrote the file's 6 and 26.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[machine]\nprofile = "generic"\n[encode]\npreset = 6\ncrf = 26\n')
            args = cli.build_parser().parse_args(
                ["-c", str(path), "--profile", "workstation", "plan", "x.mkv"])
            cfg = cli.load_config(args)
            self.assertEqual((cfg.encode.preset, cfg.encode.crf), (6, 26))
            self.assertEqual(cfg.machine.profile, "workstation")
            self.assertEqual(cfg.machine.workers, 0, "the profile's own settings still apply")
            args = cli.build_parser().parse_args(
                ["-c", str(path), "--profile", "workstation", "--crf", "24", "plan", "x.mkv"])
            self.assertEqual(cli.load_config(args).encode.crf, 24, "a flag still beats the file")

    def test_auto_profile_follows_the_device_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[machine]\nprofile = "auto"\n')
            for board, profile, workers in ((b"radxa,rock-5b\0rockchip,rk3588\0", "rk3588", 1),
                                            (None, "generic", 0)):
                read = unittest.mock.Mock(return_value=board, side_effect=None if board
                                          else FileNotFoundError())
                with unittest.mock.patch.object(Path, "read_bytes", read):
                    cfg = config.load(path)
                self.assertEqual((cfg.machine.profile, cfg.machine.workers), (profile, workers))

    def test_every_profile_encodes_at_preset_4(self):
        # The settings were all judged at preset 4, so no profile may leave it.
        for profile in config.PROFILES:
            with self.subTest(profile=profile):
                cfg = config.Config()
                config._merge_profile(cfg, profile)
                self.assertEqual(cfg.encode.preset, 4)

    def test_a_config_without_a_profile_is_auto(self):
        self.assertEqual(config.Config().machine.profile, "auto")

    def test_unknown_keys_and_sections_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[encode]\nnonsense = 1\n")
            with self.assertRaises(config.ConfigError):
                config.load(path)
            path.write_text("[nonsense]\nx = 1\n")
            with self.assertRaises(config.ConfigError):
                config.load(path)

    def _load(self, text: str) -> config.Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(text)
            return config.load(path)

    def test_a_value_of_the_wrong_type_names_the_option_and_the_type(self):
        # A string preset ended in a TypeError traceback. A string of extensions
        # was iterated letter by letter, and the library came up empty in silence.
        for text, option, wanted in (
                ('[encode]\npreset = "4"\n', "encode.preset", "a whole number"),
                ("[encode]\ncrf = 30.5\n", "encode.crf", "a whole number"),
                ('[library]\nextensions = ".mkv"\n', "library.extensions", "a list of strings"),
                ('[library]\nskip_codecs = "av1"\n', "library.skip_codecs", "a list of strings"),
                ("[staging]\nenabled = 1\n", "staging.enabled", "true or false"),
                ("[machine]\nworkers = true\n", "machine.workers", "a whole number"),
                ('[encode]\nac_bias = "1"\n', "encode.ac_bias", "a number or a table")):
            with self.subTest(text=text), \
                    self.assertRaisesRegex(config.ConfigError, f"{option} must be {wanted}"):
                self._load(text)

    def test_numbers_and_tables_are_accepted_where_they_belong(self):
        cfg = self._load('[grain]\nthreshold_db = 44\n[encode]\nac_bias = 1\n'
                         'tf_strength = {"1700" = 1, "0" = -1}\nvariance_boost_curve = 2\n')
        self.assertIsInstance(cfg.grain.threshold_db, float)
        self.assertEqual(cfg.encode.tf_strength, {"1700": 1, "0": -1})
        self.assertEqual(cfg.encode.variance_boost_curve, 2)

    def test_every_default_has_the_type_its_option_declares(self):
        cfg = config.Config()
        for section in dataclasses.fields(cfg):
            for option in dataclasses.fields(getattr(cfg, section.name)):
                value = getattr(getattr(cfg, section.name), option.name)
                config._check_type(value, str(option.type), f"{section.name}.{option.name}")

    def test_counts_and_rates_must_be_positive(self):
        for text in ("[grain]\nsamples = 0\n", "[grain]\nsample_seconds = -3\n",
                     "[audio]\nopus_kbps_per_channel = 0\n", "[estimate]\nmin_samples = 0\n",
                     "[estimate]\nmin_samples = 5\nmax_samples = 4\n",
                     "[estimate]\nsample_seconds = 0\n", "[machine]\nworkers = -1\n"):
            with self.subTest(text=text), self.assertRaises(config.ConfigError):
                self._load(text)

    def test_validation_catches_impossible_combinations(self):
        cfg = config.Config()
        cfg.encode.crf = 99
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestSkipRules(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config()

    def test_skips_existing_av1(self):
        info = make_info(codec="av1")
        with self.assertRaisesRegex(pipeline.SkipFile, "already av1"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_with_an_hdr10_base_layer_is_transcoded(self):
        # Profile 8.1: the base layer is plain HDR10, which is what Jellyfin serves
        # when it transcodes these on the fly. Losing only the RPU is acceptable.
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=8, compatibility_id=1, rpu_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "hdr10")
        pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_profile_7_base_layer_is_transcoded(self):
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=7, compatibility_id=6, rpu_present=True,
            el_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "hdr10")
        pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_profile_5_is_skipped_by_default(self):
        # No cross-compatible base layer: without the RPU it decodes to skewed
        # colour, so declining is better than shipping a broken picture.
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=5, compatibility_id=0, rpu_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "none")
        with self.assertRaisesRegex(pipeline.SkipFile, "no base layer"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_profile_5_is_accepted_when_tonemapping_is_enabled(self):
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=5, compatibility_id=0, rpu_present=True, bl_present=True))
        self.cfg.dolby_vision.profile5 = "tonemap-sdr"
        pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_mode_skip_declines_everything(self):
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=8, compatibility_id=1, rpu_present=True, bl_present=True))
        self.cfg.dolby_vision.mode = "skip"
        with self.assertRaisesRegex(pipeline.SkipFile, "Dolby Vision"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_profile_8_with_no_compatibility_id_is_out_of_spec_not_profile_5(self):
        # Profile 8 always has a cross-compatible base layer, so an id of 0 is
        # malformed rather than meaningful. Jellyfin falls back to the colour tags
        # here; so do we, which means the file is still transcoded.
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=8, compatibility_id=0, rpu_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "invalid")
        pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_profile_10_may_legitimately_signal_no_base_layer(self):
        info = make_info(**HDR10_TAGS, dolby_vision=DolbyVision(
            profile=10, compatibility_id=0, rpu_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "none")
        with self.assertRaisesRegex(pipeline.SkipFile, "no base layer"):
            pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_dolby_vision_claiming_hdr10_while_tagged_bt709_is_not_trusted(self):
        # Out of spec: encode what the colour tags say, since that is what the
        # decoder hands us, rather than what the DV signalling claims.
        info = make_info(
            color_primaries="bt709", color_transfer="bt709", color_space="bt709",
            dolby_vision=DolbyVision(profile=8, compatibility_id=1,
                                     rpu_present=True, bl_present=True))
        self.assertEqual(info.base_layer, "invalid")
        pipeline.check_skip(info, Path("/out/x.mkv"), self.cfg)

    def test_skips_sources_whose_pixel_format_cannot_be_read(self):
        # Observed on a malformed mkv: ffprobe reports pix_fmt=unknown and Av1an
        # then dies with "missing field `pix_fmt`" after the grain analysis has
        # already run.
        for bad in ("unknown", ""):
            with self.assertRaisesRegex(pipeline.SkipFile, "pixel format"):
                pipeline.check_skip(make_info(pix_fmt=bad), Path("/out/x.mkv"), self.cfg)

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

    def test_an_output_directory_mirrors_the_library_folders(self):
        # Two shows with an S01E01.mkv each must not share one output name.
        self.cfg.output.directory = "/out"
        roots = [Path("/library")]
        a = make_info(path=Path("/library/Show A/Season 1/S01E01.mkv"))
        b = make_info(path=Path("/library/Show B/Season 1/S01E01.mkv"))
        self.assertEqual(pipeline.output_path(a, self.cfg, roots),
                         Path("/out/Show A/Season 1/S01E01.av1.mkv"))
        self.assertEqual(pipeline.output_path(b, self.cfg, roots),
                         Path("/out/Show B/Season 1/S01E01.av1.mkv"))

    def test_a_film_lands_in_the_same_place_from_a_nested_root(self):
        self.cfg.output.directory = "/out"
        info = make_info(path=Path("/library/Films/Film (1975)/Film (1975).mkv"))
        whole = pipeline.output_path(info, self.cfg, [Path("/library")])
        both = pipeline.output_path(info, self.cfg, [Path("/library/Films"), Path("/library")])
        self.assertEqual(whole, Path("/out/Films/Film (1975)/Film (1975).av1.mkv"))
        self.assertEqual(both, whole)

    def test_a_file_outside_every_root_goes_to_the_top(self):
        self.cfg.output.directory = "/out"
        info = make_info(path=Path("/elsewhere/Film (1975).mkv"))
        self.assertEqual(pipeline.output_path(info, self.cfg, [Path("/library")]),
                         Path("/out/Film (1975).av1.mkv"))

    def test_only_directories_among_the_inputs_are_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "library"
            folder.mkdir()
            film = Path(tmp) / "film.mkv"
            film.write_bytes(b"x")
            self.assertEqual(pipeline.library_roots([folder, film]), [folder])

    def test_an_unused_name_counts_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            taken = Path(tmp) / "Film.av1.mkv"
            self.assertEqual(pipeline.unused_name(taken).name, "Film.av1.new.mkv")
            (Path(tmp) / "Film.av1.new.mkv").write_bytes(b"x")
            self.assertEqual(pipeline.unused_name(taken).name, "Film.av1.new2.mkv")


class TestAudioPolicy(unittest.TestCase):
    def audio_args(self, codecs_channels, mode="smart", layout="", max_channels=0):
        cfg = config.Config()
        cfg.audio.mode = mode
        cfg.audio.max_channels = max_channels
        info = make_info(audio=[
            Stream(index=i + 1, codec=c, codec_type="audio", channels=ch, channel_layout=layout)
            for i, (c, ch) in enumerate(codecs_channels)
        ])
        return " ".join(mux._audio_args(info, cfg))

    def test_side_surround_is_relabelled_for_opus(self):
        # Mainline libopus refuses 5.1(side), the usual DTS and TrueHD layout.
        args = self.audio_args([("dts", 6)], layout="5.1(side)")
        self.assertIn("-filter:a:0 channelmap=channel_layout=5.1", args)
        self.assertNotIn("channelmap", self.audio_args([("dts", 6)], layout="5.1"))
        self.assertNotIn("channelmap", self.audio_args([("eac3", 6)], layout="5.1(side)"))

    def test_a_downmix_is_not_relabelled(self):
        args = self.audio_args([("dts", 6)], layout="5.1(side)", max_channels=2)
        self.assertNotIn("channelmap", args)

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
    def test_a_newer_versions_field_does_not_lose_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"records": {"/a.mkv::1": {
                "source": "/a.mkv", "status": "done", "output": "/a.av1.mkv",
                "from_the_future": 1}}, "estimates": {}}))
            ledger = Ledger(state)
            self.assertEqual(ledger.records["/a.mkv::1"].output, "/a.av1.mkv")

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
            done = present(reloaded.already_done(source))
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

    def test_two_commands_sharing_the_ledger_keep_each_others_entries(self):
        # A run going for days and an estimate started beside it each load the
        # ledger once. Neither save may drop what the other recorded since.
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.mkv", Path(tmp) / "b.mkv"
            a.write_bytes(b"a" * 100)
            b.write_bytes(b"b" * 100)
            state = Path(tmp) / "state.json"
            run, est = Ledger(state), Ledger(state)

            est.record_guess(b, Guess(str(b), 2, 1, 60.0, 30.0, "k"))
            run.record(a, Record(str(a), "done", output="a.av1.mkv"))
            self.assertIsNotNone(Ledger(state).guess(b, "k"), "run's save dropped the estimate")

            est.record_guess(b, Guess(str(b), 2, 1, 90.0, 30.0, "k"))
            self.assertIsNotNone(Ledger(state).already_done(a), "the estimate dropped run's done")
            self.assertEqual(present(Ledger(state).guess(b, "k")).encode_seconds, 90.0)
            # And each process sees the other's entries without reloading.
            self.assertIsNotNone(est.already_done(a))


class TestGrainReport(unittest.TestCase):
    def test_describe_mentions_the_decision(self):
        report = GrainReport(38.0, [38.0], [1.0], grainy=True, denoise_filter="hqdn3d=1.4:1.0:6:6")
        text = report.describe()
        self.assertIn("38.00", text)
        self.assertIn("grain route", text)


if __name__ == "__main__":
    unittest.main()


def decode_with(chain: Toolchain, codec: str, *, grain: bool = True, times: int = 1,
                matching: bool = True) -> list[str]:
    """verify.decode_args for a film in `codec`, asked `times` times, every
    hardware decoder's earlier answer forgotten first. `matching`: every decoder
    gives software's frames; else the caller fakes verify._frame_sums."""
    verify._decoders.clear()
    video = verify._Video(codec, "yuv420p", 600.0)
    with contextlib.ExitStack() as stack:
        stack.enter_context(unittest.mock.patch.object(verify, "_video", return_value=video))
        if matching:
            stack.enter_context(unittest.mock.patch.object(
                verify, "_frame_sums", return_value=["0, 0, 0, 1, 100, same"]))
        for _ in range(times):
            argv = verify.decode_args(chain, Path("/x.mkv"), grain=grain)
    return argv


def make_chain(**overrides: Any) -> Toolchain:
    base: dict[str, Any] = dict(
        ffmpeg="ffmpeg", ffprobe="ffprobe", av1an="av1an",
        svtav1encapp="SvtAv1EncApp", grav1synth="grav1synth", mkvmerge="mkvmerge",
        svtfg="SvtFgEssential",
        svt_options={"--preset", "--crf", "--mastering-display", "--content-light",
                     "--ac-bias"},
        filters={"tonemapx", "hqdn3d"},
    )
    base.update(overrides)
    return Toolchain(**base)


MASTERING = MasteringDisplay(
    green_x=0.265, green_y=0.690, blue_x=0.150, blue_y=0.060,
    red_x=0.680, red_y=0.320, white_x=0.3127, white_y=0.3290,
    max_luminance=1000.0, min_luminance=0.0001,
)


class TestHdr10Metadata(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config()

    def test_mastering_display_matches_the_documented_svt_syntax(self):
        self.assertEqual(
            MASTERING.svt_argument(),
            "G(0.2650,0.6900)B(0.1500,0.0600)R(0.6800,0.3200)"
            "WP(0.3127,0.3290)L(1000.0000,0.0001)",
        )

    def test_static_metadata_is_passed_through_for_pq_sources(self):
        info = make_info(**HDR10_TAGS, mastering_display=MASTERING,
                         content_light=ContentLight(1000, 227))
        params = encode.svt_params(info, self.cfg, make_chain())
        self.assertIn("--mastering-display", params)
        self.assertIn("--content-light", params)
        self.assertEqual(params[params.index("--content-light") + 1], "1000,227")

    def test_static_metadata_is_omitted_when_the_encoder_lacks_the_flags(self):
        info = make_info(**HDR10_TAGS, mastering_display=MASTERING,
                         content_light=ContentLight(1000, 227))
        params = encode.svt_params(info, self.cfg, make_chain(svt_options={"--preset"}))
        self.assertNotIn("--mastering-display", params)
        self.assertNotIn("--content-light", params)

    def test_static_metadata_is_not_claimed_for_an_sdr_source(self):
        info = make_info(mastering_display=MASTERING)
        params = encode.svt_params(info, self.cfg, make_chain())
        self.assertNotIn("--mastering-display", params)


class TestDolbyVisionEncoding(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config()

    def _profile5(self) -> MediaInfo:
        return make_info(**HDR10_TAGS, mastering_display=MASTERING,
                         content_light=ContentLight(1000, 800),
                         dolby_vision=DolbyVision(profile=5, compatibility_id=0,
                                                  rpu_present=True, bl_present=True))

    def test_no_tonemap_filter_unless_profile5_conversion_is_enabled(self):
        self.assertEqual(encode.dovi_tonemap_filter(self._profile5(), self.cfg), "")

    def test_tonemap_filter_applies_the_rpu_and_targets_bt709(self):
        self.cfg.dolby_vision.profile5 = "tonemap-sdr"
        produced = encode.dovi_tonemap_filter(self._profile5(), self.cfg)
        self.assertTrue(produced.startswith("tonemapx="))
        self.assertIn("apply_dovi=1", produced)
        self.assertIn("t=bt709:m=bt709:p=bt709", produced)

    def test_tonemapped_output_is_signalled_bt709_not_hdr(self):
        self.cfg.dolby_vision.profile5 = "tonemap-sdr"
        info = self._profile5()
        self.assertEqual(encode.effective_color(info, self.cfg),
                         ("bt709", "bt709", "bt709"))
        params = encode.svt_params(info, self.cfg, make_chain())
        # 1 is BT.709 in H.273; 9/16 would be BT.2020/PQ.
        self.assertEqual(params[params.index("--color-primaries") + 1], "1")
        self.assertEqual(params[params.index("--transfer-characteristics") + 1], "1")
        # HDR10 static metadata would be a lie once the picture is SDR.
        self.assertNotIn("--mastering-display", params)
        self.assertNotIn("--content-light", params)

    def test_an_ordinary_hdr_source_keeps_its_hdr_signalling(self):
        info = make_info(**HDR10_TAGS)
        params = encode.svt_params(info, self.cfg, make_chain())
        self.assertEqual(params[params.index("--color-primaries") + 1], "9")
        self.assertEqual(params[params.index("--transfer-characteristics") + 1], "16")

    def test_tonemap_and_denoise_filters_are_chained_in_order(self):
        self.cfg.dolby_vision.profile5 = "tonemap-sdr"
        argv = encode.build_command(
            self._profile5(), Path("/tmp/out.mkv"), make_chain(), self.cfg,
            Path("/tmp/t"), "hqdn3d=1:1:6:6",
        )
        spec = argv[argv.index("-f") + 1]
        self.assertTrue(spec.startswith("-vf tonemapx="), spec)
        self.assertTrue(spec.endswith(",hqdn3d=1:1:6:6"), spec)


class TestStaging(unittest.TestCase):
    """Staging is an optimisation, so the tests are mostly about what it must NOT do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.source = self.root / "library" / "Film (1975).mkv"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"x" * 4096)

        self.cfg = config.Config()
        self.cfg.staging.enabled = True
        self.cfg.staging.directory = str(self.root / "staging")
        self.cfg.output.min_saving_mib = 0.0   # the test files are a few KiB

    def _info(self) -> MediaInfo:
        return make_info(path=self.source, size_bytes=self.source.stat().st_size)

    def test_staged_copy_is_byte_identical_and_kept_out_of_the_library(self):
        staged = present(pipeline.stage_source(self._info(), self.cfg))
        self.assertEqual(staged.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(staged.parent, self.source.parent)
        self.assertTrue(self.source.is_file(), "the source must not be moved")

    def test_two_sources_with_the_same_name_do_not_collide(self):
        other = self.root / "elsewhere" / "Film (1975).mkv"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"y" * 4096)

        first = present(pipeline.stage_source(self._info(), self.cfg))
        second = present(pipeline.stage_source(
            make_info(path=other, size_bytes=other.stat().st_size), self.cfg
        ))
        self.assertNotEqual(first, second)
        self.assertEqual(second.read_bytes(), b"y" * 4096)

    def test_an_interrupted_runs_copy_is_reused_when_the_size_still_matches(self):
        staged = present(pipeline.stage_source(self._info(), self.cfg))
        # Mark the copy so we can tell a reuse from a fresh copy.
        staged.write_bytes(b"z" * 4096)
        again = present(pipeline.stage_source(self._info(), self.cfg))
        self.assertEqual(again, staged)
        self.assertEqual(again.read_bytes(), b"z" * 4096, "should have been reused")

    def test_a_stale_copy_of_the_wrong_size_is_replaced(self):
        staged = present(pipeline.stage_source(self._info(), self.cfg))
        staged.write_bytes(b"truncated")
        again = present(pipeline.stage_source(self._info(), self.cfg))
        self.assertEqual(again.read_bytes(), self.source.read_bytes())

    def test_declines_rather_than_filling_the_disk(self):
        # Ask for more headroom than any real disk has, so the check must refuse.
        self.cfg.staging.min_free_multiple = 1e12
        self.assertIsNone(pipeline.stage_source(self._info(), self.cfg))

    def test_an_unreadable_source_falls_back_instead_of_failing_the_file(self):
        info = make_info(path=self.root / "gone.mkv", size_bytes=4096)
        self.assertIsNone(pipeline.stage_source(info, self.cfg))

    def test_the_encoder_reads_the_copy_but_the_ledger_records_the_source(self):
        # The invariant that matters: reads move to the copy, while everything
        # identifying the job -- output path, ledger entry, Av1an's resume digest --
        # stays on the real source.
        self.cfg.grain.mode = "never"
        self.cfg.paths.work_dir = str(self.root / "work")
        info = self._info()
        staged = pipeline.stage_source(info, self.cfg)
        read = replace(info, path=staged)
        work = pipeline._work_dir(self.source, self.cfg)

        record = pipeline._transcode(
            self.source, info, read, self.root / "out.mkv", None, work,
            make_chain(), self.cfg, 0.0, dry_run=True,
        )
        self.assertEqual(record.source, str(self.source))

        argv = encode.build_command(read, Path("/o.mkv"), make_chain(), self.cfg, work)
        self.assertIn(str(staged), argv, "the encoder should read the staged copy")
        self.assertNotIn(str(self.source), argv)

        self.assertEqual(pipeline.output_path(info, self.cfg).parent, self.source.parent)

    def test_staged_output_lands_in_staging_as_matroska(self):
        out = pipeline.staged_output_path(self.source, self.cfg)
        self.assertEqual(out.parent, self.cfg.staging_dir)
        self.assertTrue(out.name.endswith(".out.mkv"))

    def test_the_staged_output_does_not_collide_with_the_staged_input(self):
        staged_in = pipeline.stage_source(self._info(), self.cfg)
        self.assertNotEqual(staged_in, pipeline.staged_output_path(self.source, self.cfg))

    def _stub_externals(self, muxed_bytes: bytes) -> Path:
        """Replace the two steps that shell out, and return a ready work directory."""
        work = self.root / "work"
        work.mkdir()

        def fake_encode(*_args, **_kwargs):
            # _transcode clears any stale encode first, so produce it here.
            (work / "encoded.mkv").write_bytes(b"e" * 4096)

        def fake_mux(video, rinfo, output, chain, cfg, dry_run=False):
            Path(output).write_bytes(muxed_bytes)
            return output

        original_mux, original_encode = pipeline.mux.mux, pipeline.encode.run_encode
        pipeline.mux.mux = fake_mux
        pipeline.encode.run_encode = fake_encode
        rehearse = unittest.mock.patch.object(pipeline.mux, "rehearse")
        rehearse.start()
        self.addCleanup(rehearse.stop)
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)
        self.addCleanup(setattr, pipeline.encode, "run_encode", original_encode)
        # Its frame timing is checked with ffprobe, which a fake encode cannot pass.
        timing = pipeline.encode.keep_source_timing
        pipeline.encode.keep_source_timing = lambda *_args: None
        self.addCleanup(setattr, pipeline.encode, "keep_source_timing", timing)
        return work

    def test_a_result_that_fails_the_size_check_never_reaches_the_destination(self):
        # The whole point of staging the write: a rejected encode must not spin the
        # destination disk up at all, not even to create and delete a file.
        self.cfg.grain.mode = "never"
        self.cfg.output.min_saving_percent = 99.0
        destination = self.root / "library" / "out.mkv"
        staged_out = pipeline.staged_output_path(self.source, self.cfg)
        staged_out.parent.mkdir(parents=True, exist_ok=True)

        info = self._info()
        work = self._stub_externals(b"o" * 4096)  # no saving at all

        with self.assertRaisesRegex(pipeline.SkipFile, "below the"):
            pipeline._transcode(
                self.source, info, info, destination, staged_out, work,
                make_chain(), self.cfg, 0.0, dry_run=False,
            )
        self.assertFalse(destination.exists(), "destination must never have been written")
        self.assertFalse(staged_out.exists(), "the rejected staged output must be removed")

    def test_a_result_that_passes_is_moved_to_the_destination(self):
        self.cfg.grain.mode = "never"
        self.cfg.output.min_saving_percent = 10.0
        destination = self.root / "library" / "out.mkv"
        staged_out = pipeline.staged_output_path(self.source, self.cfg)
        staged_out.parent.mkdir(parents=True, exist_ok=True)

        info = self._info()
        work = self._stub_externals(b"o" * 8)  # a big saving

        record = pipeline._transcode(
            self.source, info, info, destination, staged_out, work,
            make_chain(), self.cfg, 0.0, dry_run=False,
        )
        self.assertEqual(record.status, "done")
        self.assertTrue(destination.is_file(), "the result should have been moved over")
        self.assertFalse(staged_out.exists(), "nothing should be left in staging")
        self.assertEqual(record.output, str(destination))

    def test_disabled_by_default(self):
        self.assertFalse(config.Config().staging.enabled)

    def test_min_free_multiple_below_one_is_rejected(self):
        cfg = config.Config()
        cfg.staging.min_free_multiple = 0.5
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestOneRunPerWorkDir(unittest.TestCase):
    """Two runs on one library start on the same film, since films already under
    way go first, and then share its chunks, its staged copy and its output."""

    HOLD = ("import sys, time\n"
            "from autotranscode import config, pipeline\n"
            "cfg = config.Config()\n"
            "cfg.paths.work_dir = sys.argv[1]\n"
            "with pipeline.only_run(cfg):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(60)\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config()
        self.cfg.paths.work_dir = str(self.root / "work")

    def test_a_second_run_on_the_same_work_dir_is_refused_and_named_by_pid(self):
        with pipeline.only_run(self.cfg), \
                self.assertRaises(pipeline.AlreadyRunning) as caught, \
                pipeline.only_run(self.cfg):
            self.fail("two runs held the work directory at once")
        self.assertIn(f"pid {os.getpid()}", str(caught.exception))

    def test_the_work_dir_is_free_once_the_run_holding_it_is_killed(self):
        # A pid left in a file outlives kill -9 and may later name another process.
        holder = subprocess.Popen(
            [sys.executable, "-c", self.HOLD, self.cfg.paths.work_dir],
            stdout=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        assert holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with self.assertRaises(pipeline.AlreadyRunning) as caught, pipeline.only_run(self.cfg):
            pass
        self.assertIn(f"pid {holder.pid}", str(caught.exception))
        holder.kill()
        holder.wait()
        with pipeline.only_run(self.cfg):
            pass

    def _run_cli(self, *flags: str, during: Any = None) -> tuple[int, unittest.mock.Mock]:
        settings = self.root / "config.toml"
        settings.write_text(f'[paths]\nwork_dir = "{self.cfg.paths.work_dir}"\n'
                            f'state_file = "{self.root / "state.json"}"\n', encoding="utf-8")
        source = self.root / "Film (1975).mkv"
        with unittest.mock.patch.object(cli.tools, "detect", return_value=tested_chain()), \
                unittest.mock.patch.object(cli.tools, "require"), \
                unittest.mock.patch.object(cli.pipeline, "discover", return_value=[source]), \
                unittest.mock.patch.object(cli.pipeline, "process_all", return_value=0,
                                           side_effect=during) as process_all:
            code = cli.main(["-c", str(settings), "run", *flags, str(source)])
        return code, process_all

    def test_run_stops_before_any_film_when_another_run_holds_the_work_dir(self):
        with pipeline.only_run(self.cfg), self.assertLogs(util.log, "ERROR") as logged:
            code, process_all = self._run_cli()
        self.assertEqual(code, 1)
        process_all.assert_not_called()
        self.assertIn(f"pid {os.getpid()}", "\n".join(logged.output))

    def test_a_dry_run_goes_ahead_while_another_run_holds_the_work_dir(self):
        with pipeline.only_run(self.cfg):
            code, process_all = self._run_cli("--dry-run")
        self.assertEqual(code, 0)
        process_all.assert_called_once()

    def test_a_run_holds_the_work_dir_while_it_works_and_frees_it_after(self):
        def busy(*_args: Any, **_kwargs: Any) -> int:
            with self.assertRaises(pipeline.AlreadyRunning), pipeline.only_run(self.cfg):
                pass
            return 0
        code, process_all = self._run_cli(during=busy)
        self.assertEqual(code, 0)
        process_all.assert_called_once()
        with pipeline.only_run(self.cfg):
            pass


class TestStopAndResume(unittest.TestCase):
    """An encode runs for hours, so stopping it must never cost more than the
    chunk in flight, and must never be mistaken for a finished or failed file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.source = self.root / "library" / "Film (1975).mkv"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"x" * 4096)

        self.cfg = config.Config()
        self.cfg.grain.mode = "never"
        self.cfg.paths.work_dir = str(self.root / "work")
        self.cfg.output.min_saving_mib = 0.0   # the test files are a few KiB
        self.cfg.staging.enabled = True
        self.cfg.staging.directory = str(self.root / "staging")
        # The mux rehearsal shells out too; TestMuxRehearsal covers it.
        rehearse = unittest.mock.patch.object(pipeline.mux, "rehearse")
        rehearse.start()
        self.addCleanup(rehearse.stop)

    def _info(self) -> MediaInfo:
        return make_info(path=self.source, size_bytes=self.source.stat().st_size)

    def _stub_encode(self, behaviour):
        original = pipeline.encode.run_encode
        pipeline.encode.run_encode = behaviour
        self.addCleanup(setattr, pipeline.encode, "run_encode", original)
        timing = pipeline.encode.keep_source_timing
        pipeline.encode.keep_source_timing = lambda *_args: None
        self.addCleanup(setattr, pipeline.encode, "keep_source_timing", timing)

    def _stub_probe(self):
        """process_one probes for real; these tests are about what happens after."""
        original = pipeline.probe
        pipeline.probe = lambda path, chain: self._info()
        self.addCleanup(setattr, pipeline, "probe", original)

    def test_an_interrupted_file_keeps_its_staged_copy(self):
        # Deleting it would force the resumed run to re-read the whole source off
        # the library disk, which is exactly what staging exists to avoid.
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        self._stub_probe()
        self._stub_encode(interrupt)
        staged = present(pipeline.stage_source(self._info(), self.cfg))

        with self.assertRaises(KeyboardInterrupt):
            pipeline.process_one(self.source, make_chain(), self.cfg)

        self.assertTrue(staged.is_file(), "the staged copy must survive an interrupt")

    def test_the_work_directory_names_its_source_and_a_run_marks_it_while_running(self):
        work = pipeline._work_dir(self.source, self.cfg)
        seen: list[bool] = []

        def interrupt(*_args, **_kwargs):
            seen.append((work / pipeline.RUNNING_FILE).is_file())
            raise KeyboardInterrupt

        self._stub_probe()
        self._stub_encode(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            pipeline.process_one(self.source, make_chain(), self.cfg)
        self.assertEqual(seen, [True])
        self.assertFalse((work / pipeline.RUNNING_FILE).exists())
        self.assertEqual((work / pipeline.SOURCE_FILE).read_text(), str(self.source.resolve()))

    def test_a_file_left_for_later_keeps_its_staged_copy(self):
        # Its chunks may be reading the copy: Av1an's script names the file.
        self._stub_probe()
        staged = present(pipeline.stage_source(self._info(), self.cfg))
        with unittest.mock.patch.object(pipeline.resume, "check",
                                        lambda argv, *_a, **_k: (argv, "left for now")), \
                self.assertRaises(pipeline.Postponed):
            pipeline.process_one(self.source, make_chain(), self.cfg)
        self.assertTrue(staged.is_file())

    def test_a_finished_file_drops_its_staged_copy(self):
        self._stub_probe()
        staged = present(pipeline.stage_source(self._info(), self.cfg))
        work = pipeline._work_dir(self.source, self.cfg)

        def encoded(*_args, **_kwargs):
            (work / "encoded.mkv").write_bytes(b"e" * 4096)

        def fake_mux(video, info, out, chain, cfg, dry_run=False):
            Path(out).write_bytes(b"o" * 8)
            return out

        self._stub_encode(encoded)
        original_mux = pipeline.mux.mux
        pipeline.mux.mux = fake_mux
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)

        record = pipeline.process_one(self.source, make_chain(), self.cfg)
        self.assertEqual(record.status, "done")
        self.assertFalse(staged.exists(), "a finished file has no use for its copy")

    def test_a_stop_while_the_staged_output_is_copied_leaves_nothing_at_the_final_name(self):
        # Staging is normally on another filesystem, so the move is a copy. A short
        # file at the final name would read as "output already exists" next run.
        self.cfg.output.min_saving_percent = -1e9   # the output is bigger than the 4 KiB source
        self.cfg.output.min_saving_mib = -1e9
        self._stub_probe()
        present(pipeline.stage_source(self._info(), self.cfg))   # before sendfile is patched
        work = pipeline._work_dir(self.source, self.cfg)
        final = pipeline.output_path(self._info(), self.cfg)
        copied: list[int] = []

        def encoded(*_args, **_kwargs):
            (work / "encoded.mkv").write_bytes(b"e" * 4096)

        def fake_mux(video, info, out, chain, cfg, dry_run=False):
            Path(out).write_bytes(b"o" * 1_000_000)
            return out

        rename = os.rename

        def across_filesystems(src, dst, *args, **kwargs):
            if str(src).endswith(".out.mkv"):
                raise OSError(18, "Invalid cross-device link")   # EXDEV
            return rename(src, dst, *args, **kwargs)

        def half_then_stop(out_fd, in_fd, offset, count):
            copied.append(os.write(out_fd, os.read(in_fd, 500_000)))
            raise KeyboardInterrupt

        self._stub_encode(encoded)
        # Python 3.14's shutil tries copy_file_range before sendfile; keep it to the
        # one this patches.
        with unittest.mock.patch.object(pipeline.mux, "mux", fake_mux), \
                unittest.mock.patch("os.rename", across_filesystems), \
                unittest.mock.patch("os.sendfile", half_then_stop), \
                unittest.mock.patch("shutil._USE_CP_COPY_FILE_RANGE", False, create=True), \
                self.assertRaises(KeyboardInterrupt):
            pipeline.process_one(self.source, make_chain(), self.cfg)

        self.assertEqual(copied, [500_000], "the stop must come partway through the output's copy")
        self.assertFalse(final.exists(), "a truncated film must not sit at the final name")
        self.assertEqual(list(final.parent.glob(final.name + "*")), [])

    def test_an_unstaged_mux_is_renamed_into_place_not_written_there(self):
        # Without staging the mux writes beside the destination and renames, so an
        # interrupt cannot leave a short file that check_skip reads as "done".
        self.cfg.staging.enabled = False
        destination = self.root / "library" / "out.mkv"
        work = pipeline._work_dir(self.source, self.cfg)
        seen: list[Path] = []

        def encoded(*_args, **_kwargs):
            (work / "encoded.mkv").write_bytes(b"e" * 4096)

        self._stub_encode(encoded)

        def fake_mux(video, info, out, chain, cfg, dry_run=False):
            seen.append(Path(out))
            Path(out).write_bytes(b"o" * 8)
            return out

        original_mux = pipeline.mux.mux
        pipeline.mux.mux = fake_mux
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)

        info = self._info()
        record = pipeline._transcode(
            self.source, info, info, destination, None, work,
            make_chain(), self.cfg, 0.0, dry_run=False,
        )
        self.assertEqual(record.status, "done")
        self.assertNotEqual(seen[0], destination, "the mux must not write the final name")
        self.assertTrue(destination.is_file())
        self.assertFalse(seen[0].exists(), "the temporary must be renamed away, not copied")

    def _output_appears_during_the_encode(self) -> tuple[Path, Path]:
        """Run process_one while a file takes the output's name mid-encode."""
        self._stub_probe()
        self.cfg.output.directory = str(self.root / "out")
        final = pipeline.output_path(self._info(), self.cfg)
        work = pipeline._work_dir(self.source, self.cfg)

        def encoded(*_args, **_kwargs):
            (work / "encoded.mkv").write_bytes(b"e" * 4096)
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(b"theirs")

        def fake_mux(video, info, out, chain, cfg, dry_run=False):
            Path(out).write_bytes(b"ours")
            return out

        self._stub_encode(encoded)
        original_mux = pipeline.mux.mux
        pipeline.mux.mux = fake_mux
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)
        return final, final.with_name("Film (1975).av1.new.mkv")

    def test_an_output_that_appears_during_the_encode_is_not_overwritten(self):
        for staging in (True, False):
            with self.subTest(staging=staging):
                self.cfg.staging.enabled = staging
                final, kept = self._output_appears_during_the_encode()
                with self.assertRaisesRegex(pipeline.OutputExists, "new.mkv"):
                    pipeline.process_one(self.source, make_chain(), self.cfg)
                self.assertEqual(final.read_bytes(), b"theirs")
                self.assertEqual(kept.read_bytes(), b"ours", "hours of encoding must not be lost")
                self.assertEqual(list(self.cfg.staging_dir.glob("*")), [],
                                 "the staged copy has served its purpose")
                final.unlink()
                kept.unlink()

    def test_a_file_that_is_not_written_leaves_no_folders(self):
        # A mirrored output directory would otherwise fill with empty folders.
        self._stub_probe()
        self.cfg.staging.enabled = False
        self.cfg.output.directory = str(self.root / "out")
        roots = [self.root]
        work = pipeline._work_dir(self.source, self.cfg)

        def encoded(*_args, **_kwargs):
            (work / "encoded.mkv").write_bytes(b"e" * 4096)

        def fake_mux(video, info, out, chain, cfg, dry_run=False):
            Path(out).write_bytes(b"o" * 8192)   # larger than the source
            return out

        self._stub_encode(encoded)
        original_mux = pipeline.mux.mux
        pipeline.mux.mux = fake_mux
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)

        pipeline.process_one(self.source, make_chain(), self.cfg, dry_run=True, roots=roots)
        self.assertFalse((self.root / "out").exists(), "a dry run writes nothing")
        with self.assertRaises(pipeline.NoSaving):
            pipeline.process_one(self.source, make_chain(), self.cfg, roots=roots)
        self.assertFalse((self.root / "out" / "library").exists())

    def test_an_output_that_appears_during_the_encode_is_a_failure(self):
        # A failure shows in the run's summary and exit code, where a skip would not.
        self.cfg.staging.enabled = False
        self._output_appears_during_the_encode()
        ledger = Ledger(self.root / "state.json")
        failures = pipeline._process_all(
            [self.source], make_chain(), self.cfg, ledger, pipeline.StopRequest()
        )
        self.assertEqual(failures, 1)
        self.assertEqual(present(ledger.get(self.source)).status, "failed")

    def test_a_partial_output_does_not_look_like_a_finished_one(self):
        # The .part name is what keeps the next run from skipping the title.
        partial = self.root / "library" / "out.mkv.part"
        partial.write_bytes(b"o" * 8)
        pipeline.check_skip(self._info(), self.root / "library" / "out.mkv", self.cfg)

    def test_a_tool_killed_by_our_own_signal_is_not_recorded_as_a_failure(self):
        # Av1an traps SIGINT and exits non-zero; blaming the file for that would
        # leave a misleading ledger entry and a misleading exit code.
        self.cfg.staging.enabled = False
        ledger = Ledger(self.root / "state.json")

        def fail(*_args, **_kwargs):
            raise CommandError(["av1an"], 1, "stopped")

        original = pipeline.process_one
        pipeline.process_one = fail
        self.addCleanup(setattr, pipeline, "process_one", original)

        stop = pipeline.StopRequest()
        stop.seen = True
        with self.assertRaises(KeyboardInterrupt):
            pipeline._process_all([self.source], make_chain(), self.cfg, ledger, stop)
        self.assertEqual(ledger.records, {}, "an interrupted file must stay unrecorded")

    def test_a_dry_run_leaves_the_ledger_as_it_was(self):
        # It recorded "skipped: dry run" over an earlier "failed", and status
        # counted every file it looked at as skipped.
        ledger = Ledger(self.root / "state.json")
        ledger.record(self.source, Record(source=str(self.source), status="failed", reason="x"))
        other = self.root / "library" / "Other (1980).mkv"
        other.write_bytes(b"y" * 4096)
        before = (self.root / "state.json").read_text()

        def look(source, *_args, **_kwargs):
            if source == other:
                raise pipeline.SkipFile("already AV1")
            return Record(source=str(source), status="skipped", reason="dry run")

        with unittest.mock.patch.object(pipeline, "process_one", look):
            pipeline._process_all([self.source, other], make_chain(), self.cfg, ledger,
                                  pipeline.StopRequest(), dry_run=True)
        self.assertEqual((self.root / "state.json").read_text(), before)
        self.assertEqual(present(ledger.get(self.source)).status, "failed")

    def test_the_same_failure_without_a_signal_is_still_a_failure(self):
        self.cfg.staging.enabled = False
        ledger = Ledger(self.root / "state.json")

        def fail(*_args, **_kwargs):
            raise CommandError(["av1an"], 1, "broken")

        original = pipeline.process_one
        pipeline.process_one = fail
        self.addCleanup(setattr, pipeline, "process_one", original)

        failures = pipeline._process_all(
            [self.source], make_chain(), self.cfg, ledger, pipeline.StopRequest()
        )
        self.assertEqual(failures, 1)
        self.assertEqual(present(ledger.get(self.source)).status, "failed")

    def test_sigterm_stops_the_run_the_same_way_ctrl_c_does(self):
        # systemctl stop and a plain kill must checkpoint, not kill outright.
        with pipeline.stop_signals() as stop:
            self.assertFalse(stop.seen)
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(stop.seen)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_a_hangup_stops_the_run_the_same_way_ctrl_c_does(self):
        # A closed SSH session or a killed screen window sends SIGHUP. Av1an has a
        # process group of its own, so unless Python stops it, it runs on orphaned.
        previous = signal.getsignal(signal.SIGHUP)
        with pipeline.stop_signals() as stop:
            # Checked first: the default action would kill the test runner.
            self.assertNotIn(signal.getsignal(signal.SIGHUP), (signal.SIG_DFL, previous))
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGHUP)
            self.assertTrue(stop.seen)
        self.assertEqual(signal.getsignal(signal.SIGHUP), previous)

    def test_sample_encodes_stop_on_sigterm_and_hangup_as_run_does(self):
        pick_ = sample.Pick(id="1080p-1", band="1080p", source=str(self.source), start=60.0,
                            length=120.0, width=1920, height=1080, codec="h264", hdr=False,
                            duration=7200.0)
        out = self.root / "samples"
        out.mkdir()
        picks = {"seed": 1, "picks": [dataclasses.asdict(pick_)]}
        (out / "picks.json").write_text(json.dumps(picks))
        caught: list[bool] = []

        def encode_pick(*_args, **_kwargs):
            handled = [signal.getsignal(sig) not in (signal.SIG_DFL, signal.SIG_IGN, None)
                       for sig in (signal.SIGTERM, signal.SIGHUP)]
            caught.extend(handled)
            if all(handled):   # never send one that would kill the test runner
                os.kill(os.getpid(), signal.SIGHUP)
            return sample.Result(pick_.id)

        with unittest.mock.patch.object(sample, "encode_pick", encode_pick), \
                self.assertRaises(KeyboardInterrupt):
            sample.run_samples([], make_chain(), self.cfg, out, per_band=1, bands=["1080p"],
                               minutes=2, skip_start=0, skip_end=0, seed=None, score=False,
                               pick_only=False)
        self.assertEqual(caught, [True, True])


class TestConfigDiscovery(unittest.TestCase):
    """Forgetting -c used to run on built-in defaults and blame the tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.root)

    def test_nothing_there_means_built_in_defaults(self):
        self.assertIsNone(config.default_path())

    def test_a_config_in_the_xdg_directory_is_found(self):
        wanted = self.root / "autotranscode.toml"
        wanted.write_text("[encode]\ncrf = 25\n", encoding="utf-8")
        self.assertEqual(config.default_path(), wanted)
        self.assertEqual(config.load(config.default_path()).encode.crf, 25)

    def test_a_directory_of_that_name_is_not_a_config(self):
        (self.root / "autotranscode.toml").mkdir()
        self.assertIsNone(config.default_path())


class TestLocaleGuard(unittest.TestCase):
    """SSH imports the client's LC_* wholesale; a headless board has almost none."""

    def setUp(self):
        self.addCleanup(os.environ.pop, "LC_TELEPHONE", None)
        os.environ.pop("LC_TELEPHONE", None)

    def test_a_locale_the_machine_lacks_is_reported(self):
        os.environ["LC_TELEPHONE"] = "zz_ZZ.UTF-8"
        self.assertIn("LC_TELEPHONE=zz_ZZ.UTF-8", util.unusable_locale_vars())

    def test_checking_does_not_disturb_our_own_locale(self):
        before = locale.setlocale(locale.LC_ALL)
        os.environ["LC_TELEPHONE"] = "zz_ZZ.UTF-8"
        util.unusable_locale_vars()
        self.assertEqual(locale.setlocale(locale.LC_ALL), before)

    def test_children_are_given_a_locale_that_exists(self):
        # The whole point: whatever the shell holds, a child gets something valid.
        os.environ["LC_TELEPHONE"] = "zz_ZZ.UTF-8"
        forced = util.child_env()["LC_ALL"]
        previous = locale.setlocale(locale.LC_ALL)
        try:
            locale.setlocale(locale.LC_ALL, forced)
        finally:
            locale.setlocale(locale.LC_ALL, previous)


class TestFfmpegShim(unittest.TestCase):
    """Av1an's chunk pipes must get exactly the frames it counted, no CFR padding."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # A stand-in "real ffmpeg" that prints the argv it was given, one per line.
        self.fake = self.dir / "real ffmpeg"
        self.fake.write_text('#!/bin/sh\nfor a do printf "%s\\n" "$a"; done\n')
        self.fake.chmod(0o755)
        encode.write_ffmpeg_shim(self.dir / "bin", str(self.fake))

    def argv_seen(self, *args: str) -> list[str]:
        return util.run([str(self.dir / "bin" / "ffmpeg"), *args]).stdout.splitlines()

    def test_y4m_pipe_gets_passthrough_just_before_the_output(self):
        chunk = ["-y", "-i", "seg 0.mkv", "-vf", r"select=between(n\,210\,446)",
                 "-pix_fmt", "yuv420p10le", "-strict", "-1", "-f", "yuv4mpegpipe", "-"]
        self.assertEqual(self.argv_seen(*chunk), [*chunk[:-1], "-fps_mode", "passthrough", "-"])

    def test_anything_else_passes_through_untouched(self):
        other = ["-hide_banner", "-i", "a b.mkv", "-map", "0:v", "-c", "copy", "out.mkv"]
        self.assertEqual(self.argv_seen(*other), other)


class TestFfprobeShim(unittest.TestCase):
    """The hybrid method's keyframe scan decodes the whole film; a rerun replays it."""

    SCAN = ("-v", "quiet", "-print_format", "json", "-show_frames", "-select_streams", "v:0",
            "-show_entries", "frame=key_frame", "film.mkv")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.calls = self.dir / "calls"
        self.cache = self.dir / "keyframes.json"
        # A stand-in "real ffprobe": counts its runs, prints its argv, and fails on "bad".
        self.fake = self.dir / "real ffprobe"
        self.fake.write_text(f'#!/bin/sh\necho run >> "{self.calls}"\n'
                             'for a do printf "%s\\n" "$a"; done\n'
                             'case " $* " in *" bad "*) exit 1 ;; esac\n')
        self.fake.chmod(0o755)
        encode.write_ffprobe_shim(self.dir / "bin", str(self.fake))

    def probe(self, *args: str, cache: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ}
        env.pop(encode.KEYFRAMES_VARIABLE, None)
        if cache is not None:
            env[encode.KEYFRAMES_VARIABLE] = str(cache)
        return subprocess.run([str(self.dir / "bin" / "ffprobe"), *args], env=env,
                              capture_output=True, text=True, check=False)

    def runs(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_the_keyframe_scan_is_kept_and_replayed(self):
        first = self.probe(*self.SCAN, cache=self.cache)
        again = self.probe(*self.SCAN, cache=self.cache)
        self.assertEqual(first.stdout.splitlines(), list(self.SCAN))
        self.assertEqual(again.stdout, first.stdout)
        self.assertEqual(self.runs(), 1)

    def test_a_failed_scan_is_not_kept(self):
        failed = self.probe(*self.SCAN, "bad", cache=self.cache)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.cache.with_name(self.cache.name + ".part").exists())

    def test_anything_else_passes_through_untouched(self):
        other = ["-v", "error", "-show_entries", "format=duration", "film.mkv"]
        self.assertEqual(self.probe(*other, cache=self.cache).stdout.splitlines(), other)
        self.probe(*self.SCAN)   # no file to keep it in
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.runs(), 2)


class TestCrfCurve(unittest.TestCase):
    """CRF is per coded pixel, so the same number means less quality as size drops."""

    def setUp(self):
        self.cfg = config.Config()
        self.cfg.encode.crf = 30

    def crf_at(self, width: int, height: int = 0) -> float:
        info = make_info(width=width, height=height or width * 9 // 16)
        return encode.effective_crf(info, self.cfg)

    def test_it_passes_through_the_values_found_by_eye(self):
        # SD 24, 720p 27, 1080p 29, 4K 30.
        widths = (720, 1280, 1920, 3840)
        self.assertEqual([self.crf_at(w) for w in widths], [24, 27, 29, 30])

    def test_between_the_known_points_it_follows_the_curve(self):
        # NTSC DVD, PAL DVD, 1440p: 24 + 3.5 * log2(width / 720), in 0.25 steps.
        self.assertEqual([self.crf_at(w) for w in (853, 1024, 2560)], [24.75, 25.75, 30])

    def test_it_is_capped_at_crf_and_floored_below_it(self):
        self.assertEqual(self.crf_at(7680), 30)
        self.assertEqual(self.crf_at(320), 23)

    def test_it_never_falls_as_width_grows(self):
        crfs = [self.crf_at(w) for w in range(320, 4000, 16)]
        self.assertEqual(crfs, sorted(crfs))

    def test_a_cropped_scope_print_is_still_its_real_resolution(self):
        # The case that motivated going by width: both of these are 4K sources
        # cropped to the picture, and their heights would call them 1080p.
        self.assertEqual(self.crf_at(3836, height=2072), 30)
        self.assertEqual(self.crf_at(3840, height=1600), 30)
        # And the same crop one step down stays 1080p rather than dropping to 720p.
        self.assertEqual(self.crf_at(1920, height=800), 29)

    def test_narrow_hd_goes_by_its_height(self):
        self.assertEqual(self.crf_at(1440, height=1080), 29)
        self.assertEqual(self.crf_at(960, height=720), 27)

    def test_non_square_pixels_go_by_their_shown_width(self):
        # HDV: 1440x1080 stored, shown 1920 wide. Only the shown width is 1080p
        # here, because the height is cut to a crop that on its own would not be.
        info = make_info(width=1440, height=800, sample_aspect="4:3")
        self.assertEqual(encode.effective_crf(info, self.cfg), 29)
        self.assertEqual(encode.effective_crf(replace(info, sample_aspect=""), self.cfg), 27.5)

    def test_dvds_go_by_their_frame_height(self):
        # 576 lines is 1024 wide at 16:9 whatever the pixel shape; 480 is 853.
        for width, height, sar, crf in ((720, 576, "16:15", 25.75), (720, 576, "64:45", 25.75),
                                        (720, 480, "32:27", 24.75)):
            info = make_info(width=width, height=height, sample_aspect=sar)
            self.assertEqual(encode.effective_crf(info, self.cfg), crf, (width, height, sar))

    def test_crf_moves_the_whole_curve(self):
        # One knob still controls the library: --crf 28 lowers every width by 2.
        self.cfg.encode.crf = 28
        self.assertEqual([self.crf_at(w) for w in (3840, 1920, 1280, 720, 320)],
                         [28, 27, 25, 22, 21])

    def test_a_flat_curve_uses_the_plain_crf(self):
        self.cfg.encode.crf_per_doubling = 0
        self.cfg.encode.crf_drop_at_720 = 0
        self.assertEqual([self.crf_at(w) for w in (3840, 854)], [30, 30])

    def test_the_encoder_gets_quarter_steps_as_written(self):
        info = make_info(width=720, height=576, sample_aspect="64:45")
        params = encode.svt_params(info, self.cfg, make_chain())
        self.assertEqual(params[params.index("--crf") + 1], "25.75")
        info = make_info(width=1920, height=1080)
        params = encode.svt_params(info, self.cfg, make_chain())
        self.assertEqual(params[params.index("--crf") + 1], "29")

    def test_rk3588_profile_puts_4k_at_the_measured_30(self):
        cfg = config.Config()
        config._merge_profile(cfg, "rk3588")
        self.assertEqual(encode.effective_crf(make_info(width=3836, height=2072), cfg), 30)

    def test_a_curve_that_leaves_the_valid_range_is_rejected(self):
        self.cfg.encode.crf = 5
        self.cfg.encode.crf_max_drop = 5
        with self.assertRaises(config.ConfigError):
            config.validate(self.cfg)

    def test_whole_numbers_in_the_file_become_floats_where_floats_are_wanted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[encode]\ncrf_per_doubling = 3\n[output]\nmin_saving_percent = 5\n")
            cfg = config.load(path)
        self.assertIsInstance(cfg.encode.crf_per_doubling, float)
        self.assertIsInstance(cfg.output.min_saving_percent, float)


class TestEfficiencyPrefilter(unittest.TestCase):
    """Reject the hopeless before the encode, not after it burns an evening."""

    def setUp(self):
        self.cfg = config.Config()

    def check(self, **overrides) -> None:
        pipeline.check_skip(make_info(**overrides), Path("/out/x.mkv"), self.cfg)

    def test_an_already_efficient_source_is_skipped(self):
        # 720p at 500 kbps: 0.023 bits/pixel, near what AV1 itself would spend.
        with self.assertRaisesRegex(pipeline.SkipFile, "bits/pixel"):
            self.check(width=1280, height=720, video_bitrate=500_000)

    def test_a_fat_source_passes(self):
        self.check(width=1280, height=720, video_bitrate=4_000_000)

    def test_an_unreadable_bitrate_is_not_evidence(self):
        # 0 means ffprobe gave us nothing, which must not be read as "efficient".
        self.check(video_bitrate=0)

    def test_the_check_can_be_turned_off(self):
        self.cfg.library.min_bits_per_pixel = 0.0
        self.check(width=1280, height=720, video_bitrate=500_000)


class TestEstimate(unittest.TestCase):
    """Sampling a file to rank it, without spending the hours a real encode costs."""

    def setUp(self):
        self.cfg = config.Config()

    def sampled(self, **overrides) -> estimate.Estimate:
        """An Estimate as if sampling had already happened, with the numbers given."""
        info = overrides.pop("info", None) or make_info(
            size_bytes=1_000_000_000, video_bytes=900_000_000, duration=7200.0,
        )
        result = estimate.Estimate(info=info, crf=30, denoise="")
        for name, value in overrides.items():
            setattr(result, name, value)
        return result

    def test_projection_scales_video_and_keeps_everything_else(self):
        # Video a tenth of its size, the 100 MB of audio and subtitles untouched.
        result = self.sampled(source_sampled=10_000_000, encoded_sampled=1_000_000)
        self.assertAlmostEqual(result.ratio, 0.1)
        self.assertEqual(result.projected_bytes, 90_000_000 + 100_000_000)
        self.assertEqual(result.saved_bytes, 810_000_000)
        self.assertAlmostEqual(result.saving_percent, 81.0)

    def test_a_file_the_encode_would_inflate_reports_a_loss(self):
        result = self.sampled(source_sampled=1_000_000, encoded_sampled=1_500_000)
        self.assertLess(result.saved_bytes, 0)
        self.assertLess(result.saving_percent, 0)

    def test_ranking_prefers_the_better_use_of_an_encoding_hour(self):
        # A big file saving 40% in 10 hours loses to a small one saving 40% in one.
        slow = self.sampled(
            info=make_info(size_bytes=40_000_000_000, video_bytes=40_000_000_000,
                           duration=7200.0),
            source_sampled=100, encoded_sampled=60, sample_seconds=60.0, encode_seconds=360.0)
        fast = self.sampled(
            info=make_info(size_bytes=8_000_000_000, video_bytes=8_000_000_000,
                           duration=1800.0),
            source_sampled=100, encoded_sampled=60, sample_seconds=60.0, encode_seconds=36.0)
        self.assertGreater(slow.saved_bytes, fast.saved_bytes)
        self.assertGreater(fast.saved_per_hour, slow.saved_per_hour)

    def test_without_container_byte_tags_the_whole_file_is_treated_as_video(self):
        # No NUMBER_OF_BYTES and no usable bitrate: assume the file is its video,
        # which is what an mp4 without tags effectively is.
        result = self.sampled(
            info=make_info(size_bytes=1_000_000_000, video_bytes=0, video_bitrate=0),
            source_sampled=10, encoded_sampled=5)
        self.assertEqual(result.projected_bytes, 500_000_000)

    def test_unsampled_files_report_nothing_rather_than_zero(self):
        self.assertFalse(self.sampled().ok)
        self.assertEqual(self.sampled().ratio, 0.0)
        self.assertEqual(self.sampled().fps, 0.0)
        self.assertEqual(self.sampled().projected_encode_seconds, 0.0)

    def test_sample_count_scales_with_runtime_within_bounds(self):
        counts = [estimate.sample_count(d, self.cfg) for d in (600, 3600, 7200, 36000)]
        self.assertEqual(counts, [3, 3, 6, 6])

    def test_an_explicit_sample_count_wins(self):
        self.cfg.estimate.samples = 2
        self.assertEqual(estimate.sample_count(7200, self.cfg), 2)


class TestContainerByteTags(unittest.TestCase):
    """Matroska carries exact per-track sizes; reading them beats demuxing a remux."""

    def test_plain_and_language_suffixed_tags_are_both_read(self):
        self.assertEqual(probe._tag_int({"tags": {"NUMBER_OF_BYTES": "778544518"}},
                                        "NUMBER_OF_BYTES"), 778544518)
        self.assertEqual(probe._tag_int({"tags": {"NUMBER_OF_BYTES-eng": "42"}},
                                        "NUMBER_OF_BYTES"), 42)

    def test_a_missing_tag_is_zero_not_a_guess(self):
        self.assertEqual(probe._tag_int({}, "BPS"), 0)
        self.assertEqual(probe._tag_int({"tags": {"TITLE": "x"}}, "BPS"), 0)


class TestMuxOutput(unittest.TestCase):
    """The mux writes to <name>.part and renames it, so the format must be named."""

    def test_the_muxer_is_named_rather_than_inferred_from_the_extension(self):
        # ffmpeg exits 234 ("unable to choose an output format") on a .part name,
        # which failed every unstaged encode at the very last step.
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(),
                                 Path("/out/Film.av1.mkv.part"), make_chain(), config.Config())
        self.assertEqual(argv[-3:], ["-f", "matroska", "/out/Film.av1.mkv.part"])

    def _mux(self, *subs: str, mode: str = "copy") -> list[str]:
        cfg = config.Config()
        cfg.subtitles.mode = mode
        info = make_info(subtitles=[probe.Stream(index=2 + n, codec=codec, codec_type="subtitle")
                                    for n, codec in enumerate(subs)])
        return mux.build_command(Path("/w/encoded.mkv"), info, Path("/out/F.av1.mkv.part"),
                                 make_chain(), cfg)

    def test_an_mp4_text_track_becomes_srt(self):
        # Matroska refuses mov_text, which failed the mux after the whole encode.
        argv = self._mux("hdmv_pgs_subtitle", "mov_text")
        self.assertIn("-c:s:1", argv)
        self.assertEqual(argv[argv.index("-c:s:1") + 1], "srt")
        self.assertNotIn("-c:s:0", argv)

    def test_text_only_counts_the_tracks_it_keeps(self):
        # The second kept track is output subtitle 1, whatever its place in the source.
        argv = self._mux("hdmv_pgs_subtitle", "subrip", "mov_text", mode="text-only")
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        self.assertIn("1:s:1", maps)
        self.assertIn("1:s:2", maps)
        self.assertNotIn("1:s:0", maps)
        self.assertEqual(argv[argv.index("-c:s:1") + 1], "srt")

    def test_fonts_come_along_with_the_subtitles(self):
        self.assertIn("1:t?", self._mux("ass"))


class TestMuxRehearsal(unittest.TestCase):
    """A track the mux cannot take (mov_text, 5.1(side) on mainline ffmpeg) failed
    the file only after the whole encode. The mux is tried on a few seconds first."""

    def _rehearse(self, fail_mux: bool = False) -> tuple[list[list[str]], Path]:
        seen: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            Path(argv[-1]).write_bytes(b"x")
            return util.Completed(argv, 0, "", "", 0.0)

        def fake_until(argv, seconds):
            seen.append(argv)
            self.assertEqual(seconds, mux.REHEARSAL_SECONDS)
            Path(argv[-1]).write_bytes(b"x")
            if fail_mux:
                raise CommandError(argv, 1, "Subtitle codec 94213 is not supported")

        dts = probe.Stream(index=1, codec="dts", codec_type="audio", channels=6)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        with unittest.mock.patch.object(mux, "run", side_effect=fake_run), \
                unittest.mock.patch.object(mux, "mux_until", side_effect=fake_until):
            try:
                mux.rehearse(make_info(audio=[dts]), make_chain(), config.Config(), work)
            finally:
                self.assertEqual(list(work.iterdir()), [], "nothing is left behind")
        return seen, work

    def test_the_real_mux_runs_with_a_stand_in_for_the_encode(self):
        stand_in, rehearsal = self._rehearse()[0]
        self.assertIn("lavfi", stand_in)
        self.assertEqual(rehearsal[rehearsal.index("-i") + 1], stand_in[-1])
        self.assertIn("/library/Film (1975).mkv", rehearsal, "the source's own tracks")
        self.assertIn("libopus", rehearsal, "the audio is converted as in the real mux")
        self.assertEqual(rehearsal[1:3], ["-progress", "pipe:1"], "for mux_until to stop it by")

    def test_a_failed_rehearsal_says_so_and_leaves_nothing_behind(self):
        with self.assertRaisesRegex(CommandError, "not supported"):
            self._rehearse(fail_mux=True)

    def _ffmpeg(self, script: str) -> list[str]:
        """A stand-in for ffmpeg: Python printing progress lines as ffmpeg does."""
        return [sys.executable, "-c", f"import sys, time\n{script}"]

    def test_the_rehearsal_is_stopped_once_its_output_is_long_enough(self):
        # ffmpeg reads on through a whole film for a subtitle track with nothing
        # early: 197 s for one with 13 PGS tracks.
        started = time.monotonic()
        mux.mux_until(self._ffmpeg(
            "print('out_time_us=N/A', flush=True)\n"
            "print('out_time_us=5200000', flush=True)\n"
            "time.sleep(60)"), 5)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_rehearsal_whose_output_time_does_not_move_stops_after_writing_a_while(self):
        # 21 PGS tracks to interleave: out_time stayed N/A while 74 MB were written.
        started = time.monotonic()
        mux.mux_until(self._ffmpeg(
            "print('total_size=0', 'out_time_us=N/A', sep='\\n', flush=True)\n"
            "while True:\n"
            "    print('total_size=37748736', 'out_time_us=N/A', sep='\\n', flush=True)\n"
            "    time.sleep(0.1)"), 1)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_mux_that_fails_before_then_raises_with_what_ffmpeg_said(self):
        with self.assertRaisesRegex(CommandError, "mov_text"):
            mux.mux_until(self._ffmpeg(
                "print('out_time_us=1000000', flush=True)\n"
                "sys.stderr.write('Subtitle codec mov_text (94213) is not supported.')\n"
                "sys.exit(1)"), 5)

    def test_a_source_shorter_than_the_rehearsal_passes_when_it_ends_well(self):
        mux.mux_until(self._ffmpeg("print('out_time_us=2000000', flush=True)"), 5)

    def test_a_file_that_cannot_be_muxed_fails_before_anything_is_encoded(self):
        steps: list[str] = []

        def refuse(*_args, **_kwargs):
            steps.append("rehearse")
            raise CommandError(["ffmpeg"], 1, "Subtitle codec mov_text is not supported")

        cfg = config.Config()
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pipeline.mux, "rehearse", side_effect=refuse), \
                unittest.mock.patch.object(pipeline, "_grain_step",
                                           side_effect=lambda *a, **k: steps.append("grain")), \
                unittest.mock.patch.object(pipeline.encode, "run_encode",
                                           side_effect=lambda *a, **k: steps.append("encode")), \
                self.assertRaises(CommandError):
            pipeline._transcode(Path("/library/F.mkv"), make_info(), make_info(),
                                Path(tmp) / "F.av1.mkv", None, Path(tmp), make_chain(), cfg,
                                0.0, dry_run=False)
        self.assertEqual(steps, ["rehearse"])


class TestVideoStartOffset(unittest.TestCase):
    """A source whose video starts after its audio must keep the gap. The encode
    always starts at 0, and ffmpeg shifts every input to start at 0 anyway, so a
    test file with video at 0.500 s and audio at 0 came out with both at 0: the
    sound half a second late."""

    def _probe(self, video_start: str | None, format_start: str | None) -> MediaInfo:
        video: dict[str, Any] = {"codec_type": "video", "codec_name": "h264", "width": 320,
                                 "height": 240, "avg_frame_rate": "25/1"}
        fmt: dict[str, Any] = {"duration": "6.5", "size": "1000", "format_name": "matroska"}
        if video_start is not None:
            video["start_time"] = video_start
        if format_start is not None:
            fmt["start_time"] = format_start
        audio = {"codec_type": "audio", "codec_name": "pcm_s16le", "start_time": "0.000000"}
        out = json.dumps({"format": fmt, "streams": [video, audio]})
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with unittest.mock.patch.object(probe, "run", return_value=done):
            return probe.probe(Path("/library/Film.mkv"), make_chain())

    def test_probe_records_how_far_the_video_starts_after_the_file(self):
        self.assertAlmostEqual(self._probe("0.500000", "0.000000").video_start, 0.5)
        # Measured on the library: video at 0.083, audio and the file at 0.050.
        self.assertAlmostEqual(self._probe("0.083000", "0.050000").video_start, 0.033)
        # Five Dolby Vision files gave no video start time at all.
        self.assertEqual(self._probe(None, "0.000000").video_start, 0.0)

    def test_the_mux_puts_the_gap_back_before_the_encode(self):
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(video_start=0.5),
                                 Path("/out/F.av1.mkv.part"), make_chain(), config.Config())
        encode_input = argv.index("/w/encoded.mkv")
        self.assertEqual(argv[encode_input - 3:encode_input], ["-itsoffset", "0.500000", "-i"])
        self.assertEqual(argv.count("-itsoffset"), 1, "the source's own timing stays as it is")

    def test_a_video_that_starts_with_the_file_needs_no_offset(self):
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(),
                                 Path("/out/F.av1.mkv.part"), make_chain(), config.Config())
        self.assertNotIn("-itsoffset", argv)


class TestStatisticsTags(unittest.TestCase):
    """mkvmerge's statistics tags describe the track they were written for. ffmpeg
    copies them onto an Opus track made from it: Battlefield Earth's Opus track
    claimed BPS-eng=3933034 and 3.5 GB, the DTS-HD MA track's figures."""

    # As ffprobe showed that DTS track.
    TAGS: ClassVar[dict[str, str]] = {
            "language": "eng", "BPS-eng": "3933034", "DURATION-eng": "01:57:58.870000000",
            "NUMBER_OF_FRAMES-eng": "663644", "NUMBER_OF_BYTES-eng": "3480180216",
            "SOURCE_ID-eng": "001100",
            "_STATISTICS_WRITING_APP-eng": "mkvmerge v51.0.0 ('I Wish') 64-bit",
            "_STATISTICS_WRITING_DATE_UTC-eng": "2020-10-08 22:06:40",
            "_STATISTICS_TAGS-eng": "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES SOURCE_ID"}
    STALE = ("BPS-eng", "DURATION-eng", "NUMBER_OF_FRAMES-eng", "NUMBER_OF_BYTES-eng",
             "_STATISTICS_WRITING_APP-eng", "_STATISTICS_WRITING_DATE_UTC-eng",
             "_STATISTICS_TAGS-eng")

    def test_probe_keeps_the_names_of_a_tracks_statistics_tags(self):
        stream = probe._stream({"index": 1, "codec_name": "dts", "codec_type": "audio",
                                "tags": {**self.TAGS, "DURATION": "01:57:58.870000000"}})
        self.assertEqual(sorted(stream.statistics_tags), sorted(self.STALE))
        self.assertEqual(stream.bitrate, 3_933_034, "the source's own tag is still read")

    def _argv(self, codec: str) -> list[str]:
        stream = probe.Stream(index=1, codec=codec, codec_type="audio", channels=2,
                              statistics_tags=self.STALE)
        return mux.build_command(Path("/w/encoded.mkv"), make_info(audio=[stream]),
                                 Path("/out/F.av1.mkv.part"), make_chain(), config.Config())

    def test_a_track_made_into_opus_drops_the_source_tracks_statistics(self):
        argv = self._argv("dts")
        blanked = [argv[i + 1] for i, a in enumerate(argv) if a == "-metadata:s:a:0"]
        self.assertEqual(blanked, [f"{name}=" for name in self.STALE])
        self.assertNotIn("-map_metadata:s:a:0", argv, "that would drop the language and title")

    def test_a_copied_track_keeps_its_statistics(self):
        # The same bytes, so the figures still hold.
        self.assertNotIn("-metadata:s:a:0", self._argv("ac3"))


class TestDeinterlace(unittest.TestCase):
    """An interlaced source encoded as it is keeps its combing, and no player
    deinterlaces AV1. capture.avi has interlaced picture and is flagged progressive
    (idet: 39,019 frames bottom field first, 25,128 top, 33,600 progressive), so
    the order can be forced."""

    BWDIF_BFF = "bwdif=mode=send_frame:parity=bff:deint=all"

    def _cfg(self, mode: str) -> config.Config:
        cfg = config.Config()
        cfg.encode.deinterlace = mode
        return cfg

    def _probe(self, field_order: str | None) -> MediaInfo:
        video: dict[str, Any] = {"codec_type": "video", "codec_name": "mpeg2video",
                                 "width": 720, "height": 576, "avg_frame_rate": "25/1"}
        if field_order is not None:
            video["field_order"] = field_order
        fmt = {"duration": "60", "size": "1000", "format_name": "matroska"}
        done = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"format": fmt, "streams": [video]}), stderr="")
        with unittest.mock.patch.object(probe, "run", return_value=done):
            return probe.probe(Path("/library/Film.mkv"), make_chain())

    def test_probe_records_the_field_order_and_whether_it_is_interlaced(self):
        self.assertEqual(self._probe("tt").field_order, "tt")
        self.assertTrue(self._probe("bb").interlaced)
        for order in ("progressive", "unknown", None):
            self.assertFalse(self._probe(order).interlaced, order)

    def test_nothing_is_deinterlaced_unless_asked(self):
        info = make_info(field_order="tt")
        self.assertEqual(encode.deinterlace_filter(info, config.Config()), "")
        argv = encode.build_command(info, Path("/o.mkv"), make_chain(), config.Config(),
                                    Path("/w"))
        self.assertNotIn("-f", argv)

    def test_auto_deinterlaces_a_flagged_source_by_the_field_shown_first(self):
        cfg = self._cfg("auto")
        shown_first = {"tt": "tff", "bt": "tff", "bb": "bff", "tb": "bff"}
        for order, parity in shown_first.items():
            self.assertIn(f"parity={parity}",
                          encode.deinterlace_filter(make_info(field_order=order), cfg), order)
        for order in ("progressive", "unknown", ""):
            self.assertEqual(encode.deinterlace_filter(make_info(field_order=order), cfg), "")

    def test_a_forced_field_order_deinterlaces_a_source_flagged_progressive(self):
        info = make_info(field_order="progressive")
        self.assertEqual(encode.deinterlace_filter(info, self._cfg("bff")), self.BWDIF_BFF)

    def test_deinterlacing_keeps_one_frame_per_frame(self):
        # Chunks, keyframes and verify all pair frames by number.
        made = encode.deinterlace_filter(make_info(), self._cfg("tff"))
        self.assertIn("mode=send_frame", made)
        self.assertIn("deint=all", made, "frame flags do not survive Av1an's pipe")

    def test_the_encode_deinterlaces_before_it_denoises(self):
        argv = encode.build_command(make_info(), Path("/o.mkv"), make_chain(), self._cfg("bff"),
                                    Path("/w"), denoise_filter="hqdn3d=2:2:3:3")
        self.assertEqual(argv[argv.index("-f") + 1], f"-vf {self.BWDIF_BFF},hqdn3d=2:2:3:3")

    def test_size_estimates_encode_their_samples_deinterlaced(self):
        seen: list[list[str]] = []

        def popen(argv, **_kwargs):
            seen.append(argv)
            raise RuntimeError("stop here")

        with unittest.mock.patch.object(estimate.subprocess, "Popen", side_effect=popen), \
                self.assertRaises(RuntimeError):
            estimate._encode_sample(make_info(), make_chain(), self._cfg("bff"), 10.0, 5.0,
                                    30, "", Path("/w/s.ivf"))
        self.assertEqual(seen[0][seen[0].index("-vf") + 1], self.BWDIF_BFF)

    def _grain_argvs(self, call) -> list[list[str]]:
        seen: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            joined = " ".join(argv)
            if (stats := re.search(r"stats_file=(\S+?)(?:[;\s]|$)", joined)):
                Path(stats.group(1)).write_text("n:1 mse_y:2.0 mse_u:1.0 mse_v:1.0 psnr_y:40\n",
                                                encoding="utf-8")
            elif argv[-1].endswith(".mkv"):
                Path(argv[-1]).write_bytes(b"x")
            return util.Completed(argv, 0, "", "", 0.0)

        with unittest.mock.patch.object(grain, "run", side_effect=fake_run), \
                tempfile.TemporaryDirectory() as tmp:
            call(Path(tmp))
        return seen

    def test_grain_analysis_clips_are_deinterlaced_before_they_are_cropped(self):
        seen = self._grain_argvs(lambda work: grain._extract_lossless(
            make_info(), make_chain(), self._cfg("bff"), [10.0], work))
        self.assertEqual(seen[0][seen[0].index("-vf") + 1],
                         f"{self.BWDIF_BFF},{grain.ANALYSIS_CROP}")

    def test_the_grain_probe_measures_the_deinterlaced_picture(self):
        seen = self._grain_argvs(lambda work: grain.measure(
            make_info(), make_chain(), self._cfg("bff"), work))
        graph = seen[0][seen[0].index("-filter_complex") + 1]
        self.assertTrue(graph.startswith(f"[0:v]{self.BWDIF_BFF},split=2"), graph)

    def test_calibration_deinterlaces_the_source_and_not_the_encode(self):
        seen = self._grain_argvs(lambda work: grain.calibrate_strength(
            Path("/library/Film.mkv"), work / "grainy.mkv", make_chain(), self._cfg("bff"),
            work, 7200.0, source_filter=self.BWDIF_BFF))
        graphs = {argv[argv.index("-i") + 1]: argv[argv.index("-filter_complex") + 1]
                  for argv in seen}
        self.assertTrue(graphs["/library/Film.mkv"].startswith(f"[0:v]{self.BWDIF_BFF},"))
        encode_graphs = [g for path, g in graphs.items() if path != "/library/Film.mkv"]
        self.assertTrue(encode_graphs)
        self.assertTrue(all(g.startswith(verify.GRAIN_GRAPH) for g in encode_graphs))

    def test_verify_deinterlaces_the_source_and_not_the_encode(self):
        seen: list[list[str]] = []

        def fake_progress(argv, *_args, **_kwargs):
            seen.append(argv)
            stats = re.search(r"stats_file=(\S+?)(?:[;\s]|$)", " ".join(argv))
            Path(present(stats).group(1)).write_text(
                "n:1 XPSNR y: 40.0 mse_y:2.0 psnr_y:40.0\n", encoding="utf-8")

        with unittest.mock.patch.object(verify, "run_progress", side_effect=fake_progress), \
                tempfile.TemporaryDirectory() as tmp:
            verify.compare(make_chain(), Path("/src.mkv"), Path("/enc.mkv"), Path(tmp),
                           reference_filter=self.BWDIF_BFF)
        self.assertEqual(len(seen), 4)
        for argv in seen[:2]:   # XPSNR and low-pass PSNR: both inputs in one graph
            graph = argv[argv.index("-lavfi") + 1]
            self.assertIn(f"[0:v]{self.BWDIF_BFF},", graph)
            self.assertNotIn(f"[1:v]{self.BWDIF_BFF}", graph)
        source_grain, encode_grain = (argv[argv.index("-filter_complex") + 1] for argv in seen[2:])
        self.assertTrue(source_grain.startswith(f"[0:v]{self.BWDIF_BFF},"))
        self.assertEqual(encode_grain.split("psnr=")[0], verify.GRAIN_GRAPH)

    def test_leaving_it_off_keeps_every_estimate_and_turning_it_on_does_not(self):
        chain = make_chain()
        encoding = pipeline._size_settings(config.Config(), chain)["encode"]
        assert isinstance(encoding, dict)
        self.assertNotIn("deinterlace", encoding)
        self.assertNotEqual(pipeline.size_key(self._cfg("auto"), chain),
                            pipeline.size_key(config.Config(), chain))

    def test_an_interlaced_source_left_as_it_is_says_so(self):
        with self.assertLogs(util.log, "WARNING") as logged:
            pipeline.note_interlacing(make_info(field_order="tt"), config.Config())
        self.assertIn("encode.deinterlace", logged.output[0])

    def test_the_setting_takes_only_its_four_values(self):
        with self.assertRaisesRegex(config.ConfigError, "encode.deinterlace"):
            config.validate(self._cfg("always"))

    def test_the_flag_sets_it_for_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "c.toml"
            empty.write_text("", encoding="utf-8")
            args = cli.build_parser().parse_args(
                ["-c", str(empty), "--deinterlace", "bff", "run", "capture.avi"])
            self.assertEqual(cli.load_config(args).encode.deinterlace, "bff")


class TestPixelAspect(unittest.TestCase):
    """AV1 carries no pixel shape, so a DVD's must be put back at the mux."""

    def aspect(self, **info: Any) -> list[str]:
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(**info),
                                 Path("/out/Film.av1.mkv.part"), make_chain(), config.Config())
        if "-aspect" not in argv:
            return []
        return argv[argv.index("-aspect") + 1:argv.index("-aspect") + 2]

    def test_pal_dvds_keep_their_shape(self):
        self.assertEqual(self.aspect(width=720, height=576, sample_aspect="16:15"), ["4:3"])
        self.assertEqual(self.aspect(width=720, height=576, sample_aspect="64:45"), ["16:9"])

    def test_square_pixels_add_nothing(self):
        self.assertEqual(self.aspect(), [])

    def test_probe_ignores_square_and_unknown_pixels(self):
        self.assertEqual([probe._sample_aspect(v) for v in ("1:1", "0:1", "N/A", "", "16:15")],
                         ["", "", "", "", "16:15"])


class TestVerify(unittest.TestCase):
    """The parts of verification that decide something, rather than run ffmpeg."""

    def test_xpsnr_log_is_read_per_frame_and_identical_frames_stay_finite(self):
        text = ("n:    1  XPSNR y: 42.1000  XPSNR u: 50.0  XPSNR v: 51.0\n"
                "n:    2  XPSNR y: inf  XPSNR u: inf  XPSNR v: inf\n")
        self.assertEqual(verify.parse_xpsnr_log(text), [42.1, verify.IDENTICAL_DB])

    def test_mse_log_is_read_per_frame(self):
        text = "n:1 mse_avg:3.10 mse_y:4.25 mse_u:1.0\nn:2 mse_avg:2.0 mse_y:2.75 mse_u:1.0\n"
        self.assertEqual(verify.parse_mse_log(text), [4.25, 2.75])

    def test_worst_frames_are_spread_out(self):
        scores = [50.0] * 100
        scores[10] = scores[11] = scores[12] = 30.0   # one bad shot
        scores[80] = 35.0                              # a different, milder one
        self.assertEqual(verify.worst_frames(scores, 2, spacing=10), [10, 80])

    def test_worst_windows_do_not_overlap_and_skip_the_ends(self):
        scores = [50.0] * 300
        for index in range(0, 10):
            scores[index] = 10.0      # a logo at the head: excluded by the margin
        for index in range(100, 130):
            scores[index] = 30.0
        for index in range(200, 230):
            scores[index] = 35.0
        self.assertEqual(verify.worst_windows(scores, window=30, count=2, margin=20),
                         [100, 200])

    def test_percentile_is_order_independent(self):
        self.assertEqual(verify.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0), 1.0)
        self.assertEqual(verify.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 100), 5.0)
        self.assertEqual(verify.percentile([], 5), 0.0)


class TestGrainRetention(unittest.TestCase):
    def test_overall_retention_is_a_ratio_of_rms_noise(self):
        # A quarter of the noise energy left is half the grain amplitude.
        self.assertAlmostEqual(verify.retention([4.0, 4.0], [1.0, 1.0]), 0.5)

    def decoder(self, codec: str, *, grain: bool, rkmpp: set[str] | None = None) -> list[str]:
        rkmpp = {"h264_rkmpp", "av1_rkmpp"} if rkmpp is None else rkmpp
        chain = make_chain(rkmpp_decoders=rkmpp, hardware_decode=("rkmpp",) if rkmpp else ())
        return decode_with(chain, codec, grain=grain)

    def test_hardware_decodes_what_it_decodes_exactly(self):
        self.assertEqual(self.decoder("h264", grain=True), ["-c:v", "h264_rkmpp", "-deint", "0"])

    def test_measurements_run_on_the_big_cores(self):
        argv = verify.ffmpeg_argv(make_chain(big_cores="4,5,6,7"))
        self.assertEqual(argv[:3], ["taskset", "-c", "4,5,6,7"])
        self.assertEqual(argv[-2:], ["-filter_complex_threads", "4"])

    def test_a_uniform_cpu_runs_plain_ffmpeg(self):
        self.assertEqual(verify.ffmpeg_argv(make_chain()), ["ffmpeg"])

    def test_av1_stays_on_the_cpu(self):
        # av1_rkmpp decoded a 1202-wide encode wrongly (jellyfin-ffmpeg 8.1.3-2).
        self.assertEqual(self.decoder("av1", grain=True), [])
        self.assertEqual(self.decoder("av1", grain=False), [])

    def test_no_rkmpp_build_means_software(self):
        self.assertEqual(self.decoder("h264", grain=False, rkmpp=set()), [])

    def test_grain_is_measured_at_one_bit_depth(self):
        # psnr reports MSE in the input's units, so an 8-bit source against its
        # 10-bit encode read 16x weaker and calibration added no grain at all.
        self.assertTrue(verify.GRAIN_GRAPH.startswith("[0:v]format=yuv420p10le,"))

    def test_grainless_source_frames_do_not_read_as_the_worst(self):
        self.assertEqual(verify.retention_by_frame([0.0, 4.0], [0.0, 1.0]), [1.0, 0.5])

    def test_windows_avoid_ones_already_chosen(self):
        # Two bad stretches exactly one window wide, the first slightly worse.
        scores = [1.0] * 50 + [50.0] * 100 + [2.0] * 50
        first = verify.worst_windows(scores, window=50, count=1)
        second = verify.worst_windows(scores, window=50, count=1, avoid=first)
        self.assertEqual((first, second), ([0], [150]))


class TestGrainAnalysisClip(unittest.TestCase):
    """grav1synth rejects rows that are not a multiple of 32 bytes."""

    def test_analysis_clips_are_cropped_to_a_width_grav1synth_accepts(self):
        seen: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            Path(argv[-1]).write_bytes(b"x")
            return util.Completed(argv, 0, "", "", 0.0)

        original = grain.run
        grain.run = fake_run
        self.addCleanup(setattr, grain, "run", original)
        with tempfile.TemporaryDirectory() as tmp:
            info = make_info(width=3836, height=2072)
            grain._extract_lossless(info, make_chain(), config.Config(), [10.0], Path(tmp))
        self.assertIn("-vf", seen[0])
        self.assertEqual(seen[0][seen[0].index("-vf") + 1], grain.ANALYSIS_CROP)
        # 3836 -> 3808 at 10-bit and 8-bit alike; nothing is cropped from the height.
        self.assertEqual(grain.ANALYSIS_CROP, "crop=trunc(iw/32)*32:ih:0:0")


class TestXpsnrAlignment(unittest.TestCase):
    """ffmpeg pairs frames by timestamp; Matroska rounds them to milliseconds."""

    def test_both_inputs_are_renumbered_so_frames_pair_by_index(self):
        seen: list[list[str]] = []

        def fake_run(argv, _label, **_kwargs):
            seen.append(argv)
            Path(argv[argv.index("-lavfi") + 1].split("stats_file=")[1]).write_text(
                "n:    1  XPSNR y: 40.0000  XPSNR u: 45.0  XPSNR v: 46.0\n")
            return 0.0

        original = verify.run_progress
        verify.run_progress = fake_run
        self.addCleanup(setattr, verify, "run_progress", original)
        with tempfile.TemporaryDirectory() as tmp:
            scores = verify.xpsnr(make_chain(), Path("/src.mkv"), Path("/enc.mkv"),
                                  Path(tmp) / "x.log")
        graph = seen[0][seen[0].index("-lavfi") + 1]
        self.assertEqual(scores, [40.0])
        self.assertIn(f"[0:v]{verify.ALIGN},", graph)
        self.assertIn(f"[1:v]{verify.ALIGN},", graph)


class TestLowpassPsnr(unittest.TestCase):
    def test_psnr_log_is_read_per_frame_and_identical_frames_stay_finite(self):
        text = ("n:1 mse_avg:0.5 mse_y:0.4 psnr_avg:51.2 psnr_y:52.30 psnr_u:55.0\n"
                "n:2 mse_avg:0.0 mse_y:0.0 psnr_avg:inf psnr_y:inf psnr_u:inf\n")
        self.assertEqual(verify.parse_psnr_log(text), [52.3, verify.IDENTICAL_DB])

    def test_worst_frames_come_from_the_structural_measure_when_present(self):
        result = verify.Comparison(frames=3, xpsnr=[10.0, 50.0, 50.0], lowpass=[50.0, 50.0, 30.0])
        self.assertEqual(result.lowpass_p5, 30.0)
        self.assertIn("low-pass PSNR mean", result.describe())


class TestVarianceBoost(unittest.TestCase):
    """The strength alone does not turn variance boost on; SVT-AV1 defaults it off."""

    def params(self, strength: int, options: set[str]) -> list[str]:
        cfg = config.Config()
        cfg.encode.variance_boost_strength = strength
        return encode.svt_params(make_info(), cfg, make_chain(svt_options=options))

    def test_it_is_switched_on_not_just_tuned(self):
        argv = self.params(2, {"--enable-variance-boost", "--variance-boost-strength"})
        on = argv.index("--enable-variance-boost")
        self.assertEqual(argv[on + 1], "1")
        self.assertEqual(argv[argv.index("--variance-boost-strength") + 1], "2")

    def test_zero_turns_it_off(self):
        argv = self.params(0, {"--enable-variance-boost", "--variance-boost-strength"})
        self.assertNotIn("--enable-variance-boost", argv)
        self.assertNotIn("--variance-boost-strength", argv)

    def test_it_is_on_by_default_at_4k_only(self):
        # The only setting that removed the flat-area blocks on the 4K test film,
        # and a loss at every lower resolution tested.
        chain = make_chain(svt_options={"--enable-variance-boost"})
        argv = encode.svt_params(make_info(width=3836, height=2072), config.Config(), chain)
        self.assertEqual(argv[argv.index("--variance-boost-strength") + 1], "2")
        for width in (1920, 1280, 720):
            argv = encode.svt_params(make_info(width=width, height=width * 9 // 16),
                                     config.Config(), chain)
            self.assertNotIn("--variance-boost-strength", argv)

    def test_a_table_is_read_by_band(self):
        cfg = config.Config()
        cfg.encode.variance_boost_strength = {"1700": 3, "0": 1}
        chain = make_chain(svt_options=set())
        strength = [encode.svt_params(make_info(width=w, height=w * 9 // 16), cfg, chain)
                    for w in (3840, 1920, 720)]
        self.assertEqual([a[a.index("--variance-boost-strength") + 1] for a in strength],
                         ["3", "3", "1"])

    def test_a_table_is_checked_band_by_band(self):
        cfg = config.Config()
        cfg.encode.variance_boost_strength = {"3000": 2, "0": 7}
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)
        cfg.encode.variance_boost_strength = {"wide": 2}
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def curve(self, cfg: config.Config | None = None, **info: Any) -> str:
        info.setdefault("width", 3840)    # variance boost is on at 4K only
        argv = encode.svt_params(make_info(**info), cfg or config.Config(),
                                 make_chain(svt_options={"--variance-boost-curve"}))
        return argv[argv.index("--variance-boost-curve") + 1]

    def test_pq_output_gets_the_pq_curve(self):
        self.assertEqual(self.curve(color_transfer="smpte2084"), "2")
        self.assertEqual(self.curve(color_transfer="bt709"), "0")

    def test_an_explicit_curve_wins(self):
        cfg = config.Config()
        cfg.encode.variance_boost_curve = 1
        self.assertEqual(self.curve(cfg, color_transfer="smpte2084"), "1")

    def test_an_encoder_without_the_curve_is_not_asked_for_it(self):
        argv = encode.svt_params(make_info(width=3840, color_transfer="smpte2084"),
                                 config.Config(),
                                 make_chain(svt_options={"--enable-variance-boost"}))
        self.assertIn("--variance-boost-strength", argv)
        self.assertNotIn("--variance-boost-curve", argv)

    def test_an_unknown_curve_is_rejected(self):
        cfg = config.Config()
        cfg.encode.variance_boost_curve = "hdr"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_out_of_range_strength_is_rejected(self):
        cfg = config.Config()
        cfg.encode.variance_boost_strength = 5
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestDarkBias(unittest.TestCase):
    """1080p keeps its dark scenes from blocking with a lower QP on dark frames."""

    OPTIONS: ClassVar[set[str]] = {"--luminance-qp-bias"}

    def bias_at(self, width: int, cfg: config.Config | None = None,
                options: set[str] = OPTIONS) -> str | None:
        argv = encode.svt_params(make_info(width=width, height=width * 9 // 16),
                                 cfg or config.Config(), make_chain(svt_options=options))
        return argv[argv.index("--luminance-qp-bias") + 1] \
            if "--luminance-qp-bias" in argv else None

    def test_only_1080p_gets_it_by_default(self):
        self.assertEqual([self.bias_at(w) for w in (3840, 1920, 1280, 720)],
                         [None, "100", None, None])

    def test_one_number_applies_everywhere(self):
        cfg = config.Config()
        cfg.encode.luminance_qp_bias = 40
        self.assertEqual([self.bias_at(w, cfg) for w in (3840, 720)], ["40", "40"])

    def test_an_encoder_without_it_is_not_asked_for_it(self):
        self.assertIsNone(self.bias_at(1920, options=set()))

    def test_out_of_range_is_rejected(self):
        cfg = config.Config()
        cfg.encode.luminance_qp_bias = {"1700": 101}
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_a_table_loads_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[encode.luminance_qp_bias]\n1700 = 60\n0 = 0\n")
            cfg = config.load(path)
        self.assertEqual([self.bias_at(w, cfg) for w in (3840, 1920, 720)], ["60", "60", None])


class TestFogSettings(unittest.TestCase):
    """4K keeps its grain in flat fog, and 1080p its rain, with ac-bias and a weak
    temporal filter."""

    OPTIONS: ClassVar[set[str]] = {"--ac-bias", "--tf-strength"}

    def flags_at(self, width: int, cfg: config.Config | None = None,
                 options: set[str] = OPTIONS) -> tuple[str | None, str | None]:
        argv = encode.svt_params(make_info(width=width, height=width * 9 // 16),
                                 cfg or config.Config(), make_chain(svt_options=options))
        def value(flag: str) -> str | None:
            return argv[argv.index(flag) + 1] if flag in argv else None
        return value("--ac-bias"), value("--tf-strength")

    def test_4k_and_1080p_get_them_by_default(self):
        self.assertEqual([self.flags_at(w) for w in (3836, 1920, 1280, 720)],
                         [("1.0", "1"), ("1.0", "1"), (None, None), (None, None)])

    def test_tables_are_read_by_band(self):
        cfg = config.Config()
        cfg.encode.ac_bias = {"1700": 0.5, "0": 0}
        cfg.encode.tf_strength = 0
        self.assertEqual([self.flags_at(w, cfg) for w in (3840, 1920, 720)],
                         [("0.5", "0"), ("0.5", "0"), (None, "0")])

    def test_an_encoder_without_them_is_not_asked_for_them(self):
        self.assertEqual(self.flags_at(3840, options=set()), (None, None))

    def test_ranges_are_checked(self):
        for name, value in (("ac_bias", 8.5), ("ac_bias", {"3000": -1.0}),
                            ("tf_strength", 5), ("tf_strength", {"0": -2}),
                            ("tf_strength", 1.5)):
            cfg = config.Config()
            setattr(cfg.encode, name, value)
            with self.assertRaises(config.ConfigError, msg=f"{name} = {value!r}"):
                config.validate(cfg)

    def test_tables_load_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("[encode.ac_bias]\n3000 = 2\n0 = 0.5\n"
                            "[encode.tf_strength]\n3000 = 0\n0 = -1\n")
            cfg = config.load(path)
        self.assertEqual([self.flags_at(w, cfg) for w in (3840, 1920)],
                         [("2", "0"), ("0.5", None)])


class TestEncoderCapabilities(unittest.TestCase):
    """An old SvtAv1EncApp would quietly give the encodes the ladder was set against."""

    FULL: ClassVar[set[str]] = {"--luminance-qp-bias", "--enable-variance-boost", "--ac-bias",
                                "--tf-strength"}

    def problems(self, options: set[str], cfg: config.Config | None = None) -> str:
        chain = make_chain(svt_options=options, svtav1_version="2.3.0")
        try:
            tools.require(chain, cfg or config.Config())
        except tools.ToolError as exc:
            return str(exc)
        return ""

    def test_a_current_encoder_passes(self):
        self.assertEqual(self.problems(self.FULL), "")

    def test_missing_dark_bias_stops_the_run(self):
        message = self.problems({"--enable-variance-boost"})
        self.assertIn("luminance_qp_bias", message)
        self.assertIn("2.3.0", message)

    def test_missing_variance_boost_stops_the_run(self):
        self.assertIn("variance_boost_strength",
                      self.problems(self.FULL - {"--enable-variance-boost"}))

    def test_missing_fog_settings_stop_the_run(self):
        self.assertIn("encode.ac_bias", self.problems(self.FULL - {"--ac-bias"}))
        self.assertIn("encode.tf_strength", self.problems(self.FULL - {"--tf-strength"}))

    def test_settings_turned_off_need_nothing(self):
        cfg = config.Config()
        cfg.encode.luminance_qp_bias = 0
        cfg.encode.variance_boost_strength = {"3000": 0}
        cfg.encode.ac_bias = 0.0
        cfg.encode.tf_strength = {"3000": -1}
        self.assertEqual(self.problems(set(), cfg), "")


def tested_chain(**overrides: Any) -> Toolchain:
    """A chain at exactly the versions toolchain.toml names."""
    pins = versions.load()
    base: dict[str, Any] = dict(
        ffmpeg_version=f"{pins['ffmpeg'].version}-Jellyfin",
        svtav1_version=pins["svtav1"].version,
        av1an_version=f"{pins['av1an'].version}-unstable",
        grav1synth_version=pins["grav1synth"].version,
        mkvmerge_version="92.0", vspipe="vspipe",
        vapoursynth_version=pins["vapoursynth"].version,
        vapoursynth_plugins={"lsmas", "std"}, lsmash_commit=pins["lsmash"].version,
        svtfg_commit=pins["svtfg"].version,
        scenechange="av-scenechange", scenechange_version=pins["scenechange"].version,
    )
    base.update(overrides)
    return make_chain(**base)


class TestToolchainVersions(unittest.TestCase):
    """One autotranscode version, one toolchain: doctor and run say when it is not."""

    def pin(self, version: str, accept: str, enforce: bool = True) -> versions.Pin:
        return versions.Pin("tool", version, accept, enforce, "tool")

    def test_the_manifest_is_complete(self):
        pins = versions.load()
        bootstrap = (Path(__file__).parent.parent / "scripts" / "bootstrap.sh").read_text()
        stages = re.search(r"stages=\((deps [^)]*)\)", bootstrap).group(1).split()
        for name, pin in pins.items():
            self.assertIn(pin.stage, stages, name)
        # Every tool versions.py reads has a pin; zimg is only built for VapourSynth.
        self.assertEqual(set(versions.installed_versions(make_chain())) | {"zimg"}, set(pins))

    def test_bootstrap_reads_the_same_versions(self):
        script = Path(__file__).parent.parent / "scripts" / "bootstrap.sh"
        lines = script.read_text().splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith("SCRIPT_DIR="))
        last = next(i for i, line in enumerate(lines) if line.startswith("LSMASH_WORKS_COMMIT="))
        header = "\n".join(lines[first:last + 1])
        names = ["FFMPEG_MAJOR", "SVT_AV1_VERSION", "ZIMG_VERSION", "VAPOURSYNTH_VERSION",
                 "AV1AN_VERSION", "GRAV1SYNTH_VERSION", "SVTFG_COMMIT", "LSMASH_WORKS_COMMIT"]
        env = {key: value for key, value in os.environ.items() if key not in names}
        env["TOOLCHAIN"] = str(versions.MANIFEST)
        with tempfile.TemporaryDirectory() as tmp:
            pins_sh = Path(tmp) / "pins.sh"
            pins_sh.write_text("set -euo pipefail\n" + header + "\n"
                               + "".join(f'echo "${name}"\n' for name in names))
            out = subprocess.run(["bash", str(pins_sh)], capture_output=True, text=True,
                                 check=True, env=env).stdout.split()
        pins = versions.load()
        self.assertEqual(out, [
            pins["ffmpeg"].version.split(".")[0], f"v{pins['svtav1'].version}",
            f"release-{pins['zimg'].version}", f"R{pins['vapoursynth'].version}",
            pins["av1an"].version, pins["grav1synth"].version.split("+")[0],
            pins["svtfg"].version, pins["lsmash"].version,
        ])

    def test_the_patch_names_the_build_the_manifest_expects(self):
        version = versions.load()["grav1synth"].version
        crate, _, revision = version.partition("+")
        patch = (Path(__file__).parent.parent / "scripts"
                 / f"grav1synth-{crate}-grain-flag.patch").read_text()
        self.assertIn(f'"+{revision}"', patch)

    def test_exact(self):
        pin = self.pin("0.6.0", "exact")
        self.assertEqual(versions.compare(pin, "0.6.0-unstable"), "ok")
        self.assertEqual(versions.compare(pin, "0.6"), "ok")
        self.assertEqual(versions.compare(pin, "0.5.2-unstable"), "older")
        self.assertEqual(versions.compare(pin, "0.6.1"), "newer")

    def test_exact_with_a_patch_needs_the_patch(self):
        pin = self.pin("0.2.0+autotranscode.1", "exact")
        self.assertEqual(versions.compare(pin, "0.2.0+autotranscode.1"), "ok")
        self.assertEqual(versions.compare(pin, "0.2.0+autotranscode.2"), "different")
        self.assertEqual(versions.compare(pin, "0.2.0"), "different")
        # A stock build has no --version at all.
        self.assertEqual(versions.compare(pin, ""), "unknown")

    def test_same_major(self):
        pin = self.pin("8.1.3", "same-major")
        self.assertEqual(versions.compare(pin, "8.1.3-Jellyfin"), "ok")
        self.assertEqual(versions.compare(pin, "8.2.0-Jellyfin"), "ok")
        self.assertEqual(versions.compare(pin, "8.0.1-Jellyfin"), "older")
        self.assertEqual(versions.compare(pin, "7.1.2-Jellyfin"), "older")
        self.assertEqual(versions.compare(pin, "9.0-Jellyfin"), "newer")

    def test_at_least_and_untracked(self):
        self.assertEqual(versions.compare(self.pin("72", "at-least"), "73"), "ok")
        self.assertEqual(versions.compare(self.pin("72", "at-least"), "71"), "older")
        self.assertEqual(versions.compare(self.pin("97.0", "untracked"), "61.0"), "ok")
        self.assertEqual(versions.compare(self.pin("97.0", "untracked"), ""), "ok")

    def test_a_commit(self):
        pin = self.pin("7e63c8400a252f4dbbeea80cc3bfec895abf12cf", "exact")
        self.assertEqual(versions.compare(pin, "7e63c8400a252f4dbbeea80cc3bfec895abf12cf"), "ok")
        self.assertEqual(versions.compare(pin, "7e63c84"), "ok")
        self.assertEqual(versions.compare(pin, "0123abc"), "different")
        self.assertEqual(versions.compare(pin, ""), "unknown")

    def test_missing_is_left_to_the_capability_checks(self):
        check = versions.Check(self.pin("1.0", "exact"), None,
                               versions.compare(self.pin("1.0", "exact"), None))
        self.assertEqual(check.status, "missing")
        self.assertFalse(check.failed)

    def test_the_tested_toolchain_passes(self):
        checks = versions.check(tested_chain())
        self.assertEqual({c.pin.name: c.status for c in checks if c.status != "ok"}, {})
        versions.require(tested_chain(), config.Config(), checks)
        self.assertEqual(versions.fixes(checks), "")

    def test_an_untested_encoder_stops_the_run(self):
        chain = tested_chain(av1an_version="0.5.2-unstable", grav1synth_version="")
        with self.assertRaises(tools.ToolError) as caught:
            versions.require(chain, config.Config())
        message = str(caught.exception)
        self.assertIn("av1an 0.5.2-unstable is older", message)
        self.assertIn("scripts/bootstrap.sh av1an", message)
        self.assertIn("grav1synth: cannot tell", message)
        self.assertEqual(versions.fixes(versions.check(chain)), "av1an grav1synth")

    def test_warn_runs_anyway(self):
        cfg = config.Config()
        cfg.tools.versions = "warn"
        with self.assertLogs(util.log, "WARNING") as logs:
            versions.require(tested_chain(svtav1_version="4.4.0"), cfg)
        self.assertIn("svtav1 4.4.0 is newer", logs.output[0])

    def test_what_is_not_enforced_is_only_reported(self):
        chain = tested_chain(lsmash_commit="", vapoursynth_version="70")
        checks = versions.check(chain)
        versions.require(chain, config.Config(), checks)
        self.assertEqual(versions.fixes(checks), "vapoursynth lsmash")
        self.assertIn("UNKNOWN (not enforced)", versions.describe(checks))

    def test_run_refuses_an_untested_toolchain_before_any_film(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "config.toml"
            settings.write_text(f'[paths]\nwork_dir = "{tmp}/work"\n', encoding="utf-8")
            chain = tested_chain(av1an_version="0.5.2-unstable")
            with unittest.mock.patch.object(cli.tools, "detect", return_value=chain), \
                    unittest.mock.patch.object(cli.tools, "require"), \
                    unittest.mock.patch.object(cli.pipeline, "process_all") as process_all, \
                    self.assertLogs(util.log, "ERROR") as logged:
                code = cli.main(["-c", str(settings), "run", str(Path(tmp) / "a.mkv")])
        self.assertEqual(code, 2)
        process_all.assert_not_called()
        self.assertIn("av1an 0.5.2-unstable is older", "\n".join(logged.output))

    def test_an_unknown_setting_is_refused(self):
        cfg = config.Config()
        cfg.tools.versions = "ignore"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_the_vapoursynth_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "libLSMASHSource.so"
            plugin.touch()
            (Path(tmp) / tools.LSMASH_COMMIT_FILE).write_text("7e63c84\n")
            chain = make_chain()
            tools.read_vapoursynth_probe(chain, (
                "Warning: something\nrelease: 72\n"
                f"plugin: lsmas {plugin}\nplugin: std \nOutput Index: 0\n"))
        self.assertEqual(chain.vapoursynth_version, "72")
        self.assertEqual(chain.vapoursynth_plugins, {"lsmas", "std"})
        self.assertEqual(chain.lsmash_commit, "7e63c84")

    def test_a_plugin_built_before_the_record_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = make_chain()
            tools.read_vapoursynth_probe(chain, f"release: 72\nplugin: lsmas {tmp}/x.so\n")
        self.assertEqual(chain.lsmash_commit, "")


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def next_version(version: str) -> str:
    """One patch release on: whatever a pin is now, a release that moves it."""
    *head, last = version.split(".")
    return ".".join([*head, str(int(last) + 1)])


NEXT_AV1AN = next_version(versions.load()["av1an"].version)


class TestUpdate(unittest.TestCase):
    """`update` says whether there is a newer version and which tools it updates, and
    queues it; `run` installs it before a film no run had started."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = config.Config()
        self.cfg.paths.work_dir = str(self.root / "work")
        patcher = unittest.mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        upstream = self.root / "upstream"
        (upstream / "autotranscode").mkdir(parents=True)
        (upstream / "scripts").mkdir()
        (upstream / "autotranscode" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
        shutil.copy(versions.MANIFEST, upstream / "autotranscode" / versions.MANIFEST.name)
        # Records the stages it is asked for.
        (upstream / "scripts" / "bootstrap.sh").write_text(
            f'echo "$@" >> "{self.root / "stages"}"\n')
        self.git(upstream, "init", "-q", "-b", "main")
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-qm", "first")
        self.local = self.root / "local"
        self.git(self.root, "clone", "-q", str(upstream), str(self.local))
        self.upstream = upstream

    def git(self, where: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(where), *args], check=True, text=True,
                              capture_output=True).stdout

    def release(self, version: str = "9.9.9", *, tag: bool = True, **pins: str) -> None:
        (self.upstream / "autotranscode" / "__init__.py").write_text(
            f'__version__ = "{version}"\n')
        manifest = self.upstream / "autotranscode" / versions.MANIFEST.name
        text, ours = manifest.read_text(), versions.load()
        for name, value in (pins or {"av1an": NEXT_AV1AN}).items():
            text = text.replace(f'version = "{ours[name].version}"', f'version = "{value}"')
        manifest.write_text(text)
        self.git(self.upstream, "commit", "-qam", f"Release {version}")
        if tag:
            self.git(self.upstream, "tag", "-a", f"v{version}", "-m", f"autotranscode {version}")

    def update(self, chain: Toolchain | None = None, **options: Any) -> tuple[int, str]:
        options.setdefault("doctor", lambda: 0)
        options.setdefault("ask", lambda _prompt: "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = update.main(self.cfg, chain or tested_chain(), root=self.local, **options)
        return code, out.getvalue()

    def stages(self) -> str:
        path = self.root / "stages"
        return path.read_text().strip() if path.exists() else ""

    def pulled(self) -> bool:
        return "9.9.9" in (self.local / "autotranscode" / "__init__.py").read_text()

    def stopped_film(self, name: str = "Film (1975).mkv") -> Path:
        source = self.root / name
        work = Path(self.cfg.paths.work_dir) / pipeline.source_digest(source)
        work.mkdir(parents=True)
        (work / pipeline.SOURCE_FILE).write_text(str(source.resolve()))
        (work / "scenes-0.json").write_text("{}")
        return source

    def test_up_to_date(self):
        code, out = self.update()
        self.assertEqual(code, 0)
        self.assertIn("is the newest, with the tools it was tested with", out)
        self.assertEqual(self.stages(), "")

    def test_commits_without_a_new_version_are_no_update(self):
        (self.upstream / "notes").write_text("x")
        self.git(self.upstream, "add", "notes")
        self.git(self.upstream, "commit", "-qm", "notes")
        code, out = self.update()
        self.assertEqual(code, 0)
        self.assertIn("is the newest", out)

    def test_a_version_on_the_branch_is_no_release(self):
        self.release(tag=False)
        code, out = self.update(check_only=True)
        self.assertEqual(code, 0)
        self.assertIn("is the newest", out)

    def test_the_newest_release_is_the_highest_version_not_the_last_tag(self):
        self.release("0.10.0")
        self.release("0.9.0")   # tagged later, but older
        code, out = self.update(check_only=True)
        self.assertEqual(code, 1)
        self.assertIn("autotranscode 0.10.0 is available", out)

    def test_it_installs_the_release_not_the_branch_after_it(self):
        self.release()
        (self.upstream / "notes").write_text("unreleased")
        self.git(self.upstream, "add", "notes")
        self.git(self.upstream, "commit", "-qm", "work in progress")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.pulled())
        self.assertFalse((self.local / "notes").exists())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"),
                         self.git(self.local, "rev-parse", "v9.9.9^{commit}"))
        self.assertEqual(self.git(self.local, "branch", "--show-current").strip(), "main")

    def test_check_names_the_version_and_the_tools_and_no_commits(self):
        self.release()
        code, out = self.update(check_only=True)
        self.assertEqual(code, 1)
        self.assertIn(f"autotranscode 9.9.9 is available (this is {__version__})", out)
        self.assertIn(f"It updates: Av1an {versions.load()['av1an'].version} -> {NEXT_AV1AN}", out)
        self.assertNotIn("Release 9.9.9", out)
        self.assertFalse(self.pulled())

    def test_what_apt_installs_takes_no_part(self):
        self.release(ffmpeg="9.0.0", av1an=NEXT_AV1AN)
        code, out = self.update(tested_chain(ffmpeg_version="7.1-Jellyfin",
                                             mkvmerge_version="61.0"), check_only=True)
        self.assertEqual(code, 1)
        self.assertNotIn("ffmpeg", out)
        self.assertNotIn("mkvmerge", out)
        self.assertIn("Av1an", out)

    def test_with_nothing_to_finish_it_installs_now(self):
        self.release()
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.pulled())
        self.assertEqual(self.stages(), "av1an")
        self.assertIn("Updated.", out)

    def test_tools_a_release_adds_are_installed(self):
        # 0.3.0 added SvtFgEssential and av-scenechange: absent, not wrong.
        self.release(av1an=versions.load()["av1an"].version)
        code, out = self.update(tested_chain(svtfg=None, scenechange=None))
        self.assertEqual(code, 0, out)
        self.assertIn("SvtFgEssential not installed", out)
        self.assertEqual(self.stages(), "svtfg scenechange")

    def test_a_version_that_moves_no_tool_rebuilds_nothing(self):
        self.release(av1an=versions.load()["av1an"].version)
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("It updates no tools", out)
        self.assertTrue(self.pulled())
        self.assertEqual(self.stages(), "")

    def test_declining_changes_nothing(self):
        self.release()
        code, _ = self.update(ask=lambda _prompt: "n")
        self.assertEqual(code, 1)
        self.assertFalse(self.pulled())
        self.assertIsNone(update.queued(self.cfg))

    def test_a_stopped_film_is_finished_first(self):
        self.release()
        source = self.stopped_film()
        prompts: list[str] = []
        code, out = self.update(ask=lambda prompt: prompts.append(prompt) or "")
        self.assertEqual(code, 0, out)
        self.assertIn("Queue it to install once the next `autotranscode run` has finished "
                      f"{source.name}? [Y/n]", prompts[0])
        self.assertEqual(present(update.queued(self.cfg))["version"], "9.9.9")
        self.assertFalse(self.pulled())
        self.assertEqual(self.stages(), "")

    def test_a_run_going_on_installs_it_after_its_film(self):
        self.release()
        source = self.stopped_film()
        work = Path(self.cfg.paths.work_dir) / pipeline.source_digest(source)
        with pipeline.only_run(self.cfg):
            (work / pipeline.RUNNING_FILE).write_text(str(os.getpid()))
            code, out = self.update(yes=True)
        self.assertEqual(code, 0, out)
        self.assertIn(f"after the run (pid {os.getpid()}) finishes {source.name}", out)
        self.assertIsNotNone(update.queued(self.cfg))
        self.assertFalse(self.pulled())

    def test_queued_already_and_cancel(self):
        self.release()
        self.stopped_film()
        self.update(yes=True)
        code, out = self.update(yes=True)
        self.assertEqual(code, 0)
        self.assertIn("queued already", out)
        code, out = self.update(cancel=True)
        self.assertIn("cancelled", out)
        self.assertIsNone(update.queued(self.cfg))

    def test_a_diverged_checkout_is_not_touched(self):
        self.release()
        (self.local / "notes").write_text("x")
        self.git(self.local, "add", "notes")
        self.git(self.local, "commit", "-qm", "local")
        code, out = self.update(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("this checkout has commits that v9.9.9 does not", out)
        self.assertEqual(self.stages(), "")

    def test_uncommitted_changes_are_not_touched(self):
        self.release()
        (self.local / "autotranscode" / "__init__.py").write_text("# mine\n")
        code, out = self.update(yes=True)
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)

    def test_tools_out_of_line_are_offered_without_a_new_version(self):
        code, out = self.update(tested_chain(av1an_version="0.5.2"))
        self.assertEqual(code, 0, out)
        self.assertIn("not the ones it was tested with: Av1an 0.5.2 ->", out)
        self.assertEqual(self.stages(), "av1an")

    def test_tools_no_run_can_encode_with_are_rebuilt_now_not_queued(self):
        # A board updated by hand from before tools reported their versions: a run
        # refuses grav1synth, so none could finish the stopped film first.
        source = self.stopped_film()
        prompts: list[str] = []
        code, out = self.update(tested_chain(grav1synth_version="", lsmash_commit=""),
                                ask=lambda prompt: prompts.append(prompt) or "")
        self.assertEqual(code, 0, out)
        self.assertIn(f"No run can encode until grav1synth is rebuilt; {source.name} "
                      "resumes after that.", out)
        self.assertEqual(prompts, ["Install it now? [Y/n] "])
        self.assertEqual(self.stages(), "lsmash grav1synth")
        self.assertIsNone(update.queued(self.cfg))

    def test_a_run_that_cannot_encode_installs_the_queued_update_first(self):
        settings = self.root / "config.toml"
        settings.write_text(f'[paths]\nwork_dir = "{self.cfg.paths.work_dir}"\n'
                            f'state_file = "{self.root / "state.json"}"\n')
        Path(self.cfg.paths.work_dir).mkdir()
        Path(self.cfg.paths.work_dir, update.QUEUED).write_text('{"version": "9.9.9"}')
        chain = tested_chain(grav1synth_version="")
        with unittest.mock.patch.object(cli.tools, "detect", return_value=chain), \
                unittest.mock.patch.object(cli.tools, "require"), \
                unittest.mock.patch.object(cli.pipeline, "process_all") as process_all, \
                unittest.mock.patch.object(cli.update, "install_queued") as install, \
                self.assertLogs(util.log, "WARNING"):
            code = cli.main(["-c", str(settings), "run", str(self.root / "a.mkv")])
        install.assert_called_once()
        process_all.assert_not_called()
        self.assertEqual(code, 2)   # only when the exec did not happen

    def test_a_run_installs_it_before_a_film_no_run_had_started(self):
        stopped = self.stopped_film()
        fresh = self.root / "New (2001).mkv"
        Path(self.cfg.paths.work_dir, update.QUEUED).write_text('{"version": "9.9.9"}')
        done: list[Path] = []

        def encode(source, *_args, **_kwargs):
            done.append(source)
            return Record(source=str(source), status="done")

        with unittest.mock.patch.object(pipeline, "process_one", encode), \
                self.assertRaises(update.InstallQueued):
            pipeline._process_all([stopped, fresh], make_chain(), self.cfg,
                                  Ledger(self.root / "state.json"), pipeline.StopRequest())
        self.assertEqual(done, [stopped])

    def test_the_run_installs_and_starts_again(self):
        self.release()
        Path(self.cfg.paths.work_dir).mkdir()
        Path(self.cfg.paths.work_dir, update.QUEUED).write_text('{"version": "9.9.9"}')
        started: list[list[str]] = []
        with unittest.mock.patch.object(update, "checkout", return_value=self.local), \
                self.assertLogs(util.log, "INFO") as logs:
            update.install_queued(self.cfg, tested_chain(),
                                  lambda _exe, argv: started.append(argv))
        self.assertTrue(self.pulled())
        self.assertEqual(self.stages(), "av1an")
        self.assertEqual(started, [sys.orig_argv])
        self.assertIsNone(update.queued(self.cfg))
        self.assertIn("installing the queued update to autotranscode 9.9.9",
                      "\n".join(logs.output))

    def test_a_failed_build_is_not_retried_before_every_film(self):
        (self.upstream / "scripts" / "bootstrap.sh").write_text("echo nope; exit 3\n")
        self.release()
        Path(self.cfg.paths.work_dir).mkdir()
        Path(self.cfg.paths.work_dir, update.QUEUED).write_text('{"version": "9.9.9"}')
        started: list[list[str]] = []
        with unittest.mock.patch.object(update, "checkout", return_value=self.local), \
                self.assertLogs(util.log, "ERROR") as logs:
            update.install_queued(self.cfg, tested_chain(),
                                  lambda _exe, argv: started.append(argv))
        self.assertIn("nope", "\n".join(logs.output))
        self.assertEqual(len(started), 1)
        self.assertIsNone(update.queued(self.cfg))

    def test_run_hands_a_queued_install_over(self):
        settings = self.root / "config.toml"
        settings.write_text(f'[paths]\nwork_dir = "{self.cfg.paths.work_dir}"\n'
                            f'state_file = "{self.root / "state.json"}"\n')
        with unittest.mock.patch.object(cli.tools, "detect", return_value=tested_chain()), \
                unittest.mock.patch.object(cli.tools, "require"), \
                unittest.mock.patch.object(cli.pipeline, "discover",
                                           return_value=[self.root / "a.mkv"]), \
                unittest.mock.patch.object(cli.pipeline, "process_all",
                                           side_effect=update.InstallQueued), \
                unittest.mock.patch.object(cli.update, "install_queued") as install:
            code = cli.main(["-c", str(settings), "run", str(self.root / "a.mkv")])
        self.assertEqual(code, 0)
        install.assert_called_once()


class TestGrainTableScaling(unittest.TestCase):
    TABLE = ("filmgrn1\nE 0 9223372036854775807 1 0 1\n"
             "\tp 3 8 0 11 0 1 128 192 256 128 192 256\n"
             "\tsY 3  0 22 81 23 255 24\n"
             "\tsCb 3 0 17 54 18 255 20\n"
             "\tsCr 2 0 200 255 250\n"
             "\tcY 6 2 0 0 -1 0 4 1 -3 -2 4 1 -1 0 -1 -2 12 70 35 14 6 3 11 76\n")

    def test_scalings_scale_and_positions_and_texture_do_not(self):
        scaled = grain.scale_table(self.TABLE, 0.5).splitlines()
        self.assertEqual(scaled[3], "\tsY 3 0 11 81 12 255 12")
        self.assertEqual(scaled[4], "\tsCb 3 0 8 54 9 255 10")
        # AR coefficients (texture) and the parameter line are left exactly as they were.
        self.assertEqual(scaled[5 + 1], self.TABLE.splitlines()[6])
        self.assertEqual(scaled[2], self.TABLE.splitlines()[2])

    def test_scalings_are_clamped_to_eight_bits(self):
        scaled = grain.scale_table(self.TABLE, 1.5).splitlines()
        self.assertEqual(scaled[5], "\tsCr 2 0 255 255 255")

    def test_scaling_by_one_changes_nothing_but_spacing(self):
        again = grain.scale_table(self.TABLE, 1.0)
        self.assertEqual(again.split(), self.TABLE.split())


class TestGrainCalibration(unittest.TestCase):
    def test_factor_matches_the_source_after_what_the_encode_kept(self):
        # Source energy 10; encode keeps 1 on its own; the table adds 20 more.
        # Needed: 9 more, so amplitude scales by sqrt(9/20).
        self.assertAlmostEqual(present(grain.strength_factor(10.0, 1.0, 21.0)), (9 / 20) ** 0.5)

    def test_an_exact_match_needs_no_change(self):
        self.assertAlmostEqual(present(grain.strength_factor(10.0, 1.0, 10.0)), 1.0)

    def test_nothing_synthesised_gives_no_answer(self):
        self.assertIsNone(grain.strength_factor(10.0, 5.0, 5.0))

    def test_an_encode_that_kept_everything_needs_no_grain(self):
        # A clean source: the encode has all its fine texture already.
        self.assertEqual(grain.strength_factor(4.0, 5.0, 9.0), 0.0)

    def test_auto_drops_grain_a_clean_source_does_not_need(self):
        self.assertEqual(grain.decide_strength((0.05, 0.0, 0.0), "auto")[0], False)
        keep, luma, *_ = grain.decide_strength((0.05, 0.0, 0.0), "always")
        self.assertEqual((keep, luma), (True, grain.NEGLIGIBLE))

    def test_measured_factors_pass_through_within_bounds(self):
        measured = (1.12, 0.74, 0.45)   # the test film, as round 3 calibrated it
        self.assertEqual(grain.decide_strength(measured, "auto"), (True, *measured))
        self.assertEqual(grain.decide_strength((3.0, None, 0.0), "auto"), (True, 2.0, 1.0, 0.0))

    def test_the_old_probe_only_runs_where_its_denoiser_still_matters(self):
        cfg = config.Config()
        self.assertFalse(grain.needs_probe(cfg))
        cfg.grain.estimator = "grav1synth"
        self.assertTrue(grain.needs_probe(cfg))


class TestPerPlaneGrain(unittest.TestCase):
    TABLE = TestGrainTableScaling.TABLE

    def test_each_plane_scales_by_its_own_factor(self):
        scaled = grain.scale_table(self.TABLE, 1.0, cb=0.5, cr=0.1).splitlines()
        self.assertEqual(scaled[3], "\tsY 3 0 22 81 23 255 24")
        self.assertEqual(scaled[4], "\tsCb 3 0 8 54 9 255 10")
        self.assertEqual(scaled[5], "\tsCr 2 0 20 255 25")

    def test_psnr_stats_are_read_per_plane(self):
        text = "n:1 mse_avg:3.0 mse_y:4.25 mse_u:1.5 mse_v:0.5 psnr_avg:40\n"
        self.assertEqual([verify.parse_mse_log(text, p) for p in "yuv"], [[4.25], [1.5], [0.5]])


def svt_frame(shift: int, luma_ar: list[int], seed: int = 7391) -> dict:
    """ffmpeg's view of one frame's grain parameters, as ffprobe reports them."""
    chroma = {"uv_points_value": "0 255", "uv_points_scaling": "15 14",
              "ar_coeffs_uv": " ".join(["1"] * 25), "uv_mult": 0, "uv_mult_luma": 64,
              "uv_offset": 0}
    return {
        "seed": str(seed), "chroma_scaling_from_luma": 0, "scaling_shift": 11,
        "ar_coeff_lag": 3, "ar_coeff_shift": shift, "grain_scale_shift": 0, "overlap_flag": 1,
        "components": [
            {"y_points_value": "0 255", "y_points_scaling": "16 16",
             "ar_coeffs_y": " ".join(map(str, luma_ar))},
            chroma, dict(chroma, uv_points_scaling="11 8"),
        ],
    }


class TestGrainFromSvt(unittest.TestCase):
    def test_table_matches_the_format_grav1synth_writes_and_keeps_the_real_shift(self):
        table = grain.table_from_params(svt_frame(8, list(range(24)))).splitlines()
        self.assertEqual(table[0], "filmgrn1")
        self.assertEqual(table[1], f"E 0 {grain.WHOLE_VIDEO_END} 1 7391 1")
        # scaling_shift 11 carried through; chroma multipliers restored to spec values.
        self.assertEqual(table[2], "\tp 3 8 0 11 0 1 128 192 256 128 192 256")
        self.assertEqual(table[3], "\tsY 2 0 16 255 16")
        self.assertEqual(table[5], "\tsCr 2 0 11 255 8")
        self.assertEqual(table[6], "\tcY " + " ".join(map(str, range(24))))
        self.assertEqual(len(table[7].split()) - 1, 25)
        # And it survives the flattening and strength scaling the pipeline applies.
        self.assertIn("\tsY 2 0 8 255 8", grain.scale_table("\n".join(table) + "\n", 0.5))

    def test_medoid_ignores_an_outlier_and_compares_across_shifts(self):
        typical = [0] * 22 + [64, 64]
        frames = [
            svt_frame(7, typical),                   # 0.5, 0.5 at shift 7
            svt_frame(8, [c * 2 for c in typical]),   # the same texture at shift 8
            svt_frame(7, [0] * 22 + [120, 5]),        # a directional one-off
        ]
        self.assertIsNot(grain.medoid(frames), frames[2])


class TestLibrarySamples(unittest.TestCase):
    """Choosing what `sample` encodes: which files, and where in them."""

    def test_band_follows_the_encode_settings(self):
        self.assertEqual(sample.band_of(make_info(width=3836, height=2072)), "4K")
        self.assertEqual(sample.band_of(make_info(width=1920, height=800)), "1080p")
        self.assertEqual(sample.band_of(make_info(width=1440, height=1080)), "1080p")
        self.assertEqual(sample.band_of(make_info(width=1280, height=720)), "720p")
        self.assertEqual(sample.band_of(make_info(width=720, height=576,
                                                  sample_aspect="64:45")), "SD")

    def test_a_failure_reports_what_the_tool_said(self):
        exc = CommandError(["/usr/bin/ffmpeg", "-i", "a long command line"], 234,
                           "\n[matroska] Can't write packet with unknown timestamp\nmore\n")
        self.assertEqual(sample._reason(exc), "ffmpeg exit 234: "
                         "[matroska] Can't write packet with unknown timestamp")
        self.assertEqual(sample._reason(OSError("disk full")), "disk full")

    def test_start_stays_clear_of_both_ends(self):
        rng = random.Random(1)
        for _ in range(200):
            start = present(sample.choose_start(7200, 240, 600, 600, rng))
            self.assertGreaterEqual(start, 600)
            self.assertLessEqual(start + 240, 7200 - 600)

    def test_short_file_skips_less_rather_than_nothing(self):
        # 22 minutes: ten minutes off each end would leave two, too few for four.
        start = present(sample.choose_start(1320, 240, 600, 600, random.Random(2)))
        self.assertGreaterEqual(start, 1320 * sample.MAX_SKIP_SHARE)
        self.assertLessEqual(start + 240, 1320 * (1 - sample.MAX_SKIP_SHARE))

    def test_too_short_to_sample(self):
        self.assertIsNone(sample.choose_start(200, 240, 0, 0, random.Random(3)))

    def test_draw_visits_every_folder_before_any_twice(self):
        series = [Path(f"/lib/Series/S01/e{i:02d}.mkv") for i in range(30)]
        films = [Path(f"/lib/Film {i}/film.mkv") for i in range(3)]
        order = sample.draw_order(series + films, random.Random(4))
        self.assertEqual(sorted(order), sorted(series + films))
        self.assertEqual({p.parent for p in order[:4]}, {p.parent for p in series[:1] + films})

    def test_same_seed_same_draw(self):
        files = [Path(f"/lib/{i}/f.mkv") for i in range(20)]
        self.assertEqual(sample.draw_order(files, random.Random(5)),
                         sample.draw_order(files, random.Random(5)))

    def test_pick_fills_each_band_and_skips_what_run_would(self):
        infos = {
            Path("/lib/a/uhd.mkv"): make_info(path=Path("/lib/a/uhd.mkv"), width=3840,
                                              height=2160),
            Path("/lib/b/hd.mkv"): make_info(path=Path("/lib/b/hd.mkv")),
            Path("/lib/c/hd2.mkv"): make_info(path=Path("/lib/c/hd2.mkv")),
            Path("/lib/d/done.mkv"): make_info(path=Path("/lib/d/done.mkv"), codec="av1"),
            Path("/lib/e/short.mkv"): make_info(path=Path("/lib/e/short.mkv"), duration=100.0),
        }
        with unittest.mock.patch.object(sample.probe, "probe",
                                        side_effect=lambda path, chain: infos[path]):
            picks = sample.pick(list(infos), make_chain(), config.Config(), per_band=2,
                                bands=["4K", "1080p"], length=240, skip_start=600,
                                skip_end=600, rng=random.Random(6))
        self.assertEqual([p.band for p in picks], ["4K", "1080p", "1080p"])
        self.assertEqual({p.id for p in picks}, {"4K-1", "1080p-1", "1080p-2"})
        self.assertNotIn("/lib/d/done.mkv", {p.source for p in picks})



class TestEarlySavingCheck(unittest.TestCase):
    """Stopping an encode that is heading for a result too large to keep."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _av1an_temp(self, scenes: list[tuple[int, int]], done: dict[int, int]) -> Path:
        """An Av1an temp directory: split_scenes, and {chunk index: bytes} finished."""
        temp = self.root / "av1an"
        temp.mkdir(exist_ok=True)
        (temp / "scenes.json").write_text(json.dumps({
            "frames": scenes[-1][1],
            "split_scenes": [{"start_frame": a, "end_frame": b} for a, b in scenes],
        }))
        (temp / "done.json").write_text(json.dumps({
            "frames": scenes[-1][1],
            "done": {f"{i:05d}": {"frames": scenes[i][1] - scenes[i][0], "size_bytes": size}
                     for i, size in done.items()},
        }))
        return temp

    def test_chunks_are_placed_by_the_scene_list(self):
        temp = self._av1an_temp([(0, 100), (100, 250), (250, 400)], {2: 900, 0: 300})
        frames, chunks = watch.finished_chunks(temp)
        self.assertEqual(frames, 400)
        self.assertEqual(sorted((c.start, c.end, c.size) for c in chunks),
                         [(0, 100, 300), (250, 400, 900)])

    def test_nothing_is_finished_before_av1an_writes_its_files(self):
        self.assertEqual(watch.finished_chunks(self.root), (0, []))
        temp = self._av1an_temp([(0, 100)], {0: 1})
        (temp / "done.json").write_text('{"frames": 100, "do')   # caught mid-write
        self.assertEqual(watch.finished_chunks(temp), (0, []))

    def test_packets_are_put_in_display_order(self):
        self.assertEqual(watch.frame_sizes(["0,10\n", "3000,40\n", "1000,20\n", "2000,30\n"]),
                         [10, 20, 30, 40])
        # Missing timestamps, as in AVI: decode order is the best there is.
        self.assertEqual(watch.frame_sizes(["N/A,10\n", "0,40\n", "N/A,20\n"]), [10, 40, 20])

    def test_projection_compares_the_same_frames(self):
        # The source spends 10 bytes a frame, but 50 in frames 100-199. The encode
        # spent 2500 bytes on those frames: half the source, not 2.5 times its average.
        sizes = [10] * 1000
        sizes[100:200] = [50] * 100
        info = make_info(size_bytes=sum(sizes) + 5000, duration=40.0)
        projection = present(watch.project(info, config.Config(), 1000,
                                           [watch.Chunk(100, 200, 2500)], sizes))
        self.assertAlmostEqual(projection.ratio, 0.5)
        self.assertEqual(projection.output_bytes, sum(sizes) // 2 + 5000)
        self.assertAlmostEqual(projection.done_percent, 10.0)

    def test_projection_counts_audio_re_encoded_to_opus(self):
        truehd = Stream(index=1, codec="truehd", codec_type="audio", channels=2,
                        bitrate=1_128_000)
        cfg = config.Config()
        info = make_info(size_bytes=10_000_000, duration=8.0, audio=[truehd])
        saved = mux.audio_saving_bytes(info, cfg)
        self.assertEqual(saved, (1_128_000 - 2 * cfg.audio.opus_kbps_per_channel * 1000))
        with_audio = present(watch.project(info, cfg, 100, [watch.Chunk(0, 100, 1000)],
                                           [10] * 100))
        without = present(watch.project(replace(info, audio=[]), cfg, 100,
                                        [watch.Chunk(0, 100, 1000)], [10] * 100))
        self.assertEqual(without.output_bytes - with_audio.output_bytes, saved)

    def _watch(self, sizes: list[int], temp: Path, min_mib: float = 0.0) -> watch.SavingWatch:
        cfg = config.Config()
        cfg.output.min_saving_mib = min_mib
        cfg.output.min_saving_percent = 10.0
        cfg.output.check_saving_after_percent = 10.0
        info = make_info(size_bytes=sum(sizes), duration=40.0)
        fake = unittest.mock.Mock(sizes=sizes)
        with unittest.mock.patch.object(watch, "SourceSizes", return_value=fake):
            return watch.SavingWatch(info, cfg, make_chain(), temp)

    def test_an_encode_heading_for_a_larger_file_is_stopped(self):
        scenes = [(i * 100, i * 100 + 100) for i in range(10)]
        # 5% of the frames is too early to judge, however bad it looks.
        early = self._watch([10] * 1000, self._av1an_temp(
            [(0, 50), *[(i * 100 + 50, i * 100 + 150) for i in range(9)], (950, 1000)],
            {0: 5000}))
        self.assertIsNone(early())
        check = self._watch([10] * 1000, self._av1an_temp(scenes, {0: 2000, 1: 2000}))
        self.assertRegex(present(check()), r"stopped at 20% of the frames: heading for "
                                           r"100\.0% larger, below the 10% threshold")

    def test_an_encode_saving_too_few_bytes_is_stopped(self):
        # 70% smaller clears the percentage, but on a 10 KB file that is nothing.
        scenes = [(i * 100, i * 100 + 100) for i in range(10)]
        check = self._watch([10] * 1000, self._av1an_temp(scenes, {0: 300, 1: 300}),
                            min_mib=1.0)
        self.assertRegex(present(check()), r"stopped at 20% of the frames: heading for "
                                           r"6\.8 KiB smaller, below the 1 MiB threshold")

    def test_an_encode_heading_for_a_good_saving_carries_on(self):
        scenes = [(i * 100, i * 100 + 100) for i in range(10)]
        check = self._watch([10] * 1000, self._av1an_temp(scenes, {0: 300, 1: 300}))
        self.assertIsNone(check())

    def test_a_cover_image_is_not_mistaken_for_the_video(self):
        scenes = [(i * 100, i * 100 + 100) for i in range(10)]
        check = self._watch([10], self._av1an_temp(scenes, {0: 5000, 1: 5000}))
        with self.assertLogs(util.log, "WARNING"):
            self.assertIsNone(check())

    def test_the_setting_must_be_a_percentage(self):
        cfg = config.Config()
        cfg.output.check_saving_after_percent = 150
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestProgressLog(unittest.TestCase):
    """With no terminal (Docker, systemd) Av1an draws no bar, and the log said
    nothing between "encoding" and "done": for a 4K film, days."""

    SCENES: ClassVar[list[tuple[int, int]]] = [(i * 1000, i * 1000 + 1000)
                                               for i in range(10)]   # 10,000 frames

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.now = 0.0

    _av1an_temp = TestEarlySavingCheck._av1an_temp

    def _log(self, done: dict[int, int], saving: Any = None) -> watch.ProgressLog:
        temp = self._av1an_temp(self.SCENES, done)
        return watch.ProgressLog(temp, saving=saving, clock=lambda: self.now)

    def _finish(self, *chunks: int) -> None:
        self._av1an_temp(self.SCENES, dict.fromkeys(chunks, 1))

    def test_nothing_is_said_within_the_hour(self):
        progress = self._log({})
        self._finish(0, 1)
        self.now = 3599.0
        with self.assertNoLogs(util.log, "INFO"):
            self.assertIsNone(progress())

    def test_each_hour_says_how_far_how_fast_and_how_long_to_go(self):
        progress = self._log({0: 1})   # finished by an earlier run: not this run's speed
        self._finish(0, 1, 2, 3)
        self.now = 3600.0
        with self.assertLogs(util.log, "INFO") as logged:
            self.assertIsNone(progress(), "it never stops the encode")
        line = logged.output[0]
        self.assertIn("40% encoded (4,000 of 10,000 frames)", line)
        self.assertIn("0.83 fps", line)          # 3,000 frames in 3,600 s
        self.assertIn("2h00m00s left", line)     # 6,000 frames to go at that rate
        self.now = 3700.0
        with self.assertNoLogs(util.log, "INFO"):
            progress()

    def test_an_hour_without_a_finished_chunk_says_so(self):
        progress = self._log({})
        self.now = 3600.0
        with self.assertLogs(util.log, "INFO") as logged:
            progress()
        self.assertIn("no chunk finished yet", logged.output[0])

    def test_the_projected_size_comes_from_the_saving_check(self):
        projection = watch.Projection(frames_done=4000, frames=10_000, ratio=0.2,
                                      output_bytes=3 * 2**30, saving_percent=85.3)
        progress = self._log({}, saving=unittest.mock.Mock(**{"projection.return_value":
                                                             projection}))
        self._finish(0)
        self.now = 3600.0
        with self.assertLogs(util.log, "INFO") as logged:
            progress()
        self.assertIn("projected 3.0 GiB, 85.3% smaller", logged.output[0])

    def test_it_runs_beside_the_saving_check_and_never_stops_the_encode(self):
        asked: list[str] = []
        check = present(watch.checks(lambda: asked.append("progress"),
                                     lambda: asked.append("saving") or "too big"))
        self.assertEqual(check(), "too big")
        self.assertEqual(asked, ["progress", "saving"])
        self.assertIsNone(watch.checks(None, None))

    def test_it_is_wanted_only_where_there_is_no_progress_bar(self):
        tty = unittest.mock.Mock(**{"isatty.return_value": True})
        pipe = unittest.mock.Mock(**{"isatty.return_value": False})
        self.assertFalse(watch.ProgressLog.wanted(tty))
        self.assertTrue(watch.ProgressLog.wanted(pipe))


class TestWatchedCommand(unittest.TestCase):
    """Stopping a command must take its children with it."""

    def test_a_stop_ends_the_command_and_its_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child"
            calls = []

            def check():
                calls.append(1)
                return "enough" if pidfile.is_file() and pidfile.read_text().strip() else None

            started = time.monotonic()
            reason = util.run_watched(
                ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], check, every=0.05)
            self.assertEqual(reason, "enough")
            self.assertLess(time.monotonic() - started, 10)
            child = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(child), "the command's own child must be stopped too")

    def test_a_command_that_ends_by_itself(self):
        self.assertIsNone(util.run_watched(["true"], lambda: "never asked", every=5))
        with self.assertRaises(CommandError):
            util.run_watched(["false"], lambda: None, every=5)


class TestMeasurementProgress(unittest.TestCase):
    """A long measurement pass says what it is doing and how far it has got."""

    def test_progress_text(self):
        self.assertEqual(util._progress(250, 1000, 60),
                         "250 of 1,000 frames (25%), 1m00s so far, about 3m00s left")
        self.assertEqual(util._progress(0, 1000, 5), "0 of 1,000 frames (0%), 5s so far")
        self.assertEqual(util._progress(1200, 1000, 90),
                         "1,200 of 1,000 frames (100%), 1m30s so far")
        self.assertEqual(util._progress(1200, 0, 90), "1,200 frames, 1m30s so far")
        self.assertEqual(util._progress(3, None, 90), "still running, 1m30s so far")

    def test_reports_frames_read_from_ffmpeg_progress_blocks(self):
        script = ("printf 'frame=10\\nprogress=continue\\n'; sleep 0.2; "
                  "printf 'frame=40\\nprogress=continue\\n'; sleep 0.2; "
                  "printf 'frame=80\\nprogress=end\\n'; echo oops >&2")
        with self.assertLogs("autotranscode", "INFO") as logs:
            util.run_progress(["sh", "-c", script], "measuring", frames=80, every=0.1)
        text = "\n".join(logs.output)
        self.assertIn("measuring...", text)
        self.assertIn("40 of 80 frames (50%)", text)
        self.assertIn("measuring: done in", text)
        self.assertNotIn("80 of 80", text, "the final block is the done line's job")

    def test_failure_carries_stderr(self):
        with self.assertRaises(CommandError) as caught, self.assertLogs("autotranscode", "INFO"):
            util.run_progress(["sh", "-c", "echo broken >&2; exit 3"], "measuring")
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn("broken", caught.exception.stderr)

    def test_every_pass_asks_ffmpeg_for_progress(self):
        argv = verify.measure_argv(make_chain())
        self.assertIn("-progress", argv)
        self.assertIn("-nostats", argv)


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie waiting for a new parent to reap it."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


class TestNoSavingIsRemembered(unittest.TestCase):
    """A file that came out too large is not encoded again for nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.source = self.root / "Film (1975).mkv"
        self.source.write_bytes(b"x" * 4096)
        self.cfg = config.Config()
        self.ledger = Ledger(self.root / "state.json")
        self.tries = 0

        def too_large(*_args, **_kwargs):
            self.tries += 1
            raise pipeline.NoSaving("stopped at 10% of the frames",
                                    pipeline.settings_key(self.cfg, make_chain()))

        original = pipeline.process_one
        pipeline.process_one = too_large
        self.addCleanup(setattr, pipeline, "process_one", original)

    def _run(self) -> None:
        pipeline._process_all([self.source], make_chain(), self.cfg, self.ledger,
                              pipeline.StopRequest())

    def test_not_tried_again_with_the_same_settings(self):
        self._run()
        self._run()
        self.assertEqual(self.tries, 1)
        record = present(self.ledger.get(self.source))
        self.assertEqual(record.status, "skipped")
        self.assertEqual(Ledger(self.root / "state.json").standing_skip(self.source), record)

    def test_tried_again_once_the_settings_change(self):
        self._run()
        self.cfg.encode.crf += 6
        self._run()
        self.assertEqual(self.tries, 2)

    def test_a_skip_recorded_without_settings_does_not_stand(self):
        self.ledger.record(self.source, Record(source=str(self.source), status="skipped",
                                               reason="already av1"))
        self.assertIsNone(self.ledger.standing_skip(self.source))


class TestChunkMethod(unittest.TestCase):
    """Choosing how Av1an cuts the film, and keeping its index out of the library."""

    def _argv(self, cfg: config.Config, chain: Toolchain) -> list[str]:
        return encode.build_command(make_info(), Path("/o.mkv"), chain, cfg, Path("/t"))

    def test_auto_takes_lsmash_when_its_plugin_loads(self):
        cfg = config.Config()
        with_plugin = make_chain(vspipe="/v/vspipe", vapoursynth_plugins={"lsmas", "std"})
        self.assertEqual(encode.chunk_method(cfg, with_plugin), "lsmash")
        self.assertEqual(encode.chunk_method(cfg, make_chain()), "hybrid")
        self.assertEqual(encode.chunk_method(cfg, make_chain(vspipe="/v/vspipe")), "hybrid")

    def test_a_vapoursynth_index_goes_to_the_temp_directory(self):
        cfg = config.Config()
        argv = self._argv(cfg, make_chain(vspipe="/v/vspipe", vapoursynth_plugins={"lsmas"}))
        self.assertEqual(argv[argv.index("-m") + 1], "lsmash")
        self.assertEqual(argv[argv.index("--cache-mode") + 1], "temp")
        self.assertNotIn("--cache-mode", self._argv(cfg, make_chain()))

    def test_scene_detection_is_downscaled_unless_turned_off(self):
        cfg = config.Config()
        argv = self._argv(cfg, make_chain())
        self.assertEqual(argv[argv.index("--sc-downscale-height") + 1], "1080")
        cfg.encode.sc_downscale_height = 0
        self.assertNotIn("--sc-downscale-height", self._argv(cfg, make_chain()))

    def test_an_explicit_lsmash_without_the_plugin_is_refused(self):
        def complaint(method: str, chain: Toolchain) -> bool:
            cfg = config.Config()
            cfg.encode.chunk_method = method
            try:
                tools.require(chain, cfg)
            except tools.ToolError as exc:
                return "chunk_method" in str(exc)
            return False

        without = make_chain(vspipe="/v/vspipe", vapoursynth_plugins={"std"})
        self.assertTrue(complaint("lsmash", without))
        self.assertTrue(complaint("lsmash", make_chain()))
        self.assertFalse(complaint("lsmash", make_chain(vspipe="/v/vspipe",
                                                        vapoursynth_plugins={"lsmas"})))
        self.assertFalse(complaint("auto", without))   # falls back to hybrid instead

    def test_unknown_methods_are_rejected(self):
        cfg = config.Config()
        cfg.encode.chunk_method = "lsmas"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_vspipe_s_folder_reaches_av1an(self):
        chain = make_chain(vspipe="/opt/vs/bin/vspipe")
        self.assertIn("/opt/vs/bin", chain.bin_dirs)


class TestSourceTiming(unittest.TestCase):
    """A phone clip at a variable frame rate must keep its length, and with it
    its sync with the audio, and its full-range picture must be signalled so."""

    def test_full_range_frames_win_over_a_limited_range_tag(self):
        self.assertEqual(probe._color_range({"color_range": "tv"}, "yuvj420p"), "pc")
        self.assertEqual(probe._color_range({"color_range": "tv"}, "yuv420p"), "tv")
        self.assertEqual(probe._color_range({}, "yuv420p10le"), "")

    def test_full_range_is_signalled_to_the_encoder(self):
        info = make_info(pix_fmt="yuvj420p", color_range="pc")
        params = encode.svt_params(info, config.Config(), make_chain())
        self.assertEqual(params[params.index("--color-range") + 1], "1")

    def test_a_track_s_duration_comes_from_mp4_or_the_matroska_tag(self):
        self.assertEqual(probe._stream_duration({"duration": "124.070967"}), 124.070967)
        self.assertAlmostEqual(
            probe._stream_duration({"tags": {"DURATION": "01:01:51.649000000"}}), 3711.649)
        self.assertAlmostEqual(
            probe._stream_duration({"tags": {"DURATION-eng": "00:00:02.5"}}), 2.5)
        self.assertEqual(probe._stream_duration({"duration": "N/A"}), 0.0)
        self.assertEqual(probe._stream_duration({}), 0.0)

    def test_the_muxer_s_duration_beats_a_copied_statistics_tag(self):
        # As ffprobe showed a 61 s excerpt cut with -c copy from a 59-minute film.
        tags = {"BPS-eng": "4937344", "DURATION-eng": "00:59:00.000000000",
                "NUMBER_OF_FRAMES-eng": "177000", "DURATION": "00:01:00.980000000"}
        self.assertAlmostEqual(probe._stream_duration({"tags": tags}), 60.98)

    def test_only_a_real_difference_in_length_counts_as_drift(self):
        self.assertTrue(encode.drifted(124.071, 111.649))
        self.assertFalse(encode.drifted(7200.000, 7200.042))   # a frame of rounding
        self.assertFalse(encode.drifted(0.0, 111.6))           # source length unknown

    def test_timestamps_are_written_for_mkvmerge_in_milliseconds(self):
        self.assertEqual(encode.timestamps_v2([0.0, 0.0185, 0.037]),
                         "# timestamp format v2\n0.000\n18.500\n37.000\n")

    def test_a_constant_rate_encode_is_left_alone(self):
        info = make_info(video_duration=7200.0)
        with unittest.mock.patch.object(encode, "_duration", return_value=7200.04), \
                unittest.mock.patch.object(encode, "frame_times") as times:
            encode.keep_source_timing(info, Path("/w/encoded.mkv"), make_chain(), Path("/w"))
        times.assert_not_called()

    def test_frames_that_cannot_take_the_source_s_times_fail_the_file(self):
        info = make_info(video_duration=124.07)
        with unittest.mock.patch.object(encode, "_duration", return_value=111.65), \
                unittest.mock.patch.object(encode, "frame_times", return_value=[0.0] * 6698), \
                unittest.mock.patch.object(encode, "_frame_count", return_value=6699), \
                self.assertRaises(ValueError):
            encode.keep_source_timing(info, Path("/w/encoded.mkv"), make_chain(), Path("/w"))


class TestWorkers(unittest.TestCase):
    """How many encodes run at once: as many as the cores keep busy and the free
    memory holds."""

    GB = 1_000_000_000
    UHD: ClassVar[dict[str, int]] = dict(width=3840, height=2160)   # 8.3 megapixels

    def test_a_configured_count_wins(self):
        cfg = config.Config()
        cfg.machine.workers = 3
        self.assertEqual(encode.workers(make_info(), cfg, make_chain(), memory=1, cores=1), 3)

    def test_free_memory_limits_4k(self):
        # hybrid: 1.3 GB per megapixel, so 10.8 GB per 4K worker.
        cfg, chain, info = config.Config(), make_chain(), make_info(**self.UHD)
        self.assertEqual(encode.workers(info, cfg, chain, memory=18 * self.GB, cores=32), 1)
        self.assertEqual(encode.workers(info, cfg, chain, memory=22 * self.GB, cores=32), 2)

    def test_lsmash_needs_more_memory_per_worker(self):
        # 1.5 GB per megapixel: two 4K workers need 24.9 GB.
        cfg, info = config.Config(), make_info(**self.UHD)
        lsmash = make_chain(vspipe="/v/vspipe", vapoursynth_plugins={"lsmas"})
        self.assertEqual(encode.workers(info, cfg, lsmash, memory=22 * self.GB, cores=32), 1)

    def test_cores_limit_when_memory_is_plentiful(self):
        cfg, chain = config.Config(), make_chain()
        self.assertEqual(encode.workers(make_info(), cfg, chain, memory=64 * self.GB, cores=8), 1)
        self.assertEqual(encode.workers(make_info(), cfg, chain, memory=64 * self.GB, cores=24), 4)

    def test_never_fewer_than_one(self):
        cfg, chain = config.Config(), make_chain()
        info = make_info(**self.UHD)
        self.assertEqual(encode.workers(info, cfg, chain, memory=self.GB, cores=2), 1)

    def test_unknown_memory_leaves_it_to_the_cores(self):
        cfg, chain = config.Config(), make_chain()
        info = make_info(**self.UHD)
        self.assertEqual(encode.workers(info, cfg, chain, memory=0, cores=24), 4)

    def test_the_count_reaches_av1an(self):
        cfg = config.Config()
        with unittest.mock.patch.object(encode, "available_memory", return_value=0), \
                unittest.mock.patch.object(os, "sched_getaffinity", return_value=set(range(12))):
            argv = encode.build_command(make_info(), Path("/o.mkv"), make_chain(), cfg, Path("/t"))
        self.assertEqual(argv[argv.index("-w") + 1], "2")

    def test_available_memory_reads_memavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp) / "meminfo"
            meminfo.write_text("MemTotal:  32000000 kB\nMemAvailable:  18183912 kB\n")
            self.assertEqual(util.available_memory(meminfo), 18183912 * 1024)
            self.assertEqual(util.available_memory(Path(tmp) / "missing"), 0)


class TestChildPath(unittest.TestCase):
    """VapourSynth's embedded Python must not find our virtualenv's python3."""

    def test_our_own_virtualenv_is_left_off_a_child_s_path(self):
        with unittest.mock.patch.object(sys, "prefix", "/proj/.venv"), \
                unittest.mock.patch.object(sys, "base_prefix", "/usr"), \
                unittest.mock.patch.dict(os.environ, {"PATH": "/proj/.venv/bin:/usr/bin:/bin"}):
            path = util.child_env(["/extra"])["PATH"].split(os.pathsep)
        self.assertEqual(path, ["/extra", "/usr/bin", "/bin"])

    def test_outside_a_virtualenv_nothing_is_removed(self):
        with unittest.mock.patch.object(sys, "prefix", "/usr"), \
                unittest.mock.patch.object(sys, "base_prefix", "/usr"), \
                unittest.mock.patch.dict(os.environ, {"PATH": "/a/bin:/usr/bin"}):
            self.assertEqual(util.child_env()["PATH"], "/a/bin:/usr/bin")


class TestInsideAContainer(unittest.TestCase):
    """What a container hides or withholds must not be mistaken for the hardware."""

    def test_profile_from_the_device_tree_the_rockchip_compose_file_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            hidden, mounted = Path(tmp) / "proc", Path(tmp) / "host"
            mounted.write_bytes(b"radxa,rock-5b\0rockchip,rk3588\0")
            with unittest.mock.patch.object(config, "DEVICE_TREE_COMPATIBLE", (hidden, mounted)):
                self.assertEqual(config.detect_profile(), "rk3588")
            with unittest.mock.patch.object(config, "DEVICE_TREE_COMPATIBLE", (hidden,)):
                self.assertEqual(config.detect_profile(), "generic")

    def test_rkmpp_decoders_need_the_device_not_just_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "mpp_service"
            with unittest.mock.patch.object(tools, "MPP_DEVICE", str(device)):
                self.assertFalse(tools._rockchip_video_engine())
                device.touch()
                self.assertTrue(tools._rockchip_video_engine())

    def _cpus(self, root: Path, capacities: list[int]) -> Path:
        for number, capacity in enumerate(capacities):
            (root / f"cpu{number}").mkdir()
            (root / f"cpu{number}" / "cpu_capacity").write_text(f"{capacity}\n")
        return root

    def test_big_cores_are_only_those_this_process_may_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpus = self._cpus(Path(tmp), [414] * 4 + [1024] * 4)
            with unittest.mock.patch.object(os, "sched_getaffinity", return_value=set(range(8))):
                self.assertEqual(tools._big_cores(cpus), "4,5,6,7")
            with unittest.mock.patch.object(os, "sched_getaffinity", return_value={0, 1, 6, 7}):
                self.assertEqual(tools._big_cores(cpus), "6,7")
            # Limited to one kind of core: nothing to choose between.
            with unittest.mock.patch.object(os, "sched_getaffinity", return_value={4, 5}):
                self.assertEqual(tools._big_cores(cpus), "")

    def test_cores_close_in_speed_are_all_used(self):
        # A Core Ultra 7 258V: two favoured P-cores read a little higher than the
        # other two, and the E-cores have two-thirds of their capacity.
        with tempfile.TemporaryDirectory() as tmp:
            cpus = self._cpus(Path(tmp), [1005, 1024, 1005, 1024, 676, 676, 676, 676])
            with unittest.mock.patch.object(os, "sched_getaffinity", return_value=set(range(8))):
                self.assertEqual(tools._big_cores(cpus), "")

    def test_big_cores_need_not_be_exactly_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpus = self._cpus(Path(tmp), [414] * 4 + [1024, 1024, 981, 981])
            with unittest.mock.patch.object(os, "sched_getaffinity", return_value=set(range(8))):
                self.assertEqual(tools._big_cores(cpus), "4,5,6,7")


class TestShortfall(unittest.TestCase):
    """A result has to clear both minimums: the share and the absolute amount."""

    def setUp(self):
        self.cfg = config.Config()
        self.cfg.output.min_saving_percent = 10.0
        self.cfg.output.min_saving_mib = 100.0

    def test_default_is_100_mib(self):
        self.assertEqual(config.Config().output.min_saving_mib, 100.0)

    def test_a_large_saving_on_a_large_file_passes(self):
        self.assertIsNone(watch.shortfall(self.cfg, 2000 * MIB, 1000 * MIB))

    def test_a_large_share_of_a_small_file_is_not_enough(self):
        reason = watch.shortfall(self.cfg, 150 * MIB, 75 * MIB)
        self.assertEqual(reason, "75.0 MiB smaller, below the 100 MiB threshold")

    def test_a_small_share_of_a_large_file_is_not_enough(self):
        reason = watch.shortfall(self.cfg, 20000 * MIB, 19000 * MIB)
        self.assertEqual(reason, "5.0% smaller, below the 10% threshold")

    def test_zero_turns_the_absolute_check_off(self):
        self.cfg.output.min_saving_mib = 0.0
        self.assertIsNone(watch.shortfall(self.cfg, 150 * MIB, 75 * MIB))

    def test_it_decides_whether_a_file_is_retried(self):
        before = pipeline.settings_key(self.cfg, make_chain())
        self.cfg.output.min_saving_mib = 50.0
        self.assertNotEqual(before, pipeline.settings_key(self.cfg, make_chain()))


class TestEstimatesSteerTheRun(unittest.TestCase):
    """`estimate` leaves its projections in the ledger; `run` orders by them and
    skips the files heading below the minimum savings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config()
        self.chain = make_chain()
        self.ledger = Ledger(self.root / "state.json")
        self.key = pipeline.size_key(self.cfg, self.chain)
        self.files = []
        for name in ("a", "b", "c", "d"):
            path = self.root / f"{name}.mkv"
            path.write_bytes(b"x" * 64)
            self.files.append(path)

    def _guess(self, path: Path, source_mib: float, projected_mib: float, hours: float,
               settings: str | None = None) -> None:
        self.ledger.record_guess(path, Guess(
            source=str(path), source_bytes=int(source_mib * MIB),
            projected_bytes=int(projected_mib * MIB), encode_seconds=hours * 3600,
            crf=29.0, settings=settings or self.key))

    def test_guesses_survive_a_reload(self):
        self._guess(self.files[0], 2000, 500, 2)
        again = Ledger(self.root / "state.json")
        guess = present(again.guess(self.files[0], self.key))
        self.assertEqual(guess.projected_bytes, 500 * MIB)
        self.assertAlmostEqual(guess.saved_per_hour, 750 * MIB)

    def test_a_guess_made_with_other_settings_is_ignored(self):
        self._guess(self.files[0], 2000, 500, 2, settings="something-else")
        self.assertIsNone(self.ledger.guess(self.files[0], self.key))

    def test_a_guess_is_forgotten_when_the_file_changes(self):
        self._guess(self.files[0], 2000, 500, 2)
        self.files[0].write_bytes(b"y" * 128)
        self.assertIsNone(self.ledger.guess(self.files[0], self.key))

    def test_the_thresholds_do_not_invalidate_an_estimate(self):
        before = (pipeline.size_key(self.cfg, self.chain),
                  pipeline.settings_key(self.cfg, self.chain))
        self.cfg.output.min_saving_mib = 500.0
        self.assertEqual(before[0], pipeline.size_key(self.cfg, self.chain))
        self.assertNotEqual(before[1], pipeline.settings_key(self.cfg, self.chain))
        self.cfg.encode.crf = 25
        self.assertNotEqual(before[0], pipeline.size_key(self.cfg, self.chain))

    def test_grain_sampling_does_not_invalidate_an_estimate(self):
        before = (pipeline.size_key(self.cfg, self.chain),
                  pipeline.settings_key(self.cfg, self.chain))
        self.cfg.grain.samples, self.cfg.grain.sample_seconds = 20, 5
        self.assertEqual(before, (pipeline.size_key(self.cfg, self.chain),
                                  pipeline.settings_key(self.cfg, self.chain)))

    def test_with_a_denoise_the_grain_sampling_decides_size(self):
        # The probe the samples feed then switches the denoise on and off.
        self.cfg.grain.denoise_before_encode = True
        before = pipeline.size_key(self.cfg, self.chain)
        self.cfg.grain.samples = 20
        self.assertNotEqual(before, pipeline.size_key(self.cfg, self.chain))

    def _digest_before_sampling_left(self, samples: int, seconds: int,
                                     savings: bool = False) -> str:
        # Exactly what size_key and settings_key wrote until then.
        grain = dataclasses.asdict(self.cfg.grain)
        grain.update(samples=samples, sample_seconds=seconds)
        encoding = dataclasses.asdict(self.cfg.encode)
        del encoding["deinterlace"]   # came later, and stays out of the digest while off
        settings = {"encode": encoding, "grain": grain,
                    "audio": dataclasses.asdict(self.cfg.audio),
                    "svt_av1": self.chain.svtav1_version}
        if savings:
            settings.update(min_saving_percent=self.cfg.output.min_saving_percent,
                            min_saving_mib=self.cfg.output.min_saving_mib)
        return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str)
                              .encode()).hexdigest()[:16]

    def test_estimates_from_before_the_sampling_left_still_count(self):
        a, b, c, _ = self.files
        self._guess(a, 2000, 1000, 1, settings=self._digest_before_sampling_left(4, 2))
        self._guess(b, 2000, 1000, 1, settings=self._digest_before_sampling_left(6, 3))
        self._guess(c, 2000, 1000, 1, settings=self._digest_before_sampling_left(8, 2))
        keys = pipeline.size_keys(self.cfg, self.chain)
        self.assertIsNotNone(self.ledger.guess(a, keys))   # the RK3588's 4 x 2 s
        self.assertIsNotNone(self.ledger.guess(b, keys))   # everyone else's 6 x 3 s
        self.assertIsNone(self.ledger.guess(c, keys))      # never shipped

    def test_a_no_saving_skip_from_before_the_sampling_left_still_counts(self):
        old = self._digest_before_sampling_left(4, 2, savings=True)
        self.assertIn(old, pipeline.settings_keys(self.cfg, self.chain))
        self.cfg.encode.crf = 25
        self.assertNotIn(old, pipeline.settings_keys(self.cfg, self.chain))

    def test_estimated_files_go_first_best_saving_per_hour_on_top(self):
        a, b, c, d = self.files
        self._guess(b, 2000, 1000, 10)    # 100 MiB/h
        self._guess(d, 2000, 1000, 1)     # 1000 MiB/h
        self._guess(a, 2000, 1000, 1, settings="stale")
        ordered, estimated = pipeline.by_estimate(self.files, self.ledger, self.cfg, self.chain)
        self.assertEqual(ordered, [d, b, a, c])
        self.assertEqual(estimated, 2)

    def test_films_an_earlier_run_started_go_before_everything_else(self):
        a, b, c, d = self.files
        self.cfg.paths.work_dir = str(self.root / "work")
        self._guess(d, 2000, 1000, 1)
        chunks = self.cfg.work_dir / pipeline.source_digest(c) / "av1an"
        chunks.mkdir(parents=True)
        (chunks / "chunks.json").write_text(json.dumps([{}] * 10))
        (chunks / "done.json").write_text(json.dumps({"frames": 100, "done": {"3": {}}}))
        parts = self.cfg.work_dir / pipeline.source_digest(b) / "scenes-0123.parts"
        parts.mkdir(parents=True)
        (parts / "0000000-0043200.json").write_text("{}")
        # Work for `a` with nothing in it yet is not a start.
        (self.cfg.work_dir / pipeline.source_digest(a) / "av1an").mkdir(parents=True)

        started = pipeline.under_way(self.files, self.cfg)
        self.assertEqual(started, {b, c})
        ordered, estimated = pipeline.by_estimate(self.files, self.ledger, self.cfg,
                                                  self.chain, started)
        self.assertEqual(ordered, [b, c, d, a])
        self.assertEqual(estimated, 1)

    def test_a_file_estimated_to_fall_short_is_skipped_before_encoding(self):
        a, b, c, d = self.files
        self._guess(a, 2000, 1950, 1)     # 2.5% smaller
        self._guess(b, 300, 250, 1)       # 50 MiB smaller
        self._guess(c, 2000, 500, 1)
        encoded = []

        def fake_process_one(source, *_args, **_kwargs):
            encoded.append(source)
            return Record(source=str(source), status="done")

        with unittest.mock.patch.object(pipeline, "process_one", fake_process_one):
            pipeline._process_all(self.files, self.chain, self.cfg, self.ledger,
                                  pipeline.StopRequest())
        self.assertEqual(encoded, [c, d])
        self.assertIsNone(self.ledger.get(a), "a guess is not a verdict: nothing is recorded")

    def test_the_count_carries_on_after_the_files_earlier_runs_settled(self):
        a, b, c, d = self.files
        self.ledger.record(b, Record(source=str(b), status="done", output="/out/b.mkv"))
        self.ledger.record(d, Record(source=str(d), status="skipped", reason="too big",
                                     settings=pipeline.settings_key(self.cfg, self.chain)))

        def fake_process_one(source, *_args, **_kwargs):
            return Record(source=str(source), status="done")

        with (unittest.mock.patch.object(pipeline, "process_one", fake_process_one),
              self.assertLogs("autotranscode", "INFO") as logs):
            pipeline._process_all(self.files, self.chain, self.cfg, self.ledger,
                                  pipeline.StopRequest())
        messages = [record.getMessage() for record in logs.records]
        self.assertIn("2 of them dealt with before (1 already transcoded, 1 tried but saved "
                      "too little): carrying on from 3", messages)
        self.assertEqual([m for m in messages if m.startswith("[")],
                         [f"[3/4] {a}", f"[4/4] {c}"])

    def test_a_run_with_nothing_left_says_so(self):
        for path in self.files:
            self.ledger.record(path, Record(source=str(path), status="done", output="/o"))
        with self.assertLogs("autotranscode", "INFO") as logs:
            pipeline._process_all(self.files, self.chain, self.cfg, self.ledger,
                                  pipeline.StopRequest())
        self.assertIn("4 of them dealt with before (4 already transcoded): nothing left to do",
                      [record.getMessage() for record in logs.records])

    def _run_counting(self) -> list[Path]:
        tried = []

        def fake_process_one(source, *_args, **_kwargs):
            tried.append(source)
            raise pipeline.SkipFile("already av1", pipeline.skip_key(self.cfg))

        with unittest.mock.patch.object(pipeline, "process_one", fake_process_one):
            pipeline._process_all(self.files, self.chain, self.cfg, self.ledger,
                                  pipeline.StopRequest())
        return tried

    def test_a_file_with_its_output_already_there_is_counted_without_a_probe(self):
        a, b, _, _ = self.files
        pipeline.output_for(b, self.cfg).write_bytes(b"encoded elsewhere")
        tried = self._run_counting()
        self.assertNotIn(b, tried)
        self.assertIn(a, tried)
        self.assertIsNone(self.ledger.get(b), "the output is the verdict: nothing is recorded")

    def test_a_skip_for_what_the_file_is_stands_until_the_skip_settings_change(self):
        self.assertEqual(len(self._run_counting()), 4)
        self.assertEqual(self._run_counting(), [])
        self.cfg.library.skip_codecs = ["av1", "vp9"]
        self.assertEqual(len(self._run_counting()), 4)

    def test_only_skips_for_what_the_file_is_carry_the_skip_settings(self):
        key = pipeline.skip_key(self.cfg)
        out = self.root / "out.mkv"
        for info, settings in ((make_info(codec="av1"), key),
                               (make_info(duration=30.0), key),
                               (make_info(pix_fmt="unknown"), ""),
                               (make_info(path=out), "")):
            with self.assertRaises(pipeline.SkipFile) as caught:
                pipeline.check_skip(info, out, self.cfg)
            self.assertEqual(caught.exception.settings, settings, caught.exception)
        out.write_bytes(b"x")
        with self.assertRaises(pipeline.SkipFile) as caught:
            pipeline.check_skip(make_info(), out, self.cfg)
        self.assertEqual(caught.exception.settings, "")

    def test_outputs_this_ledger_recorded_are_left_out_of_the_queue(self):
        a, b, c, _ = self.files
        output = pipeline.output_for(a, self.cfg)
        output.write_bytes(b"x")
        self.ledger.record(a, Record(source=str(a), status="done", output=str(output)))
        self.ledger.record(b, Record(source=str(b), status="failed", output=str(c)))
        found = pipeline.discover([self.root], self.cfg)
        self.assertIn(output, found)
        kept, left_out = pipeline.leave_out_outputs(found, self.ledger)
        self.assertEqual((kept, left_out), (self.files, 1))


class TestGrainStepResumes(unittest.TestCase):
    """A restart resumes Av1an's chunks; it must not redo the grain step before
    them unless a setting that step depends on has changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config()
        self.chain = make_chain()
        self.info = make_info()
        self.built = 0
        self.measured = 0

        def fake_build(_info, _chain, _cfg, _report, work):
            self.built += 1
            table = work / "grain.tbl"
            table.write_text(TABLE, encoding="utf-8")
            return table

        def fake_measure(*_args):
            self.measured += 1
            return GrainReport(38.0, [38.0], [1.0], grainy=True, denoise_filter="hqdn3d")

        for name, fake in (("build_table", fake_build), ("measure", fake_measure)):
            patcher = unittest.mock.patch.object(grain, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _step(self):
        return pipeline._grain_step(self.info, self.chain, self.cfg, self.work, dry_run=False)

    def test_a_restart_keeps_the_table(self):
        _, first = self._step()
        _, again = self._step()
        self.assertEqual((self.built, again), (1, first))

    def test_a_changed_grain_setting_rebuilds_it(self):
        self._step()
        self.cfg.grain.samples += 1
        self._step()
        self.assertEqual(self.built, 2)

    def test_settings_used_only_after_the_encode_do_not_rebuild_it(self):
        self._step()
        self.cfg.grain.calibrate = not self.cfg.grain.calibrate
        self.cfg.grain.keep_table = not self.cfg.grain.keep_table
        self._step()
        self.assertEqual(self.built, 1)

    def test_the_probe_report_is_kept_with_the_table(self):
        self.cfg.grain.estimator = "grav1synth"
        first, _ = self._step()
        again, _ = self._step()
        self.assertEqual((self.measured, again), (1, first))

    def test_a_missing_table_is_rebuilt(self):
        _, table = self._step()
        present(table).unlink()
        self._step()
        self.assertEqual(self.built, 2)

    def test_a_failed_table_is_tried_again(self):
        with unittest.mock.patch.object(grain, "build_table", lambda *_args: None):
            self.assertEqual(self._step(), (None, None))
        self._step()
        self.assertEqual(self.built, 1)

    def test_the_probe_alone_makes_no_table_and_is_kept(self):
        self.cfg.grain.denoise_before_encode = True
        first = pipeline._grain_step(self.info, self.chain, self.cfg, self.work,
                                     dry_run=False, with_table=False)
        again = pipeline._grain_step(self.info, self.chain, self.cfg, self.work,
                                     dry_run=False, with_table=False)
        self.assertEqual((self.built, self.measured), (0, 1))
        self.assertEqual(first, again)
        self.assertEqual(first[1], None)
        self.assertEqual(present(first[0]).denoise_filter, "hqdn3d")


class TestDenoiseBeforeSvtfg(unittest.TestCase):
    """grain.denoise_before_encode did nothing with the default estimator: the
    SvtFgEssential path skipped the probe, so there was never a filter to use,
    while `estimate` projected the denoised size."""

    class Stop(Exception):
        pass

    def _encode_argv(self, denoise: bool, report: GrainReport | None = None) -> list[str]:
        cfg = config.Config()
        cfg.grain.estimator = "svtfg"
        cfg.grain.denoise_before_encode = denoise
        report = report or GrainReport(38.0, [38.0], [1.0], grainy=True,
                                       denoise_filter="hqdn3d=1.4:1.0:6:6")
        seen: list[list[str]] = []

        def run_encode(argv, *_args, **_kwargs):
            seen.append(argv)
            raise self.Stop

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pipeline.mux, "rehearse"), \
                unittest.mock.patch.object(grain, "measure", return_value=report) as measure, \
                unittest.mock.patch.object(grain, "build_table") as build, \
                unittest.mock.patch.object(pipeline.resume, "check",
                                           side_effect=lambda argv, *a, **k: (argv, None)), \
                unittest.mock.patch.object(pipeline.scenes, "detect"), \
                unittest.mock.patch.object(pipeline.encode, "run_encode", run_encode), \
                self.assertRaises(self.Stop):
            pipeline._transcode(Path("/library/F.mkv"), make_info(), make_info(),
                                Path(tmp) / "F.av1.mkv", None, Path(tmp), make_chain(), cfg,
                                0.0, dry_run=False)
        self.assertEqual(build.call_count, 0, "SvtFgEssential makes its table after")
        self.assertEqual(measure.call_count, 1 if denoise else 0)
        return seen[0]

    def test_the_encode_is_denoised_when_asked(self):
        argv = self._encode_argv(denoise=True)
        self.assertIn("-vf hqdn3d=1.4:1.0:6:6", argv[argv.index("-f") + 1])

    def test_by_default_no_probe_runs_and_nothing_is_denoised(self):
        argv = self._encode_argv(denoise=False)
        self.assertNotIn("-f", argv)

    def test_a_source_the_probe_finds_clean_is_not_denoised(self):
        clean = GrainReport(48.0, [48.0], [1.0], grainy=False, denoise_filter="")
        argv = self._encode_argv(denoise=True, report=clean)
        self.assertNotIn("-f", argv)


class TestScenesResume(unittest.TestCase):
    """Scene detection takes hours on a 4K film. Av1an only resumes once it has
    written its chunk list, so the scene list is kept outside its reach."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.temp = self.work / "av1an"
        self.temp.mkdir()
        self.cfg = config.Config()
        self.chain = make_chain()
        self.info = make_info()

    LIST = json.dumps({"frames": 400, "scenes": [{"start_frame": 0, "end_frame": 400}],
                       "split_scenes": [{"start_frame": 0, "end_frame": 400}]})

    def _scenes(self) -> Path:
        return scenes.prepare(self.info, self.chain, self.cfg, self.work)

    def test_av1an_is_told_where_the_list_is(self):
        scenes = self._scenes()
        argv = encode.build_command(self.info, Path("/o.mkv"), self.chain, self.cfg,
                                    self.temp, scenes=scenes)
        self.assertEqual(argv[argv.index("--scenes") + 1], str(scenes))
        self.assertNotEqual(scenes.parent, self.temp)

    def test_a_list_from_an_earlier_run_is_kept(self):
        self._scenes().write_text(self.LIST)
        self.assertEqual(self._scenes().read_text(), self.LIST)

    def test_a_changed_scene_setting_detects_again(self):
        self._scenes().write_text(self.LIST)
        self.cfg.encode.min_scene_len += 1
        self.assertFalse(self._scenes().exists())
        self.assertEqual(list(self.work.glob("scenes-*.json")), [])

    def test_a_setting_scene_detection_does_not_use_keeps_it(self):
        self._scenes().write_text(self.LIST)
        self.cfg.encode.crf += 1
        self.cfg.encode.preset += 1
        self.assertTrue(self._scenes().is_file())

    def test_a_list_cut_short_is_dropped(self):
        self._scenes().write_text(self.LIST[:30])
        self.assertFalse(self._scenes().exists())

    def test_av1ans_own_list_from_before_is_kept(self):
        # A run stopped between scene detection and the chunk list, before this fix.
        (self.temp / "scenes.json").write_text(self.LIST)
        self.assertEqual(self._scenes().read_text(), self.LIST)
        self.assertFalse((self.temp / "scenes.json").exists())

    def test_a_resuming_encode_keeps_the_list_its_chunks_were_cut_by(self):
        self._scenes().write_text(self.LIST)
        self.cfg.encode.min_scene_len += 1
        for name in ("done.json", "chunks.json"):
            (self.temp / name).write_text("{}")
        self.assertEqual(self._scenes().read_text(), self.LIST)

    def test_the_watch_reads_the_list_where_av1an_keeps_it(self):
        scenes = self._scenes()
        scenes.write_text(self.LIST)
        (self.temp / "done.json").write_text(json.dumps(
            {"frames": 400, "done": {"00000": {"frames": 400, "size_bytes": 7}}}))
        self.assertEqual(watch.finished_chunks(self.temp, scenes),
                         (400, [watch.Chunk(0, 400, 7)]))

    def test_a_keyframe_scan_of_a_changed_source_is_dropped(self):
        kept = pipeline._keyframes_file(self.info, self.chain, self.cfg, self.work)
        kept.write_text("{}")
        self.assertEqual(pipeline._keyframes_file(self.info, self.chain, self.cfg, self.work),
                         kept)
        self.assertTrue(kept.is_file())
        changed = replace(self.info, size_bytes=self.info.size_bytes + 1)
        pipeline._keyframes_file(changed, self.chain, self.cfg, self.work)
        self.assertFalse(kept.exists())


class TestScenesInParts(unittest.TestCase):
    """Scene detection of a 4K film can outlast a day. Done a part at a time, a stop
    loses the part in flight and no more."""

    FRAMES = 30_000   # what the decoder finds; the probe says 28,800 at 24 fps

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config()
        self.cfg.encode.chunk_method = "lsmash"
        self.chain = make_chain()
        self.info = make_info(fps=24.0, frames=28_800, duration=1200.0)   # 4 parts of 7,200
        self.ran: list[tuple[int, int | None]] = []
        self.fail_at: int | None = None
        patcher = unittest.mock.patch.object(scenes, "run_watched", self._fake_av1an)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_av1an(self, argv: list[str], _check, **_kwargs) -> None:
        """Av1an --sc-only: one scene per 1,000 frames of what the script serves."""
        script = Path(argv[argv.index("-i") + 1]).read_text()
        if "with open(" in script:   # what the script does as Av1an evaluates it
            Path(eval(script.split("with open(", 1)[1].split(",", 1)[0])).write_text(
                str(self.FRAMES))
        bounds = script.rsplit("video[", 1)[1].split("]", 1)[0]
        start_text, end_text = bounds.split(":")
        start, end = int(start_text), int(end_text) if end_text else None
        if self.fail_at == start:
            raise KeyboardInterrupt
        self.ran.append((start, end))
        frames = (end if end is not None else self.FRAMES) - start
        cuts = [*range(0, frames, 1000), frames]
        listed = [{"start_frame": a, "end_frame": b, "zone_overrides": None}
                  for a, b in itertools.pairwise(cuts)]
        Path(argv[argv.index("--scenes") + 1]).write_text(json.dumps(
            {"frames": frames, "scenes": listed, "split_scenes": listed}))

    def _detect(self) -> Path:
        scene_list = scenes.prepare(self.info, self.chain, self.cfg, self.work)
        scenes.detect(self.info, self.chain, self.cfg, self.work, scene_list)
        return scene_list

    def test_the_parts_make_one_list_of_the_whole_film(self):
        merged = json.loads(self._detect().read_text())
        self.assertEqual(self.ran, [(0, 7200), (7200, 14400), (14400, 21600), (21600, None)])
        self.assertEqual(merged["frames"], self.FRAMES)
        ends = [scene["end_frame"] for scene in merged["split_scenes"]]
        starts = [scene["start_frame"] for scene in merged["split_scenes"]]
        self.assertEqual(starts, [0, *ends[:-1]], "the scenes must tile the film")
        self.assertEqual(ends[-1], self.FRAMES)
        self.assertIn(7200, starts, "each part starts a scene")
        self.assertEqual(list(self.work.glob("scenes-*.parts")), [], "parts are tidied away")

    def test_a_stop_keeps_the_parts_already_done(self):
        self.fail_at = 14400
        with self.assertRaises(KeyboardInterrupt):
            self._detect()
        self.fail_at = None
        self.ran.clear()
        self._detect()
        self.assertEqual(self.ran, [(14400, 21600), (21600, None)])

    def test_progress_is_logged_through_the_film(self):
        with self.assertLogs("autotranscode", level="INFO") as logs:
            self._detect()
        lines = [line for line in logs.output if "scenes: " in line]
        # 30,000 frames in parts of 7,200: 24%, 48% and 72% done before the next part.
        self.assertEqual([line.split("scenes: ")[1].split(",")[0] for line in lines],
                         ["24% (1 of 4 parts)", "48% (2 of 4 parts)", "72% (3 of 4 parts)"])
        self.assertTrue(all("so far" in line for line in lines))

    def test_parts_from_an_earlier_run_are_not_logged_as_progress(self):
        self.fail_at = 21600
        with self.assertRaises(KeyboardInterrupt):
            self._detect()
        self.fail_at = None
        with self.assertLogs("autotranscode", level="INFO") as logs:
            self._detect()
        self.assertFalse([line for line in logs.output if "scenes: " in line])
        self.assertTrue(any("3 done before" in line for line in logs.output))

    def test_the_bar_of_each_part_spans_the_whole_film(self):
        spans: list[progress.Span | None] = []
        original = self._fake_av1an

        def watching(argv, check, **kwargs):
            spans.append(progress._span)
            original(argv, check, **kwargs)
        with unittest.mock.patch.object(scenes, "run_watched", watching):
            self._detect()
        self.assertEqual([(span.start, span.total) for span in spans if span],
                         [(0, 28_800), (7200, 30_000), (14400, 30_000), (21600, 30_000)])
        self.assertIsNone(progress._span)

    def test_parts_named_by_number_are_kept(self):
        # As the first version named them: 0000.json, 0001.json...
        scene_list = scenes.prepare(self.info, self.chain, self.cfg, self.work)
        folder = scenes.parts_dir(scene_list)
        folder.mkdir()
        listed = [{"start_frame": 0, "end_frame": 7200, "zone_overrides": None}]
        for number in range(2):
            (folder / f"{number:04d}.json").write_text(json.dumps(
                {"frames": 7200, "scenes": listed, "split_scenes": listed}))
        scenes.detect(self.info, self.chain, self.cfg, self.work, scene_list)
        self.assertEqual(self.ran, [(14400, 21600), (21600, None)])
        self.assertEqual(json.loads(scene_list.read_text())["frames"], self.FRAMES)

    def test_the_list_once_made_is_not_detected_again(self):
        self._detect()
        self.ran.clear()
        self._detect()
        self.assertEqual(self.ran, [])

    def test_av1an_detects_by_itself_with_an_ffmpeg_chunk_method(self):
        self.cfg.encode.chunk_method = "hybrid"
        self.assertFalse(self._detect().exists())
        self.assertEqual(self.ran, [])

    def test_a_short_film_is_left_to_av1an(self):
        self.info = make_info(fps=24.0, frames=10_000, duration=416.0)
        self.assertFalse(self._detect().exists())
        self.assertEqual(self.ran, [])

    def test_a_failed_part_leaves_it_to_av1an(self):
        def failing(argv, _check, **_kwargs):
            raise util.CommandError(argv, 1, "")
        with unittest.mock.patch.object(scenes, "run_watched", failing):
            self.assertFalse(self._detect().exists())

    def test_the_parts_follow_the_decoders_frame_count_not_the_probes(self):
        # The probe says 50 minutes; the video holds 16. Planned on the probe, parts
        # 3 to 6 would start past the end.
        self.info = make_info(fps=24.0, frames=72_000, duration=3000.0)
        self.FRAMES = 23_000
        merged = json.loads(self._detect().read_text())
        self.assertEqual(self.ran, [(0, 7200), (7200, 14400), (14400, None)])
        self.assertEqual(merged["frames"], 23_000)

    def test_a_film_shorter_than_its_probe_said_is_left_to_av1an(self):
        self.info = make_info(fps=24.0, frames=72_000, duration=3000.0)
        self.FRAMES = 10_000
        self.assertFalse(self._detect().exists())
        self.assertEqual(list(self.work.glob("scenes-*.parts")), [])

    def test_a_part_of_the_wrong_length_is_not_merged(self):
        self.FRAMES = 30_000
        original = self._fake_av1an

        def short(argv, check, **kwargs):
            original(argv, check, **kwargs)
            out = Path(argv[argv.index("--scenes") + 1])
            data = json.loads(out.read_text())
            data["frames"] -= 1
            out.write_text(json.dumps(data))
        with unittest.mock.patch.object(scenes, "run_watched", short):
            self.assertFalse(self._detect().exists())

    def test_the_script_serves_the_part_from_the_index_the_encode_uses(self):
        script = scenes.source_script(Path("/lib/a 'b'.mkv"), "lsmash",
                                      Path("/w/av1an/split/cache.lwi"), 7200, 14400)
        self.assertIn("core.lsmas.LWLibavSource(\"/lib/a 'b'.mkv\", "
                      "cachefile='/w/av1an/split/cache.lwi')", script)
        self.assertTrue(script.endswith("video[7200:14400].set_output()\n"))
        compile(script, "part.vpy", "exec")
        last = scenes.source_script(Path("/a.mkv"), "bestsource", Path("/c"), 21600, None)
        self.assertIn("video[21600:].set_output()", last)
        compile(last, "part.vpy", "exec")

    def test_the_scene_command_matches_the_encodes_detection_settings(self):
        argv = encode.scene_command(self.chain, self.cfg, "lsmash", Path("/p/0000.vpy"),
                                    Path("/p/0000.new.json"), Path("/p/0000.av1an"))
        encode_argv = encode.build_command(self.info, Path("/o.mkv"), self.chain, self.cfg,
                                           Path("/t"))
        for flag in ("--sc-method", "--extra-split-sec", "--min-scene-len",
                     "--sc-downscale-height"):
            self.assertEqual(argv[argv.index(flag) + 1],
                             encode_argv[encode_argv.index(flag) + 1], flag)
        self.assertIn("--sc-only", argv)


class TestResumeFingerprint(unittest.TestCase):
    """Av1an resumes whatever chunks it finds; they must belong to this encode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.work = self.root / "work"
        self.temp = self.work / "av1an"
        self.source = self.root / "Film.mkv"
        self.source.write_bytes(b"x")
        self.cfg = config.Config()
        self.cfg.encode.chunk_method = "lsmash"
        self.chain = make_chain(vapoursynth_plugins={"lsmas"})
        self.info = make_info(path=self.source)
        self.asked: list[list[str]] = []
        self.answer = "skip"

    def _argv(self, denoise: str = "") -> list[str]:
        return encode.build_command(self.info, self.work / "encoded.mkv", self.chain, self.cfg,
                                    self.temp, denoise)

    def _ask(self, _name, differences, _done, _total) -> str:
        self.asked.append(differences)
        return self.answer

    def _check(self, argv: list[str] | None = None, info: MediaInfo | None = None):
        return resume.check(argv or self._argv(), info or self.info, info or self.info,
                            self.cfg, self.work, ask=self._ask)

    def _chunks(self, done: int = 2) -> None:
        """An encode stopped with `done` of 3 chunks finished."""
        (self.temp / "split").mkdir(parents=True, exist_ok=True)
        (self.temp / "split" / "cache.lwi").write_text("index")
        (self.temp / "split" / "loadscript.vpy").write_text("script")
        (self.temp / "encode").mkdir(exist_ok=True)
        (self.temp / "chunks.json").write_text(json.dumps([{}, {}, {}]))
        (self.temp / "done.json").write_text(json.dumps(
            {"frames": 300, "done": {f"{i:05d}": {} for i in range(done)}}))

    def _started(self, done: int = 2) -> None:
        self._check()          # the first run: a fingerprint and nothing to compare
        self._chunks(done)

    def test_an_unchanged_encode_resumes(self):
        self._started()
        argv = self._argv()
        self.assertEqual(self._check(argv), (argv, None))
        self.assertTrue((self.temp / "chunks.json").is_file())
        self.assertEqual(self.asked, [])

    def test_a_replaced_source_starts_again(self):
        self._started()
        self._check(info=replace(self.info, size_bytes=self.info.size_bytes + 1))
        self.assertFalse((self.temp / "chunks.json").exists())
        self.assertFalse((self.temp / "split" / "cache.lwi").exists(), "a stale index goes too")
        self.assertEqual(self.asked, [])

    def test_changed_settings_are_asked_about(self):
        self._started()
        self.cfg.encode.crf += 2
        self._check()
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(any(line.startswith("--crf: ") for line in self.asked[0]))

    def test_restart_keeps_only_the_index(self):
        self._started()
        self.cfg.encode.crf += 2
        self.answer = "restart"
        self.assertEqual(self._check()[1], None)
        self.assertFalse((self.temp / "chunks.json").exists())
        self.assertFalse((self.temp / "split" / "loadscript.vpy").exists())
        self.assertTrue((self.temp / "split" / "cache.lwi").is_file())
        self._chunks()
        self._check()
        self.assertEqual(len(self.asked), 1, "the new settings are now the encode's own")

    def test_continue_runs_with_the_flags_it_started_with(self):
        self._started()
        before = self._argv()
        self.cfg.encode.crf += 2
        self.answer = "continue"
        argv, postponed = self._check(self._argv(denoise="hqdn3d=2"))
        self.assertIsNone(postponed)
        self.assertEqual(resume.flags(argv), resume.flags(before))
        self.assertTrue((self.temp / "chunks.json").is_file())
        again, _ = self._check(self._argv(denoise="hqdn3d=2"))
        self.assertEqual(len(self.asked), 1, "a rerun does not ask again")
        self.assertEqual(resume.flags(again), resume.flags(before))

    def test_skip_keeps_the_chunks_for_later(self):
        self._started()
        self.cfg.encode.preset += 1
        _, postponed = self._check()
        self.assertTrue(postponed)
        self.assertTrue((self.temp / "chunks.json").is_file())

    def test_without_a_terminal_it_is_left_for_later(self):
        self._started()
        self.cfg.encode.crf += 2
        with unittest.mock.patch.object(sys.stdin, "isatty", return_value=False):
            _, postponed = resume.check(self._argv(), self.info, self.info, self.cfg, self.work)
        self.assertTrue(postponed)
        self.assertTrue((self.temp / "chunks.json").is_file())

    def test_a_configured_answer_is_not_asked_for(self):
        self._started()
        self.cfg.encode.crf += 2
        self.cfg.library.on_changed_settings = "restart"
        self._check()
        self.assertEqual(self.asked, [])
        self.assertFalse((self.temp / "chunks.json").exists())

    def test_changed_settings_before_any_chunk_finished_just_start_again(self):
        self._started(done=0)
        self.cfg.encode.crf += 2
        self._check()
        self.assertEqual(self.asked, [])
        self.assertFalse((self.temp / "chunks.json").exists())

    def test_a_lost_staged_copy_starts_again(self):
        staged = self.root / "staging" / "copy.mkv"
        staged.parent.mkdir()
        staged.write_bytes(b"x")
        resume.check(self._argv(), self.info, replace(self.info, path=staged), self.cfg,
                     self.work, ask=self._ask)
        self._chunks()
        staged.unlink()
        self._check()
        self.assertFalse((self.temp / "chunks.json").exists())

    def test_chunks_from_before_fingerprints_are_taken_as_they_are(self):
        self._chunks()
        self.cfg.encode.crf += 2
        self._check()
        self.assertTrue((self.temp / "chunks.json").is_file())
        self.assertEqual(self.asked, [])

    def test_changes_are_listed_flag_by_flag(self):
        before = {"-v": "--crf 29 --tf-strength -1 --tune 0", "-f": ""}
        after = {"-v": "--crf 31 --tf-strength 1 --tune 0", "-f": "-vf hqdn3d"}
        self.assertEqual(resume.changes(before, after),
                         ["--crf: 29 -> 31", "--tf-strength: -1 -> 1",
                          "filters: (none) -> -vf hqdn3d"])

    def test_flags_are_put_back_added_and_removed(self):
        argv = ["av1an", "-v", "--crf 31", "-f", "-vf x", "-w", "1"]
        self.assertEqual(resume.with_flags(argv, {"-v": "--crf 29", "-m": "lsmash"}),
                         ["av1an", "-v", "--crf 29", "-w", "1", "-m", "lsmash"])


class TestClean(unittest.TestCase):
    """`clean` removes what no run will use again, and never what one is using."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.library = self.root / "library"
        self.library.mkdir()
        self.cfg = config.Config()
        self.cfg.paths.work_dir = str(self.root / "work")
        self.cfg.paths.state_file = str(self.root / "work" / "state.json")
        self.chain = make_chain()
        self.ledger = Ledger(self.cfg.state_path)

    def _film(self, name: str = "Film.mkv", size: int = 100) -> Path:
        source = self.library / name
        source.write_bytes(b"x" * size)
        return source

    def _work(self, source: Path, *, chunks: int = 1000) -> Path:
        work = pipeline._work_dir(source, self.cfg)
        (work / "av1an").mkdir(exist_ok=True)
        (work / "av1an" / "chunks").write_bytes(b"c" * chunks)
        return work

    def _plan(self, *sources: Path, **flags) -> dict[Path, clean.Entry]:
        entries = clean.plan(self.cfg, self.chain, self.ledger, sources,
                             pipeline.library_roots([self.library]) if sources else [], **flags)
        return {entry.path: entry for entry in entries}

    def test_resumable_work_is_kept(self):
        work = self._work(self._film())
        entry = self._plan()[work]
        self.assertFalse(entry.remove)
        self.assertIn("resumable", entry.reason)
        self.assertTrue(self._plan(everything=True)[work].remove)

    def test_work_whose_source_is_gone_is_removed(self):
        source = self._film()
        work = self._work(source)
        source.unlink()
        self.assertTrue(self._plan()[work].remove)

    def test_a_missing_folder_is_taken_for_a_disk_not_mounted(self):
        source = self._film()
        work = self._work(source)
        shutil.rmtree(self.library)
        self.assertFalse(self._plan()[work].remove)
        self.assertTrue(self._plan(unknown=True)[work].remove)

    def test_finished_work_is_removed_but_a_kept_grain_table_stays(self):
        source = self._film()
        work = self._work(source)
        (work / "grain.tbl").write_text(TABLE)
        self.ledger.record(source, Record(source=str(source), status="done"))
        self.cfg.grain.keep_table = True
        entries = self._plan()
        self.assertTrue(entries[work].remove)
        clean.remove(entries.values())
        self.assertEqual([path.name for path in work.iterdir()], ["grain.tbl"])

    def test_a_running_encode_is_never_touched(self):
        source = self._film()
        work = self._work(source)
        (work / pipeline.RUNNING_FILE).write_text(str(os.getpid()))
        source.unlink()
        self.assertFalse(self._plan(everything=True)[work].remove)

    def test_a_dead_runs_mark_does_not_protect(self):
        source = self._film()
        work = self._work(source)
        (work / pipeline.RUNNING_FILE).write_text("999999999")
        source.unlink()
        self.assertTrue(self._plan()[work].remove)

    def test_work_from_before_it_named_its_source_is_matched_through_the_library(self):
        source = self._film()
        work = self._work(source)
        (work / pipeline.SOURCE_FILE).unlink()
        self.assertFalse(self._plan()[work].remove)
        self.assertIn("source unknown", self._plan()[work].reason)
        self.assertIn("resumable", self._plan(source)[work].reason)
        self.assertTrue(self._plan(unknown=True)[work].remove)

    def test_work_for_a_replaced_source_is_removed(self):
        source = self._film(size=100)
        work = self._work(source)
        (work / resume.FINGERPRINT).write_text(json.dumps({"source": [100, 60.0]}))
        self._film(size=200)
        self.assertTrue(self._plan()[work].remove)

    def test_work_run_would_skip_is_removed(self):
        source = self._film()
        work = self._work(source)
        self.ledger.record(source, Record(source=str(source), status="skipped", reason="too big",
                                          settings=pipeline.settings_key(self.cfg, self.chain)))
        self.assertTrue(self._plan()[work].remove)

    def test_work_whose_output_is_already_there_is_removed(self):
        source = self._film()
        work = self._work(source)
        pipeline.output_for(source, self.cfg).write_bytes(b"o")
        self.assertTrue(self._plan()[work].remove)

    def test_a_same_named_output_in_a_mirrored_directory_is_not_guessed(self):
        self.cfg.output.directory = str(self.root / "out")
        source = self._film()
        work = self._work(source)
        (self.root / "out").mkdir()
        (self.root / "out" / f"Film{self.cfg.output.suffix}.mkv").write_bytes(b"o")
        self.assertFalse(self._plan()[work].remove, "without the library, the root is unknown")
        self.assertTrue(self._plan(source)[work].remove)

    def test_partial_outputs_are_removed(self):
        source = self._film()
        part = pipeline.output_for(source, self.cfg)
        part = part.with_name(part.name + ".part")
        part.write_bytes(b"p")
        staged_out = pipeline.staged_output_path(source, self.cfg)
        staged_out.parent.mkdir(parents=True)
        staged_out.write_bytes(b"s")
        self.ledger.record(source, Record(source=str(source), status="failed"))
        entries = self._plan()
        self.assertTrue(entries[part].remove)
        self.assertTrue(entries[staged_out].remove)

    def test_a_staged_copy_follows_its_work(self):
        kept, gone = self._film("Kept.mkv"), self._film("Gone.mkv")
        self._work(kept)
        self._work(gone)
        copies = {}
        for source in (kept, gone):
            copies[source] = pipeline._staged_path(source, self.cfg, ".mkv")
            copies[source].parent.mkdir(parents=True, exist_ok=True)
            copies[source].write_bytes(b"x" * 100)
        gone.unlink()
        orphan = self.cfg.staging_dir / "0123456789abcdef.mkv"
        orphan.write_bytes(b"x")
        entries = self._plan()
        self.assertFalse(entries[copies[kept]].remove)
        self.assertTrue(entries[copies[gone]].remove)
        self.assertTrue(entries[orphan].remove)

    def test_old_scratch_goes_and_fresh_scratch_stays(self):
        old, fresh = self.cfg.work_dir / "estimate", self.cfg.work_dir / "verify"
        for folder in (old, fresh):
            folder.mkdir(parents=True)
            (folder / "log").write_text("x")
        long_ago = time.time() - 2 * clean.SCRATCH_QUIET_SECONDS
        for path in (old / "log", old):
            os.utime(path, (long_ago, long_ago))
        entries = self._plan()
        self.assertTrue(entries[old].remove)
        self.assertFalse(entries[fresh].remove)

    def test_other_things_in_the_work_directory_are_left_alone(self):
        for name in ("bin", "samples"):
            (self.cfg.work_dir / name).mkdir(parents=True)
        self.ledger.save()
        paths = set(self._plan())
        self.assertFalse(paths & {self.cfg.work_dir / "bin", self.cfg.work_dir / "samples",
                                  self.cfg.state_path})

    def _sample_set(self, *, done: bool, age: float = 2 * clean.SCRATCH_QUIET_SECONDS,
                    name: str = "20260101-1200") -> Path:
        folder = self.cfg.work_dir / clean.SAMPLES / name
        (folder / "1080p-1").mkdir(parents=True)
        (folder / clean.PICKS).write_text(json.dumps({"picks": [{"id": "1080p-1"}]}))
        (folder / "report.md").write_text("report")
        (folder / "1080p-1" / "Film.600s.mkv").write_bytes(b"e" * 300)
        (folder / "1080p-1" / "result.json").write_text(json.dumps({"ok": done}))
        (folder / "work" / "0123456789abcdef" / "av1an").mkdir(parents=True)
        (folder / "work" / "0123456789abcdef" / "av1an" / "chunk").write_bytes(b"c" * 1000)
        long_ago = time.time() - age
        for path in [*sorted(folder.rglob("*"), reverse=True), folder]:
            os.utime(path, (long_ago, long_ago))
        return folder

    def _sample_entries(self, folder: Path, **flags) -> list[clean.Entry]:
        return [entry for entry in clean.plan(self.cfg, self.chain, self.ledger, **flags)
                if entry.path.is_relative_to(folder)]

    def test_a_finished_sample_runs_chunks_go_and_its_results_stay(self):
        folder = self._sample_set(done=True)
        clean.remove(self._sample_entries(folder))
        self.assertFalse((folder / "work").exists())
        self.assertTrue((folder / "report.md").is_file())
        self.assertTrue((folder / "1080p-1" / "Film.600s.mkv").is_file())

    def test_an_unfinished_sample_run_keeps_its_chunks_unless_all(self):
        folder = self._sample_set(done=False)
        part = folder / "1080p-1" / "Other.900s.mkv.part"
        part.write_bytes(b"p")
        long_ago = time.time() - 2 * clean.SCRATCH_QUIET_SECONDS
        for path in (part, part.parent, folder):
            os.utime(path, (long_ago, long_ago))
        entries = self._sample_entries(folder)
        self.assertFalse(next(e for e in entries if e.path == folder / "work").remove)
        clean.remove(entries)
        self.assertTrue((folder / "work").is_dir())
        self.assertFalse((folder / "1080p-1" / "Other.900s.mkv.part").exists())
        clean.remove(self._sample_entries(folder, everything=True))
        self.assertFalse((folder / "work").exists())
        self.assertTrue((folder / "report.md").is_file())

    def test_samples_go_whole_only_when_asked(self):
        folder = self._sample_set(done=True)
        clean.remove(self._sample_entries(folder, everything=True, unknown=True))
        self.assertTrue((folder / "report.md").is_file())
        clean.remove(self._sample_entries(folder, samples=True))
        self.assertFalse(folder.exists())

    def test_a_sample_run_in_progress_is_left_alone(self):
        folder = self._sample_set(done=True, age=10)
        entries = self._sample_entries(folder, samples=True)
        self.assertTrue(entries and not any(entry.remove for entry in entries))
        folder = self._sample_set(done=True, name="20260102-1200")
        (folder / "work" / "0123456789abcdef" / pipeline.RUNNING_FILE).write_text(str(os.getpid()))
        self.assertFalse(any(entry.remove for entry in self._sample_entries(folder, samples=True)))

    def test_sample_sets_elsewhere_are_cleaned_when_named(self):
        folder = self._sample_set(done=True)
        elsewhere = self.root / "my-samples"
        folder.rename(elsewhere)
        self.assertEqual(self._sample_entries(elsewhere), [])
        clean.remove(self._sample_entries(elsewhere, sample_sets=[elsewhere]))
        self.assertFalse((elsewhere / "work").exists())

    def test_remove_frees_what_it_reports(self):
        source = self._film()
        work = self._work(source, chunks=5000)
        source.unlink()
        entries = self._plan()
        self.assertGreaterEqual(clean.remove(entries.values()), 5000)
        self.assertFalse(work.exists())


BAR_LINE = ("\x1b[1m00:12:01\x1b[0m [12/431 Chunks] ▐\x1b[34m████    \x1b[0m▌ \x1b[1m 42%\x1b[0m "
            "89369/212784 (\x1b[1m1.71 fps\x1b[0m, eta 20h, 4521.3 Kbps, est. 12.3 GiB)")


class TestProgressRedraw(unittest.TestCase):
    """Av1an's progress bar is read back and drawn to fit the terminal."""

    def test_av1an_bar_is_read(self):
        status, rest = progress.parse(progress.ESCAPE.sub("", BAR_LINE))
        self.assertEqual(rest, "")
        self.assertEqual(status, progress.Status(
            elapsed="00:12:01", pos=89369, rate="1.71 fps", percent=42, length=212784,
            eta="20h", chunks=(12, 431), kbps=4521.3, size="12.3 GiB"))

    def test_scene_detection_bars_are_read(self):
        status, _ = progress.parse("00:00:53 ▐ ▌   0% 91/212784 (1.71 fps, eta 35h)")
        assert status is not None
        self.assertEqual((status.pos, status.length, status.chunks), (91, 212784, None))
        status, _ = progress.parse("00:00:53 [       ]  91 frames (0.58 s/fr)")
        assert status is not None
        self.assertEqual((status.pos, status.rate, status.percent), (91, "0.58 s/fr", None))

    def test_a_bar_ending_in_any_partial_block_is_read(self):
        for part in "▉▊▋▌▍▎▏ ":
            line = f"00:01:21 ▐{'█' * 30}{part}{' ' * 9}▌  83% 6102/7333 (76.06 fps, eta 16s)"
            status, rest = progress.parse(line)
            assert status is not None, part
            self.assertEqual((status.pos, status.percent, rest), (6102, 83, ""), part)

    def test_scene_detection_draws_never_pile_up_above_the_bar(self):
        out = io.StringIO()
        screen = progress.Screen(out, width=100)
        feed = progress.Feed(screen)
        for pos, part in enumerate("▉▊▋▌▍▎▏ ", start=6100):
            feed.feed(f"\r\x1b[2K00:01:21 ▐{'█' * 30}{part}{' ' * 9}▌  83% {pos}/7333 "
                      f"(76.06 fps, eta 16s)".encode())
        # A layout this does not read is dropped, not passed through.
        feed.feed("\r\x1b[2K00:01:22 ▐████▌ 84% of something else".encode())
        feed.feed(b"\r\x1b[2KINFO encode_file: scenecut: found 33 scene(s)\n")
        feed.end()
        screen.finish()
        lines = progress.ESCAPE.sub("", out.getvalue()).replace("\r", "\n").splitlines()
        self.assertIn("INFO encode_file: scenecut: found 33 scene(s)", lines)
        self.assertFalse([line for line in lines if "fps, eta" in line or "something" in line])
        self.assertIn("6,107/7,333 frames", lines[-1])

    def test_a_part_of_the_scene_detection_is_drawn_as_the_whole_film(self):
        # Part 3 of 4, frames 14,400 to 21,600 of 28,800, a quarter of the way in.
        span = progress.Span(start=14_400, total=28_800, started=1000.0)
        status, _ = progress.parse("00:01:00 ▐██  ▌  25% 1800/7200 (24 fps, eta 3m)")
        assert status is not None
        whole = progress.whole(status, span, now=1000.0 + 3725)
        self.assertEqual((whole.pos, whole.length, whole.percent), (16_200, 28_800, 56))
        self.assertEqual(whole.eta, "8m")            # 12,600 frames left at 24 fps
        self.assertEqual(whole.elapsed, "1:02:05")   # since the first part began
        slow, _ = progress.parse("00:00:53 ▐ ▌   0% 91/7200 (0.5 s/fr, eta 59m)")
        assert slow is not None
        self.assertEqual(progress.whole(slow, span, now=1000.0).eta, "1h59m")

    def test_a_spanned_screen_draws_the_whole(self):
        out = io.StringIO()
        screen = progress.Screen(out, width=120, span=progress.Span(7200, 28_800, 0.0))
        feed = progress.Feed(screen)
        feed.feed("\r\x1b[2K00:01:21 ▐████▌  50% 3600/7200 (76.06 fps, eta 47s)".encode())
        feed.end()
        screen.finish()
        last = progress.ESCAPE.sub("", out.getvalue()).replace("\r", "\n").splitlines()[-1]
        self.assertIn(" 37%", last)
        self.assertIn("10,800/28,800 frames", last)

    def test_av1an_speeds_are_read(self):
        self.assertEqual(progress.frames_per_second("26.63 fps"), 26.63)
        self.assertEqual(progress.frames_per_second("0.25 s/fr"), 4.0)
        self.assertIsNone(progress.frames_per_second("0.00 fps"))
        self.assertIsNone(progress.frames_per_second("fast"))

    def test_every_width_fits(self):
        status, _ = progress.parse(progress.ESCAPE.sub("", BAR_LINE))
        assert status is not None
        for width in range(20, 200):
            lines = progress.render(status, width)
            self.assertTrue(all(len(line) < width for line in lines), width)
            self.assertIn("42%", lines[0])
            self.assertIn("eta 20h", lines[0])
        self.assertEqual(len(progress.render(status, 160)), 1)
        self.assertEqual(len(progress.render(status, 80)), 2)
        self.assertIn("12/431 chunks", progress.render(status, 80)[1])

    def test_the_film_is_named_on_a_line_above_the_bar(self):
        title = "The Quiet Danubian Hotel (2014)"
        for width in (200, 80, 30):
            out = io.StringIO()
            screen = progress.Screen(out, width=width, title=title)
            status, _ = progress.parse(progress.ESCAPE.sub("", BAR_LINE))
            assert status is not None
            screen.update(status, now=True)
            screen.write_above("INFO a chunk finished\n")
            screen.finish()
            # What the last redraw left, after it cleared the lines before.
            last = re.split(r"(?:\r?\x1b\[2K|\x1b\[1A\x1b\[2K)+", out.getvalue())[-1]
            last = progress.ESCAPE.sub("", last).splitlines()
            assert screen.status is not None
            bar = progress.render(screen.status, width)
            self.assertEqual(last, [progress.heading(title, width), *bar], width)
            self.assertTrue(all(len(line) < width for line in last), width)
        self.assertEqual(progress.heading(title, 20), "The Quiet Danubian…")

    def test_bars_drawn_while_showing_a_film_are_titled_with_it(self):
        with progress.showing("Some Film"):
            relay = progress.Relay(io.StringIO())
        self.addCleanup(relay.close)
        self.assertEqual(relay.screen.title, "Some Film")
        self.assertEqual(progress.Relay(io.StringIO()).screen.title, "")

    def test_other_output_goes_above_the_bar(self):
        out = io.StringIO()
        screen = progress.Screen(out, width=80)
        feed = progress.Feed(screen)
        for piece in ("\r\x1b[2K" + BAR_LINE, "\r\x1b[2KINFO a chunk ", "finished\n",
                      BAR_LINE[:30], BAR_LINE[30:] + "\r"):
            feed.feed(piece.encode())
        feed.end()
        screen.finish()
        text = progress.ESCAPE.sub("", out.getvalue())
        self.assertIn("INFO a chunk finished\n", text)
        self.assertNotIn("Kbps", text)
        self.assertTrue(text.rstrip("\n").endswith("00:12:01"))

    # As Av1an 0.5.2 and 0.6.0 print it for every source without subtitles.
    AUDIO_WARNING = (
        "\x1b[33mWARN\x1b[0m FFmpeg failed to encode audio!\n"
        "Output {\n"
        "    status: ExitStatus(\n"
        "        unix_wait_status(\n"
        "            59904,\n"
        "        ),\n"
        "    ),\n"
        '    stdout: "",\n'
        '    stderr: "[out#0/matroska @ 0xaaaad5685970] Output file does not contain any '
        'stream\\nError opening output file /w/av1an/audio.mkv.\\nError opening output '
        'files: Invalid argument\\n",\n'
        "}\n"
        'Params: "ffmpeg" "-y" "-hide_banner" "-loglevel" "error" "-i" "/w/film.avi" '
        '"-map_metadata" "0" "-map" "0" "-c" "copy" "-vn" "-dn" "-an" "/w/av1an/audio.mkv"\n')

    def feed_through(self, text: str) -> str:
        out = io.StringIO()
        screen = progress.Screen(out, width=80)
        feed = progress.Feed(screen)
        feed.feed(text.replace("\n", "\r\n").encode())
        feed.end()
        screen.finish()
        return progress.ESCAPE.sub("", out.getvalue())

    def test_av1an_failing_to_copy_audio_that_is_left_out_is_not_shown(self):
        text = self.feed_through("INFO encode_file: Queue 136 Workers 1\n"
                                 + self.AUDIO_WARNING + "INFO a chunk finished\n")
        self.assertEqual(text, "INFO encode_file: Queue 136 Workers 1\nINFO a chunk finished\n")

    def test_what_follows_the_audio_warning_in_another_shape_is_still_shown(self):
        text = self.feed_through("WARN FFmpeg failed to encode audio!\n"
                                 "Error: the scene list is unreadable\n")
        self.assertEqual(text, "Error: the scene list is unreadable\n")

    def test_what_a_stopped_command_says_while_dying_is_dropped(self):
        out = io.StringIO()
        screen = progress.Screen(out, width=80)
        feed = progress.Feed(screen)
        feed.feed(("\r\x1b[2K" + BAR_LINE + "\r").encode())
        screen.mute()
        feed.feed(b"Terminated\nError: fwrite() call failed\n\r\x1b[2K" + BAR_LINE.encode())
        feed.end()
        screen.finish()
        screen.write_above("too late\n")
        text = progress.ESCAPE.sub("", out.getvalue())
        self.assertIn("12/431 chunks", text)
        self.assertNotIn("Terminated", text)
        self.assertNotIn("fwrite", text)
        self.assertTrue(text.endswith("00:12:01\n"))

    INDEXING = "".join(f"Creating lwi index file {n}%\r" for n in range(101)) + "\n"

    def test_a_line_redrawn_after_a_return_is_shown_once_as_last_drawn(self):
        text = self.feed_through(self.INDEXING + "INFO encode_file: Queue 136\n")
        self.assertEqual(text, "Creating lwi index file 100%\nINFO encode_file: Queue 136\n")

    def test_quiet_drops_the_progress_reports_and_erases_the_bar(self):
        out = io.StringIO()
        screen = progress.Screen(out, width=80, title="Film", note="scenes, part 2 of 11")
        feed = progress.Feed(screen, quiet=True)
        feed.feed((self.INDEXING
                   + "\x1b[32mINFO\x1b[0m encode_file: Input: 1920x1080 @ 25.000 fps\r\n"
                   + "Scene detection\r\n"
                   + "\r\x1b[2K00:00:45 ▐████████▌ 100% 7500/7500 (166.06 fps, eta 0s)"
                   + "\r\n\x1b[32mINFO\x1b[0m encode_file: scenecut: found 60 scene(s)\r\n"
                   + "Error: the scene list is unreadable\r\n").encode())
        feed.end()
        screen.finish(keep=False)
        text = out.getvalue()
        self.assertIn("Film · scenes, part 2 of 11", text)
        self.assertIn("Error: the scene list is unreadable\n", text)
        # The title and the bar, erased from the bottom up, the cursor left where
        # the title began.
        self.assertTrue(text.endswith("\r\x1b[2K\x1b[1A\x1b[2K"), repr(text[-40:]))
        self.assertNotIn("INFO", text)
        self.assertNotIn("lwi", text)
        self.assertNotIn("Scene detection", text)

    def test_a_long_title_is_cut_before_the_note(self):
        title = "Blue.Planet.II.S01E01.1080p.BluRay.x264-SHORTBREHD"
        note = "scenes, part 2 of 11"
        self.assertEqual(progress.heading(title, 100, note), f"{title} · {note}")
        cut = progress.heading(title, 60, note)
        self.assertTrue(cut.endswith(f"… · {note}"), cut)
        self.assertLess(len(cut), 60)
        self.assertEqual(progress.heading("", 60, note), note)
        self.assertEqual(progress.heading(title, 30, note), progress.heading(
            f"{title} · {note}", 30))

    def test_bars_drawn_while_noting_a_step_carry_the_note(self):
        with progress.showing("Some Film"), progress.noting("scenes, part 1 of 3"):
            relay = progress.Relay(io.StringIO(), quiet=True)
        self.addCleanup(relay.close)
        self.assertEqual((relay.screen.title, relay.screen.note),
                         ("Some Film", "scenes, part 1 of 3"))
        self.assertEqual(progress.Relay(io.StringIO()).screen.note, "")

    def test_a_child_draws_on_a_terminal_and_is_redrawn(self):
        out = io.StringIO()
        relay = progress.Relay(out)
        script = ("import os, sys; sys.stderr.write(str(os.isatty(2)) + '\\n'); "
                  f"sys.stderr.write({BAR_LINE!r}); sys.stderr.flush()")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                                 stdout=relay.child_fd, stderr=relay.child_fd)
        relay.started()
        child.wait()
        relay.close()
        text = progress.ESCAPE.sub("", out.getvalue())
        self.assertIn("True\n", text)
        self.assertIn("12/431 chunks", text)
        self.assertNotIn("Kbps", text)


# --- SvtFgEssential, per-frame tables, per-scene calibration, regrain --------------

def entry(luma: int, seed: int = 1, apply: bool = True) -> grain.Entry:
    body = ("p 3 8 0 11 0 1 128 192 256 128 192 256", f"sY 2 0 {luma} 255 {luma}",
            "sCb 2 0 10 255 10", "sCr 2 0 12 255 12", "cY " + " ".join(["0"] * 24),
            "cCb " + " ".join(["0"] * 25), "cCr " + " ".join(["0"] * 25)) if apply else ()
    return grain.Entry(apply, seed, body)


class TestPerFrameTables(unittest.TestCase):
    def test_entries_read_back_as_written(self):
        frames = [entry(40, 7), entry(40, 8), entry(0, 9, apply=False), entry(55, 10)]
        self.assertEqual(grain.parse_entries(grain.frame_table(frames)), frames)

    def test_a_segment_that_reuses_the_last_parameters_gets_them(self):
        raw = grain.frame_table([entry(40, 7)]) + "E 1 1 1 9 0\n"
        again = grain.parse_entries(raw)
        self.assertEqual((again[1].body, again[1].seed), (again[0].body, 9))

    def test_each_frame_falls_in_its_own_segment_whatever_the_rounding(self):
        # 23.976 fps in Matroska's milliseconds: 0, 42, 83, 125 ms. Frame 3 at
        # 125 ms is before 3 x 417083 = 1251250, where a frame-number table puts it.
        stamps = [0, 420_000, 830_000, 1_250_000]
        frames = [entry(10), entry(20), entry(30), entry(40)]
        table = grain.retime(frames, stamps)
        spans = [tuple(map(int, line.split()[1:3])) for line in table.splitlines()
                 if line.startswith("E ")]
        for number, stamp in enumerate(stamps):
            found = next(i for i, (start, end) in enumerate(spans) if start <= stamp <= end)
            self.assertEqual(found, number)

    def test_runs_of_the_same_grain_become_one_segment(self):
        frames = [entry(10, 1), entry(10, 2), entry(10, 3), entry(20, 4), entry(20, 5)]
        table = grain.retime(frames, [0, 10, 20, 30, 40])
        heads = [line.split() for line in table.splitlines() if line.startswith("E ")]
        self.assertEqual([h[1:5] for h in heads],
                         [["0", "29", "1", "1"], ["30", str(grain.WHOLE_VIDEO_END), "1", "4"]])

    def test_frames_without_grain_get_a_segment_without_parameters(self):
        table = grain.retime([entry(10), entry(0, apply=False)], [0, 10])
        self.assertIn(f"E 10 {grain.WHOLE_VIDEO_END} 0 1 0\n", table)
        self.assertTrue(table.endswith("0 1 0\n"))

    def test_a_table_for_another_number_of_frames_is_refused(self):
        with self.assertRaises(ValueError):
            grain.retime([entry(10)], [0, 10])

    def test_scaling_an_entry_scales_its_strength_only(self):
        scaled = grain.scale_entry(entry(40), 0.5, 2.0, 1.0)
        self.assertIn("sY 2 0 20 255 20", scaled.body)
        self.assertIn("sCb 2 0 20 255 20", scaled.body)
        self.assertEqual(scaled.body[4:], entry(40).body[4:])


class TestFrameTimesRemembered(unittest.TestCase):
    def test_the_source_is_read_once_while_it_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "film.mkv"
            path.write_bytes(b"x")
            listing = subprocess.CompletedProcess([], 0, "0.083,\n0.0,\n0.042,\n", "")
            with unittest.mock.patch.object(encode, "run", return_value=listing) as run:
                first = encode.frame_times(make_chain(), path)
                first.append(9.0)   # a caller's change stays its own
                self.assertEqual(encode.frame_times(make_chain(), path), [0.0, 0.042, 0.083])
                self.assertEqual(run.call_count, 1)
                path.write_bytes(b"xy")
                encode.frame_times(make_chain(), path)
                self.assertEqual(run.call_count, 2)


class TestEssentialParts(unittest.TestCase):
    def test_parts_are_whole_scenes_covering_every_frame_once(self):
        starts = [0, 100, 250, 260, 600, 900]
        parts = essential.plan_parts(starts, 1000, 3)
        self.assertEqual(parts[0].start, 0)
        self.assertEqual(parts[-1].end, 1000)
        for before, after in itertools.pairwise(parts):
            self.assertEqual(before.end, after.start)
            self.assertIn(after.start, starts)
        for part in parts:
            self.assertEqual(part.cuts[0], 0)
            self.assertEqual([part.start + c for c in part.cuts],
                             [s for s in starts if part.start <= s < part.end])

    def test_one_long_scene_is_one_part(self):
        self.assertEqual(essential.plan_parts([0], 500, 4), [essential.Part(0, 500, (0,))])

    def test_parts_shrink_so_the_processes_finish_together(self):
        parts = essential.plan_parts(range(0, 100_000, 100), 100_000, 4, smallest=600)
        sizes = [part.frames for part in parts]
        self.assertEqual(sizes[0], 100_000 // (essential.GUIDED * 4))
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertTrue(all(size >= 600 for size in sizes[:-1]))
        self.assertLessEqual(sizes[-1], 600)
        self.assertEqual(sum(sizes), 100_000)

    def test_a_long_take_is_never_split(self):
        starts = [0, 1000, 9000, 9500]
        parts = essential.plan_parts(starts, 10_000, 4, smallest=100)
        self.assertFalse([part for part in parts if 1000 < part.start < 9000])

    def test_a_part_is_found_by_its_timestamps_after_a_seek_before_it(self):
        times = [i / 24 for i in range(100)]
        argv = essential.decode_command(make_chain(), Path("/f.mkv"), times, 0.0,
                                        essential.Part(48, 96, (0,)), "", "yuv420p")
        joined = " ".join(argv)
        self.assertIn("-noaccurate_seek -ss 0.000", joined)   # 2 s in, less the margin
        self.assertIn(f"select=gte(t\\,{(times[47] + times[48]) / 2:.6f})", joined)
        self.assertEqual(argv[argv.index("-frames:v") + 1], "48")
        self.assertIn("-copyts", argv)

    def test_the_first_part_needs_no_seek(self):
        argv = essential.decode_command(make_chain(), Path("/f.mkv"), [0.0, 0.04], 0.0,
                                        essential.Part(0, 2, (0,)), "bwdif", "yuv420p")
        self.assertNotIn("-ss", argv)
        self.assertEqual(argv[argv.index("-vf") + 1], "bwdif")

    def test_deinterlacing_comes_before_the_selection(self):
        times = [i / 25 for i in range(400)]
        argv = essential.decode_command(make_chain(), Path("/f.mkv"), times, 0.0,
                                        essential.Part(300, 400, (0,)), "bwdif=x", "yuv420p")
        self.assertTrue(argv[argv.index("-vf") + 1].startswith("bwdif=x,select="))

    def test_one_process_per_big_core_unless_configured(self):
        cfg = config.Config()
        self.assertEqual(essential.jobs(cfg, make_chain(big_cores="4,5,6,7")), 4)
        cfg.grain.svtfg_jobs = 6
        self.assertEqual(essential.jobs(cfg, make_chain(big_cores="4,5,6,7")), 6)

    def test_eight_bit_sources_go_as_they_are(self):
        self.assertEqual(essential.feed_format(make_info()), "yuv420p")
        self.assertEqual(essential.feed_format(make_info(pix_fmt="yuv420p10le")), "yuv420p10le")
        self.assertEqual(essential.feed_format(make_info(pix_fmt="yuv422p")), "yuv420p10le")

    def test_scene_cuts_go_in_a_file(self):
        argv = essential.estimate_command(make_chain(), Path("/w/p.tbl"), Path("/w/p.cuts"))
        self.assertEqual(argv[-2:], ["--keyframes", "@/w/p.cuts"])
        self.assertEqual(argv[argv.index("--strength") + 1], "-1")
        self.assertEqual(argv[argv.index("--keyint") + 1], "0")

    def test_the_estimate_is_redone_when_the_scenes_change(self):
        cfg, chain, info = config.Config(), make_chain(svtfg_commit="abc"), make_info()
        self.assertNotEqual(essential.key(info, chain, cfg, [0, 10]),
                            essential.key(info, chain, cfg, [0, 11]))
        self.assertNotEqual(essential.key(info, chain, cfg, [0]),
                            essential.key(info, make_chain(svtfg_commit="def"), cfg, [0]))


class TestSceneCalibration(unittest.TestCase):
    def test_windows_skip_the_keyframe_and_stay_in_their_scene(self):
        windows = scenegrain.plan_windows([0, 100], [0, 50, 100, 150], 200, 10_000, 4)
        self.assertEqual([(w.keyframe, w.start, w.frames) for w in windows],
                         [(0, 1, 4), (50, 51, 4), (100, 101, 4), (150, 151, 4)])
        self.assertEqual([w.scene for w in windows], [0, 0, 1, 1])

    def test_every_scene_gets_a_window_before_any_gets_two(self):
        windows = scenegrain.plan_windows([0, 100, 400], [0, 50, 100, 200, 300, 400],
                                          500, 12, 4)
        self.assertEqual(sorted(w.scene for w in windows), [0, 1, 2])

    def test_with_little_budget_the_longest_scenes_come_first(self):
        windows = scenegrain.plan_windows([0, 100, 400], [0, 100, 400], 500, 4, 4)
        self.assertEqual([w.scene for w in windows], [1])

    def test_a_window_stops_at_the_next_keyframe(self):
        windows = scenegrain.plan_windows([0], [0, 3], 10, 100, 8)
        self.assertEqual([(w.start, w.frames) for w in windows], [(1, 2), (4, 6)])

    def test_the_spread_order_is_a_permutation_starting_mid_way(self):
        for count in (1, 2, 3, 7, 16):
            order = scenegrain._spread(count)
            self.assertEqual(sorted(order), list(range(count)))
        self.assertEqual(scenegrain._spread(8)[0], 4)

    def test_a_scene_measured_on_few_frames_leans_on_the_film(self):
        self.assertAlmostEqual(scenegrain.shrink(2.0, 1.0, 0), 1.0)
        self.assertAlmostEqual(scenegrain.shrink(2.0, 1.0, 8, prior=8), 2 ** 0.5)
        self.assertGreater(scenegrain.shrink(2.0, 1.0, 64, prior=8), 1.8)
        self.assertEqual(scenegrain.shrink(None, 0.7, 64), 0.7)
        self.assertEqual(scenegrain.shrink(10.0, 1.9, 10_000), grain.MAX_FACTOR)
        self.assertAlmostEqual(scenegrain.shrink(0.0, 0.2, 10_000), scenegrain.LOWEST, places=3)

    def energies(self, frames: int, wanted: float, under: float, added: float):
        return scenegrain.Energies(frames, (wanted,) * 3, (under,) * 3, (under + added,) * 3)

    def test_the_film_factor_weighs_every_measured_frame_alike(self):
        found = {0: self.energies(4, 10.0, 1.0, 9.0), 1: self.energies(12, 10.0, 1.0, 36.0)}
        calibration = present(scenegrain.calibrate(found, "auto", prior=4))
        whole = present(scenegrain.overall(found))
        self.assertAlmostEqual(calibration.film[0], present(grain.strength_factor(
            whole.wanted[0], whole.underneath[0], whole.with_grain[0])))
        self.assertGreater(calibration.for_scene(0)[0], calibration.film[0])
        self.assertLess(calibration.for_scene(1)[0], calibration.film[0])
        self.assertEqual(calibration.for_scene(7), calibration.film)

    def test_a_clean_film_gets_no_grain(self):
        calibration = present(scenegrain.calibrate({0: self.energies(40, 5.0, 5.0, 9.0)}, "auto"))
        self.assertFalse(calibration.keep)

    def test_nothing_measured_is_no_calibration(self):
        self.assertIsNone(scenegrain.calibrate({}, "auto"))

    def test_each_frame_takes_its_scenes_factors(self):
        calibration = scenegrain.Calibration(True, (1.0, 1.0, 1.0), {1: (0.5, 1.0, 1.0)})
        out = scenegrain.scaled([entry(40), entry(40), entry(40)], [0, 2], calibration)
        self.assertEqual([e.body[1] for e in out],
                         ["sY 2 0 40 255 40", "sY 2 0 40 255 40", "sY 2 0 20 255 20"])

    def test_the_budget_shrinks_with_the_picture(self):
        cfg = config.Config()
        self.assertEqual(scenegrain.budget(make_info(width=1920, height=1080), cfg),
                         cfg.grain.calibration_frames)
        self.assertEqual(scenegrain.budget(make_info(width=3840, height=2160), cfg),
                         cfg.grain.calibration_frames // 4)

    def test_a_stream_not_counted_in_milliseconds_is_refused(self):
        result = subprocess.CompletedProcess([], 0, stdout="1/90000\n", stderr="")
        with unittest.mock.patch.object(scenegrain, "run", return_value=result), \
                self.assertRaises(ValueError):
            scenegrain.read_stream(make_chain(), Path("/x.mp4"))

    def test_packets_give_timestamps_and_keyframes_in_display_order(self):
        answers = iter([subprocess.CompletedProcess([], 0, "1/1000\n", ""),
                        subprocess.CompletedProcess([], 0, "42,__\n0,K_\n83,K_\n", "")])
        with unittest.mock.patch.object(scenegrain, "run", lambda *_a, **_k: next(answers)):
            stream = scenegrain.read_stream(make_chain(), Path("/x.mkv"))
        self.assertEqual(stream.stamps, [0, 420_000, 830_000])
        self.assertEqual(stream.keyframes, [0, 2])

    def test_a_window_reads_the_same_frames_of_all_three(self):
        stream = scenegrain.Stream([i * 10_000 for i in range(0, 4000, 40)],
                                   [i / 1000 for i in range(0, 4000, 40)], [0, 50])
        window = scenegrain.Window(0, 50, 51, 4)
        stats = [Path(f"/w/{n}.log") for n in range(3)]
        argv = scenegrain.window_command(make_chain(), window, Path("/src.mkv"),
                                         stream.times, 0.0, Path("/base.mkv"),
                                         Path("/grain.mkv"), stream, 0.0, "", stats)
        graph = argv[argv.index("-filter_complex") + 1]
        middle = (stream.times[50] + stream.times[51]) / 2
        self.assertEqual(graph.count(f"select=gte(t\\,{middle:.6f})"), 3)
        self.assertEqual(graph.count("trim=end_frame=4"), 3)
        self.assertEqual(argv.count("-export_side_data"), 1)
        # The encodes are sought straight to the window's keyframe.
        self.assertIn(f"{stream.times[50] + 0.0005:.4f}", argv)
        # Single-threaded: as many run at once as there are cores.
        self.assertEqual(argv[argv.index("-filter_complex") - 1], "1")
        self.assertEqual(argv[argv.index("-filter_complex") - 2], "-filter_complex_threads")
        av1_inputs = [n for n, arg in enumerate(argv) if arg == "-i"][1:]
        for at in av1_inputs:
            self.assertEqual(argv[at - 6:at - 4], ["-threads", "1"])


class TestStoppingWorkers(unittest.TestCase):
    """A stop cancels what a pool has queued and kills what it has running."""

    def test_a_child_started_after_the_stop_is_killed_at_once(self):
        children = util.Children()
        children.stop()
        with subprocess.Popen(["sleep", "30"]) as child:
            started = time.monotonic()
            children.add(child)
            child.wait(timeout=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(children.stopping.is_set())

    def test_ctrl_c_in_calibration_starts_no_more_windows(self):
        started: list[int] = []
        stream = scenegrain.Stream([i * 10_000 for i in range(400)],
                                   [i / 25 for i in range(400)], [0])
        windows = [scenegrain.Window(0, 0, n, 4) for n in range(1, 41)]

        def sleeper(*_args, **_kwargs):
            started.append(1)
            return ["sleep", "30"]

        def interrupted(_futures):
            time.sleep(0.5)
            raise KeyboardInterrupt
            yield  # pragma: no cover

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(scenegrain, "frame_times",
                                           lambda *_a: list(stream.times)), \
                unittest.mock.patch.object(scenegrain.essential, "start_time",
                                           lambda *_a: 0.0), \
                unittest.mock.patch.object(scenegrain, "window_command", sleeper), \
                unittest.mock.patch.object(scenegrain.concurrent.futures, "as_completed",
                                           interrupted):
            began = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                scenegrain.measure(make_chain(), windows, Path("/src.mkv"), Path("/b.mkv"),
                                   Path("/g.mkv"), stream, "", Path(tmp), processes=2)
            # The two running were killed, and none of the 38 queued started.
            self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(len(started), 2)


class TestChildrenLeaveTheTerminal(unittest.TestCase):
    """No child reads the terminal: ffmpeg turns its echo off while it runs and on
    as it exits, so several at once (or one killed) left a regrain's terminal
    without echo, and a key typed there reached them (q quits ffmpeg)."""

    # Run in the foreground on purpose, the terminal theirs: a build the user
    # watches (bootstrap.sh may ask for a password), and `doctor` after an update.
    INTERACTIVE: ClassVar[set[tuple[str, str]]] = {("update.py", "argv"), ("cli.py", "doctor")}

    def test_every_child_is_given_its_input(self):
        import ast
        missing = []
        for path in sorted(Path(util.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr in {"run", "Popen", "call", "check_output"}):
                    continue
                if any(k.arg in {"stdin", "input"} for k in node.keywords):
                    continue
                first = node.args[0] if node.args else None
                name = first.id if isinstance(first, ast.Name) else ""
                if (path.name, name) not in self.INTERACTIVE:
                    missing.append(f"{path.name}:{node.lineno}")
        self.assertEqual(missing, [])

    def test_run_gives_none(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with unittest.mock.patch.object(util.subprocess, "run", return_value=done) as ran:
            util.run(["true"])
        self.assertIs(ran.call_args.kwargs["stdin"], subprocess.DEVNULL)


class TestSceneList(unittest.TestCase):
    def test_scene_starts_are_the_cuts_not_the_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenes.json"
            path.write_text(json.dumps({
                "frames": 300,
                "scenes": [{"start_frame": 0, "end_frame": 200},
                           {"start_frame": 200, "end_frame": 300}],
                "split_scenes": [{"start_frame": 0, "end_frame": 100},
                                 {"start_frame": 100, "end_frame": 200},
                                 {"start_frame": 200, "end_frame": 300}]}))
            self.assertEqual(scenes.starts(path), [0, 200])
            self.assertIsNone(scenes.starts(Path(tmp) / "missing.json"))


class TestSvtfgDependency(unittest.TestCase):
    def test_it_is_the_default_estimator(self):
        self.assertEqual(config.Config().grain.estimator, "svtfg")

    def test_its_settings_are_checked(self):
        for name, value in (("estimator", "nope"), ("svtfg_jobs", -1),
                            ("calibration_frames", 10), ("calibration_window", 0)):
            cfg = config.Config()
            setattr(cfg.grain, name, value)
            with self.assertRaises(config.ConfigError, msg=name):
                config.validate(cfg)

    def test_a_run_needs_it_only_while_it_estimates(self):
        def problems(cfg: config.Config) -> str:
            try:
                tools.require(make_chain(svtfg=None), cfg)
            except tools.ToolError as exc:
                return str(exc)
            return ""

        cfg = config.Config()
        self.assertIn("SvtFgEssential", problems(cfg))
        cfg.grain.estimator = "svt"
        self.assertNotIn("SvtFgEssential", problems(cfg))
        cfg.grain.estimator, cfg.grain.mode = "svtfg", "never"
        self.assertNotIn("SvtFgEssential", problems(cfg))

    def test_its_commit_is_read_from_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "SvtFgEssential"
            binary.write_text("")
            self.assertEqual(tools.read_svtfg_commit(str(binary)), "")
            (Path(tmp) / tools.SVTFG_COMMIT_FILE).write_text("9f7ebdc5\n")
            self.assertEqual(tools.read_svtfg_commit(str(binary)), "9f7ebdc5")

    def test_it_is_found_beside_the_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SvtFgEssential").write_text("")
            chain = make_chain(svtav1encapp=str(Path(tmp) / "SvtAv1EncApp"))
            with unittest.mock.patch.object(tools.shutil, "which", return_value=None):
                self.assertEqual(tools._find_svtfg("SvtFgEssential", chain),
                                 str(Path(tmp) / "SvtFgEssential"))

    def test_cargo_tools_are_found_off_path(self):
        # Av1an and grav1synth built into ~/.cargo/bin, which was not on PATH:
        # doctor called both missing.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".cargo" / "bin").mkdir(parents=True)
            (home / ".local" / "bin").mkdir(parents=True)
            (home / ".cargo" / "bin" / "av1an").write_text("")
            (home / ".local" / "bin" / "SvtAv1EncApp").write_text("")
            with unittest.mock.patch.object(tools.shutil, "which", return_value=None), \
                    unittest.mock.patch.object(tools.Path, "home", return_value=home):
                self.assertEqual(tools._find("av1an", tools._cargo_bin()),
                                 str(home / ".cargo" / "bin" / "av1an"))
                self.assertEqual(tools._find("SvtAv1EncApp", tools._local_bin()),
                                 str(home / ".local" / "bin" / "SvtAv1EncApp"))
                self.assertIsNone(tools._find("grav1synth", tools._cargo_bin()))
                # A path in the config is taken as it is, not looked for elsewhere.
                self.assertIsNone(tools._find("/opt/av1an", tools._cargo_bin()))

    def test_an_unrecorded_build_is_unknown(self):
        checks = {c.pin.name: c for c in versions.check(tested_chain(svtfg_commit=""))}
        self.assertEqual(checks["svtfg"].status, "unknown")
        self.assertIn("svtfg", versions.fixes(list(checks.values())))

    def test_hevc_decodes_on_the_video_engine(self):
        chain = make_chain(rkmpp_decoders={"hevc_rkmpp"}, hardware_decode=("rkmpp",))
        self.assertEqual(decode_with(chain, "hevc"), ["-c:v", "hevc_rkmpp", "-deint", "0"])

    def test_dav1d_keeps_frames_in_flight(self):
        chain = make_chain(decoders={"libdav1d"})
        self.assertEqual(decode_with(chain, "av1", grain=False), verify.DAV1D_FRAME_DELAY)
        self.assertEqual(verify.software_decode_args(make_chain(), "av1"), [])


class TestHardwareDecode(unittest.TestCase):
    """Hardware decoders beyond Rockchip's, each kept only where it matches software."""

    def sums(self, wrong: set[str] = frozenset(), broken: set[str] = frozenset()):
        calls: list[list[str]] = []

        def fake(chain, path, decode, start, pix_fmt):
            calls.append(list(decode))
            method = (decode[decode.index("-hwaccel") + 1] if "-hwaccel" in decode
                      else "rkmpp" if decode else "")
            if method in broken:
                raise CommandError(["ffmpeg"], 1, "[AVHWDeviceContext @ 0x5] Device creation "
                                   "failed: -5.\nError opening output files: I/O error")
            return ["0, 0, 0, 1, 100, other" if method in wrong else "0, 0, 0, 1, 100, same"]
        return calls, unittest.mock.patch.object(verify, "_frame_sums", side_effect=fake)

    def test_vaapi_on_a_pc(self):
        calls, patch = self.sums()
        with patch:
            argv = decode_with(make_chain(hardware_decode=("vaapi",)), "hevc", matching=False)
        self.assertEqual(argv, ["-hwaccel", "vaapi"])
        # Two stretches, each decoded both ways.
        self.assertEqual(calls, [["-hwaccel", "vaapi"], [], ["-hwaccel", "vaapi"], []])

    def test_other_frames_mean_software(self):
        _, patch = self.sums(wrong={"cuda"})
        with patch, self.assertLogs(util.log, "WARNING") as logged:
            argv = decode_with(make_chain(hardware_decode=("cuda",)), "h264", matching=False)
        self.assertEqual(argv, [])
        self.assertIn("came out different from software", logged.output[0])

    def test_a_decoder_that_fails_gives_way_to_the_next(self):
        _, patch = self.sums(broken={"cuda"})
        with patch, self.assertLogs(util.log, "INFO") as logged:
            argv = decode_with(make_chain(hardware_decode=("cuda", "vaapi")), "h264",
                               matching=False)
        self.assertEqual(argv, ["-hwaccel", "vaapi"])
        self.assertIn("with cuda: failed: Device creation failed: -5.", "\n".join(logged.output))

    def test_each_film_is_checked_once(self):
        calls, patch = self.sums()
        chain = make_chain(hardware_decode=("vaapi",))
        with patch:
            decode_with(chain, "h264", times=3, matching=False)
        self.assertEqual(len(calls), 4)

    def test_rkmpp_only_for_the_codecs_it_has(self):
        _, patch = self.sums()
        chain = make_chain(rkmpp_decoders={"h264_rkmpp"}, hardware_decode=("rkmpp", "vaapi"))
        with patch:
            self.assertEqual(decode_with(chain, "hevc", matching=False), ["-hwaccel", "vaapi"])

    def test_codecs_decoders_may_differ_on_stay_in_software(self):
        calls, patch = self.sums()
        with patch:
            for codec in ("mpeg2video", "vc1", "av1"):
                self.assertEqual(decode_with(make_chain(hardware_decode=("vaapi",)), codec,
                                             matching=False), [])
        self.assertEqual(calls, [])

    def chain_with(self, setting: str, hwaccels: set[str], devices: set[str] = frozenset(),
                   rkmpp: set[str] = frozenset(),
                   opens: set[str] = frozenset(config.HARDWARE_DECODERS)) -> tuple[str, ...]:
        chain = make_chain(hwaccels=hwaccels, rkmpp_decoders=set(rkmpp))
        with tempfile.TemporaryDirectory() as tmp:
            dri = Path(tmp) / "dri"
            dri.mkdir()
            if "dri" in devices:
                (dri / "renderD128").touch()
            nvidia = Path(tmp) / "nvidiactl"
            if "nvidia" in devices:
                nvidia.touch()
            with unittest.mock.patch.multiple(tools, DRI_DIR=str(dri),
                                              NVIDIA_DEVICE=str(nvidia)), \
                    unittest.mock.patch.object(tools, "_device_opens",
                                               side_effect=lambda _, name: name in opens):
                return tools._hardware_decode(setting, chain)

    def test_a_build_lists_methods_the_machine_lacks(self):
        # jellyfin-ffmpeg's x86 build lists cuda and vaapi on every machine.
        self.assertEqual(self.chain_with("auto", {"cuda", "vaapi", "qsv"}, {"dri"}), ("vaapi",))
        self.assertEqual(self.chain_with("auto", {"cuda", "vaapi"}, {"dri", "nvidia"}),
                         ("cuda", "vaapi"))
        self.assertEqual(self.chain_with("auto", {"videotoolbox"}), ("videotoolbox",))
        self.assertEqual(self.chain_with("auto", set(), rkmpp={"h264_rkmpp"}), ("rkmpp",))

    def test_a_device_that_will_not_open_is_left_out(self):
        # An RK3588 has render nodes and jellyfin-ffmpeg lists vaapi, but no VA driver.
        self.assertEqual(self.chain_with("auto", {"vaapi"}, {"dri"}, rkmpp={"h264_rkmpp"},
                                         opens=set()), ("rkmpp",))

    def test_the_setting_narrows_or_turns_it_off(self):
        both = {"cuda", "vaapi"}
        self.assertEqual(self.chain_with("vaapi", both, {"dri", "nvidia"}), ("vaapi",))
        self.assertEqual(self.chain_with("off", both, {"dri", "nvidia"}), ())
        self.assertEqual(self.chain_with("cuda", both, {"dri"}), ())

    def test_the_setting_is_checked(self):
        cfg = config.Config()
        cfg.machine.hardware_decode = "nvenc"
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class TestRegrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config()
        self.cfg.paths.work_dir = str(self.root / "work")
        self.cfg.paths.state_file = str(self.root / "state.json")
        self.ledger = Ledger(self.cfg.state_path)
        self.out = self.root / "out"
        self.out.mkdir()
        for name in ("A", "B"):
            (self.out / f"{name}.av1.mkv").write_text("x")
            self.ledger.record(Path(f"/lib/{name}.mkv"), Record(
                source=f"/lib/{name}.mkv", status="done", output=str(self.out / f"{name}.av1.mkv")))
        self.ledger.record(Path("/lib/C.mkv"), Record(source="/lib/C.mkv", status="failed"))

    def test_folders_and_files_name_finished_films(self):
        found, strangers = regrain.targets([self.out], self.ledger)
        self.assertEqual([t.output.name for t in found], ["A.av1.mkv", "B.av1.mkv"])
        found, strangers = regrain.targets([self.out / "B.av1.mkv", self.root / "x.mkv"],
                                           self.ledger)
        self.assertEqual(([t.source for t in found], strangers),
                         ([Path("/lib/B.mkv")], [self.root / "x.mkv"]))

    def test_no_inputs_means_every_finished_film(self):
        found, _ = regrain.targets([], self.ledger)
        self.assertEqual(len(found), 2)

    def test_it_refuses_to_run_beside_a_run(self):
        with pipeline.only_run(self.cfg), \
                unittest.mock.patch.object(regrain, "_regrain_all") as work:
            self.assertEqual(regrain.main(self.cfg, make_chain(), [self.out]), 1)
        work.assert_not_called()

    def test_it_needs_the_svtfg_estimator(self):
        self.cfg.grain.estimator = "svt"
        with unittest.mock.patch.object(regrain, "_regrain_all") as work:
            self.assertEqual(regrain.main(self.cfg, make_chain(), [self.out]), 2)
        work.assert_not_called()

    @staticmethod
    def _remux(found, film, aspect="", rest=Path("/rest.mkv")):
        return regrain.remux_command(make_chain(), rest, found, film, Path("/new.mkv"),
                                     Path("/base.mkv"), aspect, Path("/o.part"))

    def test_every_track_keeps_its_place(self):
        found = [regrain.Track(0, "video", "av1", False, flags=("default",)),
                 regrain.Track(1, "audio", "opus", False),
                 regrain.Track(2, "subtitle", "subrip", False),
                 regrain.Track(3, "attachment", "ttf", False)]
        argv = self._remux(found, found[0])
        self.assertEqual(argv[argv.index("-i") + 1:argv.index("-i") + 2], ["/new.mkv"])
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        self.assertEqual(maps, ["0:0", "2:0", "2:1", "2:2"])
        self.assertIn("-copyts", argv)
        self.assertEqual(argv[argv.index("-disposition:0") + 1], "default")
        # The chapters and the film's own tags come with the rest.
        self.assertEqual(argv[argv.index("-map_chapters") + 1], "2")
        self.assertEqual(argv[argv.index("-map_metadata") + 1], "2")

    def test_every_tracks_tags_are_mapped(self):
        # One stream mapping turns off ffmpeg's own copying for every stream: the
        # first regrain of Film A lost every language and the
        # commentary's title. The film track's come from the track taken out.
        found = [regrain.Track(0, "audio", "ac3", False), regrain.Track(1, "video", "av1", False),
                 regrain.Track(2, "attachment", "ttf", False)]
        argv = self._remux(found, found[1])
        self.assertEqual([argv[argv.index(f"-map_metadata:s:{n}") + 1] for n in range(3)],
                         ["2:s:0", "1:s:0", "2:s:1"])

    def test_every_tracks_dispositions_are_set(self):
        # Left to itself ffmpeg marks the first of several subtitles default when
        # none is: Film H's English subtitles would have come on by
        # themselves.
        found = [regrain.Track(0, "video", "av1", False, flags=("default",)),
                 regrain.Track(1, "subtitle", "subrip", False),
                 regrain.Track(2, "subtitle", "hdmv_pgs_subtitle", False,
                               flags=("default", "forced"))]
        argv = self._remux(found, found[0])
        self.assertEqual([argv[argv.index(f"-disposition:{n}") + 1] for n in range(3)],
                         ["default", "0", "default+forced"])

    def test_a_changed_disposition_is_caught(self):
        before = [regrain.Track(0, "video", "av1", False),
                  regrain.Track(1, "subtitle", "subrip", False)]
        after = [before[0], replace(before[1], flags=("default",))]
        with unittest.mock.patch.object(regrain, "tracks", side_effect=[before, after]), \
                self.assertRaisesRegex(ValueError, "dispositions"):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))

    def test_one_read_takes_the_film_apart(self):
        argv = regrain.split_command(make_chain(), Path("/f.mkv"), 1, Path("/v.mkv"),
                                     Path("/r.mkv"))
        self.assertEqual(argv.count("-i"), 1)
        self.assertEqual(argv[argv.index("/v.mkv") - 6:argv.index("/v.mkv") - 4],
                         ["-map", "0:1"])
        rest = argv[argv.index("/v.mkv") + 1:]
        self.assertEqual(rest[:4], ["-map", "0", "-map", "-0:1"])
        self.assertIn("-map_chapters", rest)
        self.assertEqual(rest[-1], "/r.mkv")
        alone = regrain.split_command(make_chain(), Path("/f.mkv"), 0, Path("/v.mkv"), None)
        self.assertEqual(alone[-1], "/v.mkv")

    def test_a_film_with_no_other_track_takes_everything_from_the_track(self):
        found = [regrain.Track(0, "video", "av1", False)]
        argv = self._remux(found, found[0], rest=None)
        self.assertEqual(argv.count("-i"), 2)
        self.assertEqual(argv[argv.index("-map_chapters") + 1], "1")

    def test_lost_tags_are_caught(self):
        before = [regrain.Track(0, "video", "av1", False),
                  regrain.Track(1, "audio", "ac3", False, (("language", "eng"),))]
        after = [before[0], regrain.Track(1, "audio", "ac3", False)]
        with unittest.mock.patch.object(regrain, "tracks", side_effect=[before, after]), \
                self.assertRaises(ValueError):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))

    def test_a_film_track_after_cover_art_is_the_one_replaced(self):
        found = [regrain.Track(0, "video", "mjpeg", True), regrain.Track(1, "video", "av1", False)]
        film = regrain.film_track(found)
        argv = self._remux(found, film, aspect="2.39:1")
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        self.assertEqual(maps, ["2:0", "0:0"])
        self.assertEqual(argv[argv.index("-aspect:1") + 1], "2.39:1")

    def test_a_changed_file_is_not_put_in_place(self):
        before = [regrain.Track(0, "video", "av1", False), regrain.Track(1, "audio", "ac3", False)]
        with unittest.mock.patch.object(regrain, "tracks", side_effect=[before, before[:1]]), \
                self.assertRaises(ValueError):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))
        with unittest.mock.patch.object(regrain, "tracks", side_effect=[before, before]), \
                unittest.mock.patch.object(regrain, "_video_packets",
                                           side_effect=[(100, "0"), (99, "0")]), \
                self.assertRaises(ValueError):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))

    def test_a_moved_track_or_lost_chapter_is_caught(self):
        before = [regrain.Track(0, "video", "av1", False), regrain.Track(1, "audio", "ac3", False)]
        was = regrain.Layout(100.0, (("video", "0.000"), ("audio", "0.000")),
                             (("0.0", "50.0", "One"), ("50.0", "100.0", "Two")))
        for now in (replace(was, starts=(("video", "0.000"), ("audio", "0.042"))),
                    replace(was, chapters=was.chapters[:1])):
            with unittest.mock.patch.object(regrain, "tracks", return_value=before), \
                    unittest.mock.patch.object(regrain, "_video_packets", return_value=(9, "0")), \
                    unittest.mock.patch.object(regrain, "layout", side_effect=[was, now]), \
                    self.assertRaises(ValueError):
                regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))
        with unittest.mock.patch.object(regrain, "tracks", return_value=before), \
                unittest.mock.patch.object(regrain, "_video_packets", return_value=(9, "0")) as n, \
                unittest.mock.patch.object(regrain, "layout", side_effect=[was, was]):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"), Path("/v.mkv"))
        # The film is counted from the track taken out of it, not read whole.
        self.assertEqual(n.call_args_list[0].args[1:], (Path("/v.mkv"), 0))


class TestSceneCuts(unittest.TestCase):
    def test_parts_of_five_minutes_cover_the_film_once(self):
        parts = cuts.plan(25_000, 7_193)
        self.assertEqual([(p.start, p.end) for p in parts],
                         [(0, 7_193), (7_193, 14_386), (14_386, 25_000)])
        self.assertEqual(cuts.plan(5_000, 7_193), [essential.Part(0, 5_000, (0,))])
        self.assertEqual(cuts.plan(0, 7_193), [])

    def test_4k_is_shrunk_as_av1an_shrinks_it(self):
        cfg = config.Config()
        self.assertEqual(cuts.downscale_filter(make_info(width=3840, height=2160), cfg),
                         "scale=1920:1080:flags=bicubic:param0=1/3:param1=1/3")
        self.assertEqual(cuts.downscale_filter(make_info(width=3840, height=1600), cfg),
                         "scale=2592:1080:flags=bicubic:param0=1/3:param1=1/3")
        self.assertEqual(cuts.downscale_filter(make_info(), cfg), "")
        cfg.encode.sc_downscale_height = 0
        self.assertEqual(cuts.downscale_filter(make_info(width=3840, height=2160), cfg), "")

    def test_the_detector_gets_av1ans_options(self):
        cfg = config.Config()
        argv = cuts.detect_command(make_chain(scenechange="/bin/asc"), cfg)
        self.assertEqual(argv, ["/bin/asc", "--min-scenecut", "24", "--speed", "0", "-"])
        cfg.encode.sc_method = "fast"
        self.assertIn("1", cuts.detect_command(make_chain(scenechange="/bin/asc"), cfg))

    def test_a_kept_list_is_used_only_for_the_film_it_fits(self):
        text = cuts.attachment_text([0, 58, 125], 200)
        self.assertEqual(cuts.parse(text, 200), cuts.Kept([0, 58, 125], ""))
        self.assertEqual(cuts.parse(cuts.attachment_text([0, 9], 20, "svtfg abc"), 20),
                         cuts.Kept([0, 9], "svtfg abc"))
        self.assertIsNone(cuts.parse(text, 201))
        for bad in ('{"frames": 200, "scene_starts": [5, 58]}',
                    '{"frames": 200, "scene_starts": [0, 58, 58]}',
                    '{"frames": 200, "scene_starts": [0, 200]}',
                    '{"frames": 200}', "not json"):
            self.assertIsNone(cuts.parse(bad, 200), bad)

    def test_a_part_is_kept_only_when_it_saw_every_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "p.json"
            part.write_text('{"scene_changes":[0,58],"frame_count":100}')
            self.assertEqual(cuts._read_part(part, 100), [0, 58])
            self.assertIsNone(cuts._read_part(part, 101))

    def test_the_list_is_attached_in_place_replacing_an_old_one(self):
        chain = make_chain()
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(cuts, "mkvpropedit", return_value="mkvpropedit"), \
                unittest.mock.patch.object(cuts, "attachment_index", return_value=4), \
                unittest.mock.patch.object(cuts, "run") as ran:
            self.assertTrue(cuts.attach(chain, Path("/f.mkv"), [0, 9], 20, Path(tmp)))
        argv = ran.call_args.args[0]
        self.assertEqual(argv[:3], ["mkvpropedit", "-q", "/f.mkv"])
        self.assertLess(argv.index("--delete-attachment"), argv.index("--add-attachment"))
        self.assertEqual(argv[argv.index("--attachment-mime-type") + 1], "application/json")

    def test_no_mkvtoolnix_means_no_list_and_no_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(cuts, "mkvpropedit", return_value=None):
            self.assertFalse(cuts.attach(make_chain(), Path("/f.mkv"), [0], 9, Path(tmp)))

    def test_regrain_allows_the_list_it_adds(self):
        film = regrain.Track(0, "video", "av1", False)
        listing = regrain.Track(1, "attachment", "", False,
                                (("filename", cuts.ATTACHMENT), ("mimetype", "application/json")))
        with unittest.mock.patch.object(regrain, "tracks", side_effect=[[film], [film, listing]]), \
                unittest.mock.patch.object(regrain, "_video_packets", return_value=(9, "0")), \
                unittest.mock.patch.object(regrain, "layout",
                                           return_value=regrain.Layout(1.0, (), ())):
            regrain.check(make_chain(), Path("/old.mkv"), Path("/new.mkv"))

    def test_the_grain_method_names_the_estimator_build(self):
        cfg = config.Config()
        chain = make_chain(svtfg_commit="9f7ebdc5ffbe3c135b223dd618d5155b43427703")
        self.assertEqual(pipeline.grain_method(cfg, chain), "svtfg 9f7ebdc5ffbe")
        cfg.grain.estimator = "svt"
        self.assertEqual(pipeline.grain_method(cfg, chain), "")
        cfg.grain.estimator, cfg.grain.mode = "svtfg", "never"
        self.assertEqual(pipeline.grain_method(cfg, chain), "")

    def test_a_film_with_the_current_grain_is_left_unless_forced(self):
        chain = make_chain(svtfg_commit="9f7ebdc5ffbe3c135b223dd618d5155b43427703")
        with tempfile.TemporaryDirectory() as tmp:
            output, source = Path(tmp) / "f.av1.mkv", Path(tmp) / "f.mkv"
            output.write_bytes(b"x")
            source.write_bytes(b"x")
            cfg = config.Config()
            cfg.paths.work_dir = str(Path(tmp) / "work")
            target = regrain.Target(output, Record(source=str(source), status="done",
                                                   output=str(output)))
            current = cuts.attachment_text([0, 9], 20, "svtfg 9f7ebdc5ffbe")
            with unittest.mock.patch.object(regrain, "kept_text", return_value=current), \
                    unittest.mock.patch.object(regrain, "probe") as probed:
                outcome = regrain.regrain_one(target, chain, cfg, unittest.mock.Mock())
            self.assertTrue(outcome.startswith("skipped"), outcome)
            probed.assert_not_called()
            older = cuts.attachment_text([0, 9], 20)
            for found, force in ((older, False), (current, True), (None, False)):
                with unittest.mock.patch.object(regrain, "kept_text", return_value=found), \
                        unittest.mock.patch.object(regrain, "probe",
                                                   side_effect=OSError("went on")), \
                        self.assertRaisesRegex(OSError, "went on"):
                    regrain.regrain_one(target, chain, cfg, unittest.mock.Mock(), force)

    def test_what_made_the_grain_is_read_without_counting_frames(self):
        text = cuts.attachment_text([0, 58, 125], 200, "svtfg abc")
        self.assertEqual(cuts.parse(text, None), cuts.Kept([0, 58, 125], "svtfg abc"))
        self.assertIsNone(cuts.parse('{"frames": 100, "scene_starts": [0, 100]}', None))


class TestStagedRegrain(unittest.TestCase):
    """With staging on, regrain reads the library's disks twice and writes once."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.library = self.tmp / "library"
        self.library.mkdir()
        self.output = self.library / "Film.av1.mkv"
        self.source = self.library / "Film.mkv"
        self.output.write_bytes(b"the old film")
        self.source.write_bytes(b"the source")
        self.cfg = config.Config()
        self.cfg.paths.work_dir = str(self.tmp / "work")
        self.cfg.staging.enabled = True

    def tearDown(self):
        self._tmp.cleanup()

    def _regrain(self, tracks: list | None = None, **patches: Any) -> list[list[str]]:
        commands: list[list[str]] = []
        found = tracks or [regrain.Track(0, "video", "av1", False),
                           regrain.Track(1, "audio", "opus", False)]
        staged = self.cfg.staging_dir / (pipeline.source_digest(self.source) + ".mkv")
        self.source_at_remux: bool | None = None

        def ran(argv, *_args, **_kwargs):
            commands.append([str(arg) for arg in argv])
            if str(argv[-1]).endswith((regrain.STAGED_NEW, regrain.PART_SUFFIX)):
                self.source_at_remux = staged.exists()    # the remux: the new film
                Path(argv[-1]).write_bytes(b"the new film")
            return subprocess.CompletedProcess(argv, 0, "", "")

        work = pipeline._work_dir(self.source, self.cfg)
        defaults: dict[str, Any] = dict(
            kept_text=None, probe=make_info(path=self.source, size_bytes=10),
            tracks=found, find_scenes=[0, 5], video_details=([], ""), check=None)
        defaults.update(patches)
        target = regrain.Target(self.output, Record(source=str(self.source), status="done",
                                                    output=str(self.output)))
        grain = patches.pop("film_grain", (work / "grainy.mkv", work / "grain.tbl"))
        defaults.pop("film_grain", None)
        with contextlib.ExitStack() as stack:
            for name, value in defaults.items():
                how = ({"side_effect": value} if isinstance(value, BaseException)
                       else {"return_value": value})
                stack.enter_context(unittest.mock.patch.object(regrain, name, **how))
            stack.enter_context(unittest.mock.patch.object(regrain, "run", side_effect=ran))
            stack.enter_context(unittest.mock.patch.object(
                scenegrain, "read_stream", return_value=unittest.mock.Mock(stamps=[0] * 10)))
            how = ({"side_effect": grain} if isinstance(grain, BaseException)
                   else {"return_value": grain})
            self.film_grain = stack.enter_context(unittest.mock.patch.object(
                pipeline, "film_grain", **how))
            stack.enter_context(unittest.mock.patch.object(cuts, "attach", return_value=True))
            regrain.regrain_one(target, make_chain(), self.cfg, unittest.mock.Mock())
        return commands

    def _library(self) -> list[str]:
        return sorted(p.name for p in self.library.iterdir())

    def test_the_film_is_read_once_and_the_new_one_written_back(self):
        commands = self._regrain()
        reading = [argv for argv in commands if any(str(self.library) in a for a in argv)]
        # One read takes the film apart; the remux works from the parts.
        self.assertEqual(len(reading), 1)
        self.assertEqual(reading[0][reading[0].index("-i") + 1], str(self.output))
        self.assertIn(str(pipeline._work_dir(self.source, self.cfg) / regrain.REST), reading[0])
        # The estimate and calibration decode the staged source, gone by the remux.
        read = self.film_grain.call_args.args[0]
        self.assertEqual(read.path.parent, self.cfg.staging_dir)
        self.assertIs(self.source_at_remux, False)
        self.assertEqual(self.output.read_bytes(), b"the new film")
        self.assertEqual(self._library(), ["Film.av1.mkv", "Film.mkv"])
        self.assertEqual(list(self.cfg.staging_dir.iterdir()), [])

    def test_without_staging_it_works_in_place(self):
        self.cfg.staging.enabled = False
        commands = self._regrain()
        self.assertTrue(commands[-1][-1].endswith(regrain.PART_SUFFIX))
        self.assertEqual(self.output.read_bytes(), b"the new film")
        self.assertEqual(self._library(), ["Film.av1.mkv", "Film.mkv"])

    def test_a_stop_during_the_grain_keeps_the_staged_source(self):
        with self.assertRaises(KeyboardInterrupt):
            self._regrain(film_grain=KeyboardInterrupt())
        self.assertEqual(self.output.read_bytes(), b"the old film")
        digest = pipeline.source_digest(self.source)
        self.assertEqual([p.name for p in self.cfg.staging_dir.iterdir()], [f"{digest}.mkv"])
        work = pipeline._work_dir(self.source, self.cfg)
        self.assertTrue((work / regrain.VIDEO_NOTE).is_file())

    def test_a_stop_at_the_end_leaves_the_old_film_and_nothing_half_made(self):
        with self.assertRaises(KeyboardInterrupt):
            self._regrain(check=KeyboardInterrupt())
        self.assertEqual(self.output.read_bytes(), b"the old film")
        self.assertEqual(list(self.cfg.staging_dir.iterdir()), [])
        self.assertEqual(self._library(), ["Film.av1.mkv", "Film.mkv"])

    def test_a_failure_drops_the_staged_source(self):
        with self.assertRaises(CommandError):
            self._regrain(film_grain=CommandError(["x"], 1, "boom"))
        self.assertEqual(list(self.cfg.staging_dir.iterdir()), [])
        self.assertEqual(self.output.read_bytes(), b"the old film")

    def test_no_room_puts_the_new_film_beside_the_old_one(self):
        self.cfg.staging_dir.mkdir(parents=True)
        roomy = shutil.disk_usage(self.cfg.staging_dir)
        with unittest.mock.patch.object(regrain.shutil, "disk_usage",
                                        return_value=roomy._replace(free=4)):
            path = regrain._new_film_path(self.output, self.source, self.cfg, local=True)
        self.assertEqual(path, self.output.with_name(self.output.name + regrain.PART_SUFFIX))
        self.assertEqual(regrain._new_film_path(self.output, self.source, self.cfg, True).parent,
                         self.cfg.staging_dir)

    def test_a_film_taken_apart_whole_is_used_again_while_it_is_the_films(self):
        work = self.tmp / "w"
        work.mkdir()
        found = [regrain.Track(0, "video", "av1", False), regrain.Track(1, "audio", "ac3", False)]

        def split(argv, *_a, **_k):
            for name in (regrain.VIDEO, regrain.REST):
                (work / name).write_bytes(b"part")

        with unittest.mock.patch.object(regrain, "run", side_effect=split) as ran:
            video, rest = regrain.take_apart(make_chain(), self.output, found, found[0], work)
            self.assertEqual((video, rest), (work / regrain.VIDEO, work / regrain.REST))
            regrain.take_apart(make_chain(), self.output, found, found[0], work)
            self.assertEqual(ran.call_count, 1)
            os.utime(self.output, ns=(0, 0))    # the film changed since
            regrain.take_apart(make_chain(), self.output, found, found[0], work)
            self.assertEqual(ran.call_count, 2)
            (work / regrain.REST).unlink()      # a part gone: taken apart again
            regrain.take_apart(make_chain(), self.output, found, found[0], work)
            self.assertEqual(ran.call_count, 3)
            # Stopped while taking it apart: the note goes first, so no half parts.
            ran.side_effect = KeyboardInterrupt
            os.utime(self.output, ns=(1, 1))
            with self.assertRaises(KeyboardInterrupt):
                regrain.take_apart(make_chain(), self.output, found, found[0], work)
            self.assertFalse((work / regrain.VIDEO_NOTE).exists())
        alone = [found[0]]
        with unittest.mock.patch.object(regrain, "run", side_effect=split):
            _, rest = regrain.take_apart(make_chain(), self.output, alone, alone[0], work)
        self.assertIsNone(rest)

    def test_cuts_found_are_kept_for_a_rerun(self):
        work = self.tmp / "w"
        work.mkdir()
        with unittest.mock.patch.object(regrain, "find_scenes", return_value=[0, 40]) as found:
            first = regrain.scene_starts(make_info(), make_chain(), self.cfg, work, 100)
            again = regrain.scene_starts(make_info(), make_chain(), self.cfg, work, 100)
            self.assertEqual(found.call_count, 1)
            regrain.scene_starts(make_info(), make_chain(), self.cfg, work, 120)
            self.assertEqual(found.call_count, 2)    # for another frame count: found again
        self.assertEqual(first, [0, 40])
        self.assertEqual(again, [0, 40])

    def test_put_back_leaves_the_old_film_when_the_copy_fails(self):
        new = self.tmp / "new.mkv"
        new.write_bytes(b"the new film")
        with unittest.mock.patch.object(regrain.shutil, "copyfile",
                                        side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            regrain.put_back(new, self.output)
        self.assertEqual(self.output.read_bytes(), b"the old film")
        self.assertFalse((self.library / ("Film.av1.mkv" + regrain.PART_SUFFIX)).exists())


class TestRegrainScenes(unittest.TestCase):
    def test_regrain_falls_back_to_av1an_when_the_detector_fails(self):
        chain = make_chain(scenechange="av-scenechange")
        with unittest.mock.patch.object(cuts, "detect", side_effect=essential.Mismatch("x")), \
                unittest.mock.patch.object(scenes, "prepare", return_value=Path("/s.json")), \
                unittest.mock.patch.object(scenes, "detect") as av1an, \
                unittest.mock.patch.object(scenes, "starts", return_value=[0, 7]):
            found = regrain.find_scenes(make_info(), chain, config.Config(), Path("/w"))
        av1an.assert_called_once()
        self.assertEqual(found, [0, 7])
        with unittest.mock.patch.object(cuts, "detect", return_value=[0, 3]) as fast:
            self.assertEqual(regrain.find_scenes(make_info(), chain, config.Config(),
                                                 Path("/w")), [0, 3])
        fast.assert_called_once()

    def test_a_run_keeps_av1ans_list_in_the_film(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "scenes.json"
            listing.write_text(json.dumps({"frames": 30, "scenes": [
                {"start_frame": 0, "end_frame": 12}, {"start_frame": 12, "end_frame": 30}]}))
            with unittest.mock.patch.object(cuts, "attach") as attach:
                pipeline._keep_scenes(listing, Path("/out.part"), make_chain(), Path(tmp))
            attach.assert_called_once_with(unittest.mock.ANY, Path("/out.part"), [0, 12], 30,
                                           Path(tmp), "")
            with unittest.mock.patch.object(cuts, "attach") as attach:
                pipeline._keep_scenes(Path(tmp) / "none.json", Path("/o"), make_chain(), Path(tmp))
            attach.assert_not_called()


class TestFilmGrainRoute(unittest.TestCase):
    def test_frames_that_do_not_match_fall_back_to_the_samples(self):
        cfg = config.Config()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with unittest.mock.patch.object(essential, "frames_table",
                                            side_effect=essential.Mismatch("short")), \
                    unittest.mock.patch.object(
                        pipeline, "_sampled_grain",
                        return_value=(work / "g.mkv", work / "t.tbl")) as old:
                video, table = pipeline.film_grain(make_info(), work / "e.mkv", [0],
                                                   make_chain(), cfg, work)
        old.assert_called_once()
        self.assertEqual((video.name, present(table).name), ("g.mkv", "t.tbl"))

    def test_a_clean_film_ships_the_plain_encode(self):
        cfg = config.Config()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with unittest.mock.patch.object(essential, "frames_table", return_value=work / "f"), \
                    unittest.mock.patch.object(scenegrain, "apply",
                                               return_value=scenegrain.Result(False, None, None)):
                video, table = pipeline.film_grain(make_info(), work / "e.mkv", [0],
                                                   make_chain(), cfg, work)
        self.assertEqual((video.name, table), ("e.mkv", None))
