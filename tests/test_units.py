"""Unit tests for the logic that does not shell out.

Deliberately stdlib-only (unittest, no pytest) so this runs on a bare board:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import locale
import os
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from autotranscode import config, encode, estimate, grain, mux, pipeline, probe, util, verify
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

    def crf_at(self, width: int, height: int = 1080) -> int:
        return encode.effective_crf(make_info(width=width, height=height), self.cfg)

    def test_each_band_shifts_from_the_1080p_anchor(self):
        widths = (3840, 2560, 1920, 1280, 854, 640)
        self.assertEqual([self.crf_at(w) for w in widths], [32, 30, 30, 27, 24, 24])

    def test_a_cropped_scope_print_is_still_its_real_resolution(self):
        # The case that motivated keying on width: both of these are 4K sources
        # cropped to the picture, and their heights would file them as 1080p.
        self.assertEqual(self.crf_at(3836, height=2072), 32)
        self.assertEqual(self.crf_at(3840, height=1600), 32)
        # And the same crop one rung down stays 1080p rather than dropping to 720p.
        self.assertEqual(self.crf_at(1920, height=800), 30)

    def test_the_anchor_moves_the_whole_ladder(self):
        # One knob still controls the library: --crf 28 lowers every band by 2.
        self.cfg.encode.crf = 28
        self.assertEqual([self.crf_at(w) for w in (3840, 1920, 1280)], [30, 28, 25])

    def test_an_empty_ladder_uses_the_plain_crf(self):
        self.cfg.encode.crf_by_width = {}
        self.assertEqual([self.crf_at(w) for w in (3840, 854)], [30, 30])

    def test_rk3588_profile_still_puts_4k_at_the_measured_32(self):
        cfg = config.Config()
        config._merge_profile(cfg, "rk3588")
        self.assertEqual(encode.effective_crf(make_info(width=3836, height=2072), cfg), 32)

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

        def fake_run(argv, **_kwargs):
            seen.append(argv)
            Path(argv[argv.index("-lavfi") + 1].split("stats_file=")[1]).write_text(
                "n:    1  XPSNR y: 40.0000  XPSNR u: 45.0  XPSNR v: 46.0\n")
            return util.Completed(argv, 0, "", "", 0.0)

        original = verify.run
        verify.run = fake_run
        self.addCleanup(setattr, verify, "run", original)
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

    def test_out_of_range_strength_is_rejected(self):
        cfg = config.Config()
        cfg.encode.variance_boost_strength = 5
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


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

    def test_inconclusive_measurements_give_no_answer(self):
        self.assertIsNone(grain.strength_factor(10.0, 5.0, 5.0))    # nothing synthesised
        self.assertIsNone(grain.strength_factor(4.0, 5.0, 9.0))     # encode already busier


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
