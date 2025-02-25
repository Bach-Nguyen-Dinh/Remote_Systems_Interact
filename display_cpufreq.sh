#!/bin/bash

# Ensure the script is run as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root."
   exit 1
fi

watch -n 1 'cat /proc/cpuinfo | grep "MHz"' 
done
