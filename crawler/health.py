# -*- coding: utf-8 -*-
"""单源健康度：连续失败计数 + 冷却熔断（跨运行持久化）。

用途：某个源连续抓不到东西（改版、封禁、临时故障）时，冷却一段时间不再抓，
避免每次运行都白跑并反复烧 AI / 触发反爬。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from models import iso, parse_iso, utcnow


@dataclass(slots=True)
class SourceHealth:
    url_norm: str
    failures: int = 0
    last_error: str = ""
    last_failure_at: str = ""
    cooldown_until: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url_norm": self.url_norm,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_failure_at": self.last_failure_at,
            "cooldown_until": self.cooldown_until,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceHealth":
        return cls(
            url_norm=str(data.get("url_norm") or ""),
            failures=int(data.get("failures") or 0),
            last_error=str(data.get("last_error") or ""),
            last_failure_at=str(data.get("last_failure_at") or ""),
            cooldown_until=str(data.get("cooldown_until") or ""),
        )

    @property
    def cooling(self) -> bool:
        until = parse_iso(self.cooldown_until)
        return until is not None and until > utcnow()


class SourceHealthStore:
    """JSON 持久化（原子写入；文件损坏时按空处理）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, SourceHealth]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        health: dict[str, SourceHealth] = {}
        for key, value in raw.items():
            if isinstance(value, Mapping):
                health[str(key)] = SourceHealth.from_dict(value)
        return health

    def _save(self, data: dict[str, SourceHealth]) -> None:
        payload = {
            "version": 1,
            "sources": {key: value.to_dict() for key, value in data.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, url_norm: str) -> SourceHealth:
        return self._load().get(url_norm, SourceHealth(url_norm=url_norm))

    def cooling(self, url_norm: str) -> SourceHealth | None:
        health = self._load().get(url_norm)
        if health is not None and health.cooling:
            return health
        return None

    def record_success(self, url_norm: str) -> None:
        data = self._load()
        health = data.get(url_norm)
        if health is None:
            return
        health.failures = 0
        health.last_error = ""
        health.cooldown_until = ""
        data[url_norm] = health
        self._save(data)

    def record_failure(
        self,
        url_norm: str,
        error: str,
        *,
        threshold: int,
        cooldown_minutes: int,
    ) -> SourceHealth:
        data = self._load()
        health = data.get(url_norm, SourceHealth(url_norm=url_norm))
        health.failures += 1
        health.last_error = (error or "")[:300]
        health.last_failure_at = iso(utcnow())
        if threshold > 0 and health.failures >= threshold:
            health.cooldown_until = iso(utcnow() + timedelta(minutes=max(1, cooldown_minutes)))
        data[url_norm] = health
        self._save(data)
        return health
