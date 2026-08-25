"""Contract tests for Projector.fold_bot_role.

覆盖：
- 无事件 → None
- 单条 observed → 返回该角色
- 多条 observed → 取最后（最新）
- 账号身份过滤：payload.self_id 不匹配 bot_user_id 被忽略
- bot_user_id=None 时不过滤 self_id
- 非 bot_role_observed 类型的事件被跳过
- payload.role 为空/非法值时跳过
- 角色字符串规范化为小写
- project() 把 fold 结果落进 DecisionContext.bot_role；非法角色 → None
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from qqbot.services.agent_loop.projection import Projector, _EventSnapshot


def _ev(
    *,
    event_id: str,
    occurred_at: datetime,
    type_: str = "runtime.bot_role_observed",
    scope: str = "group",
    group_id: int | None = 100,
    payload: dict | None = None,
) -> _EventSnapshot:
    return _EventSnapshot(
        event_id=event_id,
        occurred_at=occurred_at,
        origin="runtime",
        type=type_,
        scope=scope,
        group_id=group_id,
        user_id=None,
        visibility="agent_visible",
        correlation_id=None,
        causation_id=None,
        payload=payload or {},
    )


def _at(seconds: int) -> datetime:
    return datetime(2026, 5, 28, 0, 0, 0, tzinfo=timezone.utc).replace(
        second=seconds
    )


class FoldBotRoleContractTest(unittest.TestCase):
    def test_empty_returns_none(self) -> None:
        self.assertIsNone(Projector.fold_bot_role([]))

    def test_single_observed_returns_role(self) -> None:
        evs = [_ev(event_id="E1", occurred_at=_at(1), payload={"role": "admin"})]
        self.assertEqual(Projector.fold_bot_role(evs), "admin")

    def test_latest_wins(self) -> None:
        evs = [
            _ev(event_id="E1", occurred_at=_at(1), payload={"role": "member"}),
            _ev(event_id="E2", occurred_at=_at(2), payload={"role": "admin"}),
            _ev(event_id="E3", occurred_at=_at(3), payload={"role": "owner"}),
        ]
        self.assertEqual(Projector.fold_bot_role(evs), "owner")

    def test_self_id_filter(self) -> None:
        # 防御性身份过滤：只取指定 self_id 的 baseline
        evs = [
            _ev(
                event_id="E1",
                occurred_at=_at(1),
                payload={"role": "owner", "self_id": "BOT_B"},
            ),
            _ev(
                event_id="E2",
                occurred_at=_at(2),
                payload={"role": "admin", "self_id": "BOT_A"},
            ),
            _ev(
                event_id="E3",
                occurred_at=_at(3),
                payload={"role": "owner", "self_id": "BOT_B"},
            ),
        ]
        self.assertEqual(
            Projector.fold_bot_role(evs, bot_user_id="BOT_A"), "admin"
        )

    def test_no_bot_user_id_filter(self) -> None:
        evs = [
            _ev(
                event_id="E1",
                occurred_at=_at(1),
                payload={"role": "admin", "self_id": "X"},
            ),
            _ev(
                event_id="E2",
                occurred_at=_at(2),
                payload={"role": "member", "self_id": "Y"},
            ),
        ]
        # 不传 bot_user_id → 不过滤 → 取最后一条（启动期兼容路径）
        self.assertEqual(Projector.fold_bot_role(evs), "member")

    def test_other_event_types_ignored(self) -> None:
        evs = [
            _ev(event_id="E1", occurred_at=_at(1), payload={"role": "admin"}),
            _ev(
                event_id="E2",
                occurred_at=_at(2),
                type_="external.message.group.normal",
                payload={"role": "garbage"},  # 即使有 role 字段也不该被拾取
            ),
        ]
        self.assertEqual(Projector.fold_bot_role(evs), "admin")

    def test_invalid_role_value_skipped(self) -> None:
        evs = [
            _ev(event_id="E1", occurred_at=_at(1), payload={"role": "admin"}),
            _ev(event_id="E2", occurred_at=_at(2), payload={"role": ""}),
            _ev(event_id="E3", occurred_at=_at(3), payload={}),  # 没 role
        ]
        # E2/E3 被跳过，最新合法是 E1
        self.assertEqual(Projector.fold_bot_role(evs), "admin")

    def test_role_normalized_to_lowercase(self) -> None:
        evs = [_ev(event_id="E1", occurred_at=_at(1), payload={"role": " Admin "})]
        self.assertEqual(Projector.fold_bot_role(evs), "admin")


class ProjectContextBotRoleContractTest(unittest.TestCase):
    """project() 的 bot_role 注入路径。"""

    def test_project_picks_up_fold(self) -> None:
        evs = [_ev(event_id="E1", occurred_at=_at(1), payload={"role": "admin"})]
        ctx = Projector.project(
            evs,
            scope_key="group:100",
            correlation_id="c",
            tick_seq=1,
            now=_at(2),
        )
        self.assertEqual(ctx.bot_role, "admin")

    def test_project_explicit_overrides_fold(self) -> None:
        """build_context 单独查 bot_role 时 caller 显式传入，应优先生效。"""
        evs = [_ev(event_id="E1", occurred_at=_at(1), payload={"role": "member"})]
        ctx = Projector.project(
            evs,
            scope_key="group:100",
            correlation_id="c",
            tick_seq=1,
            now=_at(2),
            bot_role="owner",
        )
        self.assertEqual(ctx.bot_role, "owner")

    def test_project_invalid_role_becomes_none(self) -> None:
        ctx = Projector.project(
            [],
            scope_key="group:100",
            correlation_id="c",
            tick_seq=1,
            now=_at(1),
            bot_role="GARBAGE",
        )
        self.assertIsNone(ctx.bot_role)

    def test_project_no_role_data_is_none(self) -> None:
        ctx = Projector.project(
            [],
            scope_key="group:100",
            correlation_id="c",
            tick_seq=1,
            now=_at(1),
        )
        self.assertIsNone(ctx.bot_role)


if __name__ == "__main__":
    unittest.main()
