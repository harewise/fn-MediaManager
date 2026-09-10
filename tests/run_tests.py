"""测试入口：python3 tests/run_tests.py（离线，不碰 TMDB / 生产容器 / 生产缓存）。"""
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)

import _bootstrap  # noqa: F401  先于一切测试导入（隔离缓存目录）

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(TESTS_DIR, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
