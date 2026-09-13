# Architecture and invariants

## Components

The API is horizontally replicable: it keeps no authoritative conversation or job state in process memory. PostgreSQL holds documents, chunks, conversations, request reservations, feedback, dependencies and audit events. Redis provides shared request admission and a disposable retrieval cache. Workers claim jobs from PostgreSQL independently of API replicas. Local semaphores and circuit breakers bound each process; their state is deliberately not authoritative.

A synchronous SQLAlchemy connection is used only inside a bounded thread offload. Each operation creates its own transaction/session; sessions are never shared across async tasks. No model or embedding HTTP request runs inside an open database transaction. PostgreSQL statement and lock timeouts bound database work. Cancellation waits for an already-running database operation rather than prematurely releasing its connection slot.

## Document write path

An upload is validated, checked for a narrow set of instruction-override patterns, and heuristically redacted. The tenant and owner come from the signed principal, never from request JSON. A content/metadata/vector-space fingerprint deduplicates the upload. Document and ingestion job are committed in one transaction; there is no database-plus-external-queue dual write.

A worker claims one due job using `FOR UPDATE SKIP LOCKED` in PostgreSQL. It receives a unique lease token and increments the attempt count. Heartbeats extend only a still-live, owned lease. Chunking and batched embedding happen outside transactions. Final publication checks the token, deadline, document state and vector space, then writes chunks, changes the document to ready, completes the job, and increments the tenant's knowledge version atomically.

Processing is **at least once**, not exactly once. A crash can repeat an embedding request and therefore its cost. Publication is fenced and transactional: an expired or replaced worker cannot publish over a replacement. Failed jobs use bounded exponential delays and eventually move to a dead-letter state. The document retry endpoint explicitly requeues failed work.

Deletion cancels in-flight jobs, changes their lease token, scrubs active source content/chunks, increments the knowledge version and revokes dependent learned documents. Restoring an identical original upload creates a fresh job lease; old workers remain fenced out.

## Retrieval

Production retrieval uses two independent parameterized queries: cosine-nearest pgvector candidates and English PostgreSQL full-text candidates. Both join only ready documents in the correct embedding space and apply tenant and owner/shared visibility conditions. HNSW iterative scans reduce the under-filling problem caused by filters. Approximate search and fixed candidate budgets can still miss relevant evidence.

Ranks are combined with reciprocal-rank fusion, followed by inexpensive lexical reranking and content deduplication. This is not a neural cross-encoder. Up to three query variants run concurrently under the request's shared model-call budget. The task group cancels sibling searches on failure.

Redis cache entries hold candidate IDs and scores, not authorization decisions. Keys include tenant, user, roles, query, vector-space identity, retrieval parameters and knowledge version. Every hit re-fetches currently authorized, ready chunks. A corrupted or stale cache cannot grant source access. Redis cache errors fall back to search; Redis admission errors fail closed with 503.

SQLite retrieval is an exact in-process cosine/lexical path capped at 10,000 permitted chunks. It exists for unit tests and small local demonstrations only. It is not the scalable production search backend.

## Chat lifecycle

`Idempotency-Key` is required. It is unique within a tenant and user, and bound to the original request body. Changing the body with the same key is a conflict. Repeating a completed request returns its stored response only after verifying that its cited sources remain authorized and active. Model calls are not repeated for a valid completed replay.

A conversation lease serializes turns. A processing request that is still live returns a conflict rather than starting duplicate model work. Once its lease expires, a retry can reclaim it. The old request cannot complete or clear the replacement's lease. Failed attempts retain a safe retry path using the same key.

The planner resolves references using previous user questions, not previous assistant statements. Retrieval supplies bounded source excerpts. The answerer must return structured claims with chunk IDs and quotes. Deterministic checks reject unavailable IDs, invented quotes and suspicious instruction-like output. A separate model call evaluates entailment, relevance and contradictions. At most one repair is attempted. Final answers consist only of accepted claims and source metadata read from the repository.

Before committing, the service rechecks source access and the tenant knowledge version. If the knowledge base changed during generation, the response is not published; the caller receives a retryable conflict in the form of a safe 503 `knowledge_changed` error. This conservative tenant-wide invalidation can cause retries even when an unrelated source changed.

## Consistency limits

No finite pre-return check can revoke bytes already sent to a client. Authorization is checked at retrieval, cached-ID resolution, final commit and replay/history read boundaries. Old transcripts remain stored for conversation continuity, but source-revoked answers are withheld from normal API history reads. Physical erasure and backup retention require a separate lifecycle policy.

The provider fallback is another configured model at the same endpoint. It does not survive every provider-wide outage. The model-verifier pass is a separate role/call, not a mathematically independent proof. Source correctness and domain evaluation remain necessary.
