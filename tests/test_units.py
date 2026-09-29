"""Unit tests for the logic that does not shell out.

Deliberately stdlib-only (unittest, no pytest) so this runs on a bare board:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import locale
import os
import random
import signal
import sys
import tempfile
import time
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from autotranscode import (
    config, encode, estimate, grain, mux, pipeline, probe, sample, tools, util, verify, watch,
)
from autotranscode.grain import DENOISE_LADDER, GrainReport, flatten_table
from autotranscode.probe import ContentLight, DolbyVision, MasteringDisplay, MediaInfo, Stream
from autotranscode.state import Ledger, Record
from autotranscode.tools import Toolchain
from autotranscode.util import CommandError

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

    def test_auto_profile_follows_the_device_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[machine]\nprofile = "auto"\n')
            for board, profile, preset in ((b"radxa,rock-5b\0rockchip,rk3588\0", "rk3588", 4),
                                           (None, "generic", 6)):
                read = unittest.mock.Mock(return_value=board, side_effect=None if board
                                          else FileNotFoundError())
                with unittest.mock.patch.object(Path, "read_bytes", read):
                    cfg = config.load(path)
                self.assertEqual((cfg.machine.profile, cfg.encode.preset), (profile, preset))

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


class TestGrainReport(unittest.TestCase):
    def test_describe_mentions_the_decision(self):
        report = GrainReport(38.0, [38.0], [1.0], grainy=True, denoise_filter="hqdn3d=1.4:1.0:6:6")
        text = report.describe()
        self.assertIn("38.00", text)
        self.assertIn("grain route", text)


if __name__ == "__main__":
    unittest.main()


def make_chain(**overrides: Any) -> Toolchain:
    base: dict[str, Any] = dict(
        ffmpeg="ffmpeg", ffprobe="ffprobe", av1an="av1an",
        svtav1encapp="SvtAv1EncApp", grav1synth="grav1synth", mkvmerge="mkvmerge",
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

    def test_staged_output_lands_in_staging_with_the_configured_container(self):
        out = pipeline.staged_output_path(self.source, self.cfg)
        self.assertEqual(out.parent, self.cfg.staging_dir)
        self.assertTrue(out.name.endswith(f".out.{self.cfg.output.container}"))

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
        self.addCleanup(setattr, pipeline.mux, "mux", original_mux)
        self.addCleanup(setattr, pipeline.encode, "run_encode", original_encode)
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
        self.cfg.staging.enabled = True
        self.cfg.staging.directory = str(self.root / "staging")

    def _info(self) -> MediaInfo:
        return make_info(path=self.source, size_bytes=self.source.stat().st_size)

    def _stub_encode(self, behaviour):
        original = pipeline.encode.run_encode
        pipeline.encode.run_encode = behaviour
        self.addCleanup(setattr, pipeline.encode, "run_encode", original)

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


class TestCrfLadder(unittest.TestCase):
    """CRF is per coded pixel, so the same number means less quality as size drops."""

    def setUp(self):
        self.cfg = config.Config()
        self.cfg.encode.crf = 30

    def crf_at(self, width: int, height: int = 0) -> int:
        info = make_info(width=width, height=height or width * 9 // 16)
        return encode.effective_crf(info, self.cfg)

    def test_each_band_shifts_from_the_4k_anchor(self):
        widths = (3840, 2560, 1920, 1280, 854, 640)
        self.assertEqual([self.crf_at(w) for w in widths], [30, 37, 37, 27, 24, 24])

    def test_a_cropped_scope_print_is_still_its_real_resolution(self):
        # The case that motivated keying on width: both of these are 4K sources
        # cropped to the picture, and their heights would file them as 1080p.
        self.cfg.encode.crf_by_width["3000"] = 2
        self.assertEqual(self.crf_at(3836, height=2072), 32)
        self.assertEqual(self.crf_at(3840, height=1600), 32)
        # And the same crop one rung down stays 1080p rather than dropping to 720p.
        self.assertEqual(self.crf_at(1920, height=800), 37)

    def test_narrow_hd_is_filed_by_its_height(self):
        # 4:3 HD and 960x720 would drop a band on width alone.
        self.assertEqual(self.crf_at(1440, height=1080), 37)
        self.assertEqual(self.crf_at(960, height=720), 27)

    def test_non_square_pixels_are_filed_by_their_shown_width(self):
        # HDV: 1440x1080 stored, shown 1920 wide. Only the shown width is 1080p
        # here, because the height is cut to a crop that on its own would not be.
        info = make_info(width=1440, height=800, sample_aspect="4:3")
        self.assertEqual(encode.effective_crf(info, self.cfg), 37)
        self.assertEqual(encode.effective_crf(replace(info, sample_aspect=""), self.cfg), 27)

    def test_dvds_stay_sd_whatever_their_pixel_shape(self):
        for width, height, sar in ((720, 576, "16:15"), (720, 576, "64:45"),
                                   (720, 480, "32:27")):
            info = make_info(width=width, height=height, sample_aspect=sar)
            self.assertEqual(encode.effective_crf(info, self.cfg), 24, (width, height, sar))

    def test_the_anchor_moves_the_whole_ladder(self):
        # One knob still controls the library: --crf 28 lowers every band by 2.
        self.cfg.encode.crf = 28
        self.assertEqual([self.crf_at(w) for w in (3840, 1920, 1280)], [28, 35, 25])

    def test_an_empty_ladder_uses_the_plain_crf(self):
        self.cfg.encode.crf_by_width = {}
        self.assertEqual([self.crf_at(w) for w in (3840, 854)], [30, 30])

    def test_rk3588_profile_puts_4k_at_the_measured_30(self):
        cfg = config.Config()
        config._merge_profile(cfg, "rk3588")
        self.assertEqual(encode.effective_crf(make_info(width=3836, height=2072), cfg), 30)

    def test_a_ladder_that_leaves_the_valid_range_is_rejected(self):
        self.cfg.encode.crf = 68
        self.cfg.encode.crf_by_width = {"0": 5}
        with self.assertRaises(config.ConfigError):
            config.validate(self.cfg)

    def test_ladder_keys_must_be_widths(self):
        self.cfg.encode.crf_by_width = {"wide": 1}
        with self.assertRaises(config.ConfigError):
            config.validate(self.cfg)


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

    def test_webm_output_gets_the_webm_muxer(self):
        cfg = config.Config()
        cfg.output.container = "webm"
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(),
                                 Path("/out/Film.av1.webm.part"), make_chain(), cfg)
        self.assertEqual(argv[-3:-1], ["-f", "webm"])


class TestPixelAspect(unittest.TestCase):
    """AV1 carries no pixel shape, so a DVD's must be put back at the mux."""

    def aspect(self, **info: Any) -> list[str]:
        argv = mux.build_command(Path("/w/encoded.mkv"), make_info(**info),
                                 Path("/out/Film.av1.mkv.part"), make_chain(), config.Config())
        return argv[argv.index("-aspect") + 1:argv.index("-aspect") + 2] if "-aspect" in argv else []

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
        chain = make_chain(rkmpp_decoders={"h264_rkmpp", "av1_rkmpp"} if rkmpp is None else rkmpp)
        with unittest.mock.patch.object(verify, "_video_codec", return_value=codec):
            return verify.decode_args(chain, Path("/x.mkv"), grain=grain)

    def test_hardware_decodes_what_it_decodes_exactly(self):
        self.assertEqual(self.decoder("h264", grain=True), ["-c:v", "h264_rkmpp", "-deint", "0"])

    def test_measurements_run_on_the_big_cores(self):
        argv = verify.ffmpeg_argv(make_chain(big_cores="4,5,6,7"))
        self.assertEqual(argv[:3], ["taskset", "-c", "4,5,6,7"])
        self.assertEqual(argv[-2:], ["-filter_complex_threads", "4"])

    def test_a_uniform_cpu_runs_plain_ffmpeg(self):
        self.assertEqual(verify.ffmpeg_argv(make_chain()), ["ffmpeg"])

    def test_av1_stays_on_the_cpu(self):
        # av1_rkmpp drops film grain and decoded a 1202-wide encode wrongly.
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
    """1080p pays for its higher CRF with a lower QP on dark frames."""

    OPTIONS = {"--luminance-qp-bias"}

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
    """4K keeps its grain in flat fog with ac-bias and a weak temporal filter."""

    OPTIONS = {"--ac-bias", "--tf-strength"}

    def flags_at(self, width: int, cfg: config.Config | None = None,
                 options: set[str] = OPTIONS) -> tuple[str | None, str | None]:
        argv = encode.svt_params(make_info(width=width, height=width * 9 // 16),
                                 cfg or config.Config(), make_chain(svt_options=options))
        return tuple(argv[argv.index(flag) + 1] if flag in argv else None
                     for flag in ("--ac-bias", "--tf-strength"))

    def test_only_4k_gets_them_by_default(self):
        self.assertEqual([self.flags_at(w) for w in (3836, 1920, 1280, 720)],
                         [("1.0", "1"), (None, None), (None, None), (None, None)])

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

    FULL = {"--luminance-qp-bias", "--enable-variance-boost", "--ac-bias", "--tf-strength"}

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
        cfg = config.Config()
        self.assertEqual(sample.band_of(make_info(width=3836, height=2072), cfg), "4K")
        self.assertEqual(sample.band_of(make_info(width=1920, height=800), cfg), "1080p")
        self.assertEqual(sample.band_of(make_info(width=1440, height=1080), cfg), "1080p")
        self.assertEqual(sample.band_of(make_info(width=1280, height=720), cfg), "720p")
        self.assertEqual(sample.band_of(make_info(width=720, height=576,
                                                  sample_aspect="64:45"), cfg), "SD")

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

    def _watch(self, sizes: list[int], temp: Path) -> watch.SavingWatch:
        cfg = config.Config()
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
        self.assertEqual(util._progress(1200, 1000, 90), "1,200 of 1,000 frames (100%), 1m30s so far")
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
        self.assertEqual(Ledger(self.root / "state.json").no_saving(self.source), record)

    def test_tried_again_once_the_settings_change(self):
        self._run()
        self.cfg.encode.crf += 6
        self._run()
        self.assertEqual(self.tries, 2)

    def test_other_skips_are_not_remembered_this_way(self):
        self.ledger.record(self.source, Record(source=str(self.source), status="skipped",
                                               reason="already av1"))
        self.assertIsNone(self.ledger.no_saving(self.source))


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
