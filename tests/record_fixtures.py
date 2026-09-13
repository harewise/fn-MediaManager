"""录制 TMDB 真实响应到 tests/fixtures/（需联网，平时测试不需要跑本脚本）。

    python3 tests/record_fixtures.py

原理：包一层 http_fetch，把真实请求的 200 响应按 tests/_bootstrap.fixture_name_for_url
命名存盘；然后照常调用各 handle_* 入口，让代码自己"走出"需要录制的请求集合。
已有 fixture 不覆盖（增量录制）。新增测试场景时在这里加一段再跑一次即可。
"""
import json
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, TESTS_DIR)

# 录制需要真实 key（.env 不入库，仅本机有）
_key = None
with open(os.path.join(BASE_DIR, ".env"), encoding="utf-8") as f:
    for line in f:
        if line.startswith("TMDB_API_KEY="):
            _key = line.strip().split("=", 1)[1]
if not _key:
    sys.exit("未在 .env 里找到 TMDB_API_KEY")

os.environ["TMDB_API_KEY"] = _key
os.environ["TMDB_CACHE_DIR"] = tempfile.mkdtemp(prefix="tmdb_record_")

import tmdb_provider as tp        # noqa: E402
import _bootstrap as bs           # noqa: E402  (fixture 命名规则唯一来源)

_real_fetch = tp.http_fetch


def _recording_fetch(url, timeout=30, headers=None):
    status, ct, body = _real_fetch(url, timeout, headers)
    name = bs.fixture_name_for_url(url)
    if name and status == 200:
        fp = os.path.join(bs.FIXTURE_DIR, name)
        if not os.path.exists(fp):
            os.makedirs(bs.FIXTURE_DIR, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(json.loads(body), f, ensure_ascii=False, indent=1)
            print("  recorded:", name)
    elif name and status == 404:
        fp = os.path.join(bs.FIXTURE_DIR, name + ".404.json")
        if not os.path.exists(fp):
            os.makedirs(bs.FIXTURE_DIR, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("{}")
            print("  recorded 404:", name)
    return status, ct, body


tp.http_fetch = _recording_fetch
tp.STATE["config"]["api_key"] = _key


def show(tag, out):
    print(f"[{tag}] code={out.get('code')} " +
          json.dumps(out.get("data"), ensure_ascii=False)[:220])


# ---- 场景1：Re:Zero (tm65942) —— 主表 S1 用全局号，S4E81 靠剧集组换算 ----
print("== Re:Zero 全局号/剧集组 ==")
show("ep S4E81", tp.handle_meta_diff({"dataVersion": "", "category": "episode",
     "language": "zh-CN", "trimId": "tm65942", "seasonNumber": 4, "episodeNumber": 81}))
show("tv", tp.handle_meta_diff({"dataVersion": "", "category": "tv",
     "language": "zh-CN", "trimId": "tm65942"}))
show("season S0", tp.handle_detail_season({"sourceId": "tm65942", "season": 0, "language": "zh-CN"}))

# ---- 场景2：无职转生 —— 新播季只在主表（剧集组未收录）+ 第0季特别篇 ----
print("== 无职转生 新播季/S0 ==")
hits = tp.search_tv("无职转生")
pick = tp.pick_tv(hits, "无职转生", 2021)
mushoku = int(pick["id"])
print("  无职转生 -> tm%d (%s)" % (mushoku, pick.get("first_air_date")))
M = f"tm{mushoku}"
show("search S3E1", tp.handle_search_item({"fileName":
     f"/media/tv/无职转生 (2021)/Season 3/无职转生 III S03E01.mkv"}))
show("tv", tp.handle_meta_diff({"dataVersion": "", "category": "tv",
     "language": "zh-CN", "trimId": M}))
show("ep S0E1", tp.handle_meta_diff({"dataVersion": "", "category": "episode",
     "language": "zh-CN", "trimId": M, "seasonNumber": 0, "episodeNumber": 1}))
show("season S0", tp.handle_detail_season({"sourceId": M, "season": 0, "language": "zh-CN"}))
show("season S3", tp.handle_detail_season({"sourceId": M, "season": 3, "language": "zh-CN"}))

# ---- 场景3：电影 hash 识别 ----
print("== 电影 ==")
show("byHash", tp.handle_search_by_hash({"thirdPartyHash": "record-only",
     "fileName": "/media/movie/让子弹飞 (2010)/让子弹飞 (2010) 1080p.mkv"}))

# ---- 场景4：genres ----
tp.handle_genres()

# ---- 场景5：咒术回战 —— 主表 S1=59 塞三季；两个同名 "Seasons" 组，去重后按组拆 S1/S2/S3 ----
print("== 咒术回战 同名剧集组 ==")
hits = tp.search_tv("咒术回战")
pick = tp.pick_tv(hits, "咒术回战", 2020)
jjk = int(pick["id"])
print("  咒术回战 -> tm%d (%s)" % (jjk, pick.get("first_air_date")))
show("tv", tp.handle_meta_diff({"dataVersion": "", "category": "tv",
     "language": "zh-CN", "trimId": f"tm{jjk}"}))
show("search S02E01", tp.handle_search_item({"fileName":
     "/media/tv/咒术回战/Season 2/咒术回战 S02E01.mp4"}))

# ---- 场景6：我独自升级 —— 主表 S1=25 塞两季，剧集组拆 Season 1/2（组内保留原集号） ----
print("== 我独自升级 主表塞两季 ==")
hits = tp.search_tv("我独自升级")
pick = tp.pick_tv(hits, "我独自升级", 2024)
solo = int(pick["id"])
print("  我独自升级 -> tm%d (%s)" % (solo, pick.get("first_air_date")))
show("tv", tp.handle_meta_diff({"dataVersion": "", "category": "tv",
     "language": "zh-CN", "trimId": f"tm{solo}"}))
show("search S02E13", tp.handle_search_item({"fileName":
     "/media/tv/我独自升级 (2024)/Season 2/我独自升级 (2024) S02E13.mkv"}))

# ---- 场景7：史莱姆 —— 多季 + S0 特别篇；缺 season 字段恒为 S0 ----
print("== 史莱姆 多季/S0 ==")
hits = tp.search_tv("关于我转生变成史莱姆这档事")
pick = tp.pick_tv(hits, "关于我转生变成史莱姆这档事", 2018)
slime = int(pick["id"])
print("  史莱姆 -> tm%d (%s)" % (slime, pick.get("first_air_date")))
show("tv", tp.handle_meta_diff({"dataVersion": "", "category": "tv",
     "language": "zh-CN", "trimId": f"tm{slime}"}))
show("season 缺season字段", tp.handle_detail_season({"sourceId": f"tm{slime}", "language": "zh-CN"}))
show("ep 缺seasonNumber字段", tp.handle_meta_diff({"dataVersion": "", "category": "episode",
     "language": "zh-CN", "trimId": f"tm{slime}", "episodeNumber": 1}))

print("完成。fixture 目录:", bs.FIXTURE_DIR)
