"""
轮询器共用的去重缓存（JSON 落盘）。

各轮询器（备份 / 影视 / 相册 / 任务计划 / trimactivity / Docker）原本各有一份
逐行相同的 `_load_dedup/_save_dedup/_prune_dedup` 实现，差异只有 TTL 与日志文案。
统一到这里后，行为完全一致：读取失败或格式不符时按「无缓存」处理，
并保证缓存键统一为字符串、值统一为整数时间戳。

用法：
    store = DedupStore(path, ttl_seconds, label, logger)
    store.load()
    store.seen["fp"] = int(time.time())
    store.prune()
    store.save()
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional


class DedupStore:
    """带 TTL 的 JSON 去重缓存。"""

    def __init__(
        self,
        path: Path,
        ttl_seconds: int,
        label: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self.label = label
        self.logger = logger or logging.getLogger(__name__)
        self.seen: Dict[str, int] = {}

    def load(self) -> Dict[str, int]:
        """读取缓存并丢弃超过 TTL 的条目；文件缺失/损坏时清空缓存。"""
        try:
            if self.path.exists():
                obj = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                if isinstance(obj, dict):
                    now = int(time.time())
                    self.seen = {
                        str(k): int(v)
                        for k, v in obj.items()
                        if isinstance(v, (int, float)) and int(v) >= now - self.ttl_seconds
                    }
                    return self.seen
        except Exception as e:
            self.logger.warning("读取%s去重缓存失败: %s", self.label, e)
        self.seen = {}
        return self.seen

    def save(self) -> None:
        """落盘；失败只记日志，不影响轮询。

        显式指定 utf-8：默认编码在 Windows/非 UTF-8 locale 下会把中文键写成 GBK，
        导致下次读取报 UnicodeDecodeError 并让缓存整体失效。
        """
        try:
            self.path.write_text(json.dumps(self.seen, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.logger.warning("写入%s去重缓存失败: %s", self.label, e)

    def prune(self) -> None:
        """就地丢弃超过 TTL 的条目。"""
        now = int(time.time())
        cutoff = now - self.ttl_seconds
        self.seen = {k: int(v) for k, v in self.seen.items() if int(v) >= cutoff}
