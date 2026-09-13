"""端到端回放测试：真实 TMDB 响应（tests/fixtures/）离线回放，走完整 handle_* 入口。

每个用例对应一个线上真实案例，fixture 由 tests/record_fixtures.py 录制：
  - Re:Zero tm65942：主表 S1=85 集全局号，S4E81 只在剧集组
  - 无职转生 tm94664：新播季 S3 只在主表、S0 特别篇、刮削季上下文
  - 让子弹飞 tm51533：电影 hash 识别（文件名匹配）
  - 咒术回战 tm95479：主表 S1=59 塞三季，同名 "Seasons" 组去重后按组拆季
  - 我独自升级 tm127532：主表 S1=25 塞两季，剧集组拆 Season 1/2
  - 史莱姆 tm82684：多季 + S0 特别篇；缺 season 字段恒为 S0
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
JJK_TV = {"dataVersion": "", "category": "tv", "language": "zh-CN",
          "trimId": "tm95479"}
JJK_FILE = "/media/tv/咒术回战/Season 2/咒术回战 S02E01.mp4"
SOLO_TV = {"dataVersion": "", "category": "tv", "language": "zh-CN",
           "trimId": "tm127532"}
SOLO_FILE = "/media/tv/我独自升级 (2024)/Season 2/我独自升级 (2024) S02E13.mkv"
SLIME_TV = {"dataVersion": "", "category": "tv", "language": "zh-CN",
            "trimId": "tm82684"}
SLIME_EP_NO_SEASON = {"dataVersion": "", "category": "episode", "language": "zh-CN",
                      "trimId": "tm82684", "episodeNumber": 1}


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

    def test_season_request_without_season_means_specials(self):
        # 飞牛对第 0 季不发 season 字段（Go omitempty）；缺字段恒为 S0，
        # 不受此前识别/扫描过什么季影响（史莱姆 S0 重刮曾被解析成 S1/S4）
        with bs.FakeTMDB():
            tp.handle_search_item({"fileName": MUSHOKU_FILE})       # 先识别 S3E1
            out = tp.handle_detail_season({"sourceId": "tm94664", "language": "zh-CN"})
        self.assertEqual(out["code"], 0)
        season = out["data"]["season"]
        self.assertEqual(season["season_number"], 0)
        self.assertEqual(season["name"], "特别篇")

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

    # ---- 咒术回战：主表 S1=59 塞三季；同名剧集组去重后按组拆季 ----

    def test_jjk_structure_from_group_not_crammed_main(self):
        # 主表只有 S1=59；同名 "Seasons" 组（64/69 集两个）去重后按组出
        # S0/S1/S2/S3 结构，季行集数不得被两个组的数据叠加翻倍
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(JJK_TV))
        tv = out["data"]["tv"]
        counts = {s["season_number"]: s["episode_count"] for s in tv["seasons"]}
        self.assertEqual(counts.get(0), 10)
        self.assertEqual(counts.get(1), 24)
        self.assertEqual(counts.get(2), 23)
        self.assertEqual(counts.get(3), 12)

    def test_jjk_s02e01_matched_via_group(self):
        # 改名后的文件（组 Season 2 E1 = 原 1x25）必须命中《怀玉》
        with bs.FakeTMDB():
            out = tp.handle_search_item({"fileName": JJK_FILE})
        self.assertEqual(out["code"], 0)
        e = out["data"]["episode"]
        self.assertEqual((e["season_number"], e["episode_number"]), (2, 1))
        self.assertEqual(e["name"], "怀玉")

    # ---- 我独自升级：主表 S1=25 塞两季；剧集组拆 Season 1/2（保留原集号） ----

    def test_solo_structure_split_by_group(self):
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(SOLO_TV))
        counts = {s["season_number"]: s["episode_count"]
                  for s in out["data"]["tv"]["seasons"]}
        self.assertEqual(counts.get(1), 12)
        self.assertEqual(counts.get(2), 13)

    def test_solo_s02e13_matched_via_group(self):
        # 组 Season 2 保留原集号 E13~25，改名后的文件必须命中《你不是E级，对吧》
        with bs.FakeTMDB():
            out = tp.handle_search_item({"fileName": SOLO_FILE})
        self.assertEqual(out["code"], 0)
        e = out["data"]["episode"]
        self.assertEqual((e["season_number"], e["episode_number"]), (2, 13))
        self.assertEqual(e["name"], "你不是E级，对吧")

    # ---- 史莱姆：多季 + S0；缺 season 字段恒为 S0 ----

    def test_slime_multi_season_structure(self):
        with bs.FakeTMDB():
            out = tp.handle_meta_diff(dict(SLIME_TV))
        counts = {s["season_number"]: s["episode_count"]
                  for s in out["data"]["tv"]["seasons"]}
        self.assertEqual(counts.get(0), 16)
        self.assertEqual(counts.get(1), 24)
        self.assertEqual(counts.get(4), 24)

    def test_slime_missing_season_fields_resolves_specials(self):
        # 线上事故回归：纯刷新/重刮流程里 S0 的季详情与单集请求都不带季字段，
        # 旧逻辑上下文过期后默认 S1，把特别篇整季刮成第 1 季
        with bs.FakeTMDB():
            season = tp.handle_detail_season({"sourceId": "tm82684",
                                              "language": "zh-CN"})["data"]["season"]
            self.assertEqual((season["season_number"], season["name"]), (0, "特别篇"))
            out = tp.handle_meta_diff(dict(SLIME_EP_NO_SEASON))
        e = out["data"]["episode"]
        self.assertEqual((e["season_number"], e["episode_number"]), (0, 1))
        self.assertIn("维鲁多拉日记", e["name"])


if __name__ == "__main__":
    unittest.main()
