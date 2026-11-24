# RISE Linux

```
 ______ _______ _______ _______      _____   __
|   __ \_     _|     __|    ___|    |     |_|__|.-----.--.--.--.--.
|      <_|   |_|__     |    ___|    |       |  ||     |  |  |_   _|
|___|__|_______|_______|_______|    |_______|__||__|__|_____|__.__|
                                 RISE Internet Security Environment
                                              by Pururin Collective
```

> *Silent by design. Secure by nature.*

RISE Linux is a SoftEther VPN server appliance built on Ubuntu Server. It has a
text console and no desktop. It runs the VPN server and the services around it
(DHCP, NAT, the firewall, the certificate and a WebGUI), and nothing else.

It is named after Matsumoto Rise of *Yuru Yuri*. She speaks so quietly that only
Nishigaki Nana can hear her. Like Rise, this server stays quiet: VPN clients
cannot reach it at all, and from the internet it shows only the ports the VPN
needs.

## What it does

| | |
|---|---|
| VPN | SoftEther VPN, compiled from source: SoftEther, L2TP/IPsec, OpenVPN, SSTP |
| Bridge | A real TAP interface (`tap_vpn`) and kernel routing, not SecureNAT |
| VPN IPv4 | Gateway `10.255.255.1/24`, DHCP `10.255.255.2` to `.254`, or your own range |
| /32 clients | Each client gets a `/32` with proxy-ARP and classless routes (DHCP options 121/249) and reaches everything through the server |
| VPN IPv6 | Gateway `fd00:255::1/64`, SLAAC and DHCPv6, prefix advertised off-link, NAT66 through the WAN's global address |
| NAT | Full cone NAT (`xt_FULLCONENAT`, IPv4 and IPv6, built with DKMS), or masquerade if the module is missing |
| Isolation | Clients cannot reach each other: blocked in the firewall and in the hub's access list |
| Lockdown | VPN clients reach DHCP, DHCPv6, neighbour discovery and a ping of the gateway, and nothing else on the server |
| TLS | Let's Encrypt for the VPN domain, renewed automatically and loaded into SoftEther without restarting it. Without a domain, the VPN works by IP address with self-signed certificates |
| TAP MAC | Fixed at install in the VRRP style: IANA's `00:00:5E` OUI, `00:00:5E:00:01:<VRID>` (for example `00:00:5E:00:01:D4`) |
| Tools | `ifconfig` (net-tools), `btop`, `htop`, `iptraf-ng`, `tcpdump`, `conntrack` |
| SSH | Port `9122`. root cannot log in; the installer creates a sudo user |
| WebGUI | `https://<domain>:18080`: status, live traffic and peak speeds, sessions, port forwarding (IPv4/IPv6), DHCP static leases, WAN settings, certificate, services, logs, reboot |
| Console | Unifont 8×16 (the Ubuntu Server installer's font) at 1024×768, doubled to 16×32 on screens 1920 pixels wide or more |
| Boot | A centred, animated banner on tty1, and a live login screen showing addresses and speed |

## Build

On a Debian or Ubuntu host (x86_64) with about 10 GiB free:

```bash
sudo ./build.sh
```

`build.sh` does every step and leaves `out/rise-linux-<version>-<date>-amd64.iso`,
a hybrid ISO for BIOS and UEFI, for DVD or USB:

1. **base**: debootstrap Ubuntu `noble` (minbase, main and universe).
2. **packages**: install `config/packages.list` and the HWE kernel.
3. **softether**: compile SoftEther VPN inside the chroot, into `/opt/softether`.
   The source is `SOFTETHER_SRC` (default `../SoftEtherVPN`). A local checkout is
   copied as it is, uncommitted changes included, and its submodules are fetched
   into the copy.
4. **fullcone**: build `xt_FULLCONENAT` with DKMS, plus its `libipt_`/`libip6t_`
   iptables extensions.
5. **overlay**: copy `rootfs/` and apply the system settings (console, GRUB, sshd,
   services).
6. **squashfs**: pack `casper/filesystem.squashfs`, the kernel and the initrd.
7. **iso**: run `grub-mkrescue` inside the chroot, so the host needs no GRUB or
   xorriso.

Each step is recorded, so an interrupted build picks up where it stopped. Other
commands:

```bash
sudo ./build.sh softether
```

Rebuilds SoftEther and every step after it. `overlay` and `iso` work the same way.

```bash
sudo ./build.sh rebuild
```

```bash
sudo ./build.sh shell
```

```bash
sudo ./build.sh clean
```

Settings are in `config/build.conf`, and each one can be overridden from the
environment. For example, `SOFTETHER_SRC=/path/to/SoftEtherVPN`,
`UBUNTU_SUITE=noble`, `SOFTETHER_OQS=ON` or `RISE_VERSION=1.0.1`.

Write the ISO to a USB stick:

```bash
sudo dd if=out/rise-linux-*.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

## Install

Boot the ISO. The installer runs on tty1, and tty2 to tty6 have a root shell
(Alt+F2) for troubleshooting; its log is `/var/log/rise-installer.log`. It asks
for:

1. **Storage**: the disk to erase.
2. **Network**: the WAN interface (`eth0`), IPv4 by DHCP or static, and IPv6 as
   automatic, static or off.
3. **Profile setup**: your name, the server's hostname, the sudo user and its
   password, and optionally an SSH identity to import (`gh:user` or `lp:user`).
4. **VPN domain**: the name for Let's Encrypt and the WebGUI (different from the
   hostname), an optional e-mail address, and agreement to the Let's Encrypt
   Subscriber Agreement. Leave it empty for a VPN by IP address: SoftEther and
   the WebGUI then use self-signed certificates, port 80 stays closed, and a
   domain can be added later in the WebGUI (System, Certificate).
5. **VPN network**: the defaults or your own addresses; proxy-ARP `/32`
   (recommended) and client isolation.
6. **SoftEther**: the server administrator password (for Server Manager), the IPsec
   pre-shared key, and an optional first VPN user. A simple key such as `vpn`
   is allowed, with a warning: every L2TP/IPsec client shares the key, and
   whoever knows it can pose as the server to those clients. The installer also
   picks the TAP interface's fixed MAC address here.
7. **WebGUI**: its own user and password. It warns you if they match the sudo
   user's or SoftEther's, but lets you keep them.
8. **Summary**: if the WAN address is private, it lists the ports to forward.

The disk gets a BIOS boot partition, an EFI partition and the root filesystem,
with GRUB installed for both, so it boots in either firmware mode.

On the first boot, `rise-firstboot` sets up SoftEther through `vpncmd`: the
admin password, the `VPN` hub, the TAP bridge, L2TP/IPsec, OpenVPN, SSTP, the
first user and hub isolation. SoftEther's ports stay closed in the firewall until
this is done. `rise-cert` then requests the certificate and retries every 30
minutes until it succeeds.

### Ports

If the server is behind NAT, forward these ports to it:

| Port | For |
|---|---|
| TCP 80 | Let's Encrypt (HTTP-01), only with a domain |
| TCP 443, 992, 1194, 5555 | SoftEther, SSTP, OpenVPN over TCP |
| UDP 500, 4500 | L2TP/IPsec |
| UDP 1194 | OpenVPN |
| UDP 1024-65535 | SoftEther UDP acceleration (optional) |
| TCP 9122 | SSH |
| TCP 18080 | WebGUI |

## Design notes

### MTU: 1350

The VPN side's MTU is sized for the largest overhead SoftEther carries:
L2TP/IPsec with NAT-T, over IPv6, over PPPoE.

| | bytes | left |
|---|---:|---:|
| Ethernet MTU | | 1500 |
| PPPoE | 8 | 1492 |
| IPv6 header | 40 | 1452 |
| UDP (NAT-T, 4500) | 8 | 1444 |
| ESP header (SPI, sequence) | 8 | 1436 |
| ESP IV (AES-CBC) | 16 | 1420 |
| ESP ICV (HMAC-SHA256-128) | 16 | 1404 |
| ESP trailer (padding up to 15, plus 2) | 17 | 1387 |
| UDP (L2TP) | 8 | 1379 |
| L2TP header (worst case) | 16 | 1363 |
| PPP | 4 | **1359** |

That leaves 1359 bytes; RISE uses 1350. The other protocols fit inside it.
OpenVPN UDP in TAP mode with HMAC-SHA1 and AES-CBC leaves about 1369, and
SoftEther UDP acceleration about 1380. TCP-based transports are not limited by
this, because TCP segments the stream. The server also tells clients the MTU (in
DHCP option 26 and in the RA MTU option) and clamps TCP MSS in both directions.

### /32 clients and isolation

With `VPN_PROXY_ARP=1`, DHCP gives each client a `/32` address and routes
(options 121 and 249) that put the gateway on-link and route everything else
through it. The TAP interface runs `proxy_arp` with `proxy_arp_pvlan`, so a client
that ARPs for anything gets the server's MAC address. IPv6 does the same: the
router advertisement leaves the on-link (L) bit off, and SLAAC (the A bit) still
forms addresses, which SoftEther's PPP clients (L2TP, SSTP) also use through
IPv6CP. All client traffic therefore passes through the server's firewall. With
`VPN_ISOLATION=1`, the firewall drops client-to-client traffic, and the hub's
access list allows unicast frames only to and from the gateway's MAC address, so
a client with a hand-set `/24` cannot get around it at layer 2.

### Full cone NAT

`xt_FULLCONENAT` provides endpoint-independent mapping and filtering, so peer-to-peer
traffic and game consoles behind the VPN get an open NAT type. Client mappings use
external ports 10000-32767. The server's own ports are excluded from inbound
mapping, so a client can never take SSH, the WebGUI or a SoftEther port, and
local sockets stay in the kernel's 32768-60999 range.

### NAT66 and ULA

VPN clients get `fd00:255::/64` (ULA), translated to the WAN's global address.
Following RFC 6724, a client prefers IPv4 over IPv6 when its only IPv6 address is
a ULA and both are available. IPv6 is used when the destination has no IPv4
address, and for IPv6-only services.

## On the server

```text
rise status            services, addresses, NAT mode, certificate
rise apply             apply /etc/rise/rise.conf after editing it
rise sessions          VPN sessions
rise cert renew        force a certificate renewal
rise psk NEW           change the IPsec pre-shared key
rise firewall show     print the generated ruleset
rise webgui-passwd     set the WebGUI login
rise console-mode 1920x1080 | native | 1024x768
```

| Path | Contents |
|---|---|
| `/etc/rise/rise.conf` | All settings (shell syntax); `rise.conf.default` documents each one |
| `/etc/rise/portfwd.conf` | Port forwards: `family proto port dest dport enabled note` |
| `/etc/rise/dhcp-static.conf` | Static leases: `mac ipv4 ipv6 hostname` |
| `/etc/rise/secrets/` | SoftEther admin password, IPsec key, WebGUI login (scrypt), the self-signed WebGUI certificate; root only |
| `/etc/netplan/50-rise.yaml` | The WAN configuration |
| `/opt/softether/` | SoftEther VPN; `vpn_server.config` is under `libexec/softether/vpnserver/` |
| `/usr/lib/rise/` | firewall, vpn-net, dnsmasq-gen, softether-setup, cert, console, banner, statsd, installer |

To manage users, groups and hub policies, connect SoftEther VPN Server Manager
to `<domain>:443` with the server administrator password.

## The WebGUI

The WebGUI's design follows
[luci-theme-footstrap](https://github.com/VizzleTF/luci-theme-footstrap): a top
bar with pill navigation, cards with hairline rows, monospaced values, thin
progress bars and port cards, in dark and light. It is a small Python server
that uses only the standard library and serves HTTPS on both IPv4 and IPv6.

- Passwords are stored with scrypt.
- The session cookie is `HttpOnly; Secure; SameSite=Strict`, every POST needs a
  CSRF token, and five failed logins block an address for five minutes.
- The Content-Security-Policy is strict, with no inline code.
- Commands run as argument lists (never through a shell), and every value is
  checked before it is written.
- VPN clients cannot reach the WebGUI; the firewall drops all their traffic to
  the server.

## Repository layout

```text
build.sh                 the whole build, base system to ISO
config/build.conf        build settings
config/packages.list     the system's packages
iso/grub.cfg             the ISO's boot menu
rootfs/                  copied over the system: RISE scripts, systemd units, WebGUI
example/*.sample         the SoftEther setup RISE started from (sanitized)
```

## License

MIT, see [LICENSE](LICENSE). SoftEther VPN and the other components keep
their own licenses (below).

## Credits

- [SoftEther VPN](https://github.com/SoftEtherVPN/SoftEtherVPN) (Apache 2.0)
- [netfilter-full-cone-nat](https://github.com/llccd/netfilter-full-cone-nat) (GPL-2.0), fetched and built during the build
- [luci-theme-footstrap](https://github.com/VizzleTF/luci-theme-footstrap), the design the WebGUI follows
- GNU Unifont, as packaged by console-setup and GRUB
- Ubuntu Server
