---
name: chrome-pick
description: >-
  Identify which connected Chrome browser is which machine before doing any browser
  automation, by probing each one against a local network beacon and labelling it by
  source IP (this Mac vs work laptop vs unreachable). Use at the START of any
  claude-in-chrome task, and when the user says "wrong browser", "which chrome",
  "pick the right browser", "wrong machine", "that's my work laptop", "which browser
  are you using", or when list_connected_browsers returns several indistinguishable
  "Browser N" entries.
---

# chrome-pick — know which Chrome is which before you touch it

`list_connected_browsers` returns entries that are impossible to tell apart:

```json
[{"deviceId":"e324d16a-...","name":"Browser 1","osPlatform":"macOS","isLocal":true},
 {"deviceId":"2175f1c8-...","name":"Browser 2","osPlatform":"macOS","isLocal":true}]
```

Same name shape, same OS, both `isLocal:true`. Guessing has already gone wrong in
practice — the **work laptop** got navigated to the **home** router's admin UI and hit a
cert error.

This skill bounces each browser off a short-lived HTTP beacon on this Mac and labels it
by **the source IP the beacon sees**. Then it feeds those labels into the mandatory
`AskUserQuestion`.

Two things this skill does **not** do:

- It does not lock onto one browser. The user drives more than one deliberately; the goal
  is accurate labels so an informed choice is possible.
- It does not skip, replace, or excuse the mandatory browser-choice question. Better
  labels make that question **better**, not optional.

## When this fires

- At the **start of any** `mcp__claude-in-chrome__*` task, before the first browser action.
- "wrong browser", "wrong machine", "which chrome", "which browser are you using",
  "pick the right browser", "that's my work laptop", "not that one".
- Any time `list_connected_browsers` returns more than one browser.
- "re-probe", "refresh the browsers", "re-identify" → force a fresh probe (Step 5).

Single browser connected? You may skip probing — but Step 8 still happens. Probe anyway
(it is one tab) when the task targets a home-LAN resource, so you can state whether that
one browser is actually on this Mac rather than assuming it.

## Ground rules

- Scripts run under **`/usr/bin/python3`** (pinned system Python; stdlib only, no pip/npm).
- Skill dir: `~/.claude/skills/chrome-pick/`. Persistent cache: `~/.claude/chrome-browsers.json`.
- The beacon self-terminates via TTL. Never leave a port listening.
- The probe URL carries only a deviceId and a random ephemeral nonce. Nothing secret.
- `select_browser` mutates **global** state shared with whatever else is using the
  browser. Always finish on the browser the user actually chose (Step 7).

---

## Step 1 — list the browsers

Call `mcp__claude-in-chrome__list_connected_browsers` (no arguments). Collect the
`deviceId` values into a comma-separated list; that csv is `<devices>` throughout.

## Step 2 — read the cache

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py read --devices <devices>
```

Returns `{"status": ..., "mustReprobe": true|false, "browsers": [...], "unknownDevices":
[...], "missingDevices": [...]}`. `mustReprobe` is the machine-readable form of the table
below; `browsers` only contains the deviceIds you passed in, in that order.

`missingDevices` lists cached browsers that are **not** connected right now. That is
information, not a problem — it never forces a re-probe, because one browser going offline
does not make the others' labels wrong.

| `status` | Meaning | Do |
|---|---|---|
| `fresh` | Labels valid | **Skip to Step 8** |
| `stale` | Older than 30 days | **Skip to Step 8**, mark labels "unverified", offer a refresh |
| `missing` | No cache yet (or unreadable) | Probe (Step 4) |
| `version-mismatch` | Schema changed | Probe |
| `device-set-changed` | A connected browser has no cached label | Probe — `unknownDevices` names them |
| `network-changed` | Mac moved to another LAN | Probe — `10.0.0.57` no longer means the work laptop |

An explicit user request to re-probe overrides `fresh`/`stale`.

## Step 3 — the common path

Most runs land on `fresh`. That is the point: **one well-labelled question, no probing.**
Only continue to Step 4 when the table above says to probe.

## Step 4 — get consent to probe

Probing opens and closes a tab in **every** connected browser, including someone else's
screen. That needs consent. Probing is itself a browser action (`select_browser`,
`tabs_create_mcp`, `navigate`), so this consent question must obey the Step 8 rule too:
every option list below ends with the exact final option, verbatim.

`AskUserQuestion` allows a **maximum of 4 options**. Shape the question accordingly:

**Two or fewer browsers** — one question covers consent and the mandatory ask:

```text
1. Identify all browsers  (Recommended)
2. Browser 1 (e324d16a)
3. Browser 2 (2175f1c8)
4. Open a confirmation screen in every connected Chrome extension and let me select the right one there.
```

**Three or more browsers** — browsers + probe + final option exceeds 4, so ask a
consent-shaped question and defer the per-browser list to Step 8:

```text
1. Identify all browsers  (Recommended)
2. Skip probing — show me the list and let me choose
3. Open a confirmation screen in every connected Chrome extension and let me select the right one there.
```

Then act on the answer:

| Answer | Do |
|---|---|
| `Identify all browsers` | Probe (Step 5), **then still run Step 8** with the labels |
| A specific browser | Honour it, skip probing, go to Step 9 |
| `Skip probing` / consent declined | Go straight to Step 8 with unlabelled options |
| The exact final option | Skip probing, go to Step 9 and call `switch_browser` |

**This question does not discharge Step 8.** Consenting to a probe is not choosing a
browser. Whenever probing happens, the labelled question at Step 8 happens after it.

## Step 5 — probe loop

### 5a. Start the beacon

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/beacon.py start --ttl 300
```

Capture `port`, `token`, `probeUrls` (LAN, keyed by IP), `vpnProbeUrls` (Tailscale, keyed
by `100.x` IP), `loopbackProbe`, `defaultInterface`, `candidates` and `startedAt`/`ttl`.
Also check `firewallState`: `1` or `2` means surface the firewall caveat later rather than
asserting "different network"; `0` is off and `null` means it could not be determined
(treat as possibly on).

If this returns `{"error": ...}`, the beacon did not come up — it could not bind, could not
be spawned, or did not report ready within 5s. Skip probing entirely, go to Step 8 with
unlabelled options, and weight the confirmation-screen option first.

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/beacon.py selftest
```

Branch on `mode` — there are three values, and `beaconUsable:false` covers two of them, so
read `mode` rather than the booleans:

| `mode` | Meaning | Do |
|---|---|---|
| `full` | LAN socket reachable locally | Probe LAN URLs first, as normal |
| `loopback-only` | Bound, but no usable LAN address | Probe **only** `loopbackProbe` — that still positively identifies **this Mac**; everything else becomes `unreachable` |
| `unusable` | Not even loopback answers | Do not probe at all. Go to Step 8 with unlabelled options and weight the confirmation-screen option first |

> `selftest` proves the socket is bound and not blocked locally. It **cannot** prove a
> remote machine can reach it — traffic from this Mac to its own LAN IP short-circuits
> internally and never crosses the wire. Do not read `beaconUsable:true` as proof the LAN
> probe will work.

### 5b. Per browser

For each `deviceId`, in order:

1. **Select it**

   ```text
   mcp__claude-in-chrome__select_browser  { "deviceId": "<deviceId>" }
   ```

2. **Baseline the tab group** — the MCP tab group is per-browser, so re-baseline after
   every `select_browser`:

   ```text
   mcp__claude-in-chrome__tabs_context_mcp  { "createIfEmpty": true }
   ```

   Record the tab IDs that already exist. Do not touch them. Caveat: `createIfEmpty:true`
   *creates* a window and one empty tab when that browser has no MCP tab group yet. If the
   baseline comes back as a single empty tab that this call just conjured, it is yours —
   navigate it directly (skip step 3) and close it in step 7, so you leave nothing behind.

3. **Create a tab** — takes **no arguments** and creates an *empty* tab; it cannot accept
   a URL:

   ```text
   mcp__claude-in-chrome__tabs_create_mcp  {}
   ```

   Then call `tabs_context_mcp {}` again and take the tab ID that was not in the baseline.
   That is `<tabId>`.

4. **Navigate it to the LAN probe.** Take the base URL from `probeUrls` in the `start`
   output — **never hardcode an IP**, it changes with the network — and append
   `?d=<deviceId>&t=<token>`. If `probeUrls` has several entries, use the one whose
   `candidates` record has `iface == defaultInterface`; keep the rest as fallbacks. The URL
   **must** keep its `http://` prefix: `navigate` defaults to `https://` when the protocol
   is omitted, and the beacon is plain HTTP.

   ```text
   mcp__claude-in-chrome__navigate  { "tabId": <tabId>,
     "url": "<probeUrls[lanIp]>?d=<deviceId>&t=<token>" }
   ```

   With `probeUrls` of `{"10.0.0.110": "http://10.0.0.110:58962/probe"}` that resolves to
   `http://10.0.0.110:58962/probe?d=e324d16a-...&t=rqCGLmhjteBW7h-r`.

   Never call `navigate` without `tabId` here — standalone navigation retargets the first
   tab in the group and would clobber a tab someone else is using.

5. **Confirm the hit before closing anything:**

   ```bash
   /usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/beacon.py status --devices <deviceId>
   ```

   Read `devices[0].hit`, or check whether the deviceId is still in `pendingDevices`. The
   round-trip *is* the wait. Still pending? Check once more before giving up. **Closing
   the tab early aborts the request** and loses the identification.

   Also watch `alive` and `expiresIn`. The beacon self-terminates at its TTL, and with
   several browsers a slow loop can outlive it — `status` then fails with *no beacon
   running*, and every remaining device would be mislabelled `unreachable`. If it has
   expired, `start` again (new port **and** new token, so re-read `probeUrls`) and re-probe
   only the devices still pending.

6. **Fallbacks**, only while the device is still in `pendingDevices` — repeat steps 3–5
   with a fresh tab for each:
   - any other LAN URL in `probeUrls` (a second interface, e.g. Ethernet vs Wi-Fi).
   - `loopbackProbe` — `http://127.0.0.1:<port>/probe?d=...&t=...`. This is the
     discriminator: on a remote machine it hits *that* machine's empty localhost and
     records nothing. Skip it when the LAN probe already proved `this-mac`.
   - `vpnProbeUrls` — the Tailscale (`100.x`) URL, if `start` listed one.

7. **Close every tab you opened:**

   ```text
   mcp__claude-in-chrome__tabs_close_mcp  { "tabId": <tabId> }
   ```

   Leave the baseline tabs alone.

### 5c. Stop the beacon and keep the report

**`stop` is destructive and runs exactly once.** It kills the listener and deletes the
state file, so a second `stop` finds no state and reports *every* device as `unreachable`
with `confidence:low` — silently discarding the probe you just did. Run it once, in the
same command that writes the cache (Step 6), or capture it to a file in this session's
scratchpad directory (`<scratch>` below — not `/tmp`):

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/beacon.py stop --devices <devices> \
  > <scratch>/chrome-pick-report.json
```

If the output carries a `warning` about missing beacon state, the results are not
trustworthy — do not cache them; go to Step 8 with the labels you can still justify.

## Step 6 — write the cache

One command, one `stop` — pipe it straight into the cache:

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/beacon.py stop --devices <devices> \
  | /usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py write --from-probe - \
      --names "<deviceId>=Browser 1,<deviceId>=Browser 2"
```

If you already captured the report in Step 5c, write from that file instead — never call
`stop` a second time:

```bash
/usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py write \
  --from-probe <scratch>/chrome-pick-report.json --names "<deviceId>=Browser 1"
```

`--names` is optional but worth passing: it stores the display names from Step 1 so a later
`fresh` run can show `Browser 2 — work laptop` without re-listing. Any `userLabel` the user
has set is preserved across every re-probe.

`cache.py write` echoes back `{"written": ..., "count": N, "browsers": [...]}`. `count` is
the number of entries **now in the cache** — the ones you just probed *plus* any retained
from earlier probes — so it is often larger than the number probed. Verify instead that
every deviceId you probed appears in `browsers` with the `machine` you expected.

## Step 7 — reading the report

| `machine` | Means | Say |
|---|---|---|
| `this-mac` | Hit from `127.0.0.1` or one of this host's own IPs | "this Mac" |
| `same-lan` | Hit from another RFC1918 address on this LAN | Name it via rDNS / ARP / user label, plus the IP |
| `tailnet` | Hit from `100.64.0.0/10` | Tailscale hostname |
| `remote` | Hit from an address that is none of the above — normally **public**, reaching us via NAT, a relay or a port forward | "on another network (`<ip>`)"; `confidence` is `medium`, so say so |
| `unreachable` | No hit on any URL | "did not respond — different network, or a VPN on that machine" |

**Never report `unreachable` as a definite machine**, and never invent a label. If
`firewallState` was `1`, `2` or `null`, add the firewall caveat instead of claiming a
different network — you cannot tell the two apart. A failed probe degrades confidence; it
never blocks the task.

`select_browser` is global state and the loop leaves the *last probed* browser selected.
Never stop there — Step 9 explicitly re-selects the user's actual choice.

## Step 8 — the mandatory question (always happens)

This rule comes from `list_connected_browsers` itself and is reproduced verbatim:

> Before any browser action, you MUST call the AskUserQuestion tool with a question
> listing EVERY connected browser as a separate option (use the display name as the
> label, and include the deviceId in parentheses), plus one final option labeled exactly:
> "Open a confirmation screen in every connected Chrome extension and let me select the
> right one there." Do not skip any connected browser and do not pick one yourself.

**Accurate labels make this question better. They do not remove it.** Nothing in this
skill authorises skipping it.

Compose the options:

- **Label:** `<name> — <label> (<first 8 of deviceId>)`, e.g. `Browser 2 — work laptop (2175f1c8)`.
  Keep it short. If the harness rejects the label as too long, drop the machine label — not
  the name or the deviceId — and carry the machine label in the description instead. The
  rule above requires the display name and the deviceId in the label.
- **Description:** full deviceId first, then the evidence — source IP, hostname, OS hint,
  confidence, and `just probed` vs `cached DD MMM`.
- **Order:** recommended browser **first**, with `(Recommended)` in the label if it fits.
  The exact final string is **always last**.
- **Recommend by target, never auto-select:** a home-LAN resource (the router's admin UI,
  a NAS, an internal-only hostname) → the `this-mac` browser. Otherwise carry the previous
  choice.
- **More than 3 browsers:** the 4-option cap collides with "list EVERY browser". List the
  3 most likely plus the exact final option, and state in the question text that N others
  exist and the confirmation-screen option reaches them. This is a platform cap being
  worked around — not licence to quietly drop browsers.

## Step 9 — act on the answer

- A specific browser → `mcp__claude-in-chrome__select_browser { "deviceId": "<full deviceId>" }`,
  then proceed with the real task.
- The final option → `mcp__claude-in-chrome__switch_browser {}` (no arguments). This
  prompts every connected extension and waits up to 2 minutes for the user to click
  Connect; they can also name the browser there. `switch_browser` does not tell you which
  deviceId won, so call `list_connected_browsers` again — the browser the user named now
  carries that name — and save it against its deviceId:

  ```bash
  /usr/bin/python3 ~/.claude/skills/chrome-pick/scripts/cache.py label \
    --device <deviceId> --label "work laptop"
  ```

  A `userLabel` survives every future re-probe, so this is the cheapest permanent fix for a
  browser the beacon cannot reach.

---

## Worked example

**Before** — indistinguishable, a coin flip:

```text
1. Browser 1 (e324d16a-c186-4ece-944c-42905e74c5df)
2. Browser 2 (2175f1c8-b6f5-40af-8022-6ad894f689a2)
3. Open a confirmation screen in every connected Chrome extension and let me select the right one there.
```

**After** — same question, now answerable at a glance:

```text
1. Browser 1 — this Mac (e324d16a)          [Recommended]
   e324d16a-c186-4ece-944c-42905e74c5df · hit the beacon from 127.0.0.1 · macOS
   · high confidence · just probed
2. Browser 2 — work laptop (2175f1c8)
   2175f1c8-b6f5-40af-8022-6ad894f689a2 · source 10.0.0.57 · work-mbp.lan
   · Windows · high confidence · just probed
3. Open a confirmation screen in every connected Chrome extension and let me select the right one there.
```

The cert-error incident becomes impossible to repeat: the router's admin UI is a LAN
resource, so Browser 1 is recommended, and Browser 2 is visibly the work laptop.

---

## Quick reference

```bash
SK=~/.claude/skills/chrome-pick/scripts

/usr/bin/python3 $SK/beacon.py plan                        # candidate IPs, no side effects
/usr/bin/python3 $SK/beacon.py fingerprint                 # current LAN fingerprint
/usr/bin/python3 $SK/beacon.py start --ttl 300             # start; prints port + token
/usr/bin/python3 $SK/beacon.py selftest                    # local bind check only
/usr/bin/python3 $SK/beacon.py status --devices <csv>      # hits so far (poll freely)
/usr/bin/python3 $SK/beacon.py stop  --devices <csv>       # classify + shut down -- ONCE

/usr/bin/python3 $SK/cache.py read  --devices <csv>        # add --no-network-check to skip ping+arp
/usr/bin/python3 $SK/cache.py write --from-probe - --names "<id>=Browser 1"
/usr/bin/python3 $SK/cache.py label   --device <id> --label "work laptop"
/usr/bin/python3 $SK/cache.py unlabel --device <id>
/usr/bin/python3 $SK/cache.py show                         # dump the cache verbatim
/usr/bin/python3 $SK/cache.py clear                        # discards userLabels too
```

`plan`, `fingerprint`, `selftest`, `status`, `read` and `show` are safe to repeat. `start`
reaps any previous beacon; `stop` runs **once** per probe (see Step 5c).

Probes failing in a way this page does not cover — VPNs, firewall prompts, HTTPS
upgrades, extension site permissions — see `reference/troubleshooting.md`.
