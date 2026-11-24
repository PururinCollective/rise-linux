#!/bin/bash
cd "$(dirname "$0")"

./vpnserver stop &

pkill dnsmasq &

iptables -F
iptables -t nat -F
iptables -t mangle -F

sysctl -w net.ipv4.ip_forward=0

echo "VPN server has been stopped!"
exit 0
