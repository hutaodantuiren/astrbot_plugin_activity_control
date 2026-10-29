"""按群持久化模式状态；聊天记录只保存在内存中。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .core import Mode, normalize_mode


@dataclass
class GroupState:
    group_id: str
    mode: str = Mode.DIVE.value
    updated_at: float = 0.0
    last_reply_at: float = 0.0
    last_seen_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, group_id: str, data: object) -> "GroupState":
        if not isinstance(data, dict):
            return cls(group_id=group_id)
        return cls(
            group_id=group_id,
            mode=normalize_mode(data.get("mode"), Mode.DIVE.value),
            updated_at=float(data.get("updated_at") or 0.0),
            last_reply_at=float(data.get("last_reply_at") or 0.0),
            last_seen_at=float(data.get("last_seen_at") or 0.0),
        )


class StateStore:
    """非常轻量的 JSON 状态仓库，写入使用临时文件原子替换。"""

    VERSION = 1

    def __init__(self, path: Path, default_mode: str = Mode.DIVE.value) -> None:
        self.path = Path(path)
        self.default_mode = normalize_mode(default_mode, Mode.DIVE.value)
        self.groups: dict[str, GroupState] = {}
        self.dirty = False
        self.load_errors: list[str] = []
        self.load()

    def load(self) -> None:
        self.groups.clear()
        self.load_errors.clear()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏档不应阻止插件启动
            self.load_errors.append(f"读取状态文件失败: {exc}")
            self._backup_corrupt()
            return

        raw_groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(raw_groups, dict):
            self.load_errors.append("状态文件缺少 groups 对象，已忽略")
            return

        for gid, item in raw_groups.items():
            group_id = str(gid or "").strip()
            if not group_id:
                continue
            state = GroupState.from_dict(group_id, item)
            # 旧存档没有模式字段时，使用当前默认模式。
            if not isinstance(item, dict) or "mode" not in item:
                state.mode = self.default_mode
            self.groups[group_id] = state

    def _backup_corrupt(self) -> None:
        try:
            suffix = time.strftime("%Y%m%d-%H%M%S")
            self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{suffix}"))
        except OSError:
            pass

    def get(self, group_id: str) -> GroupState:
        gid = str(group_id or "").strip()
        state = self.groups.get(gid)
        if state is None:
            state = GroupState(group_id=gid, mode=self.default_mode)
            self.groups[gid] = state
            self.dirty = True
        return state

    def set_mode(self, group_id: str, mode: str) -> GroupState:
        state = self.get(group_id)
        state.mode = normalize_mode(mode, self.default_mode)
        state.updated_at = time.time()
        state.last_reply_at = 0.0
        self.dirty = True
        return state

    def mark_reply(self, group_id: str, ts: float | None = None) -> None:
        state = self.get(group_id)
        state.last_reply_at = float(ts or time.time())
        state.updated_at = state.last_reply_at
        self.dirty = True

    def touch(self, group_id: str, ts: float | None = None) -> None:
        state = self.get(group_id)
        state.last_seen_at = float(ts or time.time())
        # last_seen_at 不需要每条消息都落盘。

    def prune(self, *, max_groups: int = 500, ttl_days: int = 30) -> int:
        """清理长期不活跃的群；active 模式也受 TTL 约束，避免无限增长。"""
        now = time.time()
        ttl = max(1, int(ttl_days or 30)) * 86400
        remove: list[str] = []
        for gid, state in self.groups.items():
            last = max(state.updated_at, state.last_reply_at, state.last_seen_at)
            if last and now - last > ttl:
                remove.append(gid)
        for gid in remove:
            self.groups.pop(gid, None)
        if len(self.groups) > max(1, int(max_groups or 500)):
            limit = max(1, int(max_groups or 500))
            ordered = sorted(
                self.groups.values(),
                key=lambda s: max(s.updated_at, s.last_reply_at, s.last_seen_at),
            )
            for state in ordered[: len(self.groups) - limit]:
                remove.append(state.group_id)
                self.groups.pop(state.group_id, None)
        if remove:
            self.dirty = True
        return len(remove)

    def save(self, force: bool = False) -> bool:
        if not (self.dirty or force):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "updated_at": time.time(),
            "groups": {gid: state.to_dict() for gid, state in self.groups.items()},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.dirty = False
        return True
