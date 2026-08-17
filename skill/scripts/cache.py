#!/usr/bin/env python3
"""chrome-pick cache: persist and validate the deviceId -> machine mapping.

Cache file: ~/.claude/chrome-browsers.json

Stdlib only. Run with /usr/bin/python3.
Every subcommand prints exactly one JSON object to stdout and nothing else.
Exit 0 on success, 1 on error (payload is then {"error": "..."}).

Subcommands:
  read  --devices <csv>        cache status + labelled browsers for those deviceIds
  write --from-probe <path|->  merge a `beacon.py stop` report in (keeps userLabel)
  label --device <id> --label "work laptop"    set a sticky human label
  unlabel --device <id>        clear the sticky human label
  clear                        delete the cache
  show                         dump the cache verbatim

A userLabel set by the human is NEVER discarded by any re-probe or invalidation.
"""

import argparse
import calendar
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beacon  # noqa: E402  (sibling module, same directory)

CACHE_PATH = os.path.expanduser("~/.claude/chrome-browsers.json")
CACHE_VERSION = 1
STALE_AFTER_DAYS = 30
PRUNE_AFTER_DAYS = 180

MACHINE_FALLBACK_LABELS = {
    "this-mac": "this Mac",
    "same-lan": "other machine on LAN",
    "tailnet": "machine on Tailscale",
    "remote": "machine on another network",
    "unreachable": "not reachable from here",
}


def _out(payload, code=0):
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(code)


def _fail(message, **extra):
    payload = {"error": message}
    payload.update(extra)
    _out(payload, code=1)


def _parse_devices(raw):
    if not raw:
        return []
    ordered = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in ordered:
            ordered.append(part)
    return ordered


def _load():
    try:
        with open(CACHE_PATH) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "missing"
    if not isinstance(data, dict) or not isinstance(data.get("browsers"), list):
        return None, "missing"
    if data.get("version") != CACHE_VERSION:
        return data, "version-mismatch"
    return data, None


def _save(data):
    data["version"] = CACHE_VERSION
    data["updatedAt"] = beacon._iso()
    beacon._atomic_write(CACHE_PATH, data)


def _age_days(iso_ts):
    if not iso_ts:
        return None
    try:
        parsed = time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    # timegm, not mktime: the stamp is UTC and mktime would read it as local.
    return (time.time() - calendar.timegm(parsed)) / 86400.0


def derive_label(entry):
    """Human label for a browser. userLabel always wins."""
    if entry.get("userLabel"):
        return entry["userLabel"]
    machine = entry.get("machine")
    if machine == "this-mac":
        return "this Mac"
    if machine in ("same-lan", "remote"):
        return (
            entry.get("hostname")
            or entry.get("tailscaleName")
            or MACHINE_FALLBACK_LABELS.get(machine)
        )
    if machine == "tailnet":
        return (
            entry.get("tailscaleName")
            or entry.get("hostname")
            or MACHINE_FALLBACK_LABELS["tailnet"]
        )
    return MACHINE_FALLBACK_LABELS.get(machine, "unknown")


def _relabel(entry):
    entry["label"] = derive_label(entry)
    return entry


# --------------------------------------------------------------------------


def cmd_read(args):
    devices = _parse_devices(args.devices)
    data, hard_status = _load()

    if data is None:
        _out(
            {
                "status": hard_status or "missing",
                "cachePath": CACHE_PATH,
                "browsers": [],
                "unknownDevices": devices,
                "missingDevices": [],
                "mustReprobe": True,
                "reason": "no usable cache on disk",
            }
        )
        return

    browsers = [_relabel(dict(b)) for b in data.get("browsers", [])]
    cached_ids = {b.get("deviceId") for b in browsers}
    live_ids = set(devices)

    unknown = [d for d in devices if d not in cached_ids]
    missing = sorted(cached_ids - live_ids) if devices else []

    current_fp = beacon.lan_fingerprint() if args.check_network else None
    cached_fp = data.get("lanFingerprint")

    ages = [_age_days(b.get("classifiedAt")) for b in browsers]
    oldest = max([a for a in ages if a is not None] or [0.0])

    if hard_status == "version-mismatch":
        status, reason, reprobe = ("version-mismatch", "cache written by another version", True)
    elif unknown:
        # Only a *connected* browser we cannot label forces a re-probe. A cached
        # browser merely being offline leaves every remaining label correct, so
        # it is reported in `missingDevices` and nothing is invalidated --
        # otherwise a browser that is usually off would re-probe forever.
        status, reason, reprobe = (
            "device-set-changed",
            "connected browsers with no cached label: %s" % ", ".join(unknown),
            True,
        )
    elif args.check_network and cached_fp and current_fp and cached_fp != current_fp:
        status, reason, reprobe = (
            "network-changed",
            "this Mac is on a different network (%s, cached %s) -- a cached LAN "
            "address no longer identifies the same machine" % (current_fp, cached_fp),
            True,
        )
    elif oldest > STALE_AFTER_DAYS:
        status, reason, reprobe = (
            "stale",
            "oldest entry is %.0f days old -- labels are unverified" % oldest,
            False,
        )
    else:
        status, reason, reprobe = ("fresh", None, False)

    # Only surface the browsers actually connected right now, in that order.
    by_id = {b.get("deviceId"): b for b in browsers}
    if devices:
        surfaced = [by_id[d] for d in devices if d in by_id]
    else:
        surfaced = browsers

    payload = {
        "status": status,
        "cachePath": CACHE_PATH,
        "updatedAt": data.get("updatedAt"),
        "lanFingerprint": cached_fp,
        "currentLanFingerprint": current_fp,
        "browsers": surfaced,
        "unknownDevices": unknown,
        "missingDevices": missing,
        "mustReprobe": reprobe,
        "oldestEntryAgeDays": round(oldest, 1),
    }
    if reason:
        payload["reason"] = reason
    _out(payload)


def cmd_write(args):
    source = args.from_probe
    try:
        raw = sys.stdin.read() if source == "-" else open(source).read()
    except OSError as exc:
        _fail("could not read probe report: %s" % exc)
        return
    if not raw.strip():
        _fail("probe report was empty")
        return
    try:
        report = json.loads(raw)
    except ValueError as exc:
        _fail("probe report is not valid JSON: %s" % exc)
        return

    incoming = report.get("devices")
    if not isinstance(incoming, list):
        _fail("probe report has no `devices` list")
        return

    names = {}
    for pair in (args.names or "").split(","):
        if "=" in pair:
            device, _, name = pair.partition("=")
            device, name = device.strip(), name.strip()
            if device and name:
                names[device] = name

    data, _status = _load()
    if data is None or not isinstance(data.get("browsers"), list):
        data = {"version": CACHE_VERSION, "browsers": []}

    existing = {b.get("deviceId"): b for b in data.get("browsers", [])}
    classified_at = report.get("stoppedAt") or beacon._iso()

    merged = []
    for record in incoming:
        device = record.get("deviceId")
        if not device:
            continue
        prior = existing.get(device, {})
        entry = {
            "deviceId": device,
            # Display name from list_connected_browsers, if we were told it.
            "name": names.get(device) or prior.get("name"),
            "label": None,
            # A human-set label survives every re-probe.
            "userLabel": prior.get("userLabel"),
            "machine": record.get("machine"),
            "sourceIp": record.get("sourceIp"),
            "hostname": record.get("hostname"),
            "tailscaleName": record.get("tailscaleName"),
            "mac": record.get("mac"),
            "osHint": record.get("osHint"),
            "userAgent": record.get("userAgent"),
            "confidence": record.get("confidence"),
            "note": record.get("note"),
            "classifiedAt": classified_at,
        }
        merged.append(_relabel(entry))

    # Keep cached entries for browsers that were not part of this probe, so a
    # browser that reconnects later gets its label (and userLabel) back. Drop
    # only long-dead, never-named entries so the file cannot grow forever.
    probed = {e["deviceId"] for e in merged}
    for device, prior in existing.items():
        if device in probed:
            continue
        age = _age_days(prior.get("classifiedAt"))
        if not prior.get("userLabel") and age is not None and age > PRUNE_AFTER_DAYS:
            continue
        merged.append(_relabel(dict(prior)))

    data["browsers"] = merged
    fingerprint = report.get("lanFingerprint") or beacon.lan_fingerprint()
    if fingerprint:
        data["lanFingerprint"] = fingerprint
    _save(data)

    _out(
        {
            "written": CACHE_PATH,
            "count": len(merged),
            "lanFingerprint": data.get("lanFingerprint"),
            "browsers": merged,
        }
    )


def cmd_label(args):
    data, _status = _load()
    if data is None:
        data = {"version": CACHE_VERSION, "browsers": []}

    found = False
    for entry in data.setdefault("browsers", []):
        if entry.get("deviceId") == args.device:
            entry["userLabel"] = args.label
            _relabel(entry)
            found = True
    if not found:
        entry = _relabel(
            {
                "deviceId": args.device,
                "name": None,
                "userLabel": args.label,
                "machine": None,
                "sourceIp": None,
                "hostname": None,
                "tailscaleName": None,
                "mac": None,
                "osHint": None,
                "userAgent": None,
                "confidence": "low",
                "note": "labelled by hand; never probed",
                "classifiedAt": beacon._iso(),
            }
        )
        data["browsers"].append(entry)

    _save(data)
    _out({"labelled": args.device, "userLabel": args.label, "created": not found})


def cmd_unlabel(args):
    data, _status = _load()
    if data is None:
        _fail("no cache to edit")
        return
    touched = False
    for entry in data.get("browsers", []):
        if entry.get("deviceId") == args.device:
            entry["userLabel"] = None
            _relabel(entry)
            touched = True
    if not touched:
        _fail("deviceId not in cache: %s" % args.device)
        return
    _save(data)
    _out({"unlabelled": args.device})


def cmd_clear(_args):
    existed = os.path.exists(CACHE_PATH)
    beacon._unlink(CACHE_PATH)
    _out({"cleared": CACHE_PATH, "existed": existed})


def cmd_show(_args):
    data, status = _load()
    if data is None:
        _out({"status": status or "missing", "browsers": []})
        return
    _out(data)


# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cache.py", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    p_read = sub.add_parser("read")
    p_read.add_argument("--devices", default="")
    p_read.add_argument(
        "--no-network-check",
        dest="check_network",
        action="store_false",
        default=True,
        help="skip the LAN fingerprint check (saves a ping+arp)",
    )
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write")
    p_write.add_argument("--from-probe", required=True)
    p_write.add_argument(
        "--names",
        default="",
        help='optional "deviceId=Browser 1,deviceId=Browser 2" display names',
    )
    p_write.set_defaults(func=cmd_write)

    p_label = sub.add_parser("label")
    p_label.add_argument("--device", required=True)
    p_label.add_argument("--label", required=True)
    p_label.set_defaults(func=cmd_label)

    p_unlabel = sub.add_parser("unlabel")
    p_unlabel.add_argument("--device", required=True)
    p_unlabel.set_defaults(func=cmd_unlabel)

    sub.add_parser("clear").set_defaults(func=cmd_clear)
    sub.add_parser("show").set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        _fail("no subcommand given; try: read | write | label | unlabel | clear | show")
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        _fail("%s: %s" % (type(exc).__name__, exc))
