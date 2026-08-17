#!/usr/bin/env python3
"""chrome-pick beacon: identify which connected Chrome browser is which machine.

Starts an ephemeral HTTP server on this Mac, has each browser fetch a probe URL,
and classifies each browser by the source IP the beacon observes.

Stdlib only. Run with /usr/bin/python3 so an active venv/asdf shim cannot interfere.

Every subcommand prints exactly one JSON object to stdout and nothing else.
Exit 0 on success, 1 on error (payload is then {"error": "..."}).

Subcommands:
  plan                                  candidate probe IPs, no side effects
  start   [--ttl N]                     daemonize the beacon, print connection details
  selftest                              local reachability check (see caveat below)
  status  --devices <csv>               hits so far + live classification
  stop    --devices <csv>               final classification, kill server, delete state
  serve   --token T [--port P --ttl N]  INTERNAL: spawned by start, never called directly
  fingerprint                           current LAN fingerprint (gateway ip/mac)

CAVEAT: selftest proves the socket is bound and not blocked locally. It CANNOT
prove a remote host can reach it -- traffic from this Mac to its own LAN IP is
short-circuited internally and never crosses the wire.
"""

import argparse
import errno
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import secrets

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

RUNTIME_DIR = os.path.expanduser("~/.claude/chrome-pick")
STATE_PATH = os.path.join(RUNTIME_DIR, "beacon-state.json")
STDERR_LOG = os.path.join(RUNTIME_DIR, "beacon-stderr.log")

DEFAULT_TTL = 300
START_POLL_SECONDS = 5.0

IFCONFIG = "/sbin/ifconfig"
ROUTE = "/sbin/route"
PING = "/sbin/ping"
ARP = "/usr/sbin/arp"
SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
TAILSCALE_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)

KIND_ORDER = {"lan": 0, "cgnat_vpn": 1, "tunnel": 2, "public": 3}

PROBE_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<title>chrome-pick</title>"
    "<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;"
    "margin:3rem auto;max-width:34rem;padding:0 1.5rem;color:#222}"
    "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:.5rem 0}"
    "code{background:#f2f2f2;padding:.1rem .3rem;border-radius:3px}</style>"
    "<h1>chrome-pick &mdash; this browser has been identified</h1>"
    "<p>Claude Code opened this tab only to work out which machine this Chrome "
    "is running on, by looking at the network address the request came from.</p>"
    "<p>Nothing was read from this browser and nothing was sent anywhere. "
    "You can close this tab.</p>"
    "<p><code>{device}</code></p>"
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _out(payload, code=0):
    """Print one JSON object and exit."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(code)


def _fail(message, **extra):
    payload = {"error": message}
    payload.update(extra)
    _out(payload, code=1)


def _run(cmd, timeout=4):
    """Run a command, return stdout text ('' on any failure)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", "replace")


def _now():
    return time.time()


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else _now()))


def _atomic_write(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_state():
    try:
        with open(STATE_PATH) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _parse_devices(raw):
    if not raw:
        return []
    seen = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


# --------------------------------------------------------------------------
# IP discovery
# --------------------------------------------------------------------------


def _is_private(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if octets[0] == 10:
        return True
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return True
    if octets[0] == 192 and octets[1] == 168:
        return True
    return False


def _is_cgnat(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127


def _is_loopback(ip):
    return ip.startswith("127.")


def default_interface():
    text = _run([ROUTE, "-n", "get", "default"])
    match = re.search(r"interface:\s*(\S+)", text)
    return match.group(1) if match else None


def default_gateway():
    text = _run([ROUTE, "-n", "get", "default"])
    match = re.search(r"gateway:\s*(\S+)", text)
    return match.group(1) if match else None


def discover_candidates():
    """Parse ifconfig into probe candidates, best first."""
    text = _run([IFCONFIG, "-a"], timeout=6)
    default_iface = default_interface()

    candidates = []
    iface = None
    pointopoint = False

    for line in text.splitlines():
        header = re.match(r"^([A-Za-z0-9_.:-]+):\s*flags=", line)
        if header:
            iface = header.group(1)
            pointopoint = "POINTOPOINT" in line
            continue
        if iface is None:
            continue
        inet = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if not inet:
            continue
        ip = inet.group(1)
        if iface == "lo0" or _is_loopback(ip) or ip.startswith("169.254."):
            continue
        if _is_cgnat(ip):
            kind = "cgnat_vpn"
        elif pointopoint:
            kind = "tunnel"
        elif _is_private(ip):
            kind = "lan"
        else:
            kind = "public"
        candidates.append(
            {
                "ip": ip,
                "iface": iface,
                "kind": kind,
                "is_default_route": iface == default_iface,
            }
        )

    candidates.sort(
        key=lambda c: (
            KIND_ORDER.get(c["kind"], 9),
            0 if c["is_default_route"] else 1,
            c["iface"],
            c["ip"],
        )
    )
    return candidates, default_iface


def my_ips():
    """Every IPv4 address this host owns, plus loopback."""
    owned = {"127.0.0.1"}
    text = _run([IFCONFIG, "-a"], timeout=6)
    for match in re.finditer(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", text, re.M):
        owned.add(match.group(1))
    return sorted(owned)


def firewall_state():
    """0 = off, 1 = on, 2 = block-all. None if unknown."""
    if not os.path.exists(SOCKETFILTERFW):
        return None
    text = _run([SOCKETFILTERFW, "--getglobalstate"], timeout=6)
    match = re.search(r"State\s*=\s*(\d+)", text)
    if match:
        return int(match.group(1))
    if "disabled" in text.lower():
        return 0
    if "enabled" in text.lower():
        return 1
    return None


FIREWALL_WARNING = (
    "The macOS Application Firewall is on. Probes from other machines may be "
    "silently dropped, and a 'Do you want to accept incoming network "
    "connections?' dialog may appear on this Mac. A browser that does not "
    "respond may be firewalled rather than on a different network."
)


# --------------------------------------------------------------------------
# Naming enrichment
# --------------------------------------------------------------------------


def reverse_dns(ip):
    try:
        socket.setdefaulttimeout(2.0)
        host = socket.gethostbyaddr(ip)[0]
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(None)
    if not host or host == ip:
        return None
    short = host.split(".")[0]
    return short or None


def arp_mac(ip):
    """MAC for an IP. Pings first -- ARP is cold otherwise (verified)."""
    _run([PING, "-c", "1", "-W", "900", ip], timeout=3)
    text = _run([ARP, "-n", ip], timeout=3)
    match = re.search(r"\bat\s+([0-9a-fA-F:]{11,17})\b", text)
    if not match:
        return None
    mac = match.group(1).lower()
    return None if "incomplete" in text.lower() and not mac else mac


def tailscale_map():
    """Map tailnet IP -> {hostname, os}. Empty dict when tailscale is absent."""
    binary = None
    for path in TAILSCALE_CANDIDATES:
        if os.path.exists(path):
            binary = path
            break
    if binary is None:
        return {}
    text = _run([binary, "status", "--json"], timeout=8)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}

    mapping = {}

    def absorb(node):
        if not isinstance(node, dict):
            return
        hostname = node.get("HostName") or node.get("DNSName", "").split(".")[0]
        os_name = node.get("OS")
        for ip in node.get("TailscaleIPs") or []:
            if ":" in ip:
                continue
            mapping[ip] = {"hostname": hostname or None, "os": os_name or None}

    absorb(data.get("Self"))
    for peer in (data.get("Peer") or {}).values():
        absorb(peer)
    return mapping


UA_PLATFORMS = (
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Windows NT", "Windows"),
    ("CrOS", "ChromeOS"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("X11", "Linux"),
    ("Linux", "Linux"),
)


def os_hint(headers, user_agent):
    """OS from client hints if offered, else from the UA string.

    Never infer hardware: Chrome reports 'Intel Mac OS X 10_15_7' on Apple Silicon.
    """
    headers = headers or {}
    for key, value in headers.items():
        if key.lower() == "sec-ch-ua-platform" and value:
            cleaned = value.strip().strip('"')
            if cleaned and cleaned.lower() != "unknown":
                return cleaned
    ua = user_agent or ""
    for needle, name in UA_PLATFORMS:
        if needle in ua:
            return name
    return None


def lan_fingerprint():
    """gateway-ip/gateway-mac -- detects 'same browsers, different network'."""
    gateway = default_gateway()
    if not gateway:
        return None
    return "%s/%s" % (gateway, arp_mac(gateway) or "unknown")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

UNREACHABLE_PHRASE = "did not respond -- different network, or a VPN on that machine"


def classify(device_ids, hits, enrich=True):
    """Classify each deviceId from the recorded hits, in strict precedence."""
    owned = set(my_ips())
    by_device = {}
    for hit in hits:
        device = hit.get("deviceId")
        if device and device not in by_device:
            by_device[device] = hit

    # Include devices that hit us but were not asked about.
    ordered = list(device_ids)
    for device in by_device:
        if device not in ordered:
            ordered.append(device)

    ts_map = tailscale_map() if enrich else {}
    results = []

    for device in ordered:
        hit = by_device.get(device)
        record = {
            "deviceId": device,
            "hit": bool(hit),
            "sourceIp": None,
            "userAgent": None,
            "osHint": None,
            "machine": "unreachable",
            "confidence": "low",
            "hostname": None,
            "mac": None,
            "tailscaleName": None,
            "at": None,
            "note": UNREACHABLE_PHRASE,
        }

        if not hit:
            results.append(record)
            continue

        source = hit.get("sourceIp") or ""
        # Defensive: strip any IPv4-mapped IPv6 form.
        if source.startswith("::ffff:"):
            source = source[7:]

        record["sourceIp"] = source
        record["userAgent"] = hit.get("userAgent")
        record["at"] = hit.get("ts")
        record["osHint"] = os_hint(hit.get("headers"), hit.get("userAgent"))
        record["note"] = None

        if _is_loopback(source):
            record["machine"] = "this-mac"
            record["confidence"] = "high"
        elif source in owned:
            record["machine"] = "this-mac"
            record["confidence"] = "high"
        elif _is_cgnat(source):
            record["machine"] = "tailnet"
            record["confidence"] = "high"
        elif _is_private(source):
            record["machine"] = "same-lan"
            record["confidence"] = "high"
        else:
            # Reached us from a public address: not this LAN. Some NAT, relay or
            # port-forward is in play, so name it honestly rather than guessing.
            record["machine"] = "remote"
            record["confidence"] = "medium"
            record["note"] = (
                "source IP %s is not on this LAN or tailnet -- it reached the "
                "beacon via NAT, a relay, a port forward or a directly-attached "
                "link" % source
            )

        if enrich and record["machine"] in ("same-lan", "tailnet", "remote"):
            record["hostname"] = reverse_dns(source)
            if record["machine"] != "remote":
                # ARP is only meaningful for an on-link neighbour.
                record["mac"] = arp_mac(source)
            ts_entry = ts_map.get(source)
            if ts_entry:
                record["tailscaleName"] = ts_entry.get("hostname")
                if not record["osHint"] and ts_entry.get("os"):
                    record["osHint"] = ts_entry["os"]

        results.append(record)

    return results


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class _BeaconState(object):
    """In-process mirror of the state file, flushed atomically on every hit."""

    def __init__(self, base):
        self.lock = threading.Lock()
        self.data = base

    def flush(self):
        _atomic_write(STATE_PATH, self.data)

    def record_hit(self, hit):
        with self.lock:
            self.data.setdefault("hits", []).append(hit)
            self.flush()

    def record_selftest(self, entry):
        with self.lock:
            self.data.setdefault("selftests", []).append(entry)
            self.flush()


def _make_handler(state, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "chrome-pick"
        sys_version = ""

        def log_message(self, *_args):
            pass

        def _send(self, code, body, content_type="text/html; charset=utf-8"):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def do_GET(self):  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            supplied = (query.get("t") or [""])[0]

            if parsed.path == "/favicon.ico":
                self._send(404, "no favicon")
                return

            if parsed.path not in ("/probe", "/selftest"):
                self._send(404, "not found")
                return

            if not secrets.compare_digest(supplied, token):
                self._send(403, "forbidden")
                return

            source = self.client_address[0]
            if source.startswith("::ffff:"):
                source = source[7:]

            if parsed.path == "/selftest":
                state.record_selftest({"sourceIp": source, "ts": _now()})
                self._send(200, "ok", content_type="text/plain; charset=utf-8")
                return

            device = (query.get("d") or [""])[0]
            state.record_hit(
                {
                    "deviceId": device,
                    "sourceIp": source,
                    "userAgent": self.headers.get("User-Agent"),
                    "headers": dict(self.headers.items()),
                    "ts": _now(),
                }
            )
            self._send(200, PROBE_PAGE.replace("{device}", device or "(no device id)"))

    return Handler


def cmd_serve(args):
    """INTERNAL. Spawned detached by `start`; do not call directly."""
    candidates, default_iface = discover_candidates()

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", args.port), _make_handler(None, args.token))
    except OSError as exc:
        # stdout is DEVNULL in the spawned process, so hand the error back to
        # `start` through the state file instead.
        _atomic_write(
            STATE_PATH,
            {"token": args.token, "error": "could not bind beacon socket: %s" % exc},
        )
        _fail("could not bind beacon socket: %s" % exc)
        return

    port = httpd.server_address[1]
    lan_urls = {
        c["ip"]: "http://%s:%d/probe" % (c["ip"], port)
        for c in candidates
        if c["kind"] == "lan"
    }
    vpn_urls = {
        c["ip"]: "http://%s:%d/probe" % (c["ip"], port)
        for c in candidates
        if c["kind"] == "cgnat_vpn"
    }

    fw = firewall_state()
    base = {
        "pid": os.getpid(),
        "port": port,
        "token": args.token,
        "startedAt": _now(),
        "ttl": args.ttl,
        "candidates": candidates,
        "defaultInterface": default_iface,
        "hits": [],
        "selftests": [],
        "probeUrls": lan_urls,
        "vpnProbeUrls": vpn_urls,
        "loopbackProbe": "http://127.0.0.1:%d/probe" % port,
        "selftestUrlPath": "/selftest",
        "firewallState": fw,
    }
    if fw:
        base["firewallWarning"] = FIREWALL_WARNING

    state = _BeaconState(base)
    state.flush()

    # Rebind the handler now that state exists.
    httpd.RequestHandlerClass = _make_handler(state, args.token)

    def _cleanup(*_a):
        current = _read_state()
        if current and current.get("token") == args.token:
            _unlink(STATE_PATH)
        try:
            httpd.server_close()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    def _watchdog():
        # The guarantee that a port is never left listening, even if Claude
        # crashes or abandons the run.
        time.sleep(max(1, args.ttl))
        _cleanup()

    threading.Thread(target=_watchdog, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        _cleanup()


def cmd_start(args):
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    # Reap any stale beacon from an abandoned run.
    stale = _read_state()
    if stale:
        pid = stale.get("pid")
        if _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.05)
        _unlink(STATE_PATH)

    token = secrets.token_urlsafe(12)
    # `--opt=value`, never `--opt value`: token_urlsafe draws from an alphabet
    # that includes "-", so roughly one token in 64 starts with one and argparse
    # would read it as a flag rather than the value. That killed the child
    # before it could bind, and `start` could only report a 5s timeout.
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "serve",
        "--port=0",
        "--token=%s" % token,
        "--ttl=%s" % args.ttl,
    ]
    # The child reports a bind failure through the state file, but anything that
    # kills it earlier -- an import error, a signal, an exception while reading
    # the interface list -- leaves no trace at all if stderr goes to /dev/null.
    # Keep it in a log next to the state file so a timeout can say why.
    _unlink(STDERR_LOG)
    try:
        stderr_log = open(STDERR_LOG, "wb")
    except OSError:
        stderr_log = subprocess.DEVNULL

    try:
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _fail("could not spawn beacon: %s" % exc)
        return
    finally:
        if stderr_log is not subprocess.DEVNULL:
            stderr_log.close()

    deadline = _now() + START_POLL_SECONDS
    while _now() < deadline:
        state = _read_state()
        if state and state.get("token") == token:
            if state.get("error"):
                _unlink(STATE_PATH)
                _fail(state["error"])
                return
            _out(dict(state))
            return
        if child.poll() is not None:
            # It exited without reporting. Say so now rather than burning the
            # rest of the poll window on a process that is already gone.
            _fail(
                "beacon exited with status %s before reporting ready%s"
                % (child.returncode, _stderr_tail())
            )
            return
        time.sleep(0.05)

    _fail(
        "beacon did not report ready within %.0fs%s"
        % (START_POLL_SECONDS, _stderr_tail())
    )


def _stderr_tail(limit=400):
    """Last of the child's stderr, for an error message. Empty when it said nothing."""
    try:
        with open(STDERR_LOG) as handle:
            text = handle.read().strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > limit:
        text = "..." + text[-limit:]
    return " -- beacon stderr: " + text.replace("\n", " | ")


def _fetch(url, timeout=2.0):
    try:
        with urlopen(url, timeout=timeout) as response:
            response.read(64)
            return True, None
    except HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # socket.timeout and friends
        return False, str(exc)


def cmd_selftest(_args):
    state = _read_state()
    if not state:
        _fail("no beacon running (state file missing) -- run `start` first")
        return

    port = state["port"]
    token = state["token"]
    targets = ["127.0.0.1"] + [
        c["ip"] for c in state.get("candidates", []) if c["kind"] == "lan"
    ]

    results = []
    for ip in targets:
        url = "http://%s:%d/selftest?t=%s" % (ip, port, token)
        reachable, error = _fetch(url)
        results.append(
            {
                "target": ip,
                "url": url,
                "reachable": reachable,
                "error": error,
                "isLoopback": _is_loopback(ip),
            }
        )

    loopback_usable = any(r["reachable"] for r in results if r["isLoopback"])
    lan_usable = any(r["reachable"] for r in results if not r["isLoopback"])
    has_lan = any(not r["isLoopback"] for r in results)

    if lan_usable:
        mode = "full"
    elif loopback_usable:
        mode = "loopback-only"
    else:
        mode = "unusable"

    payload = {
        "selftest": results,
        "loopbackUsable": loopback_usable,
        "lanUsable": lan_usable,
        "hasLanCandidate": has_lan,
        # beaconUsable means "LAN probing is viable"; loopback-only still
        # identifies this-mac, so check `mode` before giving up entirely.
        "beaconUsable": lan_usable,
        "mode": mode,
        "caveat": (
            "A passing LAN selftest does NOT prove a remote host can reach the "
            "beacon: traffic from this Mac to its own LAN IP is short-circuited "
            "internally and never crosses the wire. It only rules out a local "
            "bind/block failure."
        ),
        "firewallState": state.get("firewallState"),
    }
    if state.get("firewallWarning"):
        payload["firewallWarning"] = state["firewallWarning"]
    _out(payload)


def cmd_status(args):
    state = _read_state()
    if not state:
        _fail("no beacon running (state file missing)")
        return

    devices = _parse_devices(args.devices)
    hits = state.get("hits", [])
    # Fast path: no name enrichment (no ping/arp/tailscale) so the probe loop
    # can poll this cheaply.
    results = classify(devices, hits, enrich=False)
    hit_ids = sorted({h.get("deviceId") for h in hits if h.get("deviceId")})

    _out(
        {
            "pid": state.get("pid"),
            "port": state.get("port"),
            "alive": _pid_alive(state.get("pid")),
            "startedAt": state.get("startedAt"),
            "ttl": state.get("ttl"),
            "expiresIn": max(
                0.0, (state.get("startedAt", 0) + state.get("ttl", 0)) - _now()
            ),
            "hitCount": len(hits),
            "devicesHit": hit_ids,
            "pendingDevices": [d for d in devices if d not in hit_ids],
            "devices": results,
        }
    )


def cmd_stop(args):
    devices = _parse_devices(args.devices)
    state = _read_state()

    warning = None
    hits = []
    if state:
        hits = state.get("hits", [])
        pid = state.get("pid")
        if _pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
            for _ in range(40):
                if not _pid_alive(pid):
                    break
                time.sleep(0.05)
            if _pid_alive(pid):
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        _unlink(STATE_PATH)
    else:
        warning = (
            "no beacon state found -- it may have expired (TTL) or already been "
            "stopped; any hits recorded after the last status call are lost"
        )

    results = classify(devices, hits, enrich=True)

    payload = {
        "stoppedAt": _iso(),
        "myIps": my_ips(),
        "lanFingerprint": lan_fingerprint(),
        "defaultInterface": (state or {}).get("defaultInterface") or default_interface(),
        "firewallState": (state or {}).get("firewallState", firewall_state()),
        "devices": results,
        "rawHits": [
            {k: v for k, v in h.items() if k != "headers"} for h in hits
        ],
        "listening": False,
    }
    if warning:
        payload["warning"] = warning
    if (state or {}).get("firewallWarning"):
        payload["firewallWarning"] = state["firewallWarning"]
    _out(payload)


def cmd_plan(_args):
    candidates, default_iface = discover_candidates()
    _out({"candidates": candidates, "defaultInterface": default_iface})


def cmd_fingerprint(_args):
    _out({"lanFingerprint": lan_fingerprint(), "defaultInterface": default_interface()})


# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(prog="beacon.py", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("plan").set_defaults(func=cmd_plan)
    sub.add_parser("fingerprint").set_defaults(func=cmd_fingerprint)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)

    p_start = sub.add_parser("start")
    p_start.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status")
    p_status.add_argument("--devices", default="")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--devices", default="")
    p_stop.set_defaults(func=cmd_stop)

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--token", required=True)
    p_serve.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        _fail("no subcommand given; try: plan | start | selftest | status | stop")
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # never emit a traceback on stdout
        _fail("%s: %s" % (type(exc).__name__, exc))
