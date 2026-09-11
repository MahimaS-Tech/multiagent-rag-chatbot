import asyncio
import logging
import signal
import time
from pathlib import Path
from app.errors import AppError
from app.providers import validate_embeddings
from app.text import chunks

log = logging.getLogger("rag.worker")
HEARTBEAT_FILE = Path("/tmp/rag-worker-alive")


class IngestionWorker:
    def __init__(self, db, repo, provider, settings):
        self.db, self.repo, self.provider, self.settings = db, repo, provider, settings

    async def heartbeat(self, job, stop, lost):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), self.settings.job_lease_seconds / 3)
            except TimeoutError:
                try:
                    if not await self.db.call(self.repo.heartbeat, job):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

    async def run_one(self):
        job = await self.db.call(self.repo.claim_job)
        if not job:
            return False
        stop, lost = asyncio.Event(), asyncio.Event()
        heartbeat = asyncio.create_task(self.heartbeat(job, stop, lost))
        try:
            if job["embedding_space"] != self.settings.embedding_space:
                raise AppError("embedding_space_mismatch", "Re-ingest with the configured embedding model.")
            pieces = chunks(job["content"], self.settings.chunk_chars, self.settings.chunk_overlap)
            if not pieces or len(pieces) > self.settings.max_document_chunks:
                raise AppError("chunk_limit", "Invalid document chunk count.")
            embeddings = []
            for offset in range(0, len(pieces), 32):
                batch = pieces[offset:offset + 32]
                values = await self.provider.embed(batch)
                validate_embeddings([{"index": index, "embedding": vector} for index, vector in enumerate(values)], len(batch), 1536)
                embeddings.extend(values)
                if lost.is_set():
                    return True
            await self.db.call(self.repo.finish_job, job, pieces, embeddings)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, AppError) else "ingestion_error"
            await self.db.call(self.repo.fail_job, job, code)
            log.warning("ingestion_failed code=%s", code)
        finally:
            stop.set()
            await heartbeat
        return True

    async def serve(self, stop=None):
        stop = stop or asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        tasks = set()
        try:
            while not stop.is_set():
                HEARTBEAT_FILE.write_text(str(time.time()))
                completed = {task for task in tasks if task.done()}
                for task in completed:
                    try:
                        task.result()
                    except Exception:
                        log.warning("worker_iteration_failed")
                tasks -= completed
                while len(tasks) < self.settings.worker_concurrency:
                    tasks.add(asyncio.create_task(self.run_one()))
                try:
                    await asyncio.wait_for(stop.wait(), self.settings.worker_poll_seconds)
                except TimeoutError:
                    pass
        finally:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=45)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            HEARTBEAT_FILE.unlink(missing_ok=True)
