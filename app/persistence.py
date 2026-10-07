"""助手自有状态的 SQL Repository；生产以 PostgreSQL + Alembic 为准。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from app.database import create_database_engine
from app.models import AgentTrace, AuditLog, Bundle, ModelUsageLog

metadata = MetaData()
agent_traces = Table(
    "agent_traces",
    metadata,
    Column("run_id", String(80), primary_key=True),
    Column("payload_json", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
assistant_audit_logs = Table(
    "assistant_audit_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("run_id", String(80), nullable=False, index=True),
    Column("action", String(120), nullable=False),
    Column("decision", String(40), nullable=False),
    Column("reason", Text, nullable=False),
    Column("input_summary", Text, nullable=False),
    Column("outcome", Text, nullable=False),
)
model_usage_logs = Table(
    "model_usage_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(80), nullable=False, index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("payload_json", Text, nullable=False),
)
confirmation_tokens = Table(
    "confirmation_tokens",
    metadata,
    Column("token", String(160), primary_key=True),
    Column("run_id", String(80), nullable=False),
    Column("bundle_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("state", String(16), nullable=False, index=True),
)
cart_drafts = Table(
    "cart_drafts",
    metadata,
    Column("cart_draft_id", String(100), primary_key=True),
    Column("confirmation_token", String(160), nullable=False, unique=True),
    Column("bundle_json", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("price_checked_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class StoredConfirmation:
    token: str
    bundle: Bundle
    run_id: str
    created_at: datetime
    expires_at: datetime
    state: str


def utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SqlAssistantRepository:
    """在 PostgreSQL 事务中保存 Agent 审计、轨迹和一次性确认令牌。"""

    def __init__(self, url: str | None = None, engine: Engine | None = None) -> None:
        self.engine = engine or create_database_engine(url)
        # Schema 只能由 Alembic 管理；测试内存库例外，便于保留既有纯单元测试入口。
        if os.getenv("APP_ENV") == "test":
            metadata.create_all(self.engine)

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def save_trace(self, trace: AgentTrace) -> None:
        values = {
            "run_id": trace.run_id,
            "payload_json": self._json(trace.model_dump(mode="json")),
            "updated_at": datetime.now(UTC),
        }
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(agent_traces.c.run_id).where(agent_traces.c.run_id == trace.run_id)
            ).scalar_one_or_none()
            statement = (
                update(agent_traces).where(agent_traces.c.run_id == trace.run_id).values(**values)
                if exists
                else insert(agent_traces).values(**values)
            )
            connection.execute(statement)

    def get_trace(self, run_id: str) -> AgentTrace | None:
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(agent_traces.c.payload_json).where(agent_traces.c.run_id == run_id)
            ).scalar_one_or_none()
        return AgentTrace.model_validate_json(payload) if payload else None

    def add_audit_log(self, item: AuditLog) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(assistant_audit_logs).values(
                    at=utc(item.at),
                    run_id=item.run_id,
                    action=item.action,
                    decision=item.decision,
                    reason=item.reason,
                    input_summary=item.input_summary,
                    outcome=item.outcome,
                )
            )

    def list_audit_logs(self) -> list[AuditLog]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(assistant_audit_logs).order_by(assistant_audit_logs.c.id)
            ).mappings()
            return [AuditLog.model_validate(dict(row)) for row in rows]

    def add_model_usage_log(self, item: ModelUsageLog) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(model_usage_logs).values(
                    run_id=item.run_id,
                    at=utc(item.at),
                    payload_json=self._json(item.model_dump(mode="json")),
                )
            )

    def list_model_usage_logs(self) -> list[ModelUsageLog]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(model_usage_logs.c.payload_json).order_by(model_usage_logs.c.id)
            )
            return [ModelUsageLog.model_validate_json(row.payload_json) for row in rows]

    def create_confirmation(
        self, token: str, bundle: Bundle, run_id: str, created_at: datetime, expires_at: datetime
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(confirmation_tokens).values(
                    token=token,
                    run_id=run_id,
                    bundle_json=self._json(bundle.model_dump(mode="json")),
                    created_at=utc(created_at),
                    expires_at=utc(expires_at),
                    state="pending",
                )
            )

    def get_pending_confirmation(self, token: str, now: datetime) -> StoredConfirmation | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(confirmation_tokens)
                    .where(confirmation_tokens.c.token == token)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            state, expires_at = row["state"], utc(row["expires_at"])
            if state == "pending" and expires_at <= utc(now):
                state = "expired"
                connection.execute(
                    update(confirmation_tokens)
                    .where(confirmation_tokens.c.token == token)
                    .values(state=state)
                )
            return StoredConfirmation(
                token=row["token"],
                bundle=Bundle.model_validate_json(row["bundle_json"]),
                run_id=row["run_id"],
                created_at=utc(row["created_at"]),
                expires_at=expires_at,
                state=state,
            )

    def consume_confirmation_and_create_draft(
        self, token: str, bundle: Bundle, cart_draft_id: str, now: datetime, audit_log: AuditLog
    ) -> bool:
        """条件 UPDATE 和同一事务保证令牌消费、草稿与审计不可部分成功。"""
        checked_at = utc(now)
        with self.engine.begin() as connection:
            consumed = connection.execute(
                update(confirmation_tokens)
                .where(
                    and_(
                        confirmation_tokens.c.token == token,
                        confirmation_tokens.c.state == "pending",
                        confirmation_tokens.c.expires_at > checked_at,
                    )
                )
                .values(state="consumed")
            ).rowcount
            if consumed != 1:
                connection.execute(
                    update(confirmation_tokens)
                    .where(
                        and_(
                            confirmation_tokens.c.token == token,
                            confirmation_tokens.c.state == "pending",
                        )
                    )
                    .values(state="expired")
                )
                return False
            connection.execute(
                insert(cart_drafts).values(
                    cart_draft_id=cart_draft_id,
                    confirmation_token=token,
                    bundle_json=self._json(bundle.model_dump(mode="json")),
                    status="created",
                    price_checked_at=checked_at,
                    created_at=checked_at,
                )
            )
            connection.execute(
                insert(assistant_audit_logs).values(
                    at=utc(audit_log.at),
                    run_id=audit_log.run_id,
                    action=audit_log.action,
                    decision=audit_log.decision,
                    reason=audit_log.reason,
                    input_summary=audit_log.input_summary,
                    outcome=audit_log.outcome,
                )
            )
            return True
