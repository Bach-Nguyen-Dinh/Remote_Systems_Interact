#!/bin/bash
# Flush NAT and filter tables
sudo iptables -t nat -F
sudo iptables -F

# Enable IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1

# Set up NAT masquerading
sudo iptables -t nat -A POSTROUTING -o enp6s0 -j MASQUERADE
sudo iptables -t nat -A POSTROUTING -o enp4s0f0 -j MASQUERADE

# Allow forwarding between interfaces
sudo iptables -A FORWARD -i enp4s0f0 -o enp6s0 -j ACCEPT
sudo iptables -A FORWARD -i wlo1 -o enp4s0f0 -m state --state ESTABLISHED,RELATED -j ACCEPT

sudo ip route replace 10.42.1.0/24 via 10.42.1.1 dev enp4s0f0
