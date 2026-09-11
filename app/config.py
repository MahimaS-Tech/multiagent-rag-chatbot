from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True)
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = Field(default="sqlite:///./data/chatbot.db", repr=False)
    redis_url: str = Field(default="", repr=False)
    provider: Literal["mock", "openai"] = "mock"
    orchestrator: Literal["native", "langgraph"] = "native"
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    planner_model: str = "gpt-4.1-mini-2025-04-14"
    answer_model: str = "gpt-4.1-mini-2025-04-14"
    verifier_model: str = "gpt-4.1-2025-04-14"
    fallback_model: str = "gpt-4.1-mini-2025-04-14"
    embedding_model: str = "text-embedding-3-small"
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    jwt_secret: str = Field(default="", repr=False)
    jwt_public_key: str = ""
    jwt_issuer: str = "rag-chatbot-demo"
    jwt_audience: str = "rag-chatbot"
    auto_migrate: bool = False
    redact_pii: bool = True
    cors_origins: list[str] = []
    chat_timeout_seconds: float = Field(default=90, ge=1, le=300)
    provider_timeout_seconds: float = Field(default=20, ge=.1, le=60)
    provider_attempts: int = Field(default=2, ge=1, le=3)
    retry_delay: float = Field(default=.2, ge=0, le=5)
    max_model_calls: int = Field(default=10, ge=3, le=20)
    max_output_tokens: int = Field(default=2400, ge=128, le=8192)
    max_concurrent_chats: int = Field(default=16, ge=1, le=256)
    db_concurrency: int = Field(default=12, ge=1, le=64)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)
    max_request_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    max_document_chars: int = Field(default=200_000, ge=100, le=1_000_000)
    max_document_chunks: int = Field(default=300, ge=1, le=1500)
    chunk_chars: int = Field(default=1200, ge=200, le=2400)
    chunk_overlap: int = Field(default=160, ge=0, le=400)
    retrieval_k: int = Field(default=6, ge=1, le=12)
    retrieval_candidates: int = Field(default=30, ge=6, le=100)
    min_vector_similarity: float = Field(default=.20, ge=0, le=1)
    retrieval_cache_seconds: int = Field(default=120, ge=1, le=3600)
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    job_lease_seconds: float = Field(default=90, ge=2, le=600)
    job_max_attempts: int = Field(default=4, ge=1, le=10)
    worker_poll_seconds: float = Field(default=1, ge=.01, le=30)

    @model_validator(mode="after")
    def deployment_checks(self):
        if self.chunk_overlap >= self.chunk_chars:
            raise ValueError("chunk_overlap must be smaller than chunk_chars")
        if self.jwt_algorithm == "HS256" and len(self.jwt_secret) < 32:
            raise ValueError("Set JWT_SECRET to at least 32 random characters")
        if self.jwt_algorithm == "RS256" and not self.jwt_public_key:
            raise ValueError("JWT_PUBLIC_KEY is required")
        if self.provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for live mode")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL with psycopg")
            if not self.redis_url or self.provider == "mock" or self.auto_migrate:
                raise ValueError("Production requires Redis, a real provider, and separate migrations")
            if self.jwt_algorithm != "RS256" or not self.openai_base_url.startswith("https://"):
                raise ValueError("Production requires RS256 authentication and an HTTPS model endpoint")
            if not self.redact_pii:
                raise ValueError("Production defaults require PII redaction; review policy before changing this guard")
        return self

    @property
    def embedding_space(self):
        return "mock:sha256-bow-v1:1536" if self.provider == "mock" else f"openai:{self.embedding_model}:1536"
