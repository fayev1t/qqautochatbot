"""event_id 发放权：静态冻结，不依赖 sqlalchemy / loguru。

行为测试（聚水排序、内部跳窗、指纹保持）仍在
``test_event_ingest_pipeline_contract.py``。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "qqbot"


class EventIdIssuanceAuthorityTests(unittest.TestCase):
    def test_mappers_do_not_mint_event_ids(self) -> None:
        mappers_dir = PKG / "services" / "event_ingest" / "mappers"
        for path in mappers_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("new_event_id", text, path.name)
            self.assertNotIn("new_msg_hash", text, path.name)
            self.assertNotIn("issue_event_id", text, path.name)
            self.assertNotIn("msg_hash", text, path.name)

    def test_finalize_does_not_mint(self) -> None:
        text = (
            PKG / "services" / "event_ingest" / "system_event.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("new_event_id", text)
        self.assertNotIn("issue_event_id", text)
        self.assertIn("event_id: str", text)

    def test_event_writer_issues_via_registrar(self) -> None:
        text = (
            PKG / "services" / "agent_loop" / "event_writer.py"
        ).read_text(encoding="utf-8")
        self.assertIn("issue_event_id", text)
        self.assertNotIn("from qqbot.core.ids import new_event_id", text)

    def test_program_events_issues_called_event_via_registrar(self) -> None:
        text = (
            PKG / "services" / "agent_loop" / "program_events.py"
        ).read_text(encoding="utf-8")
        self.assertIn("called_event_id = issue_event_id()", text)

    def test_ids_module_has_no_msg_hash(self) -> None:
        from qqbot.core import ids

        self.assertFalse(hasattr(ids, "new_msg_hash"))
        self.assertTrue(hasattr(ids, "new_event_id"))
        text = (PKG / "core" / "ids.py").read_text(encoding="utf-8")
        self.assertNotIn("new_msg_hash", text)

    def test_registrar_owns_issue_event_id(self) -> None:
        text = (
            PKG / "services" / "event_gateway" / "registry.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def issue_event_id() -> str:", text)
        self.assertIn("async def register_now(", text)
        self.assertIn("_POOLED_CHANNELS", text)
        self.assertIn("file_hash", text)
        self.assertIn("program_sha256", text)
        self.assertIn("idempotency_key", text)

    def test_gateway_does_not_mint_event_id(self) -> None:
        inbound = (
            PKG / "services" / "event_gateway" / "inbound.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("new_event_id", inbound)
        self.assertNotIn("issue_event_id", inbound)


if __name__ == "__main__":
    unittest.main()
