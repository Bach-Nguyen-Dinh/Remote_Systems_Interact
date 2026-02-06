#!/bin/bash

# Change to the topaz_cam directory
cd /home/user/live_demo/topaz_cam || { echo "Failed to change directory"; exit 1; }

bash ./turn_on_core_1_2_3.sh

# Load the UVC video kernel module
insmod ./uvcvideo.ko

# Run the iris USB webcam script
bash ./iris_usb_webcam_standalone.sh &

# Wait for 2 seconds
sleep 2

# Run the USB webcam capture script
bash ./usb_webcam_capture.sh || { echo "Failed to run usb_webcam_capture.sh"; exit 1; }

echo "Webcam setup completed successfully"