import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cascade.db import Base


class RunState(enum.StrEnum):
    received = "received"
    planning = "planning"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    needs_attention = "needs_attention"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trello_card_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    workload: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, name="run_state"), nullable=False, default=RunState.received
    )
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["Event"]] = relationship(back_populates="run")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    trello_action_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    run: Mapped[Run | None] = relationship(back_populates="events")
