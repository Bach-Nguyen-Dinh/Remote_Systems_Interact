#!/bin/bash

# Ensure the script is run as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root."
   exit 1
fi

curl -X POST http://localhost:5000/send_message \
     -H "Content-Type: application/json" \
     -d '{"message": "Hello, Target System!"}'

