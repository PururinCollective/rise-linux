#!/usr/bin/env bash
# build.sh -- builds RISE Linux, a SoftEther VPN server appliance on Ubuntu
# Server, from nothing to a bootable hybrid ISO (BIOS and UEFI, USB or DVD).
#
#   sudo ./build.sh              every stage that has not run yet, then the ISO
#   sudo ./build.sh rebuild      forget every stage and build again from scratch
#   sudo ./build.sh softether    rebuild SoftEther (and everything after it)
#   sudo ./build.sh overlay      copy rootfs/ again (and everything after it)
#   sudo ./build.sh iso          remake the squashfs and the ISO only
#   sudo ./build.sh shell        a root shell in the build chroot
#   sudo ./build.sh clean        delete build/ (out/ is kept)
#
# Stages, each recorded in build/stamps so an interrupted build resumes:
#   base        debootstrap the Ubuntu Server base (minbase + main/universe)
#   packages    config/packages.list and the kernel
#   softether   compile SoftEther VPN from SOFTETHER_SRC inside the chroot
#   fullcone    xt_FULLCONENAT through DKMS, and its iptables extensions
#   overlay     rootfs/ (RISE scripts, units, WebGUI) and the system settings
#   squashfs    casper/filesystem.squashfs, the kernel and the initrd
#   iso         the hybrid ISO in out/
#
# Settings: config/build.conf. Host: a Debian or Ubuntu with debootstrap that
# knows UBUNTU_SUITE; the rest is installed into the chroot, not the host.
set -Eeuo pipefail

TOP=$(cd "$(dirname "$0")" && pwd)
cd "$TOP"
# shellcheck source=config/build.conf
. "$TOP/config/build.conf"

BUILD=$(realpath -m "$TOP/$BUILD_DIR")
OUT=$(realpath -m "$TOP/$OUT_DIR")
ROOT=$BUILD/chroot
STAGE=$BUILD/iso
SRC=$BUILD/src
STAMPS=$BUILD/stamps
LOG=$BUILD/build.log
STAGES=(base packages softether fullcone overlay squashfs iso)

c_b=$'\e[1m'; c_o=$'\e[38;5;202m'; c_g=$'\e[32m'; c_r=$'\e[31m'; c_0=$'\e[0m'
[ -t 1 ] || { c_b=; c_o=; c_g=; c_r=; c_0=; }
step() { printf '%s==>%s %s%s%s\n' "$c_o" "$c_0" "$c_b" "$*" "$c_0"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s  ! %s%s\n' "$c_r" "$*" "$c_0" >&2; }
die() { printf '%sbuild.sh: %s%s\n' "$c_r" "$*" "$c_0" >&2; exit 1; }

# ---- chroot plumbing ---------------------------------------------------------

mounted() { findmnt -rno TARGET "$1" >/dev/null 2>&1; }

chroot_mount() {
	mkdir -p "$ROOT"/{dev,proc,sys,run,tmp}
	mounted "$ROOT/dev" || mount --bind /dev "$ROOT/dev"
	mounted "$ROOT/dev/pts" || mount -t devpts devpts "$ROOT/dev/pts" -o gid=5,mode=620,newinstance,ptmxmode=0666
	mounted "$ROOT/proc" || mount -t proc proc "$ROOT/proc"
	mounted "$ROOT/sys" || mount -t sysfs sysfs "$ROOT/sys"
	mounted "$ROOT/run" || mount -t tmpfs tmpfs "$ROOT/run"
	mkdir -p "$ROOT/run/systemd"
	# name resolution for apt inside the chroot, removed in chroot_umount
	if [ ! -e "$ROOT/etc/resolv.conf.rise-build" ] && [ ! -L "$ROOT/etc/resolv.conf.rise-build" ] &&
	   [ ! -e "$ROOT/etc/resolv.conf.rise-build.none" ]; then
		if [ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ]; then
			mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.rise-build"
		else
			touch "$ROOT/etc/resolv.conf.rise-build.none"
		fi
		cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
	fi
	# no daemon starts inside the chroot
	printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
	chmod 755 "$ROOT/usr/sbin/policy-rc.d"
}

chroot_umount() {
	[ -d "$ROOT" ] || return 0
	local m
	for m in "$ROOT/mnt/iso" "$ROOT/mnt/out" "$ROOT/run" "$ROOT/sys" "$ROOT/proc" "$ROOT/dev/pts" "$ROOT/dev"; do
		mounted "$m" && { umount "$m" 2>/dev/null || umount -l "$m"; }
	done
	if [ -e "$ROOT/etc/resolv.conf.rise-build" ] || [ -L "$ROOT/etc/resolv.conf.rise-build" ]; then
		rm -f "$ROOT/etc/resolv.conf"
		mv "$ROOT/etc/resolv.conf.rise-build" "$ROOT/etc/resolv.conf"
	elif [ -e "$ROOT/etc/resolv.conf.rise-build.none" ]; then
		rm -f "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.rise-build.none"
	fi
	rm -f "$ROOT/usr/sbin/policy-rc.d"
	return 0
}
trap chroot_umount EXIT
trap 'die "failed at line $LINENO (see $LOG)"' ERR

# run a command in the chroot with a clean, non-interactive environment
inchroot() {
	chroot "$ROOT" /usr/bin/env -i \
		HOME=/root TERM=dumb LANG=C.UTF-8 \
		PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
		DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true \
		"$@"
}
apt_in() { inchroot apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"; }
# the squashfs stage empties apt's lists; a stage that installs refreshes them
apt_ready() { [ -n "$(ls -A "$ROOT/var/lib/apt/lists" 2>/dev/null | grep -v '^lock$\|^partial$')" ] || apt_in update >>"$LOG" 2>&1; }

kver() {
	# the newest kernel in the chroot
	ls -1 "$ROOT/lib/modules" 2>/dev/null | sort -V | tail -n 1
}

done_stage() { [ -e "$STAMPS/$1" ]; }
mark_stage() { mkdir -p "$STAMPS"; date -u +%FT%TZ > "$STAMPS/$1"; }
# forget a stage and every stage after it
reset_from() {
	local s on=0
	for s in "${STAGES[@]}"; do
		[ "$s" = "$1" ] && on=1
		[ $on = 1 ] && rm -f "$STAMPS/$s"
	done
}

# ---- host ----------------------------------------------------------------------

preflight() {
	[ "$(id -u)" = 0 ] || die "run as root (sudo ./build.sh)"
	[ "$(uname -m)" = x86_64 ] || die "the build host must be x86_64 (the target is $ARCH)"
	local need=() t
	for t in debootstrap:debootstrap mksquashfs:squashfs-tools rsync:rsync git:git curl:curl; do
		command -v "${t%%:*}" >/dev/null || need+=("${t#*:}")
	done
	if [ ${#need[@]} -gt 0 ]; then
		command -v apt-get >/dev/null || die "install on this host: ${need[*]}"
		step "Installing host tools: ${need[*]}"
		apt-get update -qq && apt-get install -y -qq "${need[@]}" >>"$LOG" 2>&1
	fi
	[ -e "/usr/share/debootstrap/scripts/$UBUNTU_SUITE" ] || \
		die "this host's debootstrap does not know '$UBUNTU_SUITE' (update debootstrap or set UBUNTU_SUITE)"
	local free
	free=$(df -Pk "$(dirname "$BUILD")" | awk 'NR==2{print int($4/1048576)}')
	[ "$free" -ge 12 ] || warn "only ${free} GiB free next to $BUILD; a build takes about 10 GiB"
}

# ---- stages --------------------------------------------------------------------

stage_base() {
	step "Bootstrapping Ubuntu $UBUNTU_SUITE ($ARCH) into $ROOT"
	chroot_umount
	rm -rf "$ROOT"
	mkdir -p "$ROOT"
	debootstrap --arch="$ARCH" --variant=minbase --components=main,universe \
		--include=ca-certificates,gnupg "$UBUNTU_SUITE" "$ROOT" "$UBUNTU_MIRROR" >>"$LOG" 2>&1
	rm -f "$ROOT/etc/apt/sources.list"
	cat > "$ROOT/etc/apt/sources.list.d/ubuntu.sources" <<-EOF
	Types: deb
	URIs: $UBUNTU_MIRROR
	Suites: $UBUNTU_SUITE $UBUNTU_SUITE-updates $UBUNTU_SUITE-backports
	Components: main restricted universe multiverse
	Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

	Types: deb
	URIs: $UBUNTU_SECURITY_MIRROR
	Suites: $UBUNTU_SUITE-security
	Components: main restricted universe multiverse
	Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
	EOF
	# a server: no documentation or other languages than English
	cat > "$ROOT/etc/dpkg/dpkg.cfg.d/01-rise-nodoc" <<-'EOF'
	path-exclude=/usr/share/doc/*
	path-include=/usr/share/doc/*/copyright
	path-exclude=/usr/share/man/*
	path-exclude=/usr/share/groff/*
	path-exclude=/usr/share/info/*
	path-exclude=/usr/share/lintian/*
	path-exclude=/usr/share/locale/*
	path-include=/usr/share/locale/en*
	path-include=/usr/share/locale/locale.alias
	EOF
	cat > "$ROOT/etc/apt/apt.conf.d/99-rise" <<-'EOF'
	APT::Install-Recommends "false";
	APT::Install-Suggests "false";
	EOF
	echo rise > "$ROOT/etc/hostname"
	mark_stage base
}

# a package name from the list, or its pre-t64 name on a suite without t64
resolve_pkg() {
	local p=$1
	case "$p" in *-) echo "$p"; return ;; esac
	if inchroot apt-cache show "$p" >/dev/null 2>&1; then echo "$p"; return; fi
	if [[ $p == *t64 ]] && inchroot apt-cache show "${p%t64}" >/dev/null 2>&1; then echo "${p%t64}"; return; fi
	warn "package '$p' is not in $UBUNTU_SUITE, skipped"
}

stage_packages() {
	step "Installing the system packages and $KERNEL_PKG"
	chroot_mount
	apt_in update >>"$LOG" 2>&1
	apt_in dist-upgrade >>"$LOG" 2>&1
	local list=() p r
	while read -r p; do
		p=${p%%#*}; p=${p//[[:space:]]/}
		[ -n "$p" ] || continue
		r=$(resolve_pkg "$p")
		[ -n "$r" ] && list+=("$r")
	done < "$TOP/config/packages.list"
	info "${#list[@]} packages"
	apt_in install "${list[@]}" >>"$LOG" 2>&1
	# the kernel last, so DKMS and initramfs-tools are already there
	apt_in install "$KERNEL_PKG" >>"$LOG" 2>&1
	[ -n "$(kver)" ] || die "no kernel was installed"
	info "kernel $(kver)"
	mark_stage packages
}

fetch_softether() {
	local dst=$SRC/softether
	rm -rf "$dst"
	mkdir -p "$SRC"
	local src=$SOFTETHER_SRC
	case "$src" in /*) ;; *://*|git@*) ;; *) src=$(realpath -m "$TOP/$src") ;; esac
	if [ -d "$src" ]; then
		info "SoftEther from $src (working tree, uncommitted changes included)"
		rsync -a --delete --exclude='/build/' --exclude='/tmp/' "$src/" "$dst/"
		if [ -d "$src/.git" ] || [ -f "$src/.git" ]; then
			info "  $(git -C "$src" log -1 --format='%h %s' 2>/dev/null || echo '?')"
			[ -z "$(git -C "$src" status --porcelain 2>/dev/null)" ] || info "  with uncommitted changes"
		fi
	else
		info "SoftEther from $src at $SOFTETHER_REF"
		git clone -q "$src" "$dst"
		git -C "$dst" checkout -q "$SOFTETHER_REF"
	fi
	# submodules into the copy only
	local subs=(3rdparty/BLAKE2 3rdparty/tinydir src/Mayaqua/3rdparty/cpu_features src/libhamcore)
	[ "$SOFTETHER_OQS" = ON ] && subs+=(src/Mayaqua/3rdparty/liboqs src/Mayaqua/3rdparty/oqs-provider)
	local s missing=()
	for s in "${subs[@]}"; do
		[ -n "$(ls -A "$dst/$s" 2>/dev/null)" ] || missing+=("$s")
	done
	if [ ${#missing[@]} -gt 0 ]; then
		[ -e "$dst/.git" ] || die "SoftEther source has empty submodules (${missing[*]}) and no git metadata to fetch them"
		info "fetching submodules: ${missing[*]}"
		git -C "$dst" -c safe.directory='*' submodule update --init --recursive --depth 1 -- "${missing[@]}" >>"$LOG" 2>&1 || \
			git -C "$dst" -c safe.directory='*' submodule update --init --recursive -- "${missing[@]}" >>"$LOG" 2>&1
	fi
	# RISE's own fixes (patches/softether), skipped when the source has them
	local p
	for p in "$TOP"/patches/softether/*.patch; do
		[ -e "$p" ] || continue
		if patch -d "$dst" -p1 -R --dry-run -s -f <"$p" >/dev/null 2>&1; then
			info "  patch $(basename "$p"): already in the source"
		elif patch -d "$dst" -p1 --dry-run -s -f <"$p" >>"$LOG" 2>&1; then
			patch -d "$dst" -p1 -s -f <"$p" >>"$LOG" 2>&1
			info "  patch $(basename "$p")"
		else
			die "patch $(basename "$p") does not apply to the SoftEther source"
		fi
	done
}

stage_softether() {
	step "Compiling SoftEther VPN"
	fetch_softether
	chroot_mount
	local deps=(cmake make gcc pkg-config libssl-dev libsodium-dev libreadline-dev zlib1g-dev libncurses-dev)
	apt_ready
	apt_in install "${deps[@]}" >>"$LOG" 2>&1
	rm -rf "$ROOT/usr/src/softether"
	rsync -a --exclude='/.git/' "$SRC/softether/" "$ROOT/usr/src/softether/"
	local bn=()
	[ -n "$SOFTETHER_BUILD_NUMBER" ] && bn=(-DBUILD_NUMBER="$SOFTETHER_BUILD_NUMBER")
	inchroot cmake -S /usr/src/softether -B /usr/src/softether/build \
		-DCMAKE_BUILD_TYPE=Release \
		-DCMAKE_INSTALL_PREFIX=/opt/softether \
		-DCMAKE_INSTALL_LIBDIR=lib \
		-DCMAKE_INSTALL_SYSTEMD_UNITDIR= \
		-DOQS_ENABLE="$SOFTETHER_OQS" "${bn[@]}" >>"$LOG" 2>&1
	inchroot cmake --build /usr/src/softether/build -j"$(nproc)" >>"$LOG" 2>&1
	rm -rf "$ROOT/opt/softether"
	inchroot cmake --install /usr/src/softether/build >>"$LOG" 2>&1
	echo /opt/softether/lib > "$ROOT/etc/ld.so.conf.d/softether.conf"
	inchroot ldconfig
	ln -sf /opt/softether/bin/vpncmd "$ROOT/usr/local/bin/vpncmd"
	# the upstream unit is replaced by rise-vpnserver.service
	rm -f "$ROOT"/{lib,usr/lib}/systemd/system/softether-*.service
	local ver build
	ver=$(sed -n 's/^[[:space:]]*VERSION "\([0-9.]*\)\${BUILD_NUMBER}".*/\1/p' "$SRC/softether/CMakeLists.txt" | head -n1)
	build=${SOFTETHER_BUILD_NUMBER:-$(sed -n 's/^[[:space:]]*set(BUILD_NUMBER "\([0-9]*\)").*/\1/p' "$SRC/softether/CMakeLists.txt" | head -n1)}
	ver="${ver}${build}"
	[ -n "$build" ] || ver=
	echo "${ver:-unknown}" > "$ROOT/opt/softether/VERSION"
	info "SoftEther ${ver:-?} in /opt/softether"
	rm -rf "$ROOT/usr/src/softether"
	apt_in purge cmake libssl-dev libsodium-dev libreadline-dev zlib1g-dev libncurses-dev >>"$LOG" 2>&1
	apt_in autoremove --purge >>"$LOG" 2>&1
	# every library vpnserver needs is still there
	if inchroot ldd /opt/softether/libexec/softether/vpnserver/vpnserver | grep -q 'not found'; then
		die "vpnserver is missing a library after the build dependencies were removed"
	fi
	mark_stage softether
}

stage_fullcone() {
	step "Full cone NAT: xt_FULLCONENAT (DKMS) and its iptables extensions"
	chroot_mount
	local d=$SRC/fullconenat k
	k=$(kver)
	rm -rf "$d"
	git clone -q "$FULLCONENAT_REPO" "$d"
	git -C "$d" checkout -q "$FULLCONENAT_REF"
	apt_ready
	apt_in install libxtables-dev pkg-config "linux-headers-$k" >>"$LOG" 2>&1
	inchroot dkms remove fullconenat/1.0 --all >/dev/null 2>&1 || true
	rm -rf "$ROOT/usr/src/fullconenat-1.0"
	rsync -a --exclude='/.git/' "$d/" "$ROOT/usr/src/fullconenat-1.0/"
	local ok=1
	inchroot dkms add -m fullconenat -v 1.0 >>"$LOG" 2>&1 || ok=0
	[ $ok = 1 ] && { inchroot dkms build -m fullconenat -v 1.0 -k "$k" >>"$LOG" 2>&1 || ok=0; }
	[ $ok = 1 ] && { inchroot dkms install -m fullconenat -v 1.0 -k "$k" >>"$LOG" 2>&1 || ok=0; }
	# the userspace half: libipt_/libip6t_FULLCONENAT.so into the xtables directory
	if [ $ok = 1 ]; then
		inchroot sh -c 'set -e; cd /usr/src/fullconenat-1.0
			xt=$(pkg-config xtables --variable=xtlibdir)
			for f in libipt_FULLCONENAT libip6t_FULLCONENAT; do
				[ -f $f.c ] || continue
				gcc -O2 -Wall -fPIC -D_INIT=${f}_init -c -o /tmp/$f.o $f.c
				gcc -shared -o "$xt/$f.so" /tmp/$f.o -lxtables
				rm -f /tmp/$f.o
			done' >>"$LOG" 2>&1 || ok=0
	fi
	apt_in purge libxtables-dev >>"$LOG" 2>&1 || true
	apt_in autoremove --purge >>"$LOG" 2>&1 || true
	if [ $ok = 1 ]; then
		# FULLCONENAT is an xtables target: the legacy backend is its tested path
		inchroot update-alternatives --set iptables /usr/sbin/iptables-legacy >>"$LOG" 2>&1 || true
		inchroot update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy >>"$LOG" 2>&1 || true
		echo 1 > "$ROOT/etc/rise-fullcone-built"
		info "xt_FULLCONENAT built for $k"
	else
		rm -f "$ROOT/etc/rise-fullcone-built"
		warn "xt_FULLCONENAT did not build for $k; the system falls back to MASQUERADE (see $LOG)"
	fi
	mark_stage fullcone
}

fetch_fonts() {
	local dir=$1/usr/share/rise/webgui/static/fonts
	mkdir -p "$dir"
	[ "$WEBGUI_FONTS" = 1 ] || return 0
	local base=https://raw.githubusercontent.com/google/fonts/main/ofl
	local f
	for f in "manrope/Manrope%5Bwght%5D.ttf:Manrope.ttf" "jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf:JetBrainsMono.ttf"; do
		[ -s "$SRC/fonts/${f#*:}" ] || {
			mkdir -p "$SRC/fonts"
			curl -fsSL --retry 3 -o "$SRC/fonts/${f#*:}.part" "$base/${f%%:*}" && \
				mv "$SRC/fonts/${f#*:}.part" "$SRC/fonts/${f#*:}" || \
				{ warn "could not fetch ${f#*:}; the WebGUI uses the system font for it"; continue; }
		}
		install -m 0644 "$SRC/fonts/${f#*:}" "$dir/${f#*:}"
	done
}

stage_overlay() {
	step "Installing the RISE overlay and system settings"
	chroot_mount
	rsync -a --chown=root:root "$TOP/rootfs/" "$ROOT/"
	chmod 0755 "$ROOT"/usr/lib/rise/* "$ROOT"/usr/sbin/rise* "$ROOT"/etc/letsencrypt/renewal-hooks/deploy/* 2>/dev/null || true
	chmod 0644 "$ROOT"/usr/lib/rise/*.sh "$ROOT"/usr/lib/rise/*.awk 2>/dev/null || true
	chmod 0755 "$ROOT/usr/share/rise/webgui/server.py"
	fetch_fonts "$ROOT"
	cat > "$ROOT/etc/rise/release" <<-EOF
	RISE_NAME="$RISE_NAME"
	RISE_VERSION="$RISE_VERSION"
	RISE_BUILD="$(date -u +%Y%m%d%H%M)"
	UBUNTU_SUITE="$UBUNTU_SUITE"
	SOFTETHER="$(cat "$ROOT/opt/softether/VERSION" 2>/dev/null)"
	EOF

	inchroot sh -s >>"$LOG" 2>&1 <<-'EOF'
	set -e
	# English, UTF-8
	sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
	locale-gen
	update-locale LANG=en_US.UTF-8
	ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime
	echo Etc/UTC > /etc/timezone

	# root never logs in: the installer creates the sudo user
	usermod -p '!' root
	# predictable /etc/rise; /var/crash because casper touches it at boot
	install -d -m 0755 /etc/rise /var/lib/rise
	install -d -m 1777 /var/crash
	install -d -m 0700 /etc/rise/secrets

	# console: Unifont 8x16, the font of the Ubuntu Server installer
	cat > /etc/default/console-setup <<'CS'
ACTIVE_CONSOLES="/dev/tty[1-6]"
CHARMAP="UTF-8"
CODESET="Uni2"
FONTFACE="Fixed"
FONTSIZE="8x16"
VIDEOMODE=
CS
	cat > /etc/default/keyboard <<'KB'
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
KB

	# GRUB: 1024x768 by default, the console follows (gfxpayload=keep)
	cat > /etc/default/grub <<'GR'
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="RISE Linux"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 systemd.show_status=false rd.systemd.show_status=false rd.udev.log_level=3"
GRUB_CMDLINE_LINUX="net.ifnames=0 biosdevname=0 video=1024x768"
GRUB_TERMINAL_OUTPUT=gfxterm
GRUB_GFXMODE=1024x768,auto
GRUB_GFXPAYLOAD_LINUX=keep
GRUB_DISABLE_OS_PROBER=true
GRUB_DISABLE_RECOVERY=false
GR
	install -d /etc/kernel/postinst.d /etc/kernel/postrm.d
	for d in postinst.d postrm.d; do
		printf '#!/bin/sh\n# keep grub.cfg current on kernel updates\ncommand -v update-grub >/dev/null && [ -e /boot/grub/grub.cfg ] && exec update-grub || exit 0\n' > /etc/kernel/$d/zz-rise-update-grub
		chmod 755 /etc/kernel/$d/zz-rise-update-grub
	done

	# quiet logins: the RISE banner replaces the Ubuntu MOTD
	chmod -x /etc/update-motd.d/* 2>/dev/null || true
	[ -e /etc/default/motd-news ] && sed -i 's/^ENABLED=.*/ENABLED=0/' /etc/default/motd-news || true
	rm -f /etc/legal

	# services
	systemctl disable ssh.socket 2>/dev/null || true
	systemctl enable ssh.service systemd-networkd.service systemd-resolved.service systemd-timesyncd.service
	systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
	systemctl enable rise-console.service rise-splash.service rise-issue.timer \
		rise-firewall.service rise-vpnserver.service rise-dnsmasq.service \
		rise-webgui.service rise-firstboot.service rise-cert.timer rise-stats.service \
		rise-installer.service
	systemctl mask motd-news.timer apt-daily-upgrade.timer 2>/dev/null || true
	systemctl set-default multi-user.target

	# the full cone module loads at boot when it was built
	if [ -e /etc/rise-fullcone-built ]; then
		echo xt_FULLCONENAT > /etc/modules-load.d/rise-fullcone.conf
		mv /etc/rise-fullcone-built /usr/share/rise/fullcone-built
	fi

	# casper's desktop helpers have nothing to do here and print errors at boot
	for s in 30accessibility 31disable_update_notifier 33enable_apport_crashes 41apt_cdrom \
		20xconfig 22desktop_settings 34disable_kde_services 37disable_screensaver_lubuntu \
		45jackd2 52gnome_initial_setup 59disable_mozc_autosetup 61desktop_canary_tweaks; do
		rm -f /usr/share/initramfs-tools/scripts/casper-bottom/$s
	done
	update-initramfs -u -k all
	EOF
	mark_stage overlay
}

stage_squashfs() {
	step "Packing the root filesystem"
	chroot_mount
	local k
	k=$(kver)
	# leave the image clean: no apt cache, no logs, no machine identity
	inchroot sh -s >>"$LOG" 2>&1 <<-'EOF'
	apt-get clean
	rm -rf /var/lib/apt/lists/* /var/cache/apt/*.bin /var/cache/debconf/*-old \
		/var/log/*.log /var/log/apt/* /tmp/* /var/tmp/* /root/.bash_history /var/lib/dkms/*/*/build
	find /var/log -type f -name '*.gz' -delete
	: > /etc/machine-id
	rm -f /var/lib/dbus/machine-id /etc/ssh/ssh_host_*
	EOF
	chroot_umount
	rm -rf "$STAGE"
	mkdir -p "$STAGE/casper" "$STAGE/.disk" "$STAGE/boot/grub"
	cp "$ROOT/boot/vmlinuz-$k" "$STAGE/casper/vmlinuz"
	cp "$ROOT/boot/initrd.img-$k" "$STAGE/casper/initrd"
	mksquashfs "$ROOT" "$STAGE/casper/filesystem.squashfs" \
		-noappend -comp xz -b 1M -Xdict-size 100% -wildcards \
		-e 'proc/*' 'sys/*' 'dev/*' 'run/*' 'tmp/*' 'mnt/*' >>"$LOG" 2>&1
	du -sx --block-size=1 "$ROOT" | cut -f1 > "$STAGE/casper/filesystem.size"
	chroot "$ROOT" dpkg-query -W --showformat='${Package} ${Version}\n' > "$STAGE/casper/filesystem.manifest"
	echo "$RISE_NAME $RISE_VERSION - amd64 ($(date -u +%Y%m%d))" > "$STAGE/.disk/info"
	touch "$STAGE/.disk/base_installable" "$STAGE/rise-linux.marker"
	sed -e "s|@NAME@|$RISE_NAME|g" -e "s|@VERSION@|$RISE_VERSION|g" \
		"$TOP/iso/grub.cfg" > "$STAGE/boot/grub/grub.cfg"
	info "squashfs $(du -h "$STAGE/casper/filesystem.squashfs" | cut -f1), kernel $k"
	mark_stage squashfs
}

stage_iso() {
	step "Making the ISO"
	chroot_mount
	# the ISO tools live in the build chroot only; the squashfs is already made
	apt_in update >>"$LOG" 2>&1
	apt_in install xorriso mtools >>"$LOG" 2>&1
	mkdir -p "$OUT" "$ROOT/mnt/iso" "$ROOT/mnt/out"
	mount --bind "$STAGE" "$ROOT/mnt/iso"
	mount --bind "$OUT" "$ROOT/mnt/out"
	local name
	name="rise-linux-$RISE_VERSION-$(date -u +%Y%m%d)-$ARCH.iso"
	rm -f "$OUT/$name"
	inchroot grub-mkrescue -o "/mnt/out/$name" /mnt/iso -- \
		-volid RISE_LINUX >>"$LOG" 2>&1
	umount "$ROOT/mnt/iso" "$ROOT/mnt/out"
	apt_in purge xorriso mtools >>"$LOG" 2>&1 || true
	apt_in autoremove --purge >>"$LOG" 2>&1 || true
	inchroot apt-get clean
	rm -rf "$ROOT/var/lib/apt/lists/"*
	( cd "$OUT" && sha256sum "$name" > "$name.sha256" )
	chown --reference="$TOP" "$OUT" "$OUT/$name" "$OUT/$name.sha256" 2>/dev/null || true
	mark_stage iso
	printf '\n%sDone:%s %s (%s)\n' "$c_g" "$c_0" "$OUT/$name" "$(du -h "$OUT/$name" | cut -f1)"
	info "write it to a USB stick:  sudo dd if=$OUT/$name of=/dev/sdX bs=4M status=progress oflag=sync"
}

run_all() {
	local s
	for s in "${STAGES[@]}"; do
		done_stage "$s" && { info "stage $s: done before, skipped"; continue; }
		"stage_$s"
	done
}

main() {
	mkdir -p "$BUILD"
	local cmd=${1:-all}
	case "$cmd" in
	-h|--help|help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	clean)
		[ "$(id -u)" = 0 ] || die "run as root"
		chroot_umount
		findmnt -rno TARGET | grep -q "^$ROOT" && die "something is still mounted under $ROOT"
		rm -rf "$BUILD"; echo "removed $BUILD"; exit 0 ;;
	shell)
		preflight; chroot_mount
		chroot "$ROOT" /usr/bin/env -i HOME=/root TERM="${TERM:-xterm}" PATH=/usr/sbin:/usr/bin:/sbin:/bin bash -l
		exit 0 ;;
	esac
	: > "$LOG"
	preflight
	step "$RISE_NAME $RISE_VERSION ($UBUNTU_SUITE, $ARCH) -> $OUT"
	info "log: $LOG"
	case "$cmd" in
	all) ;;
	rebuild) rm -rf "$STAMPS" ;;
	softether|fullcone|overlay|squashfs|iso|packages|base) reset_from "$cmd" ;;
	*) die "unknown command '$cmd' (see ./build.sh help)" ;;
	esac
	run_all
}

main "$@"
