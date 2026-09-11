from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_null_bytes(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Null bytes are not allowed")
        return value


class Principal(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    roles: frozenset[str] = frozenset({"user"})

    def require(self, role):
        from app.errors import AppError
        if role not in self.roles and (role == "operator" or "admin" not in self.roles):
            raise AppError("forbidden", "Your account does not have permission for this action.", 403)


class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=6000)
    conversation_id: UUID | None = None


class DocumentInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=1_000_000)
    visibility: Literal["private", "tenant"] = "private"
    source_uri: str = Field(default="", max_length=1000)

    @field_validator("source_uri")
    @classmethod
    def safe_uri(cls, value):
        from urllib.parse import urlsplit
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Citation URLs must use HTTP(S), without embedded credentials")
        return value


class EvidenceRef(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=8, max_length=800)


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=800)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=4)


class Plan(StrictModel):
    standalone_question: str = Field(min_length=1, max_length=1600)
    queries: list[str] = Field(min_length=1, max_length=3)
    route: Literal["retrieve", "clarify", "block"]

    @field_validator("queries")
    @classmethod
    def bounded_queries(cls, values):
        if any(not q.strip() or len(q) > 1200 or "\x00" in q for q in values):
            raise ValueError("Invalid retrieval query")
        return values


class Draft(StrictModel):
    abstain: bool
    claims: list[Claim] = Field(max_length=6)


class Verdict(StrictModel):
    supported: bool
    answers_question: bool
    contradiction: bool
    issues: list[str] = Field(max_length=8)


class Source(StrictModel):
    number: int
    chunk_id: str
    document_id: str
    title: str
    source_uri: str
    quote: str


class ChatOutput(StrictModel):
    answer_id: str
    conversation_id: str
    status: Literal["answered", "abstained", "blocked", "clarification"]
    answer: str
    sources: list[Source]
    verified: bool
    demo_mode: bool
    knowledge_version: int
    agent_steps: list[str]
    usage: dict[str, int]


class FeedbackInput(StrictModel):
    answer_id: UUID
    rating: Literal[-1, 1]
    correction: str = Field(default="", max_length=3000)
    consent_to_learning: bool = False
    learning_scope: Literal["private", "tenant"] = "private"

    @model_validator(mode="after")
    def consent_needs_correction(self):
        if self.consent_to_learning and (not self.correction or self.rating != -1):
            raise ValueError("Learning requires a correction and a negative rating")
        return self


class ReviewInput(StrictModel):
    action: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=500)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def approval_needs_evidence(self):
        if self.action == "approve" and not self.evidence:
            raise ValueError("Approval requires supporting source quotes")
        return self
