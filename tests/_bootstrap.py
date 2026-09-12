"""测试引导：隔离环境 + fixture 回放。所有测试文件第一行 import 本模块。

- 必须在 import tmdb_provider 之前把 TMDB_CACHE_DIR 指向一次性临时目录
  （CACHE_FILE 等常量在 import 时就已算好，事后改环境变量无效）；
- reset_state() 在每个用例前把模块级 STATE 重置为初始值，用例互不污染；
- FakeTMDB 把 tp.http_fetch 替换为 fixture 回放器：按 URL 路径映射
  tests/fixtures/*.json，离线、确定性、不耗 TMDB 配额。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import atexit

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(TESTS_DIR)
FIXTURE_DIR = os.path.join(TESTS_DIR, "fixtures")
for p in (BASE_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# 一次性缓存目录：测试产生的任何缓存落盘都进这里，退出时清理
_TEST_CACHE = tempfile.mkdtemp(prefix="tmdb_provider_test_")
atexit.register(shutil.rmtree, _TEST_CACHE, ignore_errors=True)
os.environ.setdefault("TMDB_CACHE_DIR", _TEST_CACHE)

import tmdb_provider as tp  # noqa: E402

_STATE_DEFAULTS = json.loads(json.dumps({k: v for k, v in tp.STATE.items()}))


def reset_state(api_key="test-key"):
    """把模块级全局状态恢复到初始值（缓存/上下文/计数器全清）。"""
    tp.STATE.clear()
    tp.STATE.update(json.loads(json.dumps(_STATE_DEFAULTS)))
    tp.STATE["config"] = {"api_key": api_key}
    tp._img_types_last_save[0] = 0.0


def _sanitize(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", s, flags=re.UNICODE).strip("_")[:40]


def fixture_name_for_url(url: str) -> str:
    """TMDB 请求 URL -> fixture 文件名（只看路径；搜索接口附加查询词）。
    剥掉 API 版本前缀 /3/，否则文件名全带 3_ 且搜索接口丢失查询词。"""
    u = urllib.parse.urlsplit(url)
    path = re.sub(r"^/\d+/", "/", u.path)
    qs = urllib.parse.parse_qs(u.query)
    if path.startswith("/search/"):
        kind = path.strip("/").replace("/", "_")          # search_tv / search_movie
        q = (qs.get("query") or [""])[0]
        return f"{kind}_{_sanitize(q)}.json"
    m = re.fullmatch(r"/tv/episode_group/([^/]+)", path)
    if m:
        return f"tv_episode_group_{m.group(1)}.json"
    name = path.strip("/").replace("/", "_")
    return f"{name}.json" if name else ""


def fake_http_fetch(url: str, timeout: int = 30, headers: dict = None):
    """fixture 回放版 http_fetch。命中 <name>.json -> 200 回放；命中 <name>.404.json
    -> 显式 404（录制时真实服务就返回 404，如 Re:Zero 主表没有 season/2 条目）；
    两者都缺 -> 404 并打提示（说明该请求没录过，别让缺失悄悄变成 404 假绿）。"""
    name = fixture_name_for_url(url)
    fp = os.path.join(FIXTURE_DIR, name) if name else ""
    if name and os.path.isfile(fp):
        with open(fp, "rb") as f:
            return 200, "application/json", f.read()
    miss404 = fp + ".404.json"
    if name and os.path.isfile(miss404):
        return 404, "application/json", b'{"status_code":34,"status_message":"recorded 404"}'
    sys.stderr.write(f"[fake] 缺 fixture: {name or url}  (按 404 处理: {url.split('?')[0]})\n")
    return 404, "application/json", b"{}"


class FakeTMDB:
    """with FakeTMDB(): 期间所有 TMDB 请求走 fixture 回放。"""

    def __enter__(self):
        self._orig = tp.http_fetch
        tp.http_fetch = fake_http_fetch
        return self

    def __exit__(self, *exc):
        tp.http_fetch = self._orig
        return False
