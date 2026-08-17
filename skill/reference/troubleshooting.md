# chrome-pick — troubleshooting

Read this when a probe fails, mislabels, or behaves oddly. The governing rule for
everything below:

> **A failed probe never blocks the task and never invents a label.** It downgrades to
> `unreachable` / low confidence, and the mandatory question proceeds — with the
> confirmation-screen option weighted more heavily in the recommendation.

---

## The one trap everyone falls into

`selftest` passing does **not** mean a remote browser can reach the beacon.

When this Mac connects to its own LAN IP (`10.0.0.110`), the kernel short-circuits the
traffic internally. It never touches the network card, never crosses the wire, never
meets the firewall the way an inbound packet would. `selftest` therefore proves exactly
one thing: **the socket is bound and nothing local is blocking it.**

Treat `beaconUsable:true` as "the beacon started correctly", never as "the LAN probe will
work". The only proof of remote reachability is an actual hit from a remote source IP.

---

## Failure modes

### 1. ProtonVPN on *this* Mac, kill-switch on / "Allow LAN" off

**Symptom:** `selftest` to the LAN IP fails, loopback fine.

**Behaviour:** report the beacon unusable for LAN and run the **loopback-only** probe. That
still positively identifies `this-mac`, which is usually the answer you actually needed.
Everything else classifies `unreachable`.

**Fix:** enable "Allow LAN connections" in ProtonVPN, or drop the VPN for the probe.

### 2. Full-tunnel VPN on the *remote* browser's machine

**Symptom:** that browser records no hit; its Chrome routes `10.0.0.x` into the tunnel.

**Behaviour:** `unreachable`, `confidence:low`. Before giving up, retry that device against
the `cgnat_vpn` (Tailscale) URL — a tunnelled machine on the tailnet often still answers.

### 3. Tailscale up on both machines

LAN may fail while the tailnet succeeds. Probe order per device is LAN candidates first,
then `cgnat_vpn` for anything still unhit.

A `100.64.0.0/10` source IP is a **positive** result, not a failure: `machine:"tailnet"`,
named from `tailscale status --json` (`Peer[].TailscaleIPs[0]` → `HostName` + `OS`). This
is the most reliable naming source available — better than rDNS or ARP.

### 4. macOS Application Firewall on, or stealth mode

**Symptom:** remote probes silently dropped; local `selftest` still passes (see the trap
above). A GUI "accept incoming connections?" prompt may also appear on this Mac and block
until someone clicks it.

**Behaviour:** `start` reports `firewallState` (`0` off, `1` on, `2` block-all) and a
`firewallWarning`. When it is `1`, `2` or `null` (unknown), label affected devices
`unreachable` **with the
firewall caveat** — do not assert "different network", because you cannot distinguish the
two.

Check it directly:

```bash
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
```

### 5. Empty `utun` interfaces

This Mac has several `utun0`–`utun9` that are up but carry no IPv4. They would generate
dead probe URLs. They are excluded at discovery: no `inet` line, and `POINTOPOINT`
interfaces classify as `tunnel` and are never probed.

### 6. Beacon cannot bind at all

`start` returns `{"error": ...}`. Skip probing entirely, go to the mandatory question with
unlabelled options, and recommend the confirmation-screen route first.

### 7. `navigate` silently upgrading to HTTPS

**Symptom:** the tab loads an error page; the beacon records nothing; the device looks
`unreachable` when it is sitting right there.

**Cause:** `navigate` defaults to `https://` when the URL has no protocol. The beacon is
plain HTTP, so an HTTPS request to it fails.

**Fix:** always write the probe URL with an explicit `http://` prefix. The URLs in the
`start` output already include it — pass them through unmodified.

Chrome's **HTTPS-First mode** can also attempt an upgrade even on an explicit `http://`
URL. Raw IP literals are normally exempt, but if you see an interstitial instead of the
beacon page, this is the cause. It is a browser setting, not a network problem — do not
record the device as `unreachable`; note the interstitial and fall back to the
confirmation-screen option.

### 8. Extension refuses to navigate (site permissions)

**Symptom:** `navigate` returns a permissions error rather than loading anything.

**Cause:** the Claude in Chrome extension requires site-level permission before acting on
a URL, and a raw `http://10.0.0.110:58962` origin has never been granted one.

**Behaviour:** this is a **permissions failure, not a network failure**. Do not classify
the device `unreachable` — that would state something false about the network. Report that
the extension blocked the probe and offer the confirmation-screen option, which needs no
site permission.

### 9. Tab closed too early

**Symptom:** intermittent misses, especially on the slower machine.

**Cause:** `tabs_close_mcp` aborts the in-flight request. Always confirm the hit with
`beacon.py status` *before* closing the tab.

### 10. Everything classifies `this-mac`

Both browsers really are on this Mac (two Chrome profiles or channels). That is a valid
result — the source IP genuinely is local for both. Distinguish them by `userAgent` and
give them user labels:

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py label \
  --device <deviceId> --label "Chrome Canary"
```

### 11. Every browser came back `unreachable` after a probe that looked fine

**Symptom:** the tabs loaded the beacon page, `status` showed the hits — and then the final
report says `unreachable`, `confidence:low`, for everything, with a `warning` field.

**Cause:** `stop` was called twice. The first call kills the listener and deletes the state
file; the second finds no state, so it has no hits to classify and reports the whole set as
`unreachable`. Piping a second `stop` into `cache.py write` persists that wrong answer.

**Fix:** call `stop` exactly once per probe — either piped directly into `cache.py write`,
or captured to a file that the write then reads. Any report containing `warning` must not
be cached.

### 12. The beacon expired mid-loop

**Symptom:** `status` fails with *no beacon running (state file missing)* partway through
the browsers; the ones already probed are fine, the rest look dead.

**Cause:** the TTL watchdog (default 300s) fired. It is a safety feature — a port is never
left listening — but a slow loop over several browsers can outlive it.

**Fix:** `start` again and re-probe only the devices still pending. The new beacon gets a
**new port and a new token**, so re-read `probeUrls` / `loopbackProbe` from the fresh
output; the old URLs now return 403 or nothing. Pass a longer `--ttl` when several browsers
are connected. Watch `expiresIn` in `status` to see it coming.

---

## Cache operations

### Rename a machine permanently

`userLabel` is set by the user and **survives every re-probe and every invalidation**.
Nothing in the classifier overwrites it.

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py label \
  --device 2175f1c8-b6f5-40af-8022-6ad894f689a2 --label "work laptop"
```

### Force a re-probe

Just ask ("re-probe", "refresh the browsers"). To do it by hand:

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py clear
```

`clear` discards user labels along with everything else. Prefer the explicit re-probe,
which keeps them.

### Why the cache invalidated

Inspect it directly:

```bash
cat ~/.claude/chrome-browsers.json
```

- `device-set-changed` — a browser is connected that has no cached label (a new browser, or
  one whose deviceId rotated). Listed in `unknownDevices`. A cached browser that is merely
  *offline* does **not** invalidate the cache — it appears in `missingDevices` and its entry
  is kept, so a laptop that is only around on weekdays does not force a probe every run.
- `network-changed` — `lanFingerprint` (default-gateway IP + gateway MAC) differs. The Mac
  moved networks, so `10.0.0.57` no longer means what it used to. This is the check that
  stops a café Wi-Fi session from confidently mislabelling a stranger's machine as the work
  laptop.
- `stale` — an entry is over 30 days old. Labels still shown, flagged unverified.

Wi-Fi SSID is deliberately **not** used as the fingerprint: modern macOS hides it behind
Location Services permission.

### Why ARP needs a ping first

`arp -n <ip>` returns *no entry* for a host that has not been talked to recently. The
classifier runs `ping -c1 -W 900 <ip>` first to warm the table. An inbound HTTP connection
usually warms it anyway, but the ping makes the silent-peer case work too.

---

## Beacon hygiene

The TTL watchdog (default 300s) shuts the server down and deletes its state file even if
Claude crashes mid-run, so a port is never left listening. `start` also reaps any stale
beacon before starting a new one.

To check or clean up by hand:

```bash
pgrep -fl 'beacon.py serve'                   # anything still running?
lsof -nP -iTCP -sTCP:LISTEN | grep -i python  # the socket -- lsof's COMMAND column
                                              # says "Python", never "beacon"
pkill -f beacon.py                            # last resort
rm -f ~/.claude/chrome-pick/beacon-state.json
```

Needing these means the TTL failed — worth investigating rather than papering over.

---

## What the other machine's user sees

The probe opens a tab, loads one page, and closes it. The page says:

```text
chrome-pick — This browser has been identified. You can close this tab.
```

It is deliberately self-explanatory, because whoever is sitting at the work laptop will
see it appear. The URL contains only a deviceId and a random single-use nonce — nothing
secret, nothing about the task in progress.
