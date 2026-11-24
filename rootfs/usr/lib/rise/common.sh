# shellcheck shell=bash
# /usr/lib/rise/common.sh -- sourced by every RISE script: settings, paths and
# small helpers. bash only.

RISE_CONF=${RISE_CONF:-/etc/rise/rise.conf}
RISE_DEFAULTS=/etc/rise/rise.conf.default
RISE_SECRETS=/etc/rise/secrets
RISE_STATE=/var/lib/rise
RISE_RUN=/run/rise
RISE_PORTFWD=/etc/rise/portfwd.conf
RISE_DHCP_STATIC=/etc/rise/dhcp-static.conf

SE_DIR=/opt/softether/libexec/softether/vpnserver
SE_SERVER=$SE_DIR/vpnserver
SE_CONFIG=$SE_DIR/vpn_server.config
VPNCMD=/opt/softether/bin/vpncmd

# defaults first, then the installed settings over them
# shellcheck disable=SC1090
[ -r "$RISE_DEFAULTS" ] && . "$RISE_DEFAULTS"
# shellcheck disable=SC1090
[ -r "$RISE_CONF" ] && . "$RISE_CONF"

TAP_IF="tap_${VPN_TAP:-vpn}"
WAN_IF=${WAN_IF:-eth0}

mkdir -p "$RISE_RUN" 2>/dev/null || true

log() { echo "rise: $*" >&2; logger -t rise -- "$*" 2>/dev/null || true; }
die() { log "error: $*"; exit 1; }
is_on() { case "${1:-0}" in 1|yes|true|on) return 0 ;; *) return 1 ;; esac; }

# ---- addresses (python3 ipaddress does the arithmetic) ---------------------------

# ipq <query> <address/prefix>: addr | prefix | network | netmask | first | last
#   first/last: the DHCP pool of an IPv6 prefix (::1000 .. ::ffff)
ipq() {
	python3 -I - "$1" "$2" <<'PY'
import ipaddress, sys
q, v = sys.argv[1], sys.argv[2]
i = ipaddress.ip_interface(v)
n = i.network
if q == "addr": print(i.ip)
elif q == "prefix": print(n.prefixlen)
elif q == "network": print(n)
elif q == "netmask": print(n.netmask)
elif q == "first": print(n.network_address + 0x1000)
elif q == "last": print(n.network_address + 0xffff)
elif q == "private":
    print(1 if (i.ip.is_private or i.ip in ipaddress.ip_network("100.64.0.0/10")) else 0)
PY
}

# the first global address of an interface: wan_addr 4|6
wan_addr() {
	ip -"$1" -o addr show dev "$WAN_IF" scope global 2>/dev/null |
		awk '{print $4}' | grep -v '^f[cd]' | head -n1
}

# ---- SoftEther administration -----------------------------------------------------

se_admin_port() { set -- $SOFTETHER_TCP_PORTS; echo "${1:-5555}"; }
se_password() { cat "$RISE_SECRETS/softether-admin" 2>/dev/null; }

# se <command line...>   runs one vpncmd command against this server, as the
# server administrator. A line starting with "@hub " runs on the VPN hub.
# The commands go through a file so quoted values (spaces, commas) survive.
se() {
	local f out rc pw hub=()
	# SE_PW= (set and empty) logs in without a password: a fresh server
	pw=${SE_PW-$(se_password)}
	f=$(mktemp "$RISE_RUN/vpncmd.XXXXXX")
	chmod 600 "$f"
	if [ "${1:-}" = "@hub" ]; then
		shift
		hub=(/ADMINHUB:"$VPN_HUB")
	fi
	printf '%s\n' "$*" > "$f"
	out=$("$VPNCMD" /SERVER localhost:"$(se_admin_port)" ${pw:+/PASSWORD:"$pw"} "${hub[@]}" \
		/CSV /IN:"$f" 2>&1)
	rc=$?
	rm -f "$f"
	printf '%s\n' "$out"
	return $rc
}

# wait until the server answers on its admin port
se_wait() {
	local i
	for i in $(seq 1 "${1:-60}"); do
		se About >/dev/null 2>&1 && return 0
		sleep 1
	done
	return 1
}

# ---- config file edits -------------------------------------------------------------

# conf_set KEY VALUE: writes KEY="VALUE" into rise.conf (adds it when missing)
conf_set() {
	local k=$1 v=$2
	[ -e "$RISE_CONF" ] || cp "$RISE_DEFAULTS" "$RISE_CONF"
	v=${v//\\/\\\\}; v=${v//\"/\\\"}; v=${v//\$/\\\$}; v=${v//\`/\\\`}
	if grep -q "^$k=" "$RISE_CONF"; then
		python3 -I - "$RISE_CONF" "$k" "$v" <<'PY'
import sys
p, k, v = sys.argv[1:]
lines = open(p).read().splitlines()
out = [f'{k}="{v}"' if l.startswith(k + "=") else l for l in lines]
open(p, "w").write("\n".join(out) + "\n")
PY
	else
		printf '%s="%s"\n' "$k" "$v" >> "$RISE_CONF"
	fi
}
