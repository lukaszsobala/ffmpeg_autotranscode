# FFmpeg AV1 Auto-Transcoder

This script automatically transcodes videos to AV1 format using jellyfin-ffmpeg with Rockchip MPP hardware acceleration. It can process individual files, directories of videos, or a list of video files.

## Features

- Automatically detects video resolution and maintains it in output
- Detects bit depth (8-bit or 10-bit) and applies the appropriate pixel format
- Optional film grain detection and synthesis
- Support for batch processing with parallel transcoding
- Detailed logging

## Requirements

- Python 3.6+
- jellyfin-ffmpeg (rkmpp version) with libsvtav1 support
- Rockchip hardware with MPP support

## Usage

Make the script executable:

```bash
chmod +x av1_transcoder.py
```

### Basic Usage

Transcode a single video:

```bash
./av1_transcoder.py -i input.mkv
```

Transcode all videos in a directory:

```bash
./av1_transcoder.py -i /path/to/videos -o /path/to/output
```

### Command-Line Options

| Option/Flag                                 | Description                                                                                         |
|---------------------------------------------|-----------------------------------------------------------------------------------------------------|
| -h, --help                                 | Show help message and exit.                                                                        |
| -i INPUT, --input INPUT                    | Input file or directory to transcode.                                                              |
| -o OUTPUT_DIR, --output-dir OUTPUT_DIR     | Output directory for transcoded files.                                                             |
| -r, --recursive                            | Recursively process all video files in subdirectories.                                             |
| -p PRESET, --preset PRESET                 | AV1 encoding preset (0-12). 0 = highest quality/slowest, 12 = lowest quality/fastest.              |
| -c CRF, --crf CRF                          | AV1 CRF value (0-63). Lower values mean higher quality and larger files.                           |
| -f, --film-grain                           | Enable film grain synthesis for detected film grain.                                               |
| -g, --grain-strength GRAIN_STRENGTH        | Film grain strength (1-50, higher values add more grain). Default: 8.                             |
| --parallel                                 | Enable parallel processing of multiple files.                                                      |
| -w MAX_WORKERS, --max-workers MAX_WORKERS  | Maximum number of parallel workers (used with --parallel).                                         |
| --overwrite                                | Overwrite existing output files if they already exist.                                             |
| --list LIST                                | Path to a text file containing a list of video files to transcode (one per line).                  |
| --verbose                                  | Enable verbose output with detailed statistics.                                                    |
| --debug                                    | Enable debug output for troubleshooting.                                                           |
| --output-format {mkv,mp4,webm}             | Output container format (default: mkv).                                                           |
| --detect-only                              | Only detect film grain without transcoding.                                                        |
| --copy-subtitles                           | Copy subtitle streams (default: True).                                                            |
| --no-subtitles                             | Do not include subtitles in the output.                                                           |
| --transcode-audio                          | Transcode audio to Opus.                                                                          |
| --audio-bitrate AUDIO_BITRATE              | Audio bitrate in kbps when transcoding audio (default: 128).                                      |
| --custom-params CUSTOM_PARAMS              | Additional custom parameters to pass to ffmpeg.                                                    |
| --use-svt-pipe                             | Use SvtAv1EncApp piped workflow instead of jellyfin-ffmpeg.                                        |

### Example Commands

Process a single file with specific quality settings:

```bash
./av1_transcoder.py -i input.mkv -p 5 -c 20 -f
```

Batch process all videos in a directory with 2 parallel workers:

```bash
./av1_transcoder.py -i /videos -o /output -r --parallel -w 2
```

Process a list of videos:

```bash
./av1_transcoder.py --list video_list.txt -o /output
```

Detect film grain only (no transcoding):

```bash
./av1_transcoder.py -i input.mkv --detect-only
```

Transcode with custom audio and output format:

```bash
./av1_transcoder.py -i input.mkv --transcode-audio --audio-bitrate 192 --output-format mp4
```

## Output

Transcoded videos will be named with "_av1" appended to the original filename and saved with the selected extension (default: .mkv).

The script also creates a `transcoder.log` file with detailed information about the transcoding process.
