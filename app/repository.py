"""Tenant-scoped persistence. No transaction remains open during external model calls."""
import json
import time
from uuid import uuid4, uuid5, NAMESPACE_URL
from sqlalchemy import select, update, delete, or_, and_, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import Tenant, Document, Chunk, Job, Conversation, Turn, Feedback, Dependency, Audit
from app.errors import AppError
from app.security import digest
from app.text import cosine, lexical_score, normalized


def uid():
    return str(uuid4())


class Repository:
    def __init__(self, database):
        self.db = database
        self.settings = database.settings

    def _ensure_tenant(self, session, tenant_id):
        insert = pg_insert if self.db.engine.dialect.name == "postgresql" else sqlite_insert
        session.execute(insert(Tenant).values(id=tenant_id, knowledge_version=0)
                        .on_conflict_do_nothing(index_elements=["id"]))

    def _lock_tenant(self, session, tenant_id):
        self._ensure_tenant(session, tenant_id)
        return session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())

    @staticmethod
    def _acl(principal):
        return and_(Document.tenant_id == principal.tenant_id,
            or_(Document.visibility == "tenant", Document.owner_id == principal.user_id))

    @staticmethod
    def _doc_info(doc):
        return {"id": doc.id, "title": doc.title, "state": doc.state, "visibility": doc.visibility,
            "source_uri": doc.source_uri, "is_learning": doc.is_learning,
            "created_at": doc.created_at, "error_code": doc.error_code}

    @staticmethod
    def _hit(chunk, doc, **scores):
        return {"id": chunk.id, "document_id": doc.id, "title": doc.title, "source_uri": doc.source_uri,
            "content": chunk.content, "visibility": doc.visibility, "is_learning": doc.is_learning, **scores}

    def _audit(self, session, principal, action, target):
        session.add(Audit(id=uid(), tenant_id=principal.tenant_id, actor_id=principal.user_id,
                         action=action, target_id=target, created_at=time.time()))

    def version(self, tenant_id):
        with self.db.sessions() as session:
            return session.scalar(select(Tenant.knowledge_version).where(Tenant.id == tenant_id)) or 0

    def submit_document(self, principal, data, space):
        principal.require("editor")
        fingerprint = digest(data.title, data.text, data.visibility, data.source_uri, space)
        for attempt in range(2):
            try:
                with self.db.sessions.begin() as session:
                    self._lock_tenant(session, principal.tenant_id)
                    old = session.scalar(select(Document).where(Document.tenant_id == principal.tenant_id,
                        Document.owner_id == principal.user_id, Document.fingerprint == fingerprint))
                    if old:
                        if old.state == "deleted":
                            session.execute(update(Job).where(Job.document_id == old.id).values(state="queued",
                                attempts=0, available_at=time.time(), lease_until=0, lease_token=uid(), last_error=""))
                            old.state, old.error_code, old.content = "queued", "", data.text
                            self._audit(session, principal, "document.restored", old.id)
                        return self._doc_info(old)
                    doc = Document(id=uid(), tenant_id=principal.tenant_id, owner_id=principal.user_id,
                        title=data.title, source_uri=data.source_uri, content=data.text, visibility=data.visibility,
                        fingerprint=fingerprint, embedding_space=space, state="queued", is_learning=False,
                        created_at=time.time(), error_code="")
                    session.add(doc)
                    session.flush()
                    session.add(Job(id=uid(), tenant_id=principal.tenant_id, document_id=doc.id, state="queued",
                        attempts=0, available_at=time.time(), lease_until=0, lease_token="", last_error=""))
                    self._audit(session, principal, "document.submitted", doc.id)
                    return self._doc_info(doc)
            except IntegrityError:
                if attempt:
                    raise AppError("document_conflict", "Please retry this upload.", 409) from None
        raise AssertionError("unreachable")

    def documents(self, principal, limit=50, offset=0):
        with self.db.sessions() as session:
            rows = session.scalars(select(Document).where(self._acl(principal), Document.state != "deleted")
                .order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset)).all()
            return [self._doc_info(doc) for doc in rows]

    def document(self, principal, document_id):
        with self.db.sessions() as session:
            doc = session.scalar(select(Document).where(self._acl(principal), Document.id == document_id,
                                                        Document.state != "deleted"))
            if not doc:
                raise AppError("not_found", "Document not found.", 404)
            result = self._doc_info(doc)
            job = session.scalar(select(Job).where(Job.document_id == doc.id, Job.tenant_id == principal.tenant_id))
            result["job"] = {"id": job.id, "state": job.state, "attempts": job.attempts, "last_error": job.last_error} if job else None
            return result

    def document_chunks(self, principal, document_id, limit=50, offset=0):
        self.document(principal, document_id)
        with self.db.sessions() as session:
            rows = session.execute(select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
                self._acl(principal), Chunk.tenant_id == principal.tenant_id, Document.id == document_id,
                Document.state == "ready").order_by(Chunk.ordinal).limit(limit).offset(offset)).all()
            return [self._hit(chunk, doc) for chunk, doc in rows]

    def _revoke(self, session, principal, ids):
        # Learned material cannot be used as approval evidence: dependency depth is one.
        dependent = session.scalars(select(Dependency.learned_id).where(Dependency.tenant_id == principal.tenant_id,
                                                                       Dependency.source_id.in_(ids))).all()
        all_ids = list(set(ids) | set(dependent))
        session.execute(update(Job).where(Job.tenant_id == principal.tenant_id, Job.document_id.in_(all_ids))
                        .values(state="cancelled", lease_until=0, lease_token=uid()))
        session.execute(update(Document).where(Document.tenant_id == principal.tenant_id, Document.id.in_(all_ids))
                        .values(state="deleted", content=""))
        session.execute(delete(Chunk).where(Chunk.tenant_id == principal.tenant_id, Chunk.document_id.in_(all_ids)))
        session.execute(update(Feedback).where(Feedback.tenant_id == principal.tenant_id,
            Feedback.learned_document_id.in_(all_ids)).values(state="revoked"))
        session.execute(update(Tenant).where(Tenant.id == principal.tenant_id)
                        .values(knowledge_version=Tenant.knowledge_version + 1))
        return all_ids

    def delete_document(self, principal, document_id):
        principal.require("editor")
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, principal.tenant_id)
            doc = session.scalar(select(Document).where(self._acl(principal), Document.id == document_id))
            if not doc:
                raise AppError("not_found", "Document not found.", 404)
            if doc.owner_id != principal.user_id and "admin" not in principal.roles:
                raise AppError("forbidden", "Only the owner or an administrator can delete this source.", 403)
            affected = self._revoke(session, principal, [document_id])
            self._audit(session, principal, "document.deleted", document_id)
            return {"deleted": affected}

    def retry_document(self, principal, document_id):
        principal.require("editor")
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, principal.tenant_id)
            doc = session.scalar(select(Document).where(self._acl(principal), Document.id == document_id))
            if not doc:
                raise AppError("not_found", "Document not found.", 404)
            if doc.owner_id != principal.user_id and "admin" not in principal.roles:
                raise AppError("forbidden", "Only the owner or an administrator can retry this source.", 403)
            if doc.state != "failed":
                raise AppError("invalid_state", "Only failed documents can be retried.", 409)
            session.execute(update(Job).where(Job.document_id == doc.id, Job.tenant_id == principal.tenant_id)
                .values(state="queued", attempts=0, available_at=time.time(), lease_until=0, lease_token=uid(), last_error=""))
            doc.state, doc.error_code = "queued", ""
            self._audit(session, principal, "document.retried", doc.id)
            return self._doc_info(doc)

    def claim_job(self, now=None):
        now = time.time() if now is None else now
        eligible = or_(and_(Job.state == "queued", Job.available_at <= now),
                       and_(Job.state == "running", Job.lease_until <= now))
        with self.db.sessions.begin() as session:
            job = session.scalar(select(Job).where(eligible).order_by(Job.available_at, Job.id)
                                 .limit(1).with_for_update(skip_locked=True))
            if not job:
                return None
            attempts, old_token, token = job.attempts, job.lease_token, uid()
            terminal = attempts >= self.settings.job_max_attempts
            changed = session.execute(update(Job).where(Job.id == job.id, eligible, Job.attempts == attempts,
                Job.lease_token == old_token).values(state="dead" if terminal else "running",
                attempts=attempts + (0 if terminal else 1), lease_token=token,
                lease_until=now + self.settings.job_lease_seconds).execution_options(synchronize_session=False)).rowcount
            if not changed:
                return None
            doc = session.get(Document, job.document_id)
            if not doc or doc.state == "deleted":
                session.execute(update(Job).where(Job.id == job.id).values(state="cancelled", lease_until=0))
                return None
            if terminal:
                doc.state, doc.error_code = "failed", "attempts_exhausted"
                session.execute(update(Job).where(Job.id == job.id).values(last_error="attempts_exhausted", lease_until=0))
                return None
            doc.state = "indexing"
            return {"id": job.id, "document_id": doc.id, "tenant_id": job.tenant_id, "lease_token": token,
                    "attempts": attempts + 1, "content": doc.content, "embedding_space": doc.embedding_space}

    def heartbeat(self, job, now=None):
        now = time.time() if now is None else now
        with self.db.sessions.begin() as session:
            return session.execute(update(Job).where(Job.id == job["id"], Job.tenant_id == job["tenant_id"],
                Job.state == "running", Job.lease_token == job["lease_token"], Job.lease_until > now)
                .values(lease_until=now + self.settings.job_lease_seconds)).rowcount == 1

    def finish_job(self, job, pieces, embeddings, now=None):
        from app.providers import validate_embeddings
        if not pieces or len(pieces) != len(embeddings):
            raise ValueError("One embedding is required per nonempty chunk")
        validate_embeddings([{"index": i, "embedding": vector} for i, vector in enumerate(embeddings)], len(pieces), 1536)
        now = time.time() if now is None else now
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, job["tenant_id"])
            changed = session.execute(update(Job).where(Job.id == job["id"], Job.tenant_id == job["tenant_id"],
                Job.state == "running", Job.lease_token == job["lease_token"], Job.lease_until > now)
                .values(state="done", lease_until=0)).rowcount
            if not changed:
                return False
            doc = session.get(Document, job["document_id"])
            if not doc or doc.state == "deleted" or doc.embedding_space != job["embedding_space"]:
                return False
            session.execute(delete(Chunk).where(Chunk.document_id == doc.id, Chunk.tenant_id == doc.tenant_id))
            for ordinal, (piece, embedding) in enumerate(zip(pieces, embeddings)):
                session.add(Chunk(id=str(uuid5(NAMESPACE_URL, f"{doc.id}:{ordinal}")), tenant_id=doc.tenant_id,
                    document_id=doc.id, ordinal=ordinal, content=piece, content_hash=digest(piece), embedding=embedding))
            doc.state, doc.error_code = "ready", ""
            session.execute(update(Tenant).where(Tenant.id == doc.tenant_id).values(knowledge_version=Tenant.knowledge_version + 1))
            session.execute(update(Feedback).where(Feedback.tenant_id == doc.tenant_id, Feedback.learned_document_id == doc.id,
                                                   Feedback.state == "approved").values(state="published"))
            return True

    def fail_job(self, job, code, now=None):
        now = time.time() if now is None else now
        terminal = job["attempts"] >= self.settings.job_max_attempts
        with self.db.sessions.begin() as session:
            changed = session.execute(update(Job).where(Job.id == job["id"], Job.tenant_id == job["tenant_id"],
                Job.state == "running", Job.lease_token == job["lease_token"], Job.lease_until > now).values(
                state="dead" if terminal else "queued", lease_until=0,
                available_at=now + min(60, 2 ** job["attempts"]), last_error=code[:80])).rowcount
            if changed:
                session.execute(update(Document).where(Document.id == job["document_id"], Document.tenant_id == job["tenant_id"],
                    Document.state != "deleted").values(state="failed" if terminal else "queued", error_code=code[:80]))
            return bool(changed)

    def get_chunks(self, principal, ids, space=None, trusted_only=False):
        if not ids:
            return []
        with self.db.sessions() as session:
            stmt = select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
                Chunk.id.in_(ids), Chunk.tenant_id == principal.tenant_id, self._acl(principal), Document.state == "ready")
            if space:
                stmt = stmt.where(Document.embedding_space == space)
            if trusted_only:
                stmt = stmt.where(Document.visibility == "tenant", Document.is_learning.is_(False))
            return [self._hit(c, d) for c, d in session.execute(stmt).all()]

    def search(self, principal, query, vector, space, limit):
        if self.db.engine.dialect.name == "postgresql":
            return self._postgres_search(principal, query, vector, space, limit)
        with self.db.sessions() as session:
            rows = session.execute(select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
                Chunk.tenant_id == principal.tenant_id, self._acl(principal), Document.state == "ready",
                Document.embedding_space == space).limit(10000)).all()
            hits = [self._hit(c, d, similarity=cosine(c.embedding, vector), lexical=lexical_score(query, c.content)) for c, d in rows]
            return {"vector": sorted(hits, key=lambda h: (-h["similarity"], h["id"]))[:limit],
                    "lexical": sorted([h for h in hits if h["lexical"] > 0], key=lambda h: (-h["lexical"], h["id"]))[:limit]}

    def _postgres_search(self, principal, query, vector, space, limit):
        params = {"tenant": principal.tenant_id, "user": principal.user_id, "space": space,
                  "q": query, "vector": json.dumps(vector, allow_nan=False), "limit": int(limit)}
        columns = "c.id, c.content, d.id AS document_id, d.title, d.source_uri, d.visibility, d.is_learning"
        base = " FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.tenant_id=:tenant " \
               "AND d.tenant_id=:tenant AND d.state='ready' AND d.embedding_space=:space " \
               "AND (d.visibility='tenant' OR d.owner_id=:user) "
        with self.db.sessions.begin() as session:
            session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
            session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            vectors = session.execute(text("SELECT " + columns +
                ", 1-(c.embedding <=> CAST(:vector AS vector)) AS similarity" + base +
                "ORDER BY c.embedding <=> CAST(:vector AS vector) LIMIT :limit"), params).mappings().all()
            lexical = session.execute(text("SELECT " + columns +
                ", ts_rank_cd(c.search_vector, websearch_to_tsquery('english', :q)) AS lexical" + base +
                "AND c.search_vector @@ websearch_to_tsquery('english', :q) ORDER BY lexical DESC, c.id LIMIT :limit"), params).mappings().all()
            return {"vector": [dict(row) for row in vectors], "lexical": [dict(row) for row in lexical]}

    def _answer_active(self, session, principal, answer):
        sources = answer.get("sources", [])
        if not sources:
            return True
        rows = session.execute(select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
            Chunk.id.in_([s["chunk_id"] for s in sources]), Chunk.tenant_id == principal.tenant_id,
            self._acl(principal), Document.state == "ready")).all()
        available = {c.id: (c, d) for c, d in rows}
        return all(s["chunk_id"] in available and available[s["chunk_id"]][1].id == s["document_id"] and
            normalized(s["quote"]) in normalized(available[s["chunk_id"]][0].content) for s in sources)

    def reserve_turn(self, principal, key, fingerprint, question, conversation_id, now=None):
        now = time.time() if now is None else now
        for attempt in range(2):
            try:
                return self._reserve_turn(principal, key, fingerprint, question, conversation_id, now)
            except IntegrityError:
                if attempt:
                    raise AppError("request_conflict", "A competing request is being recorded; retry with the same key.", 409) from None
        raise AssertionError("unreachable")

    def _reserve_turn(self, principal, key, fingerprint, question, conversation_id, now):
        with self.db.sessions.begin() as session:
            self._ensure_tenant(session, principal.tenant_id)
            turn = session.scalar(select(Turn).where(Turn.tenant_id == principal.tenant_id,
                Turn.user_id == principal.user_id, Turn.request_key == key))
            if turn and turn.request_hash != fingerprint:
                raise AppError("idempotency_conflict", "This request key was already used for different input.", 409)
            if turn and turn.state == "completed":
                if not self._answer_active(session, principal, turn.answer):
                    raise AppError("stale_answer", "A cited source was revoked. Ask again with a new request key.", 409)
                return {"cached": turn.answer}
            if turn and turn.state == "processing" and turn.lease_until > now:
                raise AppError("request_in_progress", "This request is already running. Retry with the same key.", 409)
            cid = turn.conversation_id if turn else conversation_id
            if not cid:
                cid = str(uuid5(NAMESPACE_URL, digest(principal.tenant_id, principal.user_id, key)))
                conv = session.get(Conversation, cid)
                if not conv:
                    session.add(Conversation(id=cid, tenant_id=principal.tenant_id, user_id=principal.user_id,
                        busy_until=0, busy_token="", created_at=now))
                    session.flush()
            else:
                conv = session.scalar(select(Conversation).where(Conversation.id == cid,
                    Conversation.tenant_id == principal.tenant_id, Conversation.user_id == principal.user_id))
                if not conv:
                    raise AppError("not_found", "Conversation not found.", 404)
            token, until = uid(), now + self.settings.chat_timeout_seconds + 15
            changed = session.execute(update(Conversation).where(Conversation.id == cid,
                Conversation.tenant_id == principal.tenant_id, Conversation.user_id == principal.user_id,
                Conversation.busy_until <= now).values(busy_until=until, busy_token=token)).rowcount
            if not changed:
                raise AppError("conversation_busy", "Another turn in this conversation is running.", 409)
            if not turn:
                turn = Turn(id=uid(), tenant_id=principal.tenant_id, user_id=principal.user_id, conversation_id=cid,
                    request_key=key, request_hash=fingerprint, question=question, state="processing", lease_token=token,
                    lease_until=until, answer=None, created_at=now)
                session.add(turn)
            else:
                turn.state, turn.lease_token, turn.lease_until = "processing", token, until
            previous = session.scalars(select(Turn.question).where(Turn.conversation_id == cid,
                Turn.tenant_id == principal.tenant_id, Turn.user_id == principal.user_id, Turn.state == "completed")
                .order_by(Turn.created_at.desc(), Turn.id).limit(6)).all()
            return {"id": turn.id, "conversation_id": cid, "lease_token": token,
                    "history": list(reversed(previous)), "cached": None}

    def finish_turn(self, principal, reservation, answer, expected_version, now=None):
        now = time.time() if now is None else now
        with self.db.sessions.begin() as session:
            tenant = self._lock_tenant(session, principal.tenant_id)
            if tenant.knowledge_version != expected_version or not self._answer_active(session, principal, answer):
                raise AppError("knowledge_changed", "Sources changed during this answer. Retry with the same key.", 503)
            conversation_changed = session.execute(update(Conversation).where(
                Conversation.id == reservation["conversation_id"], Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id, Conversation.busy_token == reservation["lease_token"],
                Conversation.busy_until > now).values(busy_until=0, busy_token="")).rowcount
            turn_changed = session.execute(update(Turn).where(Turn.id == reservation["id"], Turn.tenant_id == principal.tenant_id,
                Turn.user_id == principal.user_id, Turn.lease_token == reservation["lease_token"], Turn.lease_until > now,
                Turn.state == "processing").values(state="completed", answer=answer, lease_until=0)).rowcount
            if not conversation_changed or not turn_changed:
                raise AppError("lease_lost", "The request lease expired. Retry with the same key.", 409)

    def fail_turn(self, principal, reservation):
        with self.db.sessions.begin() as session:
            session.execute(update(Turn).where(Turn.id == reservation["id"], Turn.tenant_id == principal.tenant_id,
                Turn.user_id == principal.user_id, Turn.lease_token == reservation["lease_token"], Turn.state == "processing")
                .values(state="failed", lease_until=0))
            session.execute(update(Conversation).where(Conversation.id == reservation["conversation_id"],
                Conversation.tenant_id == principal.tenant_id, Conversation.user_id == principal.user_id,
                Conversation.busy_token == reservation["lease_token"]).values(busy_until=0, busy_token=""))

    def history(self, principal, conversation_id):
        with self.db.sessions() as session:
            conv = session.scalar(select(Conversation).where(Conversation.id == conversation_id,
                Conversation.tenant_id == principal.tenant_id, Conversation.user_id == principal.user_id))
            if not conv:
                raise AppError("not_found", "Conversation not found.", 404)
            turns = session.scalars(select(Turn).where(Turn.conversation_id == conv.id,
                Turn.tenant_id == principal.tenant_id, Turn.user_id == principal.user_id, Turn.state == "completed")
                .order_by(Turn.created_at.desc(), Turn.id).limit(50)).all()
            result = []
            for turn in reversed(turns):
                active = self._answer_active(session, principal, turn.answer)
                result.append({"answer_id": turn.id, "question": turn.question,
                               "response": turn.answer if active else None, "withheld": not active})
            return result

    @staticmethod
    def _feedback_info(feedback, question=""):
        return {"id": feedback.id, "answer_id": feedback.turn_id, "user_id": feedback.user_id,
            "rating": feedback.rating, "correction": feedback.correction, "question": question,
            "scope": feedback.scope, "state": feedback.state, "learned_document_id": feedback.learned_document_id,
            "reviewer_id": feedback.reviewer_id, "review_reason": feedback.review_reason, "created_at": feedback.created_at}

    def feedback(self, principal, data):
        for attempt in range(2):
            try:
                with self.db.sessions.begin() as session:
                    turn = session.scalar(select(Turn).where(Turn.id == str(data.answer_id), Turn.tenant_id == principal.tenant_id,
                        Turn.user_id == principal.user_id, Turn.state == "completed"))
                    if not turn:
                        raise AppError("not_found", "Answer not found.", 404)
                    old = session.scalar(select(Feedback).where(Feedback.turn_id == turn.id, Feedback.tenant_id == principal.tenant_id))
                    if old:
                        if (old.rating, old.correction, old.consent, old.scope) != (data.rating, data.correction,
                                                                                 data.consent_to_learning, data.learning_scope):
                            raise AppError("feedback_conflict", "Feedback already exists for this answer.", 409)
                        return self._feedback_info(old)
                    feedback = Feedback(id=uid(), tenant_id=principal.tenant_id, user_id=principal.user_id, turn_id=turn.id,
                        rating=data.rating, correction=data.correction, consent=data.consent_to_learning,
                        scope=data.learning_scope, state="pending" if data.consent_to_learning else "recorded",
                        reviewer_id="", review_reason="", review_hash="", review_evidence=[], learned_document_id="",
                        created_at=time.time())
                    session.add(feedback)
                    self._audit(session, principal, "feedback.submitted", feedback.id)
                    return self._feedback_info(feedback)
            except IntegrityError:
                if attempt:
                    raise AppError("feedback_conflict", "Feedback is already being recorded.", 409) from None
        raise AssertionError("unreachable")

    def proposals(self, principal, limit=50, offset=0):
        principal.require("admin")
        with self.db.sessions() as session:
            rows = session.execute(select(Feedback, Turn.question).join(Turn, Feedback.turn_id == Turn.id).where(
                Feedback.tenant_id == principal.tenant_id, Turn.tenant_id == principal.tenant_id, Feedback.consent.is_(True))
                .order_by(Feedback.created_at.desc(), Feedback.id).limit(limit).offset(offset)).all()
            return [self._feedback_info(feedback, question) for feedback, question in rows]

    def proposal(self, principal, proposal_id):
        principal.require("admin")
        with self.db.sessions() as session:
            row = session.execute(select(Feedback, Turn.question).join(Turn, Feedback.turn_id == Turn.id).where(
                Feedback.id == proposal_id, Feedback.tenant_id == principal.tenant_id,
                Turn.tenant_id == principal.tenant_id, Feedback.consent.is_(True))).first()
            if not row:
                raise AppError("not_found", "Learning proposal not found.", 404)
            return self._feedback_info(*row)

    def finish_review(self, principal, proposal_id, review, space):
        principal.require("admin")
        fingerprint = digest(review.model_dump())
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, principal.tenant_id)
            feedback = session.scalar(select(Feedback).where(Feedback.id == proposal_id,
                Feedback.tenant_id == principal.tenant_id, Feedback.consent.is_(True)).with_for_update())
            if not feedback:
                raise AppError("not_found", "Learning proposal not found.", 404)
            if feedback.user_id == principal.user_id:
                raise AppError("self_review", "A different administrator must review this correction.", 403)
            if feedback.state != "pending":
                if feedback.state in {"approved", "published", "rejected"} and feedback.review_hash == fingerprint and feedback.reviewer_id == principal.user_id:
                    return self._feedback_info(feedback)
                raise AppError("review_conflict", "This proposal is no longer pending.", 409)
            if review.action == "approve":
                rows = session.execute(select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
                    Chunk.id.in_([ref.chunk_id for ref in review.evidence]), Chunk.tenant_id == principal.tenant_id,
                    Document.tenant_id == principal.tenant_id, Document.visibility == "tenant", Document.state == "ready",
                    Document.is_learning.is_(False), Document.embedding_space == space)).all()
                by_id = {chunk.id: (chunk, doc) for chunk, doc in rows}
                if any(ref.chunk_id not in by_id or normalized(ref.quote) not in normalized(by_id[ref.chunk_id][0].content)
                       for ref in review.evidence):
                    raise AppError("invalid_evidence", "Approval evidence is missing, changed, or not a shared original source.", 409)
                doc_id = str(uuid5(NAMESPACE_URL, f"learned:{principal.tenant_id}:{feedback.id}"))
                session.add(Document(id=doc_id, tenant_id=principal.tenant_id, owner_id=feedback.user_id,
                    title=f"Reviewed correction {feedback.id[:8]}", source_uri="", content=feedback.correction,
                    visibility=feedback.scope, fingerprint=digest("reviewed", feedback.id, feedback.correction),
                    embedding_space=space, state="queued", is_learning=True, created_at=time.time(), error_code=""))
                session.flush()
                session.add(Job(id=uid(), tenant_id=principal.tenant_id, document_id=doc_id, state="queued", attempts=0,
                    available_at=time.time(), lease_until=0, lease_token="", last_error=""))
                for source_id in sorted({doc.id for _, doc in by_id.values()}):
                    session.add(Dependency(learned_id=doc_id, source_id=source_id, tenant_id=principal.tenant_id))
                feedback.learned_document_id, feedback.state = doc_id, "approved"
            else:
                feedback.state = "rejected"
            feedback.reviewer_id, feedback.review_reason, feedback.review_hash = principal.user_id, review.reason, fingerprint
            feedback.review_evidence = [ref.model_dump() for ref in review.evidence]
            self._audit(session, principal, "learning." + review.action, feedback.id)
            return self._feedback_info(feedback)

    def withdraw_feedback(self, principal, feedback_id):
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, principal.tenant_id)
            feedback = session.scalar(select(Feedback).where(Feedback.id == feedback_id,
                Feedback.tenant_id == principal.tenant_id, Feedback.user_id == principal.user_id).with_for_update())
            if not feedback:
                raise AppError("not_found", "Feedback not found.", 404)
            if feedback.learned_document_id:
                self._revoke(session, principal, [feedback.learned_document_id])
            feedback.consent, feedback.correction, feedback.state = False, "", "withdrawn"
            self._audit(session, principal, "learning.consent_withdrawn", feedback.id)
            return self._feedback_info(feedback)

    def revoke_learning(self, principal, feedback_id):
        principal.require("admin")
        with self.db.sessions.begin() as session:
            self._lock_tenant(session, principal.tenant_id)
            feedback = session.scalar(select(Feedback).where(Feedback.id == feedback_id,
                Feedback.tenant_id == principal.tenant_id).with_for_update())
            if not feedback or not feedback.learned_document_id:
                raise AppError("not_found", "Approved correction not found.", 404)
            self._revoke(session, principal, [feedback.learned_document_id])
            feedback.state = "revoked"
            self._audit(session, principal, "learning.revoked", feedback.id)
            return self._feedback_info(feedback)

    def audit(self, principal, limit=100, offset=0):
        principal.require("admin")
        with self.db.sessions() as session:
            rows = session.scalars(select(Audit).where(Audit.tenant_id == principal.tenant_id)
                .order_by(Audit.created_at.desc(), Audit.id).limit(limit).offset(offset)).all()
            return [{"id": row.id, "actor_id": row.actor_id, "action": row.action, "target_id": row.target_id,
                     "created_at": row.created_at} for row in rows]

    def queue_status(self, principal):
        principal.require("admin")
        with self.db.sessions() as session:
            rows = session.execute(select(Job.state, func.count(Job.id)).where(Job.tenant_id == principal.tenant_id)
                                   .group_by(Job.state)).all()
            oldest = session.scalar(select(func.min(Job.available_at)).where(Job.tenant_id == principal.tenant_id, Job.state == "queued"))
            return {"counts": dict(rows), "oldest_queued_seconds": max(0, time.time() - oldest) if oldest else 0}
