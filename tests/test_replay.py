"""端到端回放测试：真实 TMDB 响应（tests/fixtures/）离线回放，走完整 handle_* 入口。

每个用例对应一个线上真实案例，fixture 由 tests/record_fixtures.py 录制：
  - Re:Zero tm65942：主表 S1=85 集全局号，S4E81 只在剧集组
  - 无职转生 tm94664：新播季 S3 只在主表、S0 特别篇、刮削季上下文
  - 让子弹飞 tm51533：电影 hash 识别（文件名匹配）
"""
import unittest

import _bootstrap as bs
import tmdb_provider as tp

REZERO_EP = {"dataVersion": "", "category": "episode", "language": "zh-CN",
             "trimId": "tm65942", "seasonNumber": 4, "episodeNumber": 81}
MUSHOKU_TV = {"dataVersion": "", "category": "tv", "language": "zh-CN",
              "trimId": "tm94664"}
MUSHOKU_S0_EP = {"dataVersion": "", "category": "episode", "language": "zh-CN",
                 "trimId": "tm94664", "seasonNumber": 0, "episodeNumber": 1}
MUSHOKU_FILE = "/media/tv/无职转生 (2021)/Season 3/无职转生 III S03E01.mkv"
MOVIE_FILE = "/media/movie/让子弹飞 (2010)/让子弹飞 (2010) 1080p.mkv"


class TestReplay(unittest.TestCase):
    def setUp(self):
        bs.reset_state()

    def test_rezero_s04e81_matched_via_group(self):
        # 主表无 S04E81（fixture 里是显式 404），必须由剧集组兜底命中
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(REZERO_EP))
        self.assertEqual(out["code"], 0)
        data = out["data"]
        self.assertTrue(data["hasDiff"])
        e = data["episode"]
        self.assertEqual((e["season_number"], e["episode_number"]), (4, 81))
        self.assertTrue(e["name"] and e["overview"])   # 组内真实集数据，非占位

    def test_diff_is_idempotent(self):
        # trim 刷新逻辑的前提：拿上次返回的 data_version 再问，必须 hasDiff:false
        with bs.FakeTMDB():
            first = tp.handle_meta_diff(dict(REZERO_EP))
            ver = first["data"]["episode"]["data_version"]
            again = tp.handle_meta_diff(dict(REZERO_EP, dataVersion=ver))
        self.assertFalse(again["data"]["hasDiff"])

    def test_mushoku_structure_keeps_new_season_and_specials(self):
        # 修复过的 bug：新播季 S3（剧集组未收录）不能从季结构里消失，
        # S0（特别篇）必须出现在 seasons[]，否则 S00Exx 会被归到 S1
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(MUSHOKU_TV))
        tv = out["data"]["tv"]
        nums = [s["season_number"] for s in tv["seasons"]]
        self.assertIn(3, nums)
        self.assertIn(0, nums)
        self.assertEqual(tv["number_of_seasons"], 3)
        # seasons[].id 必须是字符串：飞牛 Go 结构体按 string unmarshal，数字会整单丢弃
        self.assertTrue(all(isinstance(s["id"], str) for s in tv["seasons"]))

    def test_mushoku_s0_episode_stays_specials(self):
        # 修复过的 bug：S00E01 不能被全局号兜底成 S1E1
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(MUSHOKU_S0_EP))
        e = out["data"]["episode"]
        self.assertEqual(e["season_number"], 0)
        self.assertEqual(e["episode_number"], 1)

    def test_search_item_hits_main_table_for_new_season(self):
        # 无职转生 S3E1：剧集组未收录，主表默认结构命中
        with bs.FakeTMDB():
            out = tp.handle_search_item({"fileName": MUSHOKU_FILE})
        self.assertEqual(out["code"], 0)
        e = out["data"]["episode"]
        self.assertEqual((e["season_number"], e["episode_number"]), (3, 1))

    def test_season_request_without_season_uses_scrape_context(self):
        # 飞牛对第 0 季不发 season 字段；缺字段 ≠ S1，按 /search/item 命中的季兜底
        with bs.FakeTMDB():
            tp.handle_search_item({"fileName": MUSHOKU_FILE})       # 记录上下文 S3
            out = tp.handle_detail_season({"sourceId": "tm94664", "language": "zh-CN"})
        self.assertEqual(out["code"], 0)
        self.assertEqual(out["data"]["season"]["season_number"], 3)

    def test_detail_season_zero_named_specials(self):
        with bs.FakeTMDB():
            out = tp.handle_detail_season({"sourceId": "tm94664", "season": 0,
                                           "language": "zh-CN"})
        season = out["data"]["season"]
        self.assertEqual((season["season_number"], season["name"]), (0, "特别篇"))
        self.assertEqual(len(season["episodes"]), 3)          # fixture：S0 共 3 集
        self.assertEqual(season["episodes"][0]["episode_number"], 1)
        self.assertTrue(all(e["season_number"] == 0 for e in season["episodes"]))

    def test_movie_hash_search(self):
        # 电影识别：hash 透传不参与匹配，按文件名命中，episode 为 null
        with bs.FakeTMDB():
            out = tp.handle_search_by_hash({"thirdPartyHash": "abc", "fileName": MOVIE_FILE})
        self.assertEqual(out["code"], 0)
        self.assertIsNone(out["data"]["episode"])
        self.assertEqual(out["data"]["cleanData"]["trimId"], "tt51533")

    def test_movie_hash_garbage_name_404(self):
        # 阈值拦截：不相干的文件名不许胡乱命中电影
        with bs.FakeTMDB():
            out = tp.handle_search_by_hash({"thirdPartyHash": "abc",
                                            "fileName": "/x/zzz unknown thing 2023.mkv"})
        self.assertEqual(out["code"], 404)


if __name__ == "__main__":
    unittest.main()
