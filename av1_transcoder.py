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
        Advanced film grain detection using FFT analysis, temporal noise analysis,
        and specialized filtering to detect characteristic film grain patterns.
        """
        try:
            if self.duration <= 0:
                self.has_film_grain = False
                return
            
            # Check for required libraries
            try:
                import numpy as np
                import cv2
                import tempfile
                import random
            except ImportError:
                logger.warning("Advanced film grain detection requires numpy and opencv. Using simplified detection.")
                self._detect_film_grain_simplified()
                return
                
            # Use temporary directory for frame extraction
            with tempfile.TemporaryDirectory() as temp_dir:
                logger.info(f"Using advanced film grain detection for {self.file_path}")
                print(f"Analyzing film grain using advanced methods...")
                
                # Frame extraction settings
                num_frames = 10  # Number of frames to analyze
                start_time = self.duration * 0.1
                end_time = self.duration * 0.9
                
                # Select random timestamps for analysis
                timestamps = sorted([random.uniform(start_time, end_time) for _ in range(num_frames)])
                
                # Metrics to track
                grain_scores = []
                temporal_scores = []
                fft_scores = []
                
                # Extract frames
                frame_files = []
                for i, timestamp in enumerate(timestamps):
                    frame_path = os.path.join(temp_dir, f"frame_{i:03d}.png")
                    cmd = [
                        'jellyfin-ffmpeg',
                        '-ss', str(timestamp),
                        '-i', self.file_path,
                        '-vframes', '1',
                        '-q:v', '1',
                        frame_path
                    ]
                    subprocess.run(cmd, capture_output=True)
                    if os.path.exists(frame_path):
                        frame_files.append(frame_path)
                
                if len(frame_files) < 2:
                    logger.warning("Not enough frames extracted for analysis")
                    self.has_film_grain = False
                    return
                
                # 1. FFT Analysis for high frequency noise patterns
                for i, frame_file in enumerate(frame_files):
                    # Load image
                    img = cv2.imread(frame_file, cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    
                    # Apply FFT
                    f_transform = np.fft.fft2(img)
                    f_shift = np.fft.fftshift(f_transform)
                    magnitude_spectrum = 20 * np.log(np.abs(f_shift) + 1)
                    
                    # Analyze high frequency content (typical of film grain)
                    h, w = magnitude_spectrum.shape
                    center_y, center_x = h // 2, w // 2
                    
                    # Define regions (center is low frequency, edges are high frequency)
                    mask_high = np.ones_like(magnitude_spectrum, dtype=np.uint8)
                    cv2.circle(mask_high, (center_x, center_y), min(h, w) // 4, 0, -1)
                    
                    # Calculate high frequency energy
                    high_freq_energy = np.mean(magnitude_spectrum * mask_high)
                    
                    # Save FFT score (higher values indicate more grain)
                    fft_score = high_freq_energy / np.mean(magnitude_spectrum)
                    fft_scores.append(fft_score)
                    
                    # Report FFT analysis
                    print(f"Frame {i+1}: FFT high frequency score: {fft_score:.2f}")
                
                # 2. Temporal analysis (noise pattern consistency between frames)
                for i in range(len(frame_files) - 1):
                    img1 = cv2.imread(frame_files[i], cv2.IMREAD_GRAYSCALE)
                    img2 = cv2.imread(frame_files[i+1], cv2.IMREAD_GRAYSCALE)
                    
                    if img1 is None or img2 is None:
                        continue
                    
                    # Resize if dimensions don't match
                    if img1.shape != img2.shape:
                        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
                    
                    # Calculate frame difference
                    diff = cv2.absdiff(img1, img2)
                    
                    # Apply threshold to isolate noise
                    _, noise_mask = cv2.threshold(diff, 15, 255, cv2.THRESH_BINARY)
                    
                    # Calculate noise statistics
                    noise_ratio = np.count_nonzero(noise_mask) / noise_mask.size
                    noise_mean = np.mean(diff)
                    noise_std = np.std(diff)
                    
                    # Film grain typically has consistent fine noise pattern
                    temporal_score = (noise_ratio * 10) + (noise_std / 5)
                    temporal_scores.append(temporal_score)
                    
                    print(f"Frame pair {i+1}-{i+2}: Temporal noise score: {temporal_score:.2f}")
                
                # 3. Texture analysis with specialized filters
                for i, frame_file in enumerate(frame_files):
                    img = cv2.imread(frame_file, cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    
                    # Apply Laplacian filter (edge detection)
                    laplacian = cv2.Laplacian(img, cv2.CV_64F)
                    
                    # Calculate texture metrics
                    texture_energy = np.sum(np.abs(laplacian)) / img.size
                    texture_std = np.std(laplacian)
                    
                    # Combine metrics for grain score
                    grain_score = texture_energy * 50 + texture_std / 10
                    grain_scores.append(grain_score)
                    
                    print(f"Frame {i+1}: Texture grain score: {grain_score:.2f}")
                
                # 4. Analyze metadata (codec, format, etc.)
                metadata_score = 0
                grain_prone_codecs = ['mpeg2video', 'h264', 'prores', 'dnxhd']
                if self.codec in grain_prone_codecs:
                    metadata_score += 1
                
                # Combined analysis
                # Normalize scores
                if fft_scores:
                    avg_fft = sum(fft_scores) / len(fft_scores)
                else:
                    avg_fft = 0
                
                if temporal_scores:
                    avg_temporal = sum(temporal_scores) / len(temporal_scores)
                else:
                    avg_temporal = 0
                
                if grain_scores:
                    avg_grain = sum(grain_scores) / len(grain_scores)
                else:
                    avg_grain = 0
                
                # Calculate weighted score
                # Each method contributes differently to the final decision
                weighted_score = (avg_fft * 0.4) + (avg_temporal * 0.3) + (avg_grain * 0.3) + (metadata_score * 0.2)
                
                # Threshold for detection (calibrated based on testing)
                self.has_film_grain = weighted_score >= 1.2
                
                # Calculate confidence
                confidence = min(100, weighted_score * 50)
                
                # Report final result
                result = "Detected" if self.has_film_grain else "Not detected"
                summary = (
                    f"Film grain detection result: {result}\n"
                    f"Confidence: {confidence:.1f}%\n"
                    f"FFT score: {avg_fft:.2f}, Temporal score: {avg_temporal:.2f}, "
                    f"Texture score: {avg_grain:.2f}, Metadata score: {metadata_score:.1f}"
                )
                
                logger.info(summary)
                print(summary)
                
        except Exception as e:
            logger.warning(f"Advanced film grain detection failed: {e}. Falling back to simplified method.")
            # Fall back to simplified detection
            self._detect_film_grain_simplified()
    
    def _detect_film_grain_simplified(self) -> None:
        """Simplified film grain detection method as fallback."""
        try:
            if self.duration <= 0:
                self.has_film_grain = False
                return
                
            import random
            
            # Simplified detection based on filename and codec
            lower_filename = self.file_path.lower()
            grain_indicators = ['grain', 'film', '35mm', '16mm', 'analog', 'vintage']
            grain_prone_codecs = ['mpeg2video', 'h264', 'prores', 'dnxhd']
            
            # Simple heuristic detection
            has_grain = False
            
            # Check filename for indicators
            for indicator in grain_indicators:
                if indicator in lower_filename:
                    has_grain = True
                    break
                    
            # Check codec
            if self.codec in grain_prone_codecs:
                has_grain = True
            
            self.has_film_grain = has_grain
            logger.info(f"Simplified film grain detection: {'Detected' if has_grain else 'Not detected'}")
            
        except Exception as e:
            logger.warning(f"Simplified film grain detection error: {e}")
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
                grain_strength = self.config.get('grain_strength', 8)
                cmd.extend([
                    '-svtav1-params', f'film-grain-denoise={grain_strength}'
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
            
            # Improved progress monitoring with console output
            stderr_lines = []
            for line in process.stderr:
                stderr_lines.append(line)
                if "frame=" in line and "fps=" in line:
                    # Print progress directly to console
                    print(line.strip(), flush=True)
                    logger.debug(line.strip())
                # Also print other important ffmpeg status messages
                elif any(info in line for info in ["Stream mapping", "Duration:", "Output #0"]):
                    print(line.strip(), flush=True)
            
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
    parser.add_argument('--detect-only', action='store_true', help='Only detect film grain without transcoding')
    parser.add_argument('--grain-strength', type=int, default=8, help='Film grain strength (0-16, higher values add more grain)')
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
        'overwrite': args.overwrite,
        'detect_only': args.detect_only,
        'grain_strength': args.grain_strength
    }
    
    # Create a simple function to just detect film grain
    def detect_grain_only(file_path):
        try:
            print(f"\nAnalyzing file: {file_path}")
            video_info = VideoInfo(file_path)
            result = "DETECTED" if video_info.has_film_grain else "NOT DETECTED"
            print(f"Film grain detection result for {file_path}: {result}\n")
            return video_info.has_film_grain
        except Exception as e:
            logger.error(f"Error during film grain detection: {e}")
            return False
    
    # If detect-only mode, process files but just for detection
    if args.detect_only:
        if os.path.isfile(args.input):
            # Process single file
            detect_grain_only(args.input)
        else:
            # Process directory
            file_list = []
            
            if args.recursive:
                for root, _, files in os.walk(args.input):
                    for file in files:
                        if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v')):
                            file_list.append(os.path.join(root, file))
            else:
                for file in os.listdir(args.input):
                    if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v')):
                        file_list.append(os.path.join(args.input, file))
            
            if not file_list:
                logger.warning(f"No video files found in {args.input}")
                sys.exit(0)
            
            print(f"Found {len(file_list)} video files to analyze")
            
            # Process files for grain detection only
            grain_detected = 0
            for file_path in file_list:
                if detect_grain_only(file_path):
                    grain_detected += 1
            
            print(f"\nAnalysis complete: Film grain detected in {grain_detected}/{len(file_list)} files")
            
        # Exit after detection is complete
        sys.exit(0)
    
    # Continue with normal transcoding if not in detect-only mode
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
