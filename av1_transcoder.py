#!/usr/bin/env python3
"""
AV1 Automatic Transcoder

This script automatically transcodes videos to AV1 format using jellyfin-ffmpeg with Rockchip MPP
hardware acceleration. It detects input resolution, color depth, and optionally film grain.
"""

import os
import sys
import subprocess
import json
import argparse
import logging
from pathlib import Path
import concurrent.futures
from typing import List, Dict, Any, Optional

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('transcoder.log')
    ]
)
logger = logging.getLogger('av1_transcoder')

class VideoInfo:
    """Class to store and handle video information"""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.width = 0
        self.height = 0
        self.bit_depth = 8
        self.has_film_grain = False
        self.duration = 0
        self.codec = ""
        self._probe_video()
        
    def _probe_video(self) -> None:
        """Probe video file to extract information"""
        try:
            cmd = ['jellyfin-ffprobe', '-v', 'error', '-select_streams', 'v:0', 
                  '-show_entries', 'stream=width,height,pix_fmt,codec_name:format=duration,format_name', 
                  '-of', 'json', self.file_path]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(result.stdout)
            
            # Extract basic video information
            stream_info = info.get('streams', [{}])[0]
            self.width = stream_info.get('width', 0)
            self.height = stream_info.get('height', 0)
            self.codec = stream_info.get('codec_name', "")
            
            # Determine bit depth from pixel format
            pix_fmt = stream_info.get('pix_fmt', '')
            self.bit_depth = 10 if '10' in pix_fmt else 8
            
            # Extract duration and format name
            format_info = info.get('format', {})
            self.duration = float(format_info.get('duration', 0))
            self.format_name = format_info.get('format_name', '').split(',')[0]  # Get first format if multiple
            
            # Detect film grain (simplified approach)
            self._detect_film_grain()
            
            logger.info(f"Video info: {self.width}x{self.height}, {self.bit_depth}-bit, codec: {self.codec}, format: {self.format_name}")
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Error probing video: {e}")
            raise
        except json.JSONDecodeError:
            logger.error("Error parsing ffprobe output")
            raise
            
    def _detect_film_grain(self) -> None:
        """
        Detect film grain in the video by analyzing multiple frames.
        
        This method extracts 20 random frames from the video, analyzes
        their noise patterns, and calculates a confidence score for
        film grain presence. Film grain is reported if confidence
        exceeds 60%.
        """
        try:
            if self.duration <= 0:
                self.has_film_grain = False
                return
                
            import random
            
            # Number of frames to analyze
            num_frames = 20
            
            # Generate random timestamps throughout the video
            # Avoid the first and last 10% of the video
            start_time = self.duration * 0.1
            end_time = self.duration * 0.9

            # If the video is too short, just use the whole duration
            if self.duration < 600:
                start_time = 0
                end_time = self.duration
            
            # Track frames with detected grain
            grain_detected_count = 0
            grain_confidence = 0.0

            # Generate random timestamps for frame analysis
            timestamps = [random.uniform(start_time, end_time) for _ in range(num_frames)]
            
            logger.info(f"Analyzing {num_frames} frames for film grain in {self.file_path}")
            
            for i, timestamp in enumerate(timestamps):
                # Use noise estimation and histogram analysis for grain detection
                cmd = [
                    'jellyfin-ffmpeg', '-ss', str(timestamp), '-i', self.file_path,
                    '-vframes', '1', '-vf', 'format=gray,signalstats=stat=astd:outfilter=astats',
                    '-f', 'null', '-'
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                # Analyze output for noise patterns
                # Look for standard deviation and noise metrics in the output
                stderr = result.stderr.lower()
                
                # Simplified heuristics for grain detection
                # High ASTD (average standard deviation) often indicates film grain
                has_grain_indicators = False
                
                # Check for high standard deviation or noise metrics
                if "astd:" in stderr:
                    try:
                        # Extract the ASTD value
                        astd_parts = [part for part in stderr.split() if "astd:" in part]
                        if astd_parts:
                            astd_str = astd_parts[0].split("astd:")[1].strip()
                            astd_value = float(astd_str)
                            
                            # Higher ASTD values typically indicate more grain/noise
                            # Threshold is empirical and may need tuning
                            if astd_value > 5.0:  # Typical threshold for film grain
                                has_grain_indicators = True
                    except (ValueError, IndexError):
                        pass
                
                # Additional checks based on codec
                if self.codec in ['mpeg2video', 'mpeg4'] and "noise" in stderr:
                    has_grain_indicators = True
                
                if has_grain_indicators:
                    grain_detected_count += 1
                
                # Log progress every 5 frames
                if (i + 1) % 5 == 0:
                    logger.debug(f"Film grain analysis: {i + 1}/{num_frames} frames analyzed")
            
            # Calculate confidence as percentage of frames with detected grain
            grain_confidence = (grain_detected_count / num_frames) * 100.0
            
            # Set result based on confidence threshold
            self.has_film_grain = grain_confidence >= 60.0
            
            logger.info(f"Film grain detection result: {'Detected' if self.has_film_grain else 'Not detected'} "
                        f"(confidence: {grain_confidence:.1f}%, frames with grain: {grain_detected_count}/{num_frames})")
            
        except Exception as e:
            logger.warning(f"Error during film grain detection: {e}")
            # Default to no film grain if detection fails
            self.has_film_grain = False

class Transcoder:
    """Class to handle video transcoding"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
    def transcode_video(self, input_file: str, output_file: Optional[str] = None) -> bool:
        """
        Transcode a video file to AV1 format
        
        Args:
            input_file: Path to input video file
            output_file: Path to output file (if None, will be generated)
            
        Returns:
            bool: True if transcoding was successful
        """
        try:
            video_info = VideoInfo(input_file)
            
            if not output_file:
                input_path = Path(input_file)
                output_file = str(input_path.with_stem(f"{input_path.stem}_av1").with_suffix('.mkv'))
            
            # Skip if output already exists and overwrite not enabled
            if os.path.exists(output_file) and not self.config.get('overwrite', False):
                logger.info(f"Output file {output_file} already exists, skipping")
                return False
            
            # Build ffmpeg command with correct hardware acceleration flags
            cmd = [
                'jellyfin-ffmpeg',
            ]
            
            # Add input format if detected
            if hasattr(video_info, 'format_name') and video_info.format_name:
                cmd.extend(['-f', video_info.format_name])
                
            # Add hardware acceleration options
            cmd.extend([
                '-hwaccel', 'rkmpp',
                '-hwaccel_output_format', 'drm_prime',
                '-afbc', 'rga',
            #    '-hwaccel_device', self.config.get('device', '/dev/dri/card0'),
                '-i', input_file,
                '-c:a', 'copy'
            ])
            
            # Add video scaling and mapping options with correct filters
            cmd.extend([
                '-vf', f'scale_rkrga=w={video_info.width}:h={video_info.height}:format=nv12,hwmap=mode=read,format=nv12',
                '-map', '0',
                '-map_metadata', '-1',
                '-map_chapters', '-1',
                '-threads', '0'
            ])
            
            # Add video codec options
            # For 8-bit, the pixel format should be 'yuv420p'
            # For 10-bit, the pixel format should be 'yuv420p10le'
            pixel_format = 'yuv420p10le' if video_info.bit_depth == 10 else 'yuv420p'
            cmd.extend([
                '-c:v', 'libsvtav1',
                '-pix_fmt', pixel_format,
                '-preset', str(self.config.get('preset', 6)),
                '-crf', str(self.config.get('crf', 23))
            ])
            
            # Add film grain synthesis if detected and enabled
            if video_info.has_film_grain and self.config.get('enable_film_grain', True):
                cmd.extend([
                    '-svtav1-params', 'film-grain-denoise=8'
                ])
            
            # Add output file
            cmd.append(output_file)
            
            # Log the command
            print("FFmpeg command:", ' '.join(cmd))
            logger.info(f"Transcoding command: {' '.join(cmd)}")
            
            # Run the transcoding process with proper output capture
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1  # Line buffered
            )
            
            # Improved progress monitoring
            stderr_lines = []
            for line in process.stderr:
                stderr_lines.append(line)
                if "frame=" in line and "fps=" in line:
                    logger.debug(line.strip())
            
            # Get return code and handle errors better
            returncode = process.wait()
            
            if returncode == 0:
                logger.info(f"Successfully transcoded {input_file} to {output_file}")
                return True
            else:
                error_output = '\n'.join(stderr_lines[-10:])  # Show last 10 lines of error
                logger.error(f"Transcoding failed with return code {returncode}. Error: {error_output}")
                return False
                
        except Exception as e:
            logger.error(f"Error during transcoding: {e}")
            return False
    
    def process_file_list(self, file_list: List[str], output_dir: Optional[str] = None) -> None:
        """
        Process a list of files for transcoding
        
        Args:
            file_list: List of input file paths
            output_dir: Output directory (if None, same as input)
        """
        successful = 0
        failed = 0
        
        # Create output directory if specified and doesn't exist
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # Process files sequentially or in parallel
        if self.config.get('parallel', False):
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.get('max_workers', 1)) as executor:
                futures = []
                
                for input_file in file_list:
                    output_file = None
                    if output_dir:
                        input_path = Path(input_file)
                        output_file = str(Path(output_dir) / f"{input_path.stem}_av1.mkv")
                        
                    futures.append(executor.submit(self.transcode_video, input_file, output_file))
                
                for future in concurrent.futures.as_completed(futures):
                    if future.result():
                        successful += 1
                    else:
                        failed += 1
        else:
            for input_file in file_list:
                output_file = None
                if output_dir:
                    input_path = Path(input_file)
                    output_file = str(Path(output_dir) / f"{input_path.stem}_av1.mkv")
                    
                if self.transcode_video(input_file, output_file):
                    successful += 1
                else:
                    failed += 1
        
        logger.info(f"Transcoding complete: {successful} successful, {failed} failed")

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description='AV1 Automatic Transcoder')
    
    parser.add_argument('-i', '--input', required=True, help='Input file or directory')
    parser.add_argument('-o', '--output-dir', help='Output directory')
    parser.add_argument('-r', '--recursive', action='store_true', help='Recursively process directories')
    parser.add_argument('-p', '--preset', type=int, default=6, help='AV1 encoding preset (0-13, 0=highest quality/slowest, 13=lowest quality/fastest)')
    parser.add_argument('-c', '--crf', type=int, default=23, help='AV1 CRF value (0-63, lower = higher quality)')
    parser.add_argument('-g', '--enable-film-grain', action='store_true', help='Enable film grain synthesis detection and application')
    parser.add_argument('--parallel', action='store_true', help='Enable parallel processing')
    parser.add_argument('-w', '--max-workers', type=int, default=1, help='Maximum number of parallel workers')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite existing output files')
    parser.add_argument('--list', help='File containing list of videos to transcode')
    
    args = parser.parse_args()
    
    config = {
        'preset': args.preset,
        'crf': args.crf,
        'enable_film_grain': args.enable_film_grain,
        'parallel': args.parallel,
        'max_workers': args.max_workers,
        'overwrite': args.overwrite
    }
    
    transcoder = Transcoder(config)
    
    # Process file list if provided
    if args.list:
        try:
            with open(args.list, 'r') as f:
                file_list = [line.strip() for line in f if line.strip()]
            transcoder.process_file_list(file_list, args.output_dir)
            return
        except Exception as e:
            logger.error(f"Error reading file list: {e}")
            sys.exit(1)
    
    # Process input file or directory
    input_path = args.input
    
    if not os.path.exists(input_path):
        logger.error(f"Input path does not exist: {input_path}")
        sys.exit(1)
    
    if os.path.isfile(input_path):
        # Process single file
        transcoder.transcode_video(input_path, None if not args.output_dir else 
                                  str(Path(args.output_dir) / f"{Path(input_path).stem}_av1.mkv"))
    else:
        # Process directory
        file_list = []
        
        if args.recursive:
            for root, _, files in os.walk(input_path):
                for file in files:
                    if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v')):
                        file_list.append(os.path.join(root, file))
        else:
            for file in os.listdir(input_path):
                if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v')):
                    file_list.append(os.path.join(input_path, file))
        
        if not file_list:
            logger.warning(f"No video files found in {input_path}")
            sys.exit(0)
        
        logger.info(f"Found {len(file_list)} video files to process")
        transcoder.process_file_list(file_list, args.output_dir)

if __name__ == "__main__":
    main()
