/* RISE WebGUI. No framework: h() builds elements (text goes in as text, never
 * as HTML), each page is a function that fills <main>, and a poll refreshes the
 * live parts every two seconds while the page is visible. */
"use strict";

const S = { user: null, csrf: null, page: "status", timer: null, ov: null, theme: null };
const PAGES = [
	["status", "Status"], ["vpn", "VPN"], ["network", "Network"],
	["forwarding", "Port Forwarding"], ["dhcp", "DHCP"], ["system", "System"],
];
const TAGLINE = "Silent by design. Secure by nature.";

// ---- small helpers --------------------------------------------------------------------

function h(tag, attrs, ...kids) {
	const el = document.createElement(tag);
	for (const [k, v] of Object.entries(attrs || {})) {
		if (v === null || v === undefined || v === false) continue;
		if (k === "class") el.className = v;
		else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
		else if (k === "value") el.value = v;
		else if (k === "checked") el.checked = !!v;
		else el.setAttribute(k, v === true ? "" : v);
	}
	for (const kid of kids.flat(Infinity)) {
		if (kid === null || kid === undefined || kid === false) continue;
		el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
	}
	return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function icon(d) {
	const s = document.createElementNS(svgNS, "svg");
	s.setAttribute("viewBox", "0 0 24 24");
	for (const p of [].concat(d)) {
		const path = document.createElementNS(svgNS, "path");
		path.setAttribute("d", p);
		s.append(path);
	}
	return s;
}
const ICONS = {
	logout: ["M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3", "M10 17l-5-5 5-5", "M5 12h11"],
	moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
	sun: ["M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
	chevron: "M6 15l6-6 6 6",
};

function bytes(n) {
	if (n === null || n === undefined) return "-";
	const u = ["B", "KiB", "MiB", "GiB", "TiB"];
	let i = 0; n = Number(n);
	while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
	return (i ? n.toFixed(n >= 100 ? 1 : 2) : n) + " " + u[i];
}
function bps(n) {
	const u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
	let i = 0; n = Number(n || 0);
	while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
	return (i ? n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2) : n.toFixed(0)) + " " + u[i];
}
function duration(s) {
	s = Math.floor(s);
	const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
	return `${d}d ${hh}h ${m}m ${s % 60}s`;
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "-"; }
function pct(a, b) { return b ? Math.round(100 * a / b) : 0; }

function toast(msg, ok = true) {
	const t = h("div", { class: "toast" + (ok ? "" : " bad"), role: "status" }, msg);
	document.getElementById("toasts").append(t);
	setTimeout(() => t.remove(), ok ? 4000 : 9000);
}

async function api(path, body) {
	const opt = { credentials: "same-origin", headers: {} };
	if (body !== undefined) {
		opt.method = "POST";
		opt.headers["Content-Type"] = "application/json";
		if (S.csrf) opt.headers["X-CSRF-Token"] = S.csrf;
		opt.body = JSON.stringify(body);
	}
	const r = await fetch("/api/" + path, opt);
	if (r.status === 401 && path !== "login") { S.user = null; renderLogin(); throw new Error("login"); }
	let data = {};
	try { data = await r.json(); } catch (e) { /* empty */ }
	if (!r.ok && body !== undefined) throw new Error(data.message || data.error || r.statusText);
	return data;
}

// an action button: disables itself, shows the answer as a toast
function act(label, path, body, opts = {}) {
	const b = h("button", { class: "btn " + (opts.cls || ""), type: "button" }, label);
	b.addEventListener("click", async () => {
		if (opts.confirm && !confirm(opts.confirm)) return;
		b.disabled = true;
		try {
			const r = await api(path, typeof body === "function" ? body() : body);
			toast(r.message || "done", r.ok !== false);
			if (opts.after) opts.after(r);
		} catch (e) { if (e.message !== "login") toast(e.message, false); }
		b.disabled = false;
	});
	return b;
}

function card(title, body, opts = {}) {
	const c = h("section", { class: "card" + (opts.wide ? " wide" : "") });
	const btn = h("button", { class: "collapse", type: "button", "aria-label": "Collapse" }, icon(ICONS.chevron));
	btn.addEventListener("click", () => c.classList.toggle("closed"));
	c.append(h("div", { class: "card-head" }, h("h2", {}, title), opts.sub ? h("span", { class: "sub" }, opts.sub) : null, btn),
		h("div", { class: "card-body" }, body));
	return c;
}
function kv(rows) {
	return h("div", { class: "kv" }, rows.filter(Boolean).map(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "v" }, v)]));
}
function meter(label, used, total, text) {
	const p = pct(used, total);
	const f = h("div", { class: "fill" + (p > 90 ? " bad" : p > 75 ? " warn" : "") });
	requestAnimationFrame(() => { f.style.width = p + "%"; });
	return h("div", { class: "meter" },
		h("div", { class: "meter-row" }, h("span", {}, label), h("span", { class: "mono" }, text || `${bytes(used)} / ${bytes(total)} (${p}%)`)),
		h("div", { class: "track" }, f));
}
function chip(state) {
	const cls = state === "active" ? "good" : state === "failed" ? "bad" : state === "activating" ? "warn" : "dim";
	return h("span", { class: "chip " + cls }, state);
}
function table(cols, rows, empty) {
	return h("div", { class: "table-wrap" }, h("table", {},
		h("thead", {}, h("tr", {}, cols.map(c => h("th", {}, c)))),
		h("tbody", {}, rows.length ? rows.map(r => h("tr", {}, r.map(c => h("td", c instanceof Node ? {} : { class: "mono" }, c))))
			: h("tr", {}, h("td", { class: "empty", colspan: cols.length }, empty || "nothing here")))));
}
function field(label, input, hint) {
	return [h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null];
}
function input(name, value, attrs = {}) { return h("input", Object.assign({ name, value: value ?? "", class: "mono", autocomplete: "off", spellcheck: "false" }, attrs)); }
function toggle(name, on, label) {
	return h("label", { class: "switch" }, h("input", { type: "checkbox", name, checked: on === true || on === "1" }), label || "");
}
function select(name, value, options) {
	return h("select", { name }, options.map(([v, t]) => h("option", { value: v, selected: v === value ? "" : null }, t)));
}
function formData(form) {
	const d = {};
	for (const el of form.querySelectorAll("input[name], select[name], textarea[name]")) {
		d[el.name] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value.trim();
	}
	return d;
}

// ---- shell -------------------------------------------------------------------------------

function applyTheme() {
	const t = localStorageGet("rise-theme");
	if (t) document.documentElement.setAttribute("data-theme", t);
	else document.documentElement.removeAttribute("data-theme");
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function localStorageSet(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) { /* private mode */ } }
function isDark() {
	const t = document.documentElement.getAttribute("data-theme");
	return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}

function brand(host) {
	const logo = h("span", { class: "logo" });
	fetch("logo.svg").then(r => r.text()).then(t => {
		const doc = new DOMParser().parseFromString(t, "image/svg+xml");
		logo.append(document.importNode(doc.documentElement, true));
	}).catch(() => {});
	return h("a", { class: "brand", href: "#status" }, logo, h("span", { class: "host" }, host || "RISE Linux"));
}

function renderShell() {
	const app = document.getElementById("app");
	const pill = h("span", { class: "pill", id: "pill", title: "Live refresh; click to pause" }, "Refreshing");
	pill.addEventListener("click", () => {
		pill.classList.toggle("paused");
		pill.textContent = pill.classList.contains("paused") ? "Paused" : "Refreshing";
	});
	const themeBtn = h("button", { class: "iconbtn", type: "button", "aria-label": "Light or dark" }, icon(isDark() ? ICONS.sun : ICONS.moon));
	themeBtn.addEventListener("click", () => {
		localStorageSet("rise-theme", isDark() ? "light" : "dark");
		applyTheme();
		themeBtn.replaceChildren(icon(isDark() ? ICONS.sun : ICONS.moon));
	});
	const out = h("button", { class: "iconbtn", type: "button", "aria-label": "Log out", title: "Log out" }, icon(ICONS.logout));
	out.addEventListener("click", async () => { try { await api("logout", {}); } catch (e) { /* gone */ } S.user = null; renderLogin(); });
	app.replaceChildren(
		h("header", { class: "bar" }, h("div", { class: "bar-inner" },
			h("span", { id: "brand" }, brand(S.ov && S.ov.hostname)),
			h("ul", { class: "nav" }, PAGES.map(([id, t]) => h("li", {}, h("a", { href: "#" + id, "data-page": id }, t)))),
			h("div", { class: "tools" }, pill, themeBtn, out))),
		h("main", { id: "main" }),
		h("div", { class: "foot" }, "RISE Linux · RISE Internet Security Environment · ", TAGLINE));
}

function live() {
	const p = document.getElementById("pill");
	return !document.hidden && !(p && p.classList.contains("paused"));
}

async function route() {
	if (!S.user) return;
	const id = (location.hash || "#status").slice(1);
	S.page = PAGES.some(p => p[0] === id) ? id : "status";
	document.querySelectorAll(".nav a").forEach(a => a.setAttribute("aria-current", a.dataset.page === S.page ? "page" : "false"));
	clearInterval(S.timer);
	const main = document.getElementById("main");
	main.replaceChildren();
	try { await VIEWS[S.page](main); } catch (e) { if (e.message !== "login") put(main, h("div", { class: "alert warn" }, e.message)); }
}

function put(el, ...kids) { el.append(...kids.flat().filter(k => k !== null && k !== undefined && k !== false)); }
function pageHead(title, sub) { return h("div", { class: "page-head" }, h("h1", {}, title), sub ? h("p", {}, sub) : null); }

// ---- login ---------------------------------------------------------------------------------

function renderLogin() {
	clearInterval(S.timer);
	const err = h("div", { class: "err", role: "alert" });
	const user = h("input", { name: "user", autocomplete: "username", placeholder: "User", required: true, autofocus: true });
	const pw = h("input", { name: "password", type: "password", autocomplete: "current-password", placeholder: "Password", required: true });
	const btn = h("button", { class: "btn primary", type: "submit" }, "Log in");
	const form = h("form", {}, user, pw, btn, err);
	form.addEventListener("submit", async ev => {
		ev.preventDefault();
		btn.disabled = true; err.textContent = "";
		try {
			const r = await api("login", { user: user.value, password: pw.value });
			S.user = r.user; S.csrf = r.csrf;
			start();
		} catch (e) { err.textContent = e.message; pw.value = ""; pw.focus(); }
		btn.disabled = false;
	});
	document.getElementById("app").replaceChildren(h("div", { class: "login" },
		h("div", { class: "card" }, brand("RISE Linux"), h("p", { class: "tag" }, TAGLINE), form)));
	user.focus();
}

// ---- status -----------------------------------------------------------------------------------

const COLORS = () => {
	const cs = getComputedStyle(document.documentElement);
	return { wrx: cs.getPropertyValue("--accent").trim(), wtx: cs.getPropertyValue("--good").trim(),
		vrx: cs.getPropertyValue("--warn").trim(), vtx: cs.getPropertyValue("--danger").trim(),
		grid: cs.getPropertyValue("--border").trim(), text: cs.getPropertyValue("--dim").trim() };
};

function drawChart(canvas, series, n) {
	const dpr = window.devicePixelRatio || 1;
	const w = canvas.clientWidth, hgt = canvas.clientHeight;
	if (!w) return;
	canvas.width = w * dpr; canvas.height = hgt * dpr;
	const g = canvas.getContext("2d");
	g.scale(dpr, dpr);
	const c = COLORS();
	let max = 1000;
	for (const s of series) for (const v of s.data) max = Math.max(max, v);
	// a round top: 1, 2 or 5 times a power of ten
	const p = Math.pow(10, Math.floor(Math.log10(max)));
	max = [1, 2, 5, 10].map(m => m * p).find(m => m >= max);
	const L = 70, R = 8, T = 8, B = 20, W = w - L - R, H = hgt - T - B;
	g.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
	g.fillStyle = c.text; g.strokeStyle = c.grid; g.lineWidth = 1;
	for (let i = 0; i <= 4; i++) {
		const y = T + H - H * i / 4;
		g.globalAlpha = .6; g.beginPath(); g.moveTo(L, y + .5); g.lineTo(L + W, y + .5); g.stroke(); g.globalAlpha = 1;
		g.fillText(bps(max * i / 4), 4, y + 4);
	}
	g.fillText(`${Math.round(n / 60)} min`, L, hgt - 4);
	g.fillText("now", L + W - 22, hgt - 4);
	for (const s of series) {
		g.strokeStyle = s.color; g.lineWidth = 1.8; g.beginPath();
		const d = s.data, off = n - d.length;
		d.forEach((v, i) => {
			const x = L + W * (off + i) / Math.max(n - 1, 1), y = T + H - H * v / max;
			i ? g.lineTo(x, y) : g.moveTo(x, y);
		});
		g.stroke();
		g.globalAlpha = .08; g.lineTo(L + W, T + H); g.lineTo(L + W * off / Math.max(n - 1, 1), T + H); g.closePath();
		g.fillStyle = s.color; g.fill(); g.globalAlpha = 1;
	}
}

function portCard(name, info, rate) {
	const up = info && info.up;
	const addr = info ? [...(info.v4 || []), ...(info.v6 || [])].join("  ") : "";
	return h("div", { class: "port" },
		h("div", { class: "port-head" }, h("span", {}, name), h("span", { class: "dot" + (up ? " on" : "") })),
		h("div", { class: "line" + (up ? "" : " off") }),
		h("div", { class: "port-foot" },
			h("span", { class: "speed" + (up ? "" : " off") }, up ? (info.speed ? (info.speed >= 1000 ? info.speed / 1000 + " GbE" : info.speed + " Mb") : "up") : "no link"),
			h("span", { class: "tot" }, "▲ " + bytes(rate ? rate.tx_bytes : 0), h("br"), "▼ " + bytes(rate ? rate.rx_bytes : 0))),
		addr ? h("div", { class: "addr" }, addr) : null);
}

async function viewStatus(main) {
	const ov = S.ov = await api("overview");
	document.getElementById("brand").replaceChildren(brand(ov.hostname));
	const sysBody = h("div"), memBody = h("div"), diskBody = h("div"), portsBody = h("div", { class: "ports" });
	const svcBody = h("div", { class: "svc" }), peakBody = h("div", { class: "stats" });
	const canvas = h("canvas", { class: "chart" });
	const legend = h("div", { class: "legend" });
	const nowStats = h("div", { class: "stats" });
	put(main, pageHead("Status", ov.rise),
		ov.firstboot ? h("div", { class: "alert warn" }, "First boot: SoftEther is being configured; its ports open when that is done.") : null,
		h("div", { class: "grid" },
			card("System", sysBody), h("div", {}, card("Memory", memBody), card("Storage", diskBody)),
			card("Traffic", [nowStats, canvas, legend], { wide: true, sub: "last 5 minutes" }),
			card("Peak speed", peakBody, { wide: true }),
			card("Port status", portsBody, { wide: true }),
			card("Services", svcBody)));

	const fill = (ov) => {
		sysBody.replaceChildren(kv([
			["Hostname", ov.hostname], ["Version", ov.rise + (ov.build ? ` (build ${ov.build})` : "")],
			["Base", ov.os], ["Kernel", ov.kernel], ["SoftEther VPN", ov.softether || "-"],
			["Processor", `${ov.cpu} × ${ov.cpus}`], ["CPU load", ov.cpu_percent === null ? "Calculating..." : ov.cpu_percent + " %"],
			["Load average", ov.load.map(x => x.toFixed(2)).join(", ")],
			["Local time", ov.time], ["Uptime", duration(ov.uptime)],
			["NAT", `IPv4 ${ov.fullcone.v4 ? "full cone" : "masquerade"} · IPv6 ${ov.conf.NAT66 === "1" ? (ov.fullcone.v6 ? "full cone NAT66" : "NAT66") : "off"}`],
			["Certificate", ov.cert.present ? `${ov.domain}: ${ov.cert.days} days left` : (ov.domain ? `${ov.domain}: not issued yet` : "self-signed (no domain, VPN by IP address)")],
		]));
		const m = ov.memory;
		memBody.replaceChildren(
			meter("Total available", m.available, m.total), meter("Used", m.total - m.available, m.total),
			meter("Buffered", m.buffers, m.total), meter("Cached", m.cached, m.total));
		diskBody.replaceChildren(meter("Disk space", ov.disk.total - ov.disk.free, ov.disk.total));
		svcBody.replaceChildren(...Object.entries(ov.services).flatMap(([u, st]) => {
			const short = { "rise-vpnserver": "vpnserver", "rise-dnsmasq": "dhcp", "rise-firewall": "firewall",
				"rise-webgui": "webgui", "rise-stats": "stats", "ssh": "ssh" }[u];
			return [h("span", { class: "name" }, u), chip(st),
				act("Restart", "service", { name: short, action: "restart" }, { cls: "small",
					confirm: u === "rise-vpnserver" ? "Restart SoftEther? Every VPN session drops." : null })];
		}));
	};
	fill(ov);

	const tick = async () => {
		if (!live()) return;
		let r;
		try { r = await api("rates"); } catch (e) { return; }
		if (!r || !r.recent) return;
		const rec = r.recent, c = COLORS();
		const series = [
			{ name: "WAN ▼ RX", color: c.wrx, data: rec.map(x => x[1]) }, { name: "WAN ▲ TX", color: c.wtx, data: rec.map(x => x[2]) },
			{ name: "VPN ▼ RX", color: c.vrx, data: rec.map(x => x[3]) }, { name: "VPN ▲ TX", color: c.vtx, data: rec.map(x => x[4]) }];
		drawChart(canvas, series, 300);
		const last = rec[rec.length - 1] || [0, 0, 0, 0, 0];
		legend.replaceChildren(...series.map(s => h("span", {}, h("i", { "data-c": s.color }), s.name)));
		legend.querySelectorAll("i").forEach(i => { i.style.background = i.dataset.c; });
		nowStats.replaceChildren(
			h("div", { class: "stat" }, h("div", { class: "label" }, `${r.wan} ▼ download`), h("div", { class: "value" }, bps(last[1]))),
			h("div", { class: "stat" }, h("div", { class: "label" }, `${r.wan} ▲ upload`), h("div", { class: "value" }, bps(last[2]))),
			h("div", { class: "stat" }, h("div", { class: "label" }, `VPN ▼ from clients`), h("div", { class: "value" }, bps(last[3]))),
			h("div", { class: "stat" }, h("div", { class: "label" }, `VPN ▲ to clients`), h("div", { class: "value" }, bps(last[4]))));
		const pk = r.peaks || {};
		const peak = (k, t) => {
			const p = pk[k] || {};
			return h("div", { class: "stat" }, h("div", { class: "label" }, t),
				h("div", { class: "value" }, bps(p.bps)), h("div", { class: "when" }, when(p.at)),
				h("div", { class: "when" }, "today " + bps(p.day_bps)));
		};
		peakBody.replaceChildren(peak("wan_rx", "WAN RX peak"), peak("wan_tx", "WAN TX peak"), peak("vpn_rx", "VPN RX peak"), peak("vpn_tx", "VPN TX peak"));
		portsBody.replaceChildren(portCard(r.wan, S.ov.wan, r.rates[r.wan]), portCard(r.vpn, S.ov.vpn, r.rates[r.vpn]),
			...Object.keys(r.rates).filter(n => n !== r.wan && n !== r.vpn && n !== "lo").map(n => portCard(n, null, r.rates[n])));
	};
	let slow = 0;
	await tick();
	S.timer = setInterval(async () => {
		await tick();
		if (++slow % 5 === 0 && live()) { try { S.ov = await api("overview"); fill(S.ov); } catch (e) { /* next time */ } }
	}, 2000);
}

// ---- VPN ----------------------------------------------------------------------------------------

async function viewVpn(main) {
	const [conf, ses, users, psk] = await Promise.all([api("settings"), api("sessions"), api("users"), api("psk")]);
	const sesBody = h("div"), usersBody = h("div");
	const fillSessions = s => {
		if (!s.ok) { sesBody.replaceChildren(h("div", { class: "alert warn" }, "SoftEther did not answer.")); return; }
		const pick = (r, keys) => keys.map(k => r[k] ?? "").find(Boolean) || "";
		sesBody.replaceChildren(table(["Session", "User", "Source", "Transfer", ""], s.sessions.map(r => {
			const name = pick(r, ["Session Name", Object.keys(r)[0]]);
			return [name, pick(r, ["User Name"]), pick(r, ["Source Host Name", "Location"]), pick(r, ["Transfer Bytes"]),
				name.startsWith("SID-LOCALBRIDGE") ? "" : act("Disconnect", "vpn", { action: "disconnect", session: name },
					{ cls: "small danger", confirm: `Disconnect ${name}?`, after: async () => fillSessions(await api("sessions")) })];
		}), "no VPN sessions"));
	};
	fillSessions(ses);
	usersBody.replaceChildren(users.ok ? table(["User", "Full name", "Auth", "Logins", "Last login", "Expires"],
		users.users.map(u => [u["User Name"] || "", u["Full Name"] || "", u["Auth Method"] || "", u["Num Logins"] || "",
			u["Last Login"] || "", u["Expiration Date"] || ""]), "no users yet: add them with SoftEther VPN Server Manager")
		: h("div", { class: "alert warn" }, "SoftEther did not answer."));

	const net = h("form", { class: "form" },
		field("Gateway IPv4", input("VPN_IPV4", conf.VPN_IPV4), "this server's address on the VPN side"),
		field("DHCP range", h("div", { class: "inline" }, input("VPN_DHCP_START", conf.VPN_DHCP_START), "–", input("VPN_DHCP_END", conf.VPN_DHCP_END))),
		field("Lease time", input("VPN_DHCP_LEASE", conf.VPN_DHCP_LEASE)),
		field("DNS for clients (IPv4)", input("VPN_DNS4", conf.VPN_DNS4)),
		field("IPv6 for clients", toggle("VPN_IPV6_ENABLE", conf.VPN_IPV6_ENABLE, "SLAAC and DHCPv6, NAT66")),
		field("Gateway IPv6", input("VPN_IPV6", conf.VPN_IPV6)),
		field("DNS for clients (IPv6)", input("VPN_DNS6", conf.VPN_DNS6)),
		field("MTU", input("VPN_MTU", conf.VPN_MTU, { type: "number", min: 1280, max: 1500 }),
			"1350 fits L2TP/IPsec over IPv6 over PPPoE, the largest overhead"),
		field("Proxy-ARP /32", toggle("VPN_PROXY_ARP", conf.VPN_PROXY_ARP, "clients get /32 and route everything through here")),
		field("Isolate clients", toggle("VPN_ISOLATION", conf.VPN_ISOLATION, "no client-to-client traffic")),
		field("Ping the gateway", toggle("VPN_ALLOW_PING", conf.VPN_ALLOW_PING, "clients may ping the gateway (nothing else here)")),
		field("Block private networks", toggle("VPN_BLOCK_LAN", conf.VPN_BLOCK_LAN, "keep clients off LANs beside this server")));
	const pskIn = input("psk", psk.psk);
	put(main, pageHead("VPN", `${conf.VPN_DOMAIN || ""} · hub ${conf.VPN_HUB}`),
		h("div", { class: "grid" },
			card("Sessions", sesBody, { wide: true }),
			card("Users", [usersBody, h("p", { class: "hint" }, "Users, groups and hub policies are managed with SoftEther VPN Server Manager, connecting to ", h("span", { class: "mono" }, `${conf.VPN_DOMAIN || "this server"}:443`), " with the server admin password.")], { wide: true }),
			card("VPN network", [net, h("div", { class: "actions" }, act("Save & Apply", "settings", () => formData(net), { cls: "primary" }))]),
			h("div", {},
				card("Connect", kv([
					["Server", conf.VPN_DOMAIN || ((S.ov && S.ov.wan.v4[0]) || "").split("/")[0] || "-"],
					["Certificate", conf.VPN_DOMAIN ? "Let's Encrypt" : "self-signed"],
					["TAP MAC", conf.VPN_TAP_MAC || "chosen by SoftEther"], ["SoftEther / SSTP", "TCP " + (conf.SOFTETHER_TCP_PORTS || "").split(" ").join(", ")],
					["OpenVPN", "UDP 1194, TCP 443/1194"], ["L2TP/IPsec", "UDP 500, 4500"], ["Hub", conf.VPN_HUB]])),
				card("IPsec pre-shared key", [h("div", { class: "inline" }, pskIn,
					act("Set", "vpn", () => ({ action: "psk", psk: pskIn.value.trim() }), { cls: "primary" })),
					psk.weak ? h("p", { class: "hint" }, h("span", { class: "chip warn" }, "weak"),
						" Easy to guess: whoever knows it can pose as this server to L2TP/IPsec clients.") : null]),
				card("SoftEther", h("div", { class: "actions" },
					act("Restart SoftEther", "service", { name: "vpnserver", action: "restart" }, { cls: "danger", confirm: "Every VPN session drops. Restart?" }))))));
	S.timer = setInterval(async () => { if (live()) { try { fillSessions(await api("sessions")); } catch (e) { /* later */ } } }, 10000);
}

// ---- network ------------------------------------------------------------------------------------

async function viewNetwork(main) {
	const [n, conf, fw] = await Promise.all([api("network"), api("settings"), api("firewall")]);
	const wan = h("form", { class: "form" },
		field("IPv4", select("v4", n.v4, [["dhcp", "DHCP"], ["static", "Static"]])),
		field("Address/prefix", input("addr4", n.addr4, { placeholder: "203.0.113.10/24" })),
		field("Gateway", input("gw4", n.gw4)),
		field("IPv6", select("v6", n.v6, [["auto", "Automatic (RA, DHCPv6)"], ["static", "Static"], ["off", "Disabled"]])),
		field("Address/prefix", input("addr6", n.addr6, { placeholder: "2001:db8::10/64" })),
		field("Gateway", input("gw6", n.gw6)),
		field("DNS servers", input("dns", n.dns), "spaces between; needed with a static IPv4"));
	const sync = () => {
		const d = formData(wan);
		wan.querySelectorAll("[name=addr4],[name=gw4]").forEach(e => { e.disabled = d.v4 !== "static"; });
		wan.querySelectorAll("[name=addr6],[name=gw6]").forEach(e => { e.disabled = d.v6 !== "static"; });
	};
	wan.addEventListener("change", sync); sync();
	const nat = h("form", { class: "form" },
		field("Full cone NAT", toggle("NAT_FULLCONE", conf.NAT_FULLCONE, "endpoint-independent mapping and filtering (xt_FULLCONENAT)")),
		field("NAT66", toggle("NAT66", conf.NAT66, "the VPN's IPv6 prefix leaves through the WAN's global address")),
		field("UDP acceleration", toggle("SOFTETHER_UDP_ACCEL", conf.SOFTETHER_UDP_ACCEL, "open UDP 1024-65535 for SoftEther UDP acceleration")),
		field("VPN over DNS", toggle("SOFTETHER_VPN_OVER_DNS", conf.SOFTETHER_VPN_OVER_DNS, "UDP 53")),
		field("VPN over ICMP", toggle("SOFTETHER_VPN_OVER_ICMP", conf.SOFTETHER_VPN_OVER_ICMP)));
	const live4 = n.live || {};
	put(main, pageHead("Network", `WAN ${n.iface}`),
		h("div", { class: "grid" },
			card(`WAN (${n.iface})`, [kv([["Now", [...(live4.v4 || []), ...(live4.v6 || [])].join("  ") || "no address"], ["MAC", live4.mac || "-"], ["MTU", live4.mtu || "-"]]),
				h("p", {}), wan,
				h("div", { class: "actions" }, act("Save & Apply", "network", () => formData(wan),
					{ cls: "primary", confirm: "Apply the WAN settings? A wrong address cuts this page off (the console still works)." }))]),
			card("NAT and SoftEther ports", [nat, h("div", { class: "actions" }, act("Save & Apply", "settings", () => formData(nat), { cls: "primary" }))]),
			card("Firewall rules", h("pre", { class: "log" }, fw.text || ""), { wide: true, sub: "generated from the settings" })));
	main.querySelector(".grid > .card:last-child").classList.add("closed");
}

// ---- port forwarding ------------------------------------------------------------------------------

async function viewForwarding(main) {
	const conf = await api("settings");
	const list = h("div");
	const fill = async () => {
		const rules = await api("portfwd");
		list.replaceChildren(table(["", "Family", "Protocol", "External port", "Destination", "Port", "Note", ""], rules.map(r => [
			h("label", { class: "switch" }, (() => {
				const cb = h("input", { type: "checkbox", checked: r.enabled, "aria-label": "Enabled" });
				cb.addEventListener("change", async () => { try { const x = await api("portfwd", { op: "toggle", index: r.index }); toast(x.message); } catch (e) { toast(e.message, false); } fill(); });
				return cb;
			})()),
			"IPv" + r.family, r.proto, r.port, r.dest, r.dport === "-" ? r.port : r.dport, r.note,
			act("Delete", "portfwd", { op: "delete", index: r.index }, { cls: "small danger", confirm: "Delete this forward?", after: fill })]),
			"no port forwards"));
	};
	await fill();
	const add = h("form", { class: "form" },
		field("Family", select("family", "4", [["4", "IPv4"], ["6", "IPv6 (to a client's ULA, through NAT66)"]])),
		field("Protocol", select("proto", "tcp", [["tcp", "TCP"], ["udp", "UDP"], ["both", "TCP and UDP"]])),
		field("External port", input("port", "", { placeholder: "8080 or 10000-10010" })),
		field("Destination", input("dest", "", { placeholder: conf.VPN_IPV4 ? conf.VPN_IPV4.replace(/\.\d+\/\d+$/, ".50") : "" }), "a VPN client's address: give it a static lease on the DHCP page"),
		field("Destination port", input("dport", "", { placeholder: "same as external" })),
		field("Note", h("input", { name: "note", value: "", maxlength: 80 })));
	put(main, pageHead("Port Forwarding", `from ${conf.WAN_IF} to VPN clients`),
		card("Forwards", list),
		card("Add a forward", [add, h("div", { class: "actions" },
			act("Add", "portfwd", () => Object.assign(formData(add), { op: "add" }), { cls: "primary", after: () => { add.reset(); fill(); } }))]));
}

// ---- DHCP ------------------------------------------------------------------------------------------

async function viewDhcp(main) {
	const conf = await api("settings");
	const leasesBody = h("div"), staticBody = h("div");
	const fill = async () => {
		const d = await api("dhcp");
		leasesBody.replaceChildren(table(["Hostname", "Address", "MAC", "Expires", ""], d.leases.map(l => [
			l.name || "-", l.ip, l.mac || "-", l.expires ? when(l.expires) : "never",
			l.mac && !l.ip.includes(":") ? h("button", { class: "btn small", type: "button", onclick: () => {
				add.querySelector("[name=mac]").value = l.mac; add.querySelector("[name=ip]").value = l.ip;
				add.querySelector("[name=name]").value = l.name || ""; add.scrollIntoView({ behavior: "smooth" });
			} }, "Make static") : ""]), "no active leases"));
		staticBody.replaceChildren(table(["MAC", "IPv4", "IPv6", "Hostname", ""], d.static.map(s => [s.mac, s.ip, s.ip6, s.name,
			act("Delete", "dhcp", { op: "delete", index: s.index }, { cls: "small danger", confirm: `Delete the lease of ${s.mac}?`, after: fill })]),
			"no static leases"));
	};
	const add = h("form", { class: "form" },
		field("MAC address", input("mac", "", { placeholder: "aa:bb:cc:dd:ee:ff" })),
		field("IPv4", input("ip", "", { placeholder: conf.VPN_DHCP_START })),
		field("IPv6 (optional)", input("ip6", "")),
		field("Hostname (optional)", input("name", "")));
	await fill();
	put(main, pageHead("DHCP", `${conf.VPN_DHCP_START} – ${conf.VPN_DHCP_END}, lease ${conf.VPN_DHCP_LEASE}`),
		h("div", { class: "grid" },
			card("Active leases", leasesBody, { wide: true }),
			card("Static leases", staticBody, { wide: true }),
			card("Add a static lease", [add, h("div", { class: "actions" },
				act("Add", "dhcp", () => Object.assign(formData(add), { op: "add" }), { cls: "primary", after: () => { add.reset(); fill(); } }))]),
			card("DHCP server", h("div", { class: "actions" }, act("Restart DHCP", "service", { name: "dhcp", action: "restart" })))));
	S.timer = setInterval(() => { if (live()) fill().catch(() => {}); }, 15000);
}

// ---- system ------------------------------------------------------------------------------------------

async function viewSystem(main) {
	const [ov, conf] = await Promise.all([api("overview"), api("settings")]);
	const host = input("hostname", ov.hostname);
	const tz = input("timezone", Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
	const mode = select("mode", conf.CONSOLE_MODE || "1024x768",
		["1024x768", "1280x720", "1280x1024", "1600x900", "1920x1080", "2560x1440", "3840x2160", "native"].map(m => [m, m === "native" ? "Native (the monitor's own)" : m]));
	const scale = select("scale", conf.CONSOLE_FONT_SCALE || "auto", [["auto", "Auto (double from 1920 wide)"], ["1", "8x16"], ["2", "16x32"]]);
	const dom = input("domain", conf.VPN_DOMAIN), mail = input("email", conf.ACME_EMAIL);
	const pw = h("form", { class: "form" },
		field("Current password", h("input", { type: "password", name: "current", autocomplete: "current-password" })),
		field("User", input("user", S.user)),
		field("New password", h("input", { type: "password", name: "new", autocomplete: "new-password", minlength: 8 })));
	const logSel = select("unit", "vpnserver", [["vpnserver", "SoftEther"], ["dhcp", "DHCP"], ["firewall", "Firewall"], ["cert", "Certificate"],
		["firstboot", "First boot"], ["webgui", "WebGUI"], ["ssh", "SSH"], ["kernel", "Kernel"], ["system", "Everything"]]);
	const logOut = h("pre", { class: "log" }, "");
	const loadLog = async () => { const r = await api("logs?unit=" + encodeURIComponent(logSel.value) + "&n=300"); logOut.textContent = r.text || "(empty)"; logOut.scrollTop = logOut.scrollHeight; };
	logSel.addEventListener("change", loadLog);
	put(main, pageHead("System", ov.rise),
		h("div", { class: "grid" },
			card("Identity", h("div", { class: "form" },
				field("Hostname", h("div", { class: "inline" }, host, act("Set", "system", () => ({ action: "hostname", hostname: host.value.trim() })))),
				field("Time zone", h("div", { class: "inline" }, tz, act("Set", "system", () => ({ action: "timezone", timezone: tz.value.trim() })))))),
			card("Certificate", [kv([["Domain", conf.VPN_DOMAIN || "-"],
				["Status", ov.cert.present ? `valid, ${ov.cert.days} days left` : "not issued"],
				["Expires", ov.cert.expires || "-"], ["Issuer", ov.cert.issuer || "-"]]),
				h("p", { class: "hint" }, "Let's Encrypt checks the domain over HTTP on port 80. It renews by itself and is loaded into SoftEther live."),
				h("div", { class: "form" }, field("Domain", dom), field("E-mail", mail)),
				h("div", { class: "actions" },
					act("Change domain & request", "cert", () => ({ action: "domain", domain: dom.value.trim(), email: mail.value.trim() })),
					act("Request now", "cert", { action: "obtain" }), act("Renew", "cert", { action: "renew" }, { confirm: "Force a renewal now?" }))]),
			card("Console", [h("div", { class: "form" },
				field("Resolution", h("div", { class: "inline" }, mode, act("Set", "system", () => ({ action: "console", mode: mode.value })))),
				field("Font", h("div", { class: "inline" }, scale, act("Set", "system", () => ({ action: "fontscale", scale: scale.value }))))),
				h("p", { class: "hint" }, "Unifont, the Ubuntu Server installer's font. The resolution applies from the next boot.")]),
			card("WebGUI login", [pw, h("div", { class: "actions" }, act("Change password", "password", () => formData(pw),
				{ cls: "primary", after: r => { if (r.ok !== false) { S.user = null; renderLogin(); } } }))]),
			card("Power", h("div", { class: "actions" },
				act("Reboot", "system", { action: "reboot" }, { cls: "danger", confirm: "Reboot the server? VPN sessions drop." }),
				act("Shut down", "system", { action: "poweroff" }, { cls: "danger", confirm: "Shut the server down? It needs someone at the machine to come back." }))),
			card("Logs", [h("div", { class: "inline" }, logSel, h("button", { class: "btn", type: "button", onclick: loadLog }, "Refresh")), h("p"), logOut], { wide: true })));
	loadLog();
}

const VIEWS = { status: viewStatus, vpn: viewVpn, network: viewNetwork, forwarding: viewForwarding, dhcp: viewDhcp, system: viewSystem };

// ---- start ---------------------------------------------------------------------------------------------

async function start() {
	try { S.ov = await api("overview"); } catch (e) { return; }
	document.title = `${S.ov.hostname} · RISE Linux`;
	renderShell();
	route();
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { /* the poll checks live() */ });
applyTheme();
(async () => {
	try {
		const s = await api("session");
		if (s.user) { S.user = s.user; S.csrf = s.csrf; start(); } else renderLogin();
	} catch (e) { renderLogin(); }
})();
