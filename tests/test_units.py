"""Unit tests for the logic that does not shell out.

Deliberately stdlib-only (unittest, no pytest) so this runs on a bare board:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autotranscode import config, encode, mux, pipeline
from autotranscode.grain import DENOISE_LADDER, GrainReport, flatten_table
from autotranscode.probe import ContentLight, DolbyVision, MasteringDisplay, MediaInfo, Stream
from autotranscode.state import Ledger, Record
from autotranscode.tools import Toolchain

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
            self.assertEqual(cfg.encode.preset, 4)   # from the rk3588 profile
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


def make_chain(**overrides) -> Toolchain:
    base = dict(
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
        staged = pipeline.stage_source(self._info(), self.cfg)
        self.assertIsNotNone(staged)
        self.assertEqual(staged.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(staged.parent, self.source.parent)
        self.assertTrue(self.source.is_file(), "the source must not be moved")

    def test_two_sources_with_the_same_name_do_not_collide(self):
        other = self.root / "elsewhere" / "Film (1975).mkv"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"y" * 4096)

        first = pipeline.stage_source(self._info(), self.cfg)
        second = pipeline.stage_source(
            make_info(path=other, size_bytes=other.stat().st_size), self.cfg
        )
        self.assertNotEqual(first, second)
        self.assertEqual(second.read_bytes(), b"y" * 4096)

    def test_an_interrupted_runs_copy_is_reused_when_the_size_still_matches(self):
        staged = pipeline.stage_source(self._info(), self.cfg)
        # Mark the copy so we can tell a reuse from a fresh copy.
        staged.write_bytes(b"z" * 4096)
        again = pipeline.stage_source(self._info(), self.cfg)
        self.assertEqual(again, staged)
        self.assertEqual(again.read_bytes(), b"z" * 4096, "should have been reused")

    def test_a_stale_copy_of_the_wrong_size_is_replaced(self):
        staged = pipeline.stage_source(self._info(), self.cfg)
        staged.write_bytes(b"truncated")
        again = pipeline.stage_source(self._info(), self.cfg)
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
