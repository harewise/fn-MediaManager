"""纯函数测试：文件名解析 / 标题清洗 / 候选择优。全部离线，无网络无状态。"""
import unittest

import _bootstrap as bs  # noqa: F401  必须最先 import（隔离环境）
import tmdb_provider as tp


class TestChineseNum(unittest.TestCase):
    def test_digits_and_chinese(self):
        self.assertEqual(tp.parse_chinese_num("12"), 12)
        self.assertEqual(tp.parse_chinese_num("三"), 3)
        self.assertEqual(tp.parse_chinese_num("十二"), 12)
        self.assertEqual(tp.parse_chinese_num("十"), 10)


class TestParseFilename(unittest.TestCase):
    def test_season_episode_and_year(self):
        info = tp.parse_filename("/media/tv/无职转生 (2021)/Season 01/无职转生 S01E02.mkv")
        self.assertEqual((info["season"], info["episode"]), (1, 2))
        self.assertEqual(info["year"], 2021)
        self.assertEqual(info["parent"], "Season 01")
        self.assertEqual(info["grand"], "无职转生 (2021)")

    def test_movie_file_has_no_se(self):
        info = tp.parse_filename("/media/电影/让子弹飞 (2010)/让子弹飞 (2010) 1080p.mkv")
        self.assertEqual((info["season"], info["episode"]), (0, 0))
        self.assertEqual(info["year"], 2010)
        self.assertEqual(info["stem"], "让子弹飞 (2010) 1080p")

    def test_chinese_season_from_parent_dir(self):
        # 文件名没有 SxxExx 时，季号从父目录"第N季"解析
        info = tp.parse_filename("/media/tv/某番/第2季/某番 第3集.mkv")
        self.assertEqual(info["season"], 2)


class TestCleanTitle(unittest.TestCase):
    def test_strips_year_and_quality_tags(self):
        self.assertEqual(tp.clean_title("让子弹飞 (2010) 1080p"), "让子弹飞")
        self.assertEqual(tp.clean_title("Some Movie 1080p"), "Some Movie")

    def test_specials_dir_is_not_a_title(self):
        # "Specials" 曾被当剧名搜出无关剧集
        self.assertEqual(tp.clean_title("Specials"), "")

    def test_strips_chinese_and_english_season(self):
        self.assertEqual(tp.clean_title("无职转生 第3季"), "无职转生")
        self.assertEqual(tp.clean_title("Some Show Season 2"), "Some Show")


class TestPick(unittest.TestCase):
    TV_RESULTS = [
        {"id": 999, "name": "无关剧", "original_name": "x", "first_air_date": "1999-01-01"},
        {"id": 65942, "name": "Re:Zero", "original_name": "Re:ゼロから始める異世界生活",
         "first_air_date": "2016-04-04"},
    ]

    def test_pick_tv_prefers_name_match_with_year(self):
        self.assertEqual(tp.pick_tv(self.TV_RESULTS, "Re:Zero", 2016)["id"], 65942)

    def test_pick_movie_short_title_trap(self):
        # 无职转生总集篇案例：短标题《无职转生》会被"无职转生 特别篇"这类文件名
        # 反向包含抢走命中，反向包含时短标题( len<6 )必须不得分
        results = [
            {"id": 1, "title": "无职转生", "original_title": "Mushoku Tensei"},
            {"id": 2, "title": "无职转生 特别篇", "original_title": ""},
        ]
        m, _score = tp.pick_movie(results, "无职转生 特别篇", 0)
        self.assertEqual(m["id"], 2)


if __name__ == "__main__":
    unittest.main()
