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

### Advanced Options

```
usage: av1_transcoder.py [-h] -i INPUT [-o OUTPUT_DIR] [-r] [-p PRESET] [-c CRF]
                         [-f] [--parallel] [-w MAX_WORKERS] [--overwrite]
                         [--list LIST]

AV1 Automatic Transcoder

options:
  -h, --help            show this help message and exit
  -i INPUT, --input INPUT
                        Input file or directory
  -o OUTPUT_DIR, --output-dir OUTPUT_DIR
                        Output directory
  -r, --recursive       Recursively process directories
  -p PRESET, --preset PRESET
                        AV1 encoding preset (0-13, 0=highest quality/slowest,
                        13=lowest quality/fastest)
  -c CRF, --crf CRF     AV1 CRF value (0-63, lower = higher quality)
  -f, --enable-film-grain
                        Enable film grain synthesis detection and application
  --parallel            Enable parallel processing
  -w MAX_WORKERS, --max-workers MAX_WORKERS
                        Maximum number of parallel workers
  --overwrite           Overwrite existing output files
  --list LIST           File containing list of videos to transcode
```

## Example Commands

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

## Output

Transcoded videos will be named with "_av1" appended to the original filename and saved with the .mkv extension.

The script also creates a `transcoder.log` file with detailed information about the transcoding process.
