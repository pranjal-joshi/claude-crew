"""The HTTP handler's caller tiers, LAN limits and body guards, on an in-process server."""
import http.client
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from tests.helpers import TempCrewCase, server


class AuthCase(TempCrewCase):
    def setUp(self):
        super().setUp()
        self._old_crew = getattr(server, "CREW", None)
        server.CREW = self.crew
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self._old_crew is not None:
            server.CREW = self._old_crew

    def post(self, path, body=None, token=None, headers=None):
        h = {"Content-Type": "application/json", "X-Crew": "1"} | ({"X-Crew-Auth": token} if token else {}) | (headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body or {}).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw.startswith(b"{") else {}

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()


class Tiers(AuthCase):
    def test_page_carries_the_ui_token(self):
        page = self.get("/")
        self.assertIn(server.UI_TOKEN.encode(), page)
        self.assertNotIn(server.MCP_TOKEN.encode(), page)
        self.assertIn(b'name="crew-lan" content="0"', page)

    def test_act_needs_a_token(self):
        self.assertEqual(self.post("/api/act/add", {"title": "x"})[0], 403)
        self.assertEqual(self.post("/api/act/add", {"title": "x"}, token="nope")[0], 403)
        self.assertEqual(self.crew.tasks, [])

    def test_missing_x_crew_header_is_refused(self):
        code, _ = self.post("/api/act/add", {"title": "x"}, token=server.UI_TOKEN, headers={"X-Crew": "0"})
        self.assertEqual(code, 403)

    def test_ui_token_works(self):
        code, res = self.post("/api/act/add", {"title": "from the page"}, token=server.UI_TOKEN)
        self.assertEqual((code, res), (200, {"ok": True}))
        self.assertEqual(self.crew.tasks[0]["title"], "from the page")

    def test_mcp_token_is_limited_to_the_allowlist(self):
        self.assertEqual(self.post("/api/act/add", {"title": "from boss"}, token=server.MCP_TOKEN)[0], 200)
        for action, body in (("mode", {"mode": "bypassPermissions"}), ("trigger_update", {"id": "t1", "enabled": False}), ("clear", {})):
            self.assertEqual(self.post(f"/api/act/{action}", body, token=server.MCP_TOKEN)[0], 403, action)
        self.assertEqual(self.crew.config["permission_mode"], "acceptEdits")

    def test_mcp_cannot_create_poll_triggers(self):
        body = {"name": "p", "kind": "poll", "spec": {"command": "echo hi", "every": 5}, "task": {"title": "t"}}
        code, res = self.post("/api/act/trigger_add", body, token=server.MCP_TOKEN)
        self.assertGreaterEqual(code, 400)
        self.assertIn("poll", res["error"])
        self.assertEqual(self.crew.triggers, [])
        # a non-shell kind still works for the boss, and the page may create a poll
        ok = {"name": "w", "kind": "webhook", "spec": {}, "task": {"title": "t"}}
        self.assertEqual(self.post("/api/act/trigger_add", ok, token=server.MCP_TOKEN)[0], 200)
        self.assertEqual(self.post("/api/act/trigger_add", body, token=server.UI_TOKEN)[0], 200)
        self.assertEqual([t["kind"] for t in self.crew.triggers], ["webhook", "poll"])

    def test_browse_and_project_are_ui_only(self):
        self.assertEqual(self.post("/api/browse", {"path": str(self.project)})[0], 403)
        self.assertEqual(self.post("/api/browse", {"path": str(self.project)}, token=server.MCP_TOKEN)[0], 403)
        self.assertEqual(self.post("/api/project", {"path": str(self.project)}, token=server.MCP_TOKEN)[0], 403)
        self.assertEqual(self.post("/api/browse", {"path": str(self.project)}, token=server.UI_TOKEN)[0], 200)


class LanClient(AuthCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(server.Handler, "_local", return_value=False)  # as if the request came from another device
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, server, "LAN_KEY", server.LAN_KEY)
        server.LAN_KEY = "k" * 24
        self.cookie = {"Cookie": f"crew_key={server.LAN_KEY}"}

    def test_lan_page_is_flagged_and_gets_a_token(self):
        page = self.get("/", self.cookie)
        self.assertIn(b'name="crew-lan" content="1"', page)
        self.assertIn(server.UI_TOKEN.encode(), page)

    def test_lan_client_cannot_browse_or_switch_project(self):
        for path in ("/api/browse", "/api/project"):
            code, res = self.post(path, {"path": str(self.project)}, token=server.UI_TOKEN, headers=self.cookie)
            self.assertEqual(code, 403, path)
            self.assertIn("computer running Crew", res["error"])
        self.assertEqual(self.post("/api/act/add", {"title": "from the phone"}, token=server.UI_TOKEN, headers=self.cookie)[0], 200)


class Webhook(AuthCase):
    def setUp(self):
        super().setUp()
        self.crew.act("trigger_add", {"name": "hook", "kind": "webhook", "spec": {}, "task": {"title": "Hooked"}})
        self.tr = self.crew.triggers[0]

    def raw(self, path, length=None, body=b""):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", path)
        if length is not None:
            c.putheader("Content-Length", length)
        c.endheaders(body)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def test_bad_content_length_is_400(self):
        path = f"/api/hook/{self.tr['id']}?token={self.tr['spec']['token']}"
        self.assertEqual(self.raw(path, "abc")[0], 400)
        self.assertEqual(self.raw(path, "-5")[0], 400)
        self.assertEqual(self.raw(path)[0], 400)  # missing
        self.assertEqual(self.raw(path, str(server.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.crew.tasks, [])

    def test_good_hook_and_bad_token(self):
        path = f"/api/hook/{self.tr['id']}"
        self.assertEqual(self.raw(f"{path}?token={self.tr['spec']['token']}", "2", b"{}")[0], 200)
        self.assertEqual(self.raw(f"{path}?token=wrong", "2", b"{}")[0], 403)
        self.assertEqual(len(self.crew.tasks), 1)


if __name__ == "__main__":
    unittest.main()
