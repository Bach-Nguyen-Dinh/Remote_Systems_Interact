#!/bin/bash
# Show the camera feed coming from the Topaz board. Quit it by pressing q IN THE
# VIDEO WINDOW -- display_demo.py only checks for that key, so killing it any
# other way leaves the board's capture script running with nowhere to send to.
#
# /usr/bin/python3 explicitly, NOT bare python3: run_hpc.sh's .venv-hpc is first
# on PATH in the usual shell here, and that venv has no cv2 -- the bare name
# would pick it and die on `import cv2` before a window ever appeared.
exec /usr/bin/python3 ~/live_demo/target_plugged_camera/display_demo.py
