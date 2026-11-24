"""Catalog tables. Every chunk can be traced back to its document, ingestion
run, source and the raw file in the lake (lineage)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    slug: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    collection_alias: Mapped[str] = mapped_column(String)
    collection_name: Mapped[str] = mapped_column(String)
    collection_version: Mapped[int] = mapped_column(Integer, default=1)
    # Snapshot of the embedding chain the collection was created with.
    vector_config: Mapped[list] = mapped_column(JSON, default=list)
    chunk_size: Mapped[int] = mapped_column(Integer, default=1500)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list["Document"]] = relationship(back_populates="kb", cascade="all, delete-orphan")
    model_stats: Mapped[list["KBModelStat"]] = relationship(back_populates="kb", cascade="all, delete-orphan")


class KBModelStat(Base):
    """How many points of a KB were embedded by each model (drives query fan-out)."""

    __tablename__ = "kb_model_stats"
    __table_args__ = (UniqueConstraint("kb_id", "vector_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    vector_name: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    points: Mapped[int] = mapped_column(Integer, default=0)
    kb: Mapped[KnowledgeBase] = relationship(back_populates="model_stats")


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String)  # upload | text | url | web_search | feed | dataset
    name: Mapped[str] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    kb_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")  # running | done | failed
    raw_path: Mapped[str] = mapped_column(String, default="")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("kb_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    mime: Mapped[str] = mapped_column(String, default="text/plain")
    raw_path: Mapped[str] = mapped_column(String)  # file in lake/raw
    clean_path: Mapped[str] = mapped_column(String)  # file in lake/clean
    content_hash: Mapped[str] = mapped_column(String)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"
    # INTEGER PRIMARY KEY aliases the rowid, which the FTS5 index points at
    # (a stable rowid, unlike the implicit one VACUUM may renumber).
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True)  # also the Qdrant point id (uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kb_id: Mapped[str] = mapped_column(String, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embed_model: Mapped[str] = mapped_column(String)
    vector_name: Mapped[str] = mapped_column(String)
    document: Mapped[Document] = relationship(back_populates="chunks")


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    table_name: Mapped[str] = mapped_column(String, unique=True)
    zone: Mapped[str] = mapped_column(String, default="clean")  # clean (ds_) | curated (cur_)
    description: Mapped[str] = mapped_column(Text, default="")
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    raw_path: Mapped[str] = mapped_column(String, default="")
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)  # for curated tables
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    columns: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavedQuery(Base):
    __tablename__ = "saved_queries"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String)
    sql: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")  # queued | running | done | failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Feed(Base):
    """A topic that is re-searched with Keiro on an interval and ingested."""

    __tablename__ = "feeds"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    query: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String, default="content")  # lite | fast | content
    max_results: Mapped[int] = mapped_column(Integer, default=5)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Library(Base):
    """An offline library: one ZIM file (Wikipedia, iFixit, ...) on local disk."""

    __tablename__ = "libraries"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)  # catalog name without date, e.g. wikipedia_en_all_nopic
    title: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    filename: Mapped[str] = mapped_column(String, default="")
    path: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str] = mapped_column(String, default="")
    issued: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="queued")  # queued | downloading | ready | failed
    message: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)  # used as a source in Ask
    index_tier: Mapped[int] = mapped_column(Integer, default=0)  # 0 built-in search, 1 intros, 2 passages
    index_cursor: Mapped[int] = mapped_column(Integer, default=0)  # next entry id to index (resumable)
    indexed: Mapped[int] = mapped_column(Integer, default=0)
    index_model: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmergencyGuide(Base):
    """Legacy (0.4 emergency guides). No longer used; kept so existing data is never dropped."""

    __tablename__ = "emergency_guides"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String, default="")
    sources: Mapped[dict] = mapped_column(JSON, default=dict)
    safety: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Watch(Base):
    """A folder kept in sync with a knowledge base (an Obsidian vault, a documents folder...)."""

    __tablename__ = "watches"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # relative path -> [mtime_ns, size]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Flashcard(Base):
    """A study card with spaced-repetition state."""

    __tablename__ = "flashcards"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    deck: Mapped[str] = mapped_column(String)
    front: Mapped[str] = mapped_column(Text)
    back: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String, default="")  # where it came from, shown on the card
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    interval_days: Mapped[float] = mapped_column(Float, default=0.0)
    ease: Mapped[float] = mapped_column(Float, default=2.5)
    reps: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pipeline(Base):
    """A data pipeline: KeiroLabs collects pages, a model extracts fields, rows land in a table and/or a knowledge base."""

    __tablename__ = "pipelines"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    table_name: Mapped[str] = mapped_column(String, default="")
    kb_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = run by hand
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    """An Ask thread. Messages are kept so a chat survives page changes and restarts."""

    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String, default="New chat")
    kb_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    messages: Mapped[list["ConversationMessage"]] = relationship(back_populates="conversation", cascade="all, delete-orphan",
                                                                 order_by="ConversationMessage.created_at")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String)  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="done")  # running | done | stopped | failed | interrupted
    # Sources, model, route and safety notice for assistant messages.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ApiKey(Base):
    """A key that lets one of the user's own apps use the data API (/v1 and /mcp)."""

    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String)
    prefix: Mapped[str] = mapped_column(String)  # first characters, shown so the user can tell keys apart
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)  # sha256 of the full key; the key itself is never stored
    scopes: Mapped[list] = mapped_column(JSON, default=list)  # read | ask | write | sql
    kb_ids: Mapped[list] = mapped_column(JSON, default=list)  # knowledge bases it may use; empty = all
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessLog(Base):
    __tablename__ = "access_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    key_name: Mapped[str] = mapped_column(String, default="")
    method: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    status: Mapped[int] = mapped_column(Integer)
    client: Mapped[str] = mapped_column(String, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Webhook(Base):
    """An address that is told when new data arrives."""

    __tablename__ = "webhooks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String)
    events: Mapped[list] = mapped_column(JSON, default=list)
    secret: Mapped[str] = mapped_column(String)  # signs each delivery (HMAC-SHA256)
    enabled: Mapped[bool] = mapped_column(Integer, default=1)
    last_status: Mapped[str] = mapped_column(String, default="")
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Sheet(Base):
    """A table in the lake that works like a spreadsheet. The rows live in the SQLite table `table_name`."""

    __tablename__ = "sheets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String)
    table_name: Mapped[str] = mapped_column(String, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # Per column: {"name", "type": text|number|date|boolean|url, "kind": plain|ai|web, "prompt"}
    columns: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String, default="manual")  # manual | import | build | dataset
    pipeline_id: Mapped[str | None] = mapped_column(String, nullable=True)  # set when the sheet is built from a description
    attribution: Mapped[str] = mapped_column(Text, default="")  # licence and credit for imported open data
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SheetCellMeta(Base):
    """Where a filled-in cell came from: its sources and how sure the model was."""

    __tablename__ = "sheet_cell_meta"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String, index=True)
    row_id: Mapped[int] = mapped_column(Integer)
    column: Mapped[str] = mapped_column(String)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("table_name", "row_id", "column", name="uq_sheet_cell"),)


class EvidenceRow(Base):
    """A passage research found on the web, kept so later questions can reuse it."""

    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String, default="")
    query: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String, default="", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    domain: Mapped[str] = mapped_column(String, default="")
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthConfig(Base):
    """Small key/value store for the sign-in password hash."""

    __tablename__ = "auth_config"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuthSession(Base):
    """A signed-in browser or device. Only a hash of its cookie is stored."""

    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    device: Mapped[str] = mapped_column(String, default="")
    client: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Connector(Base):
    """A saved external source that keeps a sheet up to date: a JSON API, a feed or a folder of files."""

    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)  # rest | rss | folder
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    sheet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    key_column: Mapped[str] = mapped_column(String, default="")  # rows with the same key are updated, new keys are added
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Topic(Base):
    """Everything held about one subject: a sheet of facts, a searchable collection of the pages, and a CSV copy."""

    __tablename__ = "topics"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    question: Mapped[str] = mapped_column(Text, default="")
    urls: Mapped[list] = mapped_column(JSON, default=list)
    sheet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kb_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="new")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavedView(Base):
    """A named read-only query over the lake's tables, served at /v1/views/<name>."""

    __tablename__ = "saved_views"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    sql: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TableIndex(Base):
    """A table's rows embedded into a collection, so rows can be found by meaning."""

    __tablename__ = "table_indexes"
    sheet_id: Mapped[str] = mapped_column(String, primary_key=True)
    kb_id: Mapped[str] = mapped_column(String)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
