import json
from sqlalchemy import String, Text, Integer, Float, Boolean, JSON, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, UserDefinedType


class PGVector(UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kwargs):
        return "VECTOR(1536)"


class Vector(TypeDecorator):
    """Real pgvector on PostgreSQL; JSON text on the SQLite development backend."""
    impl = Text
    cache_ok = True
    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(PGVector() if dialect.name == "postgresql" else Text())
    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps([float(v) for v in value], allow_nan=False)
    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(value) if isinstance(value, str) else [float(v) for v in value]


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    knowledge_version: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    owner_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    source_uri: Mapped[str] = mapped_column(String(1000), default="")
    visibility: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    embedding_space: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), index=True)
    is_learning: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    __table_args__ = (UniqueConstraint("tenant_id", "owner_id", "fingerprint", name="uq_document_fingerprint"),)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list] = mapped_column(Vector())
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_document_chunk"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str] = mapped_column(String(36), default="")
    last_error: Mapped[str] = mapped_column(String(80), default="")
    __table_args__ = (Index("ix_jobs_claim", "state", "available_at", "lease_until"),)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    busy_until: Mapped[float] = mapped_column(Float, default=0)
    busy_token: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[float] = mapped_column(Float)


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    lease_token: Mapped[str] = mapped_column(String(36))
    lease_until: Mapped[float] = mapped_column(Float)
    answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", "request_key", name="uq_turn_request"),)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"), unique=True)
    rating: Mapped[int] = mapped_column(Integer)
    correction: Mapped[str] = mapped_column(Text, default="")
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str] = mapped_column(String(20), default="private")
    state: Mapped[str] = mapped_column(String(20))
    reviewer_id: Mapped[str] = mapped_column(String(100), default="")
    review_reason: Mapped[str] = mapped_column(String(500), default="")
    review_hash: Mapped[str] = mapped_column(String(64), default="")
    review_evidence: Mapped[list] = mapped_column(JSON, default=list)
    learned_document_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[float] = mapped_column(Float)


class Dependency(Base):
    __tablename__ = "document_dependencies"
    learned_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[float] = mapped_column(Float)
