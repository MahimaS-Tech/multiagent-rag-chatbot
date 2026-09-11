import asyncio
from contextlib import asynccontextmanager
from app.agents import AgentWorkflow, VerificationAgent
from app.errors import AppError
from app.providers import Budget
from app.retrieval import RetrievalAgent
from app.security import digest, redact, suspicious


def unwrap_error(error):
    """TaskGroup can wrap a provider error; preserve its safe status instead of leaking a traceback."""
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            result = unwrap_error(child)
            if isinstance(result, (AppError, TimeoutError)):
                return result
    return error


class ChatService:
    def __init__(self, db, repo, provider, controls, settings):
        self.db, self.repo, self.provider, self.controls, self.settings = db, repo, provider, controls, settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_chats)
        self.retrieval = RetrievalAgent(db, repo, provider, controls, settings)
        self.workflow = AgentWorkflow(provider, self.retrieval, settings)
        self.verifier = VerificationAgent(provider)

    @asynccontextmanager
    async def admission(self):
        try:
            await asyncio.wait_for(self.slots.acquire(), .05)
        except TimeoutError:
            raise AppError("overloaded", "The service is busy. Retry shortly.", 503) from None
        try:
            yield
        finally:
            self.slots.release()

    async def chat(self, principal, data, key):
        fingerprint = digest(data.model_dump(mode="json"))
        question = redact(data.message) if self.settings.redact_pii else data.message
        async with self.admission():
            reservation = await self.db.call(self.repo.reserve_turn, principal, key, fingerprint, question,
                                             str(data.conversation_id) if data.conversation_id else None)
            if reservation["cached"]:
                from app.schemas import ChatOutput
                return ChatOutput.model_validate(reservation["cached"])
            try:
                async with asyncio.timeout(self.settings.chat_timeout_seconds):
                    version = await self.db.call(self.repo.version, principal.tenant_id)
                    output = await self.workflow.run({"principal": principal, "question": question,
                        "history": reservation["history"], "version": version, "reservation": reservation,
                        "budget": Budget(self.settings.max_model_calls)})
                    await self.db.call(self.repo.finish_turn, principal, reservation, output.model_dump(mode="json"), version)
                    return output
            except BaseException as error:
                try:
                    await asyncio.shield(self.db.call(self.repo.fail_turn, principal, reservation))
                except Exception:
                    pass  # Lease expiry still recovers requests when the database is unavailable.
                error = unwrap_error(error)
                if isinstance(error, TimeoutError):
                    raise AppError("chat_timeout", "The request timed out. Retry with the same idempotency key.", 504) from None
                raise error

    async def submit_document(self, principal, data):
        principal.require("editor")
        if len(data.text) > self.settings.max_document_chars:
            raise AppError("document_too_large", "This document exceeds the configured text limit.", 413)
        if suspicious(data.text) or suspicious(data.title):
            raise AppError("suspicious_document", "Review the possible instruction-override text before uploading this document.", 422)
        if self.settings.redact_pii:
            data = data.model_copy(update={"text": redact(data.text), "title": redact(data.title)})
        return await self.db.call(self.repo.submit_document, principal, data, self.settings.embedding_space)

    async def feedback(self, principal, data):
        if suspicious(data.correction):
            raise AppError("suspicious_correction", "The correction contains an instruction-override attempt.", 422)
        if self.settings.redact_pii:
            data = data.model_copy(update={"correction": redact(data.correction)})
        return await self.db.call(self.repo.feedback, principal, data)

    async def review(self, principal, proposal_id, review):
        principal.require("admin")
        proposal = await self.db.call(self.repo.proposal, principal, proposal_id)
        if proposal["user_id"] == principal.user_id:
            raise AppError("self_review", "A different administrator must review this correction.", 403)
        if self.settings.redact_pii:
            review = review.model_copy(update={"reason": redact(review.reason)})
        if review.action == "approve" and proposal["state"] == "pending":
            if suspicious(proposal["correction"]):
                raise AppError("suspicious_correction", "This correction cannot be promoted.", 422)
            try:
                async with self.admission(), asyncio.timeout(self.settings.chat_timeout_seconds):
                    sources = await self.db.call(self.repo.get_chunks, principal,
                        [ref.chunk_id for ref in review.evidence], self.settings.embedding_space, True)
                    claims = [{"text": proposal["correction"], "evidence": [ref.model_dump() for ref in review.evidence]}]
                    verdict = await self.verifier.run(proposal["question"], claims, sources, Budget(self.settings.max_model_calls))
                    if not verdict.supported or not verdict.answers_question or verdict.contradiction:
                        raise AppError("correction_not_supported", "The correction is not fully supported by the supplied original sources.", 422)
            except TimeoutError:
                raise AppError("review_timeout", "Verification timed out; the correction was not promoted.", 504) from None
        return await self.db.call(self.repo.finish_review, principal, proposal_id, review, self.settings.embedding_space)
