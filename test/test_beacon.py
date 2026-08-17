"""End-to-end tests for the chrome-pick beacon and cache.

Stdlib only, no network beyond this machine's own loopback/LAN address. Every
test runs against a temporary HOME, so the developer's real
~/.claude/chrome-browsers.json and ~/.claude/chrome-pick/ are never touched.

    /usr/bin/python3 -m unittest discover -s test -v

A browser is simulated with urllib: the beacon classifies purely on the source
IP of the probe request, so a request from urllib on this machine is
indistinguishable from a request Chrome on this machine would make. What these
tests cannot cover is a genuinely remote browser -- that needs a second machine.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

PY = "/usr/bin/python3"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEACON = os.path.join(ROOT, "skill", "scripts", "beacon.py")
CACHE = os.path.join(ROOT, "skill", "scripts", "cache.py")


class ScriptTestCase(unittest.TestCase):
    """Runs the scripts under an isolated HOME and parses their one JSON object."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="chrome-pick-test-")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.env = dict(os.environ, HOME=self.home)
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_script(self, script, *args, expect_ok=True, stdin=None):
        proc = subprocess.run(
            [PY, script, *args],
            capture_output=True,
            text=True,
            env=self.env,
            input=stdin,
            timeout=60,
        )
        if expect_ok:
            self.assertEqual(
                proc.returncode, 0, f"{script} {args} failed: {proc.stdout}{proc.stderr}"
            )
        # Contract: exactly one JSON object on stdout and nothing else.
        return json.loads(proc.stdout)

    def beacon(self, *args, **kw):
        return self.run_script(BEACON, *args, **kw)

    def cache(self, *args, **kw):
        return self.run_script(CACHE, *args, **kw)

    def probe(self, url, device, token):
        """Stand in for a Chrome tab fetching the probe URL."""
        with urllib.request.urlopen(f"{url}?d={device}&t={token}", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            return resp.read().decode()


class TestPlan(ScriptTestCase):
    def test_plan_lists_candidate_addresses_without_binding(self):
        plan = self.beacon("plan")
        self.assertIn("candidates", plan)
        kinds = {c["kind"] for c in plan["candidates"]}
        # There is always at least one address to reason about, and every
        # candidate is classified so the skill can prefer the right one.
        self.assertTrue(plan["candidates"], "no candidate addresses discovered")
        self.assertTrue(kinds <= {"lan", "cgnat_vpn", "tunnel", "loopback", "other"}, kinds)
        for candidate in plan["candidates"]:
            self.assertIn("ip", candidate)
            self.assertIn("iface", candidate)


class TestBeaconLifecycle(ScriptTestCase):
    def start(self, ttl=45):
        started = self.beacon("start", "--ttl", str(ttl))
        self.assertNotIn("error", started, started)
        self.addCleanup(self._force_stop)
        return started

    def _force_stop(self):
        subprocess.run(
            [PY, BEACON, "stop", "--devices", "cleanup"],
            capture_output=True,
            env=self.env,
        )

    def test_probe_classifies_this_mac_and_leaves_nothing_listening(self):
        started = self.start()
        port, token = started["port"], started["token"]

        selftest = self.beacon("selftest")
        self.assertIn(selftest["mode"], {"full", "loopback-only"})
        self.assertTrue(selftest["loopbackUsable"])

        page = self.probe(started["loopbackProbe"], "DEV-LOCAL", token)
        # The page the user may glimpse must explain itself and name the device.
        self.assertIn("DEV-LOCAL", page)
        self.assertIn("chrome-pick", page)

        status = self.beacon("status", "--devices", "DEV-LOCAL,DEV-SILENT")
        self.assertTrue(status["alive"])
        by_id = {d["deviceId"]: d for d in status["devices"]}
        self.assertEqual(by_id["DEV-LOCAL"]["machine"], "this-mac")
        self.assertEqual(by_id["DEV-LOCAL"]["confidence"], "high")
        self.assertFalse(by_id["DEV-SILENT"]["hit"])
        self.assertEqual(by_id["DEV-SILENT"]["machine"], "unreachable")
        self.assertEqual(status["pendingDevices"], ["DEV-SILENT"])

        report = self.beacon("stop", "--devices", "DEV-LOCAL,DEV-SILENT")
        self.assertFalse(report["listening"], "beacon left a port listening after stop")
        stopped = {d["deviceId"]: d for d in report["devices"]}
        self.assertEqual(stopped["DEV-LOCAL"]["machine"], "this-mac")
        self.assertEqual(stopped["DEV-SILENT"]["machine"], "unreachable")

        with socket.socket() as sock:
            sock.settimeout(2)
            self.assertNotEqual(
                sock.connect_ex(("127.0.0.1", port)), 0, "port still accepting connections"
            )

    def test_own_lan_address_is_this_mac_not_another_machine(self):
        """The Mac's own LAN IP must not read as a second machine."""
        started = self.start()
        lan_urls = started.get("probeUrls") or {}
        if not lan_urls:
            self.skipTest("no LAN address available on this host")
        ip, url = next(iter(lan_urls.items()))

        self.probe(url, "DEV-LAN", started["token"])
        status = self.beacon("status", "--devices", "DEV-LAN")
        device = status["devices"][0]
        self.assertEqual(device["sourceIp"], ip)
        self.assertEqual(device["machine"], "this-mac")

    def test_wrong_token_is_rejected_and_records_no_hit(self):
        started = self.start()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.probe(started["loopbackProbe"], "DEV-FORGED", "not-the-token")
        self.assertEqual(caught.exception.code, 403)

        status = self.beacon("status", "--devices", "DEV-FORGED")
        self.assertFalse(status["devices"][0]["hit"])

    def test_second_stop_warns_instead_of_silently_reporting_unreachable(self):
        started = self.start()
        self.probe(started["loopbackProbe"], "DEV-LOCAL", started["token"])
        self.beacon("stop", "--devices", "DEV-LOCAL")

        again = self.beacon("stop", "--devices", "DEV-LOCAL")
        # A second stop has no state left. It must say so -- results that look
        # like "unreachable" but are really "we lost the report" would poison
        # the cache.
        self.assertIn("warning", again)
        self.assertEqual(again["devices"][0]["machine"], "unreachable")


class TestLeadingDashToken(unittest.TestCase):
    """`secrets.token_urlsafe` can return a token starting with "-".

    Passed as `--token <value>`, argparse reads that as a flag, the child dies
    before it binds, and `start` can only report a 5s timeout. Roughly 1 start
    in 64. This drives the real cmd_start with such a token forced.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="chrome-pick-dash-")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self._real_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(self._restore_home)

        # Import beacon.py fresh so its module-level paths resolve under the
        # temporary HOME rather than the developer's real one.
        import importlib.util

        spec = importlib.util.spec_from_file_location("beacon_under_test", BEACON)
        self.beacon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.beacon)
        self.assertTrue(self.beacon.RUNTIME_DIR.startswith(self.home), self.beacon.RUNTIME_DIR)

    def _restore_home(self):
        if self._real_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._real_home

    def _stop_quietly(self):
        import argparse
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                self.beacon.cmd_stop(argparse.Namespace(devices="cleanup"))
            except SystemExit:
                pass

    def test_start_survives_a_token_beginning_with_a_dash(self):
        import argparse
        import contextlib
        import io

        self.beacon.secrets.token_urlsafe = lambda n=12: "-leading-dash-token"
        self.addCleanup(self._stop_quietly)

        # Every subcommand ends in sys.exit after printing its one JSON object.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as exited:
                self.beacon.cmd_start(argparse.Namespace(ttl=15))

        started = json.loads(buffer.getvalue())
        self.assertEqual(exited.exception.code, 0, started)
        self.assertNotIn("error", started, started)
        self.assertEqual(started["token"], "-leading-dash-token")
        self.assertTrue(started["port"])


class TestCache(ScriptTestCase):
    def probe_report(self, device="DEV-A"):
        started = self.beacon("start", "--ttl", "45")
        self.probe(started["loopbackProbe"], device, started["token"])
        return self.beacon("stop", "--devices", device)

    def test_read_reports_missing_before_anything_is_written(self):
        result = self.cache("read", "--devices", "DEV-A")
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["browsers"], [])

    def test_write_then_read_returns_fresh_labelled_browsers(self):
        report = self.probe_report()
        written = self.cache(
            "write", "--from-probe", "-", "--names", "DEV-A=Browser 1", stdin=json.dumps(report)
        )
        self.assertEqual(written["count"], 1)

        read = self.cache("read", "--devices", "DEV-A")
        self.assertEqual(read["status"], "fresh")
        self.assertFalse(read["mustReprobe"])
        self.assertEqual(read["browsers"][0]["machine"], "this-mac")
        self.assertEqual(read["browsers"][0]["name"], "Browser 1")

    def test_an_unseen_device_forces_a_reprobe(self):
        report = self.probe_report()
        self.cache("write", "--from-probe", "-", stdin=json.dumps(report))

        read = self.cache("read", "--devices", "DEV-A,DEV-NEW")
        self.assertEqual(read["status"], "device-set-changed")
        self.assertTrue(read["mustReprobe"])
        self.assertIn("DEV-NEW", read["unknownDevices"])

    def test_a_disconnected_browser_does_not_force_a_reprobe(self):
        started = self.beacon("start", "--ttl", "45")
        self.probe(started["loopbackProbe"], "DEV-A", started["token"])
        self.probe(started["loopbackProbe"], "DEV-B", started["token"])
        report = self.beacon("stop", "--devices", "DEV-A,DEV-B")
        self.cache("write", "--from-probe", "-", stdin=json.dumps(report))

        # Both cached, only DEV-A connected now. One browser going offline does
        # not make the other's label wrong, so this is reported, not invalidated.
        read = self.cache("read", "--devices", "DEV-A")
        self.assertEqual(read["status"], "fresh")
        self.assertFalse(read["mustReprobe"], read)
        self.assertEqual(read["missingDevices"], ["DEV-B"])
        self.assertEqual([b["deviceId"] for b in read["browsers"]], ["DEV-A"])

    def test_a_human_label_survives_a_reprobe(self):
        report = self.probe_report()
        self.cache("write", "--from-probe", "-", stdin=json.dumps(report))
        self.cache("label", "--device", "DEV-A", "--label", "work laptop")

        second = self.probe_report()
        self.cache("write", "--from-probe", "-", stdin=json.dumps(second))

        read = self.cache("read", "--devices", "DEV-A")
        self.assertEqual(read["browsers"][0]["userLabel"], "work laptop")

        self.cache("unlabel", "--device", "DEV-A")
        cleared = self.cache("read", "--devices", "DEV-A")
        self.assertIsNone(cleared["browsers"][0].get("userLabel"))

    def test_clear_removes_the_cache(self):
        report = self.probe_report()
        self.cache("write", "--from-probe", "-", stdin=json.dumps(report))
        self.cache("clear")
        self.assertEqual(self.cache("read", "--devices", "DEV-A")["status"], "missing")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("chrome-pick targets macOS; these tests bind a local port and read the ARP table")
    unittest.main()
