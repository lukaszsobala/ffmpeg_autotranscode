#!/bin/bash
# Simple wrapper script for av1_transcoder.py

# Make sure the Python script is executable
chmod +x "$(dirname "$0")/av1_transcoder.py"

# Run the Python script with all arguments passed
"$(dirname "$0")/av1_transcoder.py" "$@"
