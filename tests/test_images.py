"""图像特征相关测试：无 ffmpeg 时去重禁用路径 + 特征打包/判同纯函数。"""
import unittest
from unittest import mock

import _bootstrap as bs  # noqa: F401
import tmdb_provider as tp


def _data(n):
    """合法字节序列（0..255 循环），含方差，供特征构造用。"""
    return bytes(i % 256 for i in range(n))


class TestNoFfmpeg(unittest.TestCase):
    def test_sim_feat_disabled_without_ffmpeg(self):
        """镜像不含 ffmpeg：_sim_feat 直接禁用（不去 TMDB 取图、不抛异常）。"""
        bs.reset_state()
        with mock.patch.object(tp, "FFMPEG_BIN", ""), \
             mock.patch.object(tp, "fetch_tmdb_bytes") as fetch:
            self.assertIsNone(tp._sim_feat("abc.jpg", "post"))
        fetch.assert_not_called()

    def test_sim_feat_uses_cache_before_ffmpeg(self):
        """已有持久化特征的图：即使没有 ffmpeg 也能用缓存特征参与去重。"""
        bs.reset_state()
        tp.STATE["img_feat"]["abc.jpg"] = tp._feat_pack(_data(432), _data(576))
        with mock.patch.object(tp, "FFMPEG_BIN", ""):
            feat = tp._sim_feat("abc.jpg", "post")
        self.assertEqual(len(feat[0]), 432)   # 12x12x3 RGB 去均值向量
        self.assertEqual(len(feat[1]), 576)   # 24x24 边缘梯度


class TestFeatPure(unittest.TestCase):
    def test_pack_unpack_roundtrip(self):
        packed = tp._feat_pack(_data(432), _data(576))
        rgb, edge = tp._feat_unpack(packed)
        self.assertEqual(len(rgb), 432)
        self.assertEqual(len(edge), 576)

    def test_identical_features_are_similar(self):
        f = tp._feat_unpack(tp._feat_pack(_data(432), _data(576)))
        self.assertTrue(tp._similar(f, f))


if __name__ == "__main__":
    unittest.main()
