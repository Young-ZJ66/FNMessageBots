#!/usr/bin/env python3
"""一次跑完 tests/ 下所有回归测试。

用法（项目根目录）：
    python tests/run_all.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent


def main() -> int:
    sys.path.insert(0, str(_ROOT))
    sys.path.insert(0, str(_ROOT / "src"))
    sys.path.insert(0, str(_TESTS / "_support"))
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(_TESTS), pattern="test_*.py", top_level_dir=str(_TESTS))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
