"""JSON 文件持久化：原子写入，进程重启后可完整恢复。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
