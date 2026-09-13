# Testing and evaluation

Read `reports/VERIFICATION.md` for the exact executed results, runtime and omissions. A skipped test is not a passing test. The offline mock validates application behavior, not the accuracy or safety of a real model.

## Included test layers

`tests/test_api.py` covers browser assets, health, JWT access, roles, upload validation, indexing, answers/citations, unknown-question abstention, deduplication, request replay/conflicts, private/shared tenant boundaries, conversation ownership, redaction, prompt-override blocking, size limits, error sanitization and operational endpoints.

`tests/test_learning.py` covers consent default-off, feedback ownership, a separate reviewer, private-by-default promotion, explicit tenant sharing, approval idempotency, false corrections despite real quotes, invented quotes, cross-tenant denial, rejection, dependency revocation, withdrawal and audit metadata.

`tests/test_repository.py` covers repeatable migration, exclusive/concurrent claims, expired-lease recovery, stale-worker fencing, heartbeats, transaction rollback, repeated completion, deletion during indexing, restore, dead letters/retry, abandoned attempts, chat serialization, stale request cleanup, knowledge-version races, tenant-scoped completion, safe history and vector-space isolation.

`tests/test_providers.py` tests the actual HTTP adapter with `httpx.MockTransport`: Responses/embedding request structure, strict output schemas, token counters, retryable versus permanent errors, model fallback, timeout/refusal/incomplete/malformed outputs, embedding count/index/dimension/finite-value checks, budgets and circuit states. These are contract tests, not live provider calls.

`tests/test_failures.py` tests fabricated source IDs, supported quotes paired with false claims, bounded repair, contradictory sources, model/embedding failures, whole-request deadlines, call budgets, knowledge changes during generation, overload, source quote normalization, worker retry and planner topic changes.

`tests/test_security_controls.py` covers JWT algorithms/claims, an ephemeral RSA-signed identity token, unsafe citation URLs, production guards, redaction, chunk/vector helpers, rank fusion, bounded caches, rate limits, Redis fail-open/fail-closed behavior and reauthorization of forged cache entries.

`integration_tests/test_external_backends.py` contains four opt-in checks: real PostgreSQL pgvector/full-text access filtering, real PostgreSQL `SKIP LOCKED`, shared Redis limiting/cache, and the optional LangGraph end-to-end workflow. Use the Compose test profile or the supplied CI workflow. A PostgreSQL test database must be disposable and end in `_test`; fixtures use a unique schema, never drop an application database, and remove only their own schema.

## Running checks

```bash
python -m compileall -q app scripts
python -m pytest -q --cov=app --cov-report=term-missing --junitxml=reports/junit.xml
# With dependencies installed and disposable services configured:
TEST_POSTGRES_URL='postgresql+psycopg://test:password@localhost:5432/rag_test' \
TEST_REDIS_URL='redis://localhost:6379/0' python -m pytest -q
```

Alternatively, after configuration, `docker compose --profile test run --build --rm tests` creates isolated test services and installs LangGraph. CI runs the same categories with real service containers. A workflow file is not evidence that CI has already run.

## Running-application checks

`scripts/smoke.py` exercises an actual HTTP listener: authenticated upload, wait for worker publication, cited answer, idempotent replay, and source cleanup. It requires a running API and worker. The script creates and deletes a dedicated source. Run against a dedicated tenant with no conflicting policy documents.

`scripts/evaluate.py` checks the small JSONL fixture against already-seeded, ready sample documents. It records status, required text and citation presence, with a `demo_mode` flag. Six successful fixture cases do not establish a real-world accuracy rate or even full factual coverage of those responses.

`scripts/loadtest.js` is a k6 harness for a dedicated test environment. It is not executed by the offline unit suite. Tune virtual users, request rate, provider limits and assertions before use. Live-model tests and load tests incur provider charges. Do not run high-volume tests against a shared production tenant.

## Release evaluation still needed

Use a representative held-out domain dataset with gold source labels and human-reviewed expected answers. Measure retrieval recall, citation correctness, claim support, relevant-answer precision, appropriate abstention, contradiction handling, privacy isolation, malicious-source resilience, latency and cost. Test multilingual content, ambiguous pronouns, policy versions, dates, negation and exceptions. Include multiple reviewers where subjective judgments matter.

Fault-inject API/worker termination, real database/Redis failover and network partitions in a disposable deployment. Test browser interactions with a real browser automation suite. Run dependency/image scans and deployment-policy validation. These are explicit remaining release checks, not capabilities silently assumed to have been tested here.
