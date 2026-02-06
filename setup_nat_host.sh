#!/bin/bash
# Flush NAT and filter tables
sudo iptables -t nat -F
sudo iptables -F

# Enable IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1

# Set up NAT masquerading
sudo iptables -t nat -A POSTROUTING -o wlo1 -j MASQUERADE

# Allow forwarding between interfaces
sudo iptables -A FORWARD -i enx98fc84e12360 -o wlo1 -j ACCEPT
sudo iptables -A FORWARD -i wlo1 -o enx98fc84e12360 -m state --state ESTABLISHED,RELATED -j ACCEPT

sudo ip route replace 10.42.1.0/24 via 10.42.0.101 dev enx98fc84e12360
