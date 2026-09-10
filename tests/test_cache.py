"""缓存持久化测试：roundtrip / 损坏容忍 / 剧集组 TTL。全部落到临时目录。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import _bootstrap as bs
import tmdb_provider as tp


class IsolatedCache(unittest.TestCase):
    """把 CACHE_FILE 指向用例专属临时文件，结束后还原。"""

    def setUp(self):
        bs.reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (tp.CACHE_FILE, tp.IMG_CACHE_DIR)
        tp.CACHE_FILE = os.path.join(self._tmp.name, "cache.json")
        tp.IMG_CACHE_DIR = os.path.join(self._tmp.name, "img")

    def tearDown(self):
        tp.CACHE_FILE, tp.IMG_CACHE_DIR = self._orig
        self._tmp.cleanup()


class TestRoundtrip(IsolatedCache):
    def test_save_load_restores_key_types(self):
        tp.STATE["group_cache"][94664] = [{"episode_number": 1}]
        tp.STATE["group_ts"][94664] = 123.0
        tp.STATE["poster_cache"][(65942, 0)] = "/poster.jpg"   # tuple key
        tp.STATE["img_types"]["a.jpg"] = "post"
        tp.STATE["img_feat"]["a.jpg"] = "aa|bb"
        tp._save_caches()
        bs.reset_state()                                        # 清空后重载
        tp._load_caches()
        self.assertEqual(tp.STATE["group_cache"][94664], [{"episode_number": 1}])
        self.assertEqual(tp.STATE["group_ts"][94664], 123.0)
        self.assertEqual(tp.STATE["poster_cache"][(65942, 0)], "/poster.jpg")
        self.assertEqual(tp.STATE["img_types"]["a.jpg"], "post")
        self.assertEqual(tp.STATE["img_feat"]["a.jpg"], "aa|bb")

    def test_legacy_img_feat_format_ignored(self):
        tp.STATE["img_feat"]["old.jpg"] = "aabb"                # 旧格式（无 |）
        tp._save_caches()
        tp.STATE["img_feat"].clear()
        tp._load_caches()
        self.assertNotIn("old.jpg", tp.STATE["img_feat"])


class TestCorrupt(IsolatedCache):
    def test_garbage_file_is_tolerated(self):
        with open(tp.CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("not json at all")
        tp._load_caches()                                       # 不抛异常即可
        self.assertEqual(tp.STATE["group_cache"], {})

    def test_missing_file_is_tolerated(self):
        tp._load_caches()
        self.assertEqual(tp.STATE["poster_cache"], {})


class TestGroupTTL(IsolatedCache):
    def test_ttl_cache_avoids_refetch(self):
        with bs.FakeTMDB(), mock.patch.object(tp, "tmdb_get", wraps=tp.tmdb_get) as m:
            flat1 = tp._flat_groups(65942)
            self.assertTrue(flat1)                              # fixture：Re:Zero 有剧集组
            n1 = m.call_count
            self.assertGreater(n1, 0)
            tp._flat_groups(65942)                              # TTL 内：零请求
            self.assertEqual(m.call_count, n1)
            tp.STATE["group_ts"][65942] = 0                     # 过期 → 重取
            flat3 = tp._flat_groups(65942)
            self.assertGreater(m.call_count, n1)
            self.assertEqual(len(flat3), len(flat1))

    def test_failed_refetch_keeps_old_cache(self):
        # TMDB 瞬时故障时沿用旧缓存，不能把空结果写进缓存
        with bs.FakeTMDB():
            flat1 = tp._flat_groups(65942)
            self.assertTrue(flat1)
        tp.STATE["group_ts"][65942] = 0
        with mock.patch.object(tp, "tmdb_get", return_value={"_status": -1}):
            flat2 = tp._flat_groups(65942)
        self.assertEqual(flat2, flat1)
        self.assertTrue(tp.STATE["group_cache"].get(65942))


class TestDataVersion(unittest.TestCase):
    def test_deterministic_and_content_sensitive(self):
        a = {"name": "第 3 季", "episode_count": 12}
        b = {"name": "第 3 季", "episode_count": 13}
        self.assertEqual(tp.data_version(a), tp.data_version(dict(a)))
        self.assertNotEqual(tp.data_version(a), tp.data_version(b))
        # data_version 字段本身不参与哈希（递归防死循环/版本幂等）
        self.assertEqual(tp.data_version({**a, "data_version": "x"}), tp.data_version(a))


if __name__ == "__main__":
    unittest.main()
