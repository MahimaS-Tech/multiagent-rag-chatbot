import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from fastapi import FastAPI, Depends, Header, Request, UploadFile, File, Form, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from app.config import Settings
from app.database import Database
from app.repository import Repository
from app.controls import MemoryControls, RedisControls
from app.errors import AppError
from app.providers import build_provider
from app.schemas import Principal, ChatInput, ChatOutput, DocumentInput, FeedbackInput, ReviewInput
from app.security import decode_token, digest
from app.service import ChatService
from app.worker import IngestionWorker

log = logging.getLogger("rag.api")
WEB = Path(__file__).parent / "web"
BEARER = HTTPBearer(auto_error=False)


class BodyLimitMiddleware:
    """Bound normal AND chunked requests before JSON/multipart parsing."""
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        try:
            length = int(dict(scope.get("headers", [])).get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"error": {"code": "invalid_length", "message": "Invalid Content-Length."}}, 400)(scope, receive, send)
        async def too_big():
            await JSONResponse({"error": {"code": "request_too_large", "message": "Request body is too large."}}, 413)(scope, receive, send)
        if length < 0 or length > self.maximum:
            return await too_big()
        pieces, count = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            count += len(part)
            if count > self.maximum:
                return await too_big()
            pieces.append(part)
            if not message.get("more_body", False):
                break
        body, consumed = b"".join(pieces), False
        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


async def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)):
    if credentials is None or len(credentials.credentials) > 9000:
        raise AppError("unauthorized", "A valid bearer token is required.", 401)
    runtime = request.app.state.runtime
    principal = decode_token(credentials.credentials, runtime.settings)
    await runtime.controls.rate(digest(principal.tenant_id, principal.user_id))
    return principal


def create_app(settings=None, provider=None, database=None, controls=None):
    cfg = settings or Settings()
    registry = CollectorRegistry()
    requests = Counter("rag_http_requests_total", "HTTP requests", ["route", "status"], registry=registry)
    latency = Histogram("rag_http_duration_seconds", "HTTP duration", ["route"], registry=registry,
                        buckets=(.01, .05, .1, .5, 1, 2, 5, 10, 30, 60, 120))
    deliveries = Counter("rag_answer_deliveries_total", "Completed answer deliveries including idempotent replays",
                         ["status", "mode"], registry=registry)

    @asynccontextmanager
    async def lifespan(app):
        db = database or Database(cfg)
        llm, control = None, None
        try:
            if cfg.auto_migrate:
                await db.call(db.migrate)
            await db.call(db.check_schema)
            repo = Repository(db)
            llm = provider or build_provider(cfg)
            control = controls or (RedisControls(cfg.redis_url, cfg.rate_limit_per_minute) if cfg.redis_url
                                   else MemoryControls(cfg.rate_limit_per_minute))
            service = ChatService(db, repo, llm, control, cfg)
            app.state.runtime = SimpleNamespace(settings=cfg, db=db, repo=repo, provider=llm, controls=control,
                service=service, worker=IngestionWorker(db, repo, llm, cfg))
            yield
        finally:
            if llm is not None:
                await llm.close()
            if control is not None:
                await control.close()
            await db.call(db.close)

    app = FastAPI(title="Evidence-first Multiagent RAG Chatbot", version="1.0.0", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, maximum=cfg.max_request_bytes)
    if cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.middleware("http")
    async def observe(request, call_next):
        request.state.request_id = str(uuid4())
        start = time.monotonic()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.monotonic() - start
        requests.labels(route, str(response.status_code)).inc()
        latency.labels(route).observe(elapsed)
        response.headers.update({"X-Request-ID": request.state.request_id, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
        policy = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            # Only the built-in, static documentation UI needs its published CDN and inline bootstrap.
            policy = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'"
        response.headers["Content-Security-Policy"] = policy
        log.info(json.dumps({"request_id": request.state.request_id, "route": route,
                             "status": response.status_code, "duration_ms": round(elapsed * 1000)}))
        return response

    @app.exception_handler(AppError)
    async def application_error(request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
        if error.status in {429, 503}:
            headers["Retry-After"] = "5"
        return JSONResponse({"error": {"code": error.code, "message": error.message},
                             "request_id": getattr(request.state, "request_id", "")}, error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"error": {"code": "validation_error", "message": "One or more input fields are invalid.",
                             "fields": [list(item["loc"]) for item in error.errors()[:20]]}}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        log.error("request_failed request_id=%s error_type=%s", getattr(request.state, "request_id", ""), type(error).__name__)
        return JSONResponse({"error": {"code": "internal_error", "message": "The request could not be completed."},
                             "request_id": getattr(request.state, "request_id", "")}, 500)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        rt = request.app.state.runtime
        try:
            await rt.db.call(rt.db.ping)
            if not await rt.controls.ping():
                raise RuntimeError("Unavailable control backend")
        except Exception:
            return JSONResponse({"status": "not_ready"}, 503)
        return {"status": "ready", "demo_mode": cfg.provider == "mock"}

    @app.get("/metrics")
    async def metrics(principal: Principal = Depends(authenticate)):
        principal.require("operator")
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/chat", response_model=ChatOutput)
    async def chat(data: ChatInput, request: Request, principal: Principal = Depends(authenticate),
                   idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=100,
                                                pattern=r"^[A-Za-z0-9_.:-]+$")):
        result = await request.app.state.runtime.service.chat(principal, data, idempotency_key)
        deliveries.labels(result.status, "mock" if result.demo_mode else "live").inc()
        return result

    @app.get("/v1/conversations/{conversation_id}")
    async def history(conversation_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return {"turns": await rt.db.call(rt.repo.history, principal, str(conversation_id))}

    @app.post("/v1/documents", status_code=202)
    async def submit(data: DocumentInput, request: Request, principal: Principal = Depends(authenticate)):
        return await request.app.state.runtime.service.submit_document(principal, data)

    @app.post("/v1/documents/upload", status_code=202)
    async def upload(request: Request, file: UploadFile = File(...), visibility: str = Form("private"),
                     principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        principal.require("editor")
        filename = Path(file.filename or "document.txt").name
        if Path(filename).suffix.lower() not in {".txt", ".md"}:
            await file.close()
            raise AppError("unsupported_file", "Only UTF-8 .txt and .md files are supported.", 415)
        content = await file.read(cfg.max_request_bytes + 1)
        await file.close()
        if len(content) > cfg.max_request_bytes:
            raise AppError("document_too_large", "The uploaded file is too large.", 413)
        try:
            value = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AppError("invalid_encoding", "The file must contain UTF-8 text.", 422) from None
        if len(value) > cfg.max_document_chars:
            raise AppError("document_too_large", "This document exceeds the configured text limit.", 413)
        if visibility not in {"private", "tenant"} or not value.strip() or "\x00" in value:
            raise AppError("invalid_document", "Invalid visibility or empty/binary document.", 422)
        return await rt.service.submit_document(principal, DocumentInput(title=filename[:200], text=value, visibility=visibility))

    @app.get("/v1/documents")
    async def documents(request: Request, principal: Principal = Depends(authenticate),
                        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        rt = request.app.state.runtime
        return {"documents": await rt.db.call(rt.repo.documents, principal, limit, offset)}

    @app.get("/v1/documents/{document_id}")
    async def document(document_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.document, principal, str(document_id))

    @app.get("/v1/documents/{document_id}/chunks")
    async def document_chunks(document_id: UUID, request: Request, principal: Principal = Depends(authenticate),
                              limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        rt = request.app.state.runtime
        return {"chunks": await rt.db.call(rt.repo.document_chunks, principal, str(document_id), limit, offset)}

    @app.delete("/v1/documents/{document_id}")
    async def delete_document(document_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.delete_document, principal, str(document_id))

    @app.post("/v1/documents/{document_id}/retry")
    async def retry_document(document_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.retry_document, principal, str(document_id))

    @app.post("/v1/feedback", status_code=201)
    async def feedback(data: FeedbackInput, request: Request, principal: Principal = Depends(authenticate)):
        return await request.app.state.runtime.service.feedback(principal, data)

    @app.post("/v1/feedback/{feedback_id}/withdraw")
    async def withdraw(feedback_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.withdraw_feedback, principal, str(feedback_id))

    @app.get("/v1/learning/proposals")
    async def proposals(request: Request, principal: Principal = Depends(authenticate),
                        limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        rt = request.app.state.runtime
        return {"proposals": await rt.db.call(rt.repo.proposals, principal, limit, offset)}

    @app.post("/v1/learning/proposals/{proposal_id}/review")
    async def review(proposal_id: UUID, data: ReviewInput, request: Request, principal: Principal = Depends(authenticate)):
        return await request.app.state.runtime.service.review(principal, str(proposal_id), data)

    @app.post("/v1/learning/proposals/{proposal_id}/revoke")
    async def revoke(proposal_id: UUID, request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.revoke_learning, principal, str(proposal_id))

    @app.get("/v1/audit")
    async def audit(request: Request, principal: Principal = Depends(authenticate),
                    limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        rt = request.app.state.runtime
        return {"events": await rt.db.call(rt.repo.audit, principal, limit, offset)}

    @app.get("/v1/admin/queue")
    async def queue(request: Request, principal: Principal = Depends(authenticate)):
        rt = request.app.state.runtime
        return await rt.db.call(rt.repo.queue_status, principal)

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(WEB / "index.html")

    app.mount("/assets", StaticFiles(directory=WEB), name="assets")
    return app
