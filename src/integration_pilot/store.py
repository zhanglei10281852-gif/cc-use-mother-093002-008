"""JSON 文件持久化：写入采用临时文件 + 原子替换，进程重启后可完整恢复。"""
from __future__ import annotations

import json
from pathlib import Path

COLLECTIONS = ("artifacts", "plans", "pilots", "freezes", "recalls")


class JsonStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {name: {} for name in COLLECTIONS}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {name: raw.get(name, {}) for name in COLLECTIONS}

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
