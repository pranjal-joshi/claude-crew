"""Boot the real HTTP handler on a free port and fetch the page and the state endpoint."""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from tests.helpers import ROOT, TempCrewCase, server


class HttpSmoke(TempCrewCase):
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

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.read()

    def test_index(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        page = (ROOT / "index.html").read_bytes().replace(b"__CREW_TOKEN__", server.UI_TOKEN.encode()).replace(b"__CREW_LAN__", b"0")
        self.assertEqual(body, page)
        self.assertIn(b"<title>Crew</title>", body)

    def test_state(self):
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["app_version"], server.__version__)
        self.assertIn("builder", [a["name"] for a in data["agents"]])


if __name__ == "__main__":
    unittest.main()
