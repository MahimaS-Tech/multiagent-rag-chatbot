# Verification report

Generated: 2026-09-11T07:42:35+00:00

## Executed results

| Check | Result |
|---|---|
| Pytest test cases | **131 passed, 4 skipped, 0 failed, 0 errors** |
| Application statement coverage | **86.6%** (1505/1737 statements); not branch coverage |
| Python source/script compilation | Passed |
| Browser JavaScript and k6 harness syntax | Passed with Node; not browser interaction testing |
| Compose/Kubernetes/CI YAML parsing | 17 files parsed; syntax only, not deployment-policy validation |
| Development configuration helper | Generated fresh secrets, mode 0600 file, refused overwrite |
| Separate-process application smoke test | Passed: real local HTTP listener + separate worker + SQLite + mock provider |
| Seeded fixture evaluation over HTTP | 6/6 passed in mock mode; not a production accuracy estimate |
| Worker health command and process shutdown | Passed |

Runtime: Python 3.13.5 on Linux. The Dockerfile targets Python 3.12, which was not exercised here. No real model API key was used and no live inference or embedding calls were made.

The actual HTTP smoke test launched Uvicorn and the ingestion worker as separate processes, migrated and seeded a disposable database, waited for indexing, uploaded a dedicated source, checked a cited answer and identical idempotent replay, deleted that source, ran the fixture evaluation, and stopped the processes. This was not a Docker or production-infrastructure test.

## The four skipped checks

1. Real PostgreSQL vector/full-text retrieval and tenant ACL checks: no configured PostgreSQL service/driver.
2. Real PostgreSQL SKIP LOCKED worker claim behavior: no configured PostgreSQL service/driver.
3. Shared Redis rate limiting and cache behavior: no configured Redis service/client.
4. Optional LangGraph workflow: dependency unavailable in the isolated verification environment.

Their test implementations are included in `integration_tests/`. The Docker Compose test profile and CI workflow are provided to execute them with disposable services. Those workflows have **not** been run here.

## Not verified

Live OpenAI integration or real-model answer quality; Docker build/Compose deployment; Kubernetes admission, rollout or cloud infrastructure; real database/cache failover; browser automation; load/soak capacity; dependency or image vulnerability scans; regulatory compliance. No uptime, throughput, or real-world accuracy percentage is claimed.

The deterministic mock has deliberately narrow extraction and verification rules. Passing mock tests does not establish that a language model will reason correctly, resist every prompt injection, or always choose the right evidence. Run domain-specific, human-reviewed evaluations and infrastructure tests before production use.

## Supporting artifacts

`pytest.txt`: captured pytest/coverage output. `junit.xml`: machine-readable test results. `coverage.json`: statement coverage data. `additional-verification.json`: separate process and syntax checks. `evaluation-results.json`: the six fixture cases.

The ZIP contains no generated runtime `.env`, API key, JWT signing key, application database, virtual environment, or cache directory. Static test-only credentials and placeholders are clearly identified in source/config examples. `MANIFEST.sha256` records packaged-file digests.
