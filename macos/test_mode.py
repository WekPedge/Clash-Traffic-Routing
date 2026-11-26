"""Regression tests for user mode changes while the Codex overlay owns Mihomo."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

import clash_route as c


class ModeChangeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.enter_patch(patch.multiple(c, STATE=root, JOURNAL=root / "journal.json",
                                        PREFERENCES=root / "config.yaml", PROFILE=root / "profile.yaml",
                                        ENGINE=str(root / "codex")))
        Path(c.ENGINE).touch()
        c.PROFILE.write_text('proxies:\n- {"name":"HK"}\nrules:\n- "MATCH,HK"\n')
        self.enter_patch(patch.object(c, "node", return_value={"name": c.NAME}))
        self.enter_patch(patch.object(c, "api", side_effect=self.api))
        self.mode = "global"
        self.pref("global")
        self.applications = 0
        self.reset_core()

    def enter_patch(self, p):
        p.start()
        self.addCleanup(p.stop)

    def pref(self, mode, log_level="info"):
        c.PREFERENCES.write_text(f"mode: {mode}\nlog-level: {log_level}\n")

    def reset_core(self):
        self.proxies = {"GLOBAL": {"type": "Selector", "all": ["HK", "DIRECT"], "now": "HK"}, "HK": {}, "DIRECT": {}}
        self.rules = [{"type": "Match", "payload": "", "proxy": "HK"}]

    def api(self, method, path, body=None, **_):
        if method == "GET":
            if path == "/configs":
                return {"mode": self.mode, "tun": {"enable": True}, "find-process-mode": "strict"}
            if path == "/rules":
                return {"rules": self.rules}
            if path == "/proxies":
                return {"proxies": dict(self.proxies)}
            if path == "/proxies/" + c.NAME:
                return {} if c.NAME in self.proxies else None
        if method == "PATCH" and path == "/configs":
            self.mode = body["mode"]
            return
        if method == "PUT" and path.startswith("/proxies/"):
            self.proxies[unquote(path[len("/proxies/"):])]["now"] = body["name"]
            return
        if method == "PUT" and path == "/configs?force=false":
            self.applications += 1
            self.reset_core()
            if "payload" in body:
                self.mode = "rule"
                self.proxies[c.NAME] = {}
                rules = body["payload"].split("\nrules:\n")[1].split("\n")
                self.rules = []
                for line in rules:
                    if not line.startswith("- "):
                        break
                    fields = json.loads(line[2:]).split(",")
                    self.rules.append({"type": {"PROCESS-PATH": "ProcessPath", "MATCH": "Match"}[fields[0]],
                                       "payload": fields[1] if len(fields) == 3 else "", "proxy": fields[-1]})
            else:
                self.mode = "global"  # the service restores its original runtime file
            return
        self.fail((method, path))

    def test_change_to_rules_while_running(self):
        c.install(12345)
        self.assertEqual([r["proxy"] for r in self.rules], [c.NAME, "GLOBAL", "HK"])
        self.proxies["GLOBAL"]["now"] = "DIRECT"
        self.pref("rule")
        self.assertTrue(c.sync_mode(12345))
        self.assertEqual([r["proxy"] for r in self.rules], [c.NAME, "HK"])
        self.assertEqual(self.proxies["GLOBAL"]["now"], "DIRECT")
        c.restore()
        self.assertEqual(self.mode, "rule")
        self.assertEqual(self.proxies["GLOBAL"]["now"], "DIRECT")

    def test_unchanged_stale_preference_does_not_replace_live_mode(self):
        self.pref("rule")
        c.install(12345)
        self.pref("rule", "warning")
        self.assertTrue(c.sync_mode(12345))
        self.assertEqual(self.applications, 1)
        self.assertEqual(self.rules[1]["proxy"], "GLOBAL")
        c.restore()
        self.assertEqual(self.mode, "global")

    def test_quit_before_poll_honors_latest_preference(self):
        c.install(12345)
        self.pref("direct")
        c.restore()
        self.assertEqual(self.mode, "direct")

    def test_mode_changed_directly_in_core(self):
        self.mode = "rule"
        self.pref("rule")
        c.install(12345)
        self.mode = "direct"
        self.assertTrue(c.sync_mode(12345))
        self.assertEqual(self.mode, "rule")
        self.assertEqual(self.rules[1]["proxy"], "DIRECT")
        c.restore()
        self.assertEqual(self.mode, "direct")

    def test_external_reload_supersedes_overlay(self):
        c.install(12345)
        self.pref("rule")
        self.reset_core()
        self.assertFalse(c.sync_mode(12345))
        c.restore()
        self.assertEqual(self.applications, 1)
        self.assertFalse(c.JOURNAL.exists())


if __name__ == "__main__":
    unittest.main()
