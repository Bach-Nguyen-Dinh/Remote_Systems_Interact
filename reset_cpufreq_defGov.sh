#!/bin/bash

# Ensure the script is run as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root."
   exit 1
fi

# Set CPU frequency governor and frequency for cores 0, 1, and 2
for core in 0 1 2; do
    cpufreq-set -c $core -g schedutil
done

echo "CPU frequency settings applied."

