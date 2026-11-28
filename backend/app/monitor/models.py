"""Tables for the monitoring desk. They share the catalog's database and Base, so init_db creates them."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.catalog.models import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex


class MonitorSource(Base):
    """One data source the user can switch on: its schedule and how its last run went."""

    __tablename__ = "monitor_sources"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    interval_min: Mapped[int] = mapped_column(Integer, default=60)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_count: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class MonitorEvent(Base):
    """Something that happened in the world: an earthquake, an outbreak, a newly exploited vulnerability, a news item."""

    __tablename__ = "monitor_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    source: Mapped[str] = mapped_column(String(40))
    ext_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # quake | disaster | outbreak | news | cve | ioc
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="info")  # info | low | medium | high | critical
    severity_n: Mapped[int] = mapped_column(Integer, default=0)
    country: Mapped[str] = mapped_column(String(80), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("source", "ext_id", name="uq_monitor_event"), Index("ix_monitor_event_time", "occurred_at"))


class MarketPrice(Base):
    """One trading day for one symbol (NSE symbols end in .NS, as on Yahoo Finance)."""

    __tablename__ = "market_prices"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    close: Mapped[float] = mapped_column(Float)
    prev_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)  # in lakh rupees (NSE), when known
    source: Mapped[str] = mapped_column(String(20), default="")
    currency: Mapped[str] = mapped_column(String(5), default="")


class MonitorRule(Base):
    __tablename__ = "monitor_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(20))  # keyword | severity | price_move | new_high | new_low
    scope: Mapped[str] = mapped_column(String(10), default="any")  # crisis | cyber | market | any
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MonitorAlert(Base):
    __tablename__ = "monitor_alerts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    rule_id: Mapped[str] = mapped_column(String(32), default="")
    ref: Mapped[str] = mapped_column(String(200), default="")  # an event id or a symbol+day
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="info")
    scope: Mapped[str] = mapped_column(String(10), default="any")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    seen: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("rule_id", "ref", name="uq_monitor_alert"),)


class MonitorBrief(Base):
    __tablename__ = "monitor_briefs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
