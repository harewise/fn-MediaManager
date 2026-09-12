"""匹配与季结构逻辑测试：合成剧集组布局 + 打桩 main_group_flat，纯离线。

覆盖线上踩过的坑：
  - Re:Zero 式全局号：文件 S04E81 按组内全局号直接命中
  - 季内号换算：文件 E3 在全局号起始 72 的季里应换算为 74
  - 组内无此季时不跨季兜底（无职转生 S3 案例，避免 S3E11→S1E11 错配）
  - 季结构数据源：主表全局号塞 S1 时剧集组优先，正常时主表优先
"""
import unittest
from unittest import mock

import _bootstrap as bs  # noqa: F401
import tmdb_provider as tp


def ep(num, s=4, group="Seasons", sub="Season 4", placeholder=False):
    d = {"episode_number": num, "name": f"E{num}",
         "air_date": "2020-01-01", "_group": group, "_sub": sub, "_sub_season": s}
    if not placeholder:
        d["still_path"] = "/x.jpg"
        d["overview"] = "x"
    return d


# 示意布局：S1 = 全局号 1..25，S4 = 全局号 72..84（Re:Zero 式整季拆分）
FLAT = [ep(n, s=1, sub="Season 1") for n in range(1, 26)] + \
       [ep(n, s=4, sub="Season 4") for n in range(72, 85)]

STUB = (mock.patch.object(tp, "main_group_flat", return_value=FLAT),
        mock.patch.object(tp, "group_priority", return_value=["Seasons"]))


class TestMatchInGroup(unittest.TestCase):
    def _match(self, season, n):
        with STUB[0], STUB[1]:
            return tp._match_episode_in_group(65942, season, n)

    def test_global_number_direct_hit(self):
        # 文件 S04E81：81 落在 S4 的全局号范围 72..84 内，直接命中
        got, gname, s_num = self._match(4, 81)
        self.assertEqual((got["episode_number"], gname, s_num), (81, "Seasons", 4))

    def test_intra_season_number_conversion(self):
        # 文件 S04E03：3 小于季起始号 72 → 换算为 72+3-1=74
        got, _g, s_num = self._match(4, 3)
        self.assertEqual((got["episode_number"], s_num), (74, 4))

    def test_no_cross_season_when_season_missing_from_group(self):
        # 无职转生 S3 案例：组里没有这一季 → 返回 None 退回主表，
        # 绝不能把 S3E11 按全局 11 兜底成 S1E11
        self.assertEqual(self._match(9, 11), (None, "", 9))

    def test_cross_season_fallback_only_when_in_no_range(self):
        # 文件 S04E25：25 不在 S4 范围(72..84)内、按季内号换算 96 也不存在，
        # 最后才允许跨季全局号兜底 → 命中 S1 的 25
        got, _g, s_num = self._match(4, 25)
        self.assertEqual((got["episode_number"], s_num), (25, 1))


class TestMatchEpisodeRefetch(unittest.TestCase):
    def test_placeholder_triggers_rate_limited_refetch(self):
        placeholder_flat = [ep(75, s=4, placeholder=True)]
        calls = []
        orig_flat_groups = tp._flat_groups

        def counting(tid, force=False):
            calls.append(force)
            return orig_flat_groups(tid, force)

        with mock.patch.object(tp, "main_group_flat", return_value=placeholder_flat), \
             mock.patch.object(tp, "group_priority", return_value=["Seasons"]), \
             mock.patch.object(tp, "_flat_groups", side_effect=counting), \
             mock.patch.object(tp, "tmdb_get", return_value={}):
            got, _g, s_num = tp.match_episode(65942, 4, 75)
        self.assertTrue(got and got.get("still_path") is None)   # 占位集仍返回（强刷后无新数据）
        self.assertEqual(calls, [True])   # 恰好一次限频强制重取


class TestPlaceholder(unittest.TestCase):
    def test_aired_without_still_and_overview(self):
        self.assertTrue(tp._ep_is_placeholder({"air_date": "2024-01-01"}))

    def test_not_aired(self):
        self.assertFalse(tp._ep_is_placeholder({"air_date": "2099-01-01"}))

    def test_aired_with_still(self):
        self.assertFalse(tp._ep_is_placeholder(
            {"air_date": "2024-01-01", "still_path": "/x.jpg"}))


class TestSeasonStructure(unittest.TestCase):
    def test_group_numbered_seasons_counts_distinct(self):
        self.assertEqual(tp._group_numbered_seasons(FLAT), 2)

    def test_rezero_global_numbering_uses_group_first(self):
        # 主表把 S1..S4 塞在 S1=85 集里 → 剧集组认识更多季，结构按剧集组出
        rezero_main = {"seasons": [{"season_number": 1, "episode_count": 85},
                                   {"season_number": 0, "episode_count": 82}]}
        self.assertFalse(tp.structure_source_is_main(rezero_main, 4))

    def test_normal_show_uses_main_table(self):
        # 无职转生式：主表 S1..S3 齐全且剧集组只认识 2 季 → 主表优先
        mushoku_main = {"seasons": [{"season_number": 0, "episode_count": 2},
                                    {"season_number": 1, "episode_count": 24},
                                    {"season_number": 2, "episode_count": 12},
                                    {"season_number": 3, "episode_count": 12}]}
        self.assertTrue(tp.structure_source_is_main(mushoku_main, 2))

    def test_true_single_season_ignores_dirty_group(self):
        # Silent Witch 式：真单季番，主表 S1=13 是全部，组里脏数据不引入
        single = {"seasons": [{"season_number": 1, "episode_count": 13}]}
        self.assertTrue(tp.structure_source_is_main(single, 1))


class TestSeasonFromBody(unittest.TestCase):
    def test_explicit_season_wins(self):
        # 显式字段（含 0）为准；字段名两套都认
        bs.reset_state()
        self.assertEqual(tp._season_from_body(94664, {"season": 0}), 0)
        self.assertEqual(tp._season_from_body(94664, {"season": 2}), 2)
        self.assertEqual(tp._season_from_body(94664, {"seasonNumber": 4}), 4)

    def test_missing_means_season_zero(self):
        # Go omitempty 只会丢 0 值：缺字段当且仅当第 0 季，不得默认 S1
        # （史莱姆 S0 全量重刮曾被"缺字段→默认 S1"刮成第 1 季）
        bs.reset_state()
        self.assertEqual(tp._season_from_body(94664, {}), 0)


class TestFlatGroupsSameNameDedupe(unittest.TestCase):
    def test_same_name_groups_keep_largest(self):
        # TMDB 常有同名新旧剧集组（如咒术回战两个 "Seasons" 64/69 集），
        # 展平必须按组名去重取集数最多的，否则季集数翻倍
        bs.reset_state()

        def fake_get(path, **kw):
            if path.endswith("episode_groups"):
                return {"results": [{"id": "A", "name": "Seasons"},
                                    {"id": "B", "name": "Seasons"}]}
            if path == "/tv/episode_group/A":
                return {"groups": [{"name": "Season 1", "episodes":
                                    [{"episode_number": 1}, {"episode_number": 2}]}]}
            if path == "/tv/episode_group/B":
                return {"groups": [{"name": "Season 1", "episodes":
                                    [{"episode_number": 1}, {"episode_number": 2},
                                     {"episode_number": 3}]}]}
            return {"_status": 404}

        with mock.patch.object(tp, "tmdb_get", side_effect=fake_get):
            flat = tp._fetch_flat_groups(1)
        self.assertEqual(len(flat), 3)
        self.assertEqual(sorted(e["episode_number"] for e in flat), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
