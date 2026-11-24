#!/bin/bash
cd "$(dirname "$0")"

nice -n -10 ./vpnserver start
sleep 3

ifconfig tap_0 hw ether 02:00:FF:EE:EE:EE
ifconfig tap_0 198.51.100.1 netmask 255.255.255.0
sleep 1

ip link set dev tap_0 mtu 1400
iptables -t mangle -A FORWARD -o ens18 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
iptables -t mangle -A FORWARD -i ens18 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

sysctl -w net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -s 198.51.100.0/24 -o ens18 -j MASQUERADE
iptables -A FORWARD -i tap_0 -o ens18 -s 198.51.100.0/24 -j ACCEPT
iptables -A FORWARD -i ens18 -o tap_0 -d 198.51.100.0/24 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sleep 1

dnsmasq --no-daemon --interface=tap_0 --bind-interfaces --port=0 --dhcp-range=198.51.100.2,198.51.100.254,255.255.255.0,1h --dhcp-option=3,198.51.100.1 --dhcp-option=6,151.158.198.49,151.158.198.2 &
sleep 1

echo "VPN server has been started!"
exit 0
