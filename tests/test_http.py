"""HTTP 层测试：进程内起真实 ThreadingHTTPServer（随机端口），urllib 自打自。

验证路由、404 行为、信封格式与飞牛兼容的关键约定（GET /match 必须裸 404，
这是与原版 fnnas 服务一致的行为，不是 bug）。
"""
import json
import threading
import unittest
import urllib.error
import urllib.request

import _bootstrap as bs
import tmdb_provider as tp

REZERO_EP = {"dataVersion": "", "category": "episode", "language": "zh-CN",
             "trimId": "tm65942", "seasonNumber": 4, "episodeNumber": 81}


class TestHTTP(unittest.TestCase):
    def setUp(self):
        bs.reset_state()
        self._fake = bs.FakeTMDB()
        self._fake.__enter__()
        self.srv = tp.ThreadingHTTPServer(("127.0.0.1", 0), tp.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._fake.__exit__()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, obj):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    def test_healthz(self):
        with urllib.request.urlopen(self._url("/healthz"), timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.load(resp)["data"]["status"], "ok")

    def test_get_match_returns_bare_404_like_fnnas(self):
        # 与飞牛原服务行为一致，不是故障 —— 用例钉住，防止将来被"修复"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/match"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(ctx.exception.read(), b"404 page not found")

    def test_unknown_post_is_envelope_404(self):
        # 业务路由不裸 404：HTTP 200 + 信封 {"code": 404}
        resp = self._post("/no/such/route", {})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.load(resp)["code"], 404)

    def test_meta_diff_full_path(self):
        with self._post("/meta/diff", REZERO_EP) as resp:
            out = json.load(resp)
        self.assertEqual(out["code"], 0)
        self.assertTrue(out["data"]["hasDiff"])
        self.assertEqual(out["data"]["episode"]["episode_number"], 81)

    def test_genres(self):
        with self._post("/genres", {}) as resp:
            out = json.load(resp)
        self.assertEqual(out["code"], 0)
        self.assertTrue(out["data"]["genres"])


if __name__ == "__main__":
    unittest.main()
