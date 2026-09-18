# Evidence-first Multiagent RAG Chatbot

A runnable Python application with a browser UI, multiagent question answering, document ingestion, tenant isolation, durable jobs, source verification, and human-reviewed learning. Includes application code, tests, deployment examples, and operating notes—not just an architecture skeleton.

**Read `reports/VERIFICATION.md` for what was actually executed.** The included Docker, Kubernetes, real PostgreSQL/Redis, optional LangGraph, and live-model paths must be verified in their target environment before production use. This project does not promise a particular uptime, throughput, or accuracy percentage.

## Start with Docker

Prerequisites: Python 3.12+ for the helper scripts, Docker, and Docker Compose v2. Initial image builds need internet access. No model API key is needed for the default extractive demo.

```bash
unzip multiagent-rag-chatbot.zip
cd multiagent-rag-chatbot
python3 scripts/configure.py
docker compose up --build -d
```

Seed a few original documents and generate a development token:

```bash
docker compose exec api python -m app.cli seed --tenant acme
docker compose exec api python -m app.cli token --tenant acme --user demo
```

Open **http://localhost:8000**, paste the printed token, and click **Connect**. Refresh the document list until the documents are **ready**, then ask **“What is the refund policy?”**. The response should include a source quote and numbered citation.

The API specification is at `/openapi.json`; interactive documentation is at `/docs`. The main UI is self-hosted and does not require a CDN. The built-in interactive API documentation uses its normal CDN assets.

The configuration helper generates fresh secrets and refuses to overwrite an existing `.env`. Do not share or commit `.env`. Demo tokens expire after one hour by default. There is no unauthenticated token-minting HTTP endpoint.

## Demo versus real AI

| Mode | Behavior |
|---|---|
| `PROVIDER=mock` | Deterministic bag-of-words embeddings and extractive sentence selection. Useful for wiring, tests and demos. **Not a language model or a real accuracy evaluation.** |
| `PROVIDER=openai` | Real embeddings and Responses API calls for planning, answer generation and a separate verification pass. Requires credentials and incurs provider usage charges. |

For a fresh configuration with a real API key:

```bash
python3 scripts/configure.py --live
```

To switch an existing demo configuration without replacing database passwords:

```bash
python3 scripts/configure.py --enable-live
docker compose up -d --force-recreate api worker
```

**Re-ingest documents after changing provider or embedding model.** Vector spaces are explicitly separated, so mock vectors are never compared to real embeddings. Delete obsolete demo documents, then upload them again or run `seed` again. The embedding dimension is fixed at 1536; changing the dimension requires a schema/index migration and reindexing, not only an environment-variable change.

The default model snapshots are configurable through `PLANNER_MODEL`, `ANSWER_MODEL`, `VERIFIER_MODEL`, `FALLBACK_MODEL`, and `EMBEDDING_MODEL`. These are explicit model choices, not a claim that they are the latest available models. Fallback changes the model on the configured provider endpoint; this is **not** cross-provider disaster recovery.

## Main workflow

```text
Browser / API client
       |
 JWT validation + tenant/user identity + rate limit
       |
 Durable request reservation + conversation lease
       |
 Planner agent: resolve references and build retrieval queries
       |
 Retrieval agent: ACL-filtered vector + full-text search -> rank fusion
       |
 Answer agent: structured factual claims with exact source quotes
       |
 Verifier agent: source existence + quote checks + model evidence review
       |                           |
       | accepted                  | failed: at most one repair attempt
       |                           | then abstain
       v
 Recheck source access + knowledge version -> commit -> return answer
```

An answer is rendered only from verified claims. The browser never displays raw, unverified model drafts. A source quote must exist in an authorized, currently ready document. Unknown source IDs and invented quotes are rejected. A separate model pass checks whether the source actually supports the claim and whether relevant sources conflict.

`verified=true` means these checks passed. It is not a probability estimate or a guarantee of truth. A false original document can still support a false answer, and model-based verification can be wrong.

Conversation history persists in PostgreSQL. Only previous **user questions** are used for reference resolution; previous assistant answers are not treated as factual evidence. Conversations are private to their owner, even from other tenant administrators.

## Learning that can be reviewed and rolled back

```text
Negative feedback + correction + explicit consent
             |
       Pending proposal (not searchable)
             |
 A different administrator supplies original source quotes
             |
 Verifier checks the correction against those sources
             |
 Atomic approval + queued ingestion
             |
 Indexed reviewed document, private unless tenant sharing was consented
```

The user can withdraw learning consent. An administrator can revoke a promoted correction. Deleting an original source also revokes learned documents that depend on it. Approval is idempotent; a retry does not publish duplicate knowledge.

To try this, suggest a correction under an answer and check the learning-consent box. Then generate a token for a **different** administrator:

```bash
docker compose exec api python -m app.cli token --tenant acme --user reviewer --roles user,editor,admin
```

Connect with that token, load the review queue, and provide a shared original source's chunk ID and exact quote. Source chunk IDs are available under **Inspect chunks**. A real correction containing new facts first needs a trusted source document that establishes those facts.

This is **reviewed knowledge-base learning**, not automatic weight updates, fine-tuning, or blindly treating thumbs-up feedback as truth. See `docs/LEARNING.md`.

## Included features

| Area | Implementation |
|---|---|
| Backend | FastAPI, Pydantic validation, versioned schema migration, SQLAlchemy repository |
| Multiagent execution | Shared planner/retriever/answerer/verifier nodes; bounded native runner; optional LangGraph runner |
| RAG | Text/Markdown ingestion, chunking, real embeddings, PostgreSQL pgvector HNSW and full-text search, reciprocal-rank fusion, lexical reranking |
| Access control | Verified JWT tenant and user claims, private/shared document visibility, editor/admin/operator roles, reauthorization on cache hits and answer replay |
| Reliability | Transactional document/job creation, durable queue, leases, heartbeats, stale-worker fencing, retries, dead letters, idempotent chat requests |
| Request control | Per-user shared Redis rate limits, bounded concurrency, model-call and output limits, deadlines, model fallback and circuit breakers |
| Learning | Explicit consent, independent reviewer, evidence verification, private-by-default promotion, withdrawal/revocation, source dependencies and audit metadata |
| Operations | Health endpoints, authenticated Prometheus metrics, tenant queue status, structured content-free request logs, Docker Compose and Kubernetes examples |
| Testing | Unit/API/security/concurrency/failure tests, HTTP adapter contract tests, optional infrastructure tests, smoke/evaluation/load-test scripts |

Uploads support **UTF-8 `.txt` and `.md`**, or JSON text documents. There is no PDF/DOCX parser, OCR, website crawler, executable code tool, or arbitrary URL-fetch agent in this version. `source_uri` is a citation label; it is not fetched by the server.

## Run tests

With Python dependencies installed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q --cov=app --cov-report=term-missing
```

The base tests use disposable SQLite databases and the mock provider; no API key is needed. Real PostgreSQL/Redis and optional LangGraph tests skip unless their dependencies and settings are available.

For the separate, disposable infrastructure-test services:

```bash
# Run configure.py first so Compose can resolve its required development variables.
docker compose --profile test run --build --rm tests
```

This test image installs the optional LangGraph dependency and runs PostgreSQL/Redis tests against isolated test services. It does not require a live model API key. Do not point integration tests at an application database; the PostgreSQL test database name must end in `_test` and the runner creates and removes its own schema.

To check the running API after deployment:

```bash
export RAG_TOKEN="$(docker compose exec -T api python -m app.cli token --tenant acme --user demo)"
python3 scripts/smoke.py
python3 scripts/evaluate.py
```

Run `seed` and wait for **ready** before evaluation. Use a dedicated test tenant without conflicting documents. The small fixture evaluation is a smoke gate, **not a production accuracy statistic**. See `docs/TESTING.md` for failure cases and live-model evaluation guidance.

## Optional LangGraph execution

The same agent nodes can run through LangGraph instead of the built-in bounded runner:

```bash
pip install -r requirements-langgraph.txt
# Set ORCHESTRATOR=langgraph in .env before starting the API.
```

For Docker, set both `ORCHESTRATOR=langgraph` and `INSTALL_LANGGRAPH=true` in `.env`, then rebuild:

```bash
docker compose up --build -d
```

The graph is bounded to one repair attempt. Durable complete turns are in the database; this implementation does not persist an LLM call's intermediate graph state or resume halfway through a model request. A failed turn retries as a whole using the same idempotency key.

## Production deployment

Compose is a **single-host development deployment**, not highly available infrastructure. Kubernetes examples include multiple API and worker replicas, probes, resource limits, rollout settings, disruption budgets and autoscaling. They require your own image registry, identity provider, TLS ingress, and managed multi-zone PostgreSQL and Redis services.

Before exposing the application, complete `docs/OPERATIONS.md` and `docs/SECURITY.md`. Set `ENVIRONMENT=production`; configuration then rejects mock models, SQLite, in-memory request admission, HS256 development authentication, disabled redaction, and automatic startup migrations.

CPU autoscaling is a starting example. Model latency and queue age are often more useful scaling signals; measure the workload and configure the appropriate metrics adapter. Size database connection budgets before increasing replicas. No throughput or uptime numbers have been measured for this package.

## Layout

```text
app/                  API, agents, model adapter, storage, security, worker, browser UI
sample_data/          Original sample documents
scripts/              Configuration, running-API smoke test, evaluation, k6 harness
evals/                Small fixture question set
tests/                Offline unit/API/security/failure/concurrency tests
integration_tests/    Optional real PostgreSQL, Redis and LangGraph checks
deploy/k8s/           Kubernetes workload templates and separate secret examples
.github/workflows/    CI workflow with disposable test services
docs/                 Architecture, security, learning, API and operations notes
reports/              Actual isolated verification results
```
