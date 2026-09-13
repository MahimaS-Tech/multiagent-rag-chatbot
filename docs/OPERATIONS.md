# Operations and deployment

## Development

Use the README's Docker commands. `docker compose ps` shows service health; `docker compose logs api worker migrate` shows safe application events and startup failures. The API becomes ready only after its schema exists and its database/request-control dependencies answer. Provider health is not included in readiness to avoid restart storms during a provider outage; watch chat 503s and provider error codes separately.

`docker compose stop` retains data. **`docker compose down -v` deletes the development database and Redis volumes.** Never run it as a troubleshooting step without deciding that data loss is acceptable. Do not regenerate database passwords for an existing volume without coordinating the database credential change.

Document states are `queued`, `indexing`, `ready`, `failed`, and `deleted`. A failed job can be retried through `POST /v1/documents/{id}/retry` after fixing the cause. Deleting an in-flight source cancels its job and fences out its old worker. Queue status is available to a tenant administrator at `/v1/admin/queue`.

## Kubernetes setup order

The manifests are editable deployment examples, not a preconfigured cluster. Before applying them, build and push the runtime image to your registry. Replace the example image in both deployments and the migration job. Pin a vetted image digest in a production release.

Provision a PostgreSQL service that supports pgvector, with multi-zone failover, backups and restore testing. Provision a private, authenticated Redis service with a failover configuration appropriate to your availability target. Use a separate application database/cache namespace per environment. Size the total database connection budget: maximum API replicas times `DB_CONCURRENCY`, plus maximum worker replicas times their pool ceiling, plus maintenance headroom. With the unmodified example maxima the pool ceilings can total 240 connections before maintenance. Reduce the pools or maxima when the database cannot support that.

Configure your identity provider's issuer, audience, RSA public key and signed tenant/user/role claims in `configmap.yaml`. The server does not supply production identities. Replace the secret examples through your secret-management process. Runtime and migration credentials are separate; the API must not receive a schema-owner/superuser credential. Protect all database/cache connections with authenticated, verified TLS. The example `verify-full` PostgreSQL URL requires an appropriate trusted root certificate installed or mounted in the container.

Apply prerequisites and migrate before starting serving replicas:

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
# Create real rag-runtime and rag-migration secrets using your secret manager.
# The *.example.yaml files are documentation; do not apply their placeholder values.
kubectl apply -f deploy/k8s/migrate-job.yaml
kubectl -n rag-chatbot wait --for=condition=complete job/rag-migrate-v1 --timeout=300s
# Grant the runtime database role only the required DML/sequence privileges.
kubectl apply -k deploy/k8s
kubectl -n rag-chatbot rollout status deployment/rag-api
kubectl -n rag-chatbot rollout status deployment/rag-worker
```

The migration process deliberately overrides `ENVIRONMENT=development` and `PROVIDER=mock` **only for the non-serving migration command**, so it needs no live model credential. API/worker pods remain in guarded production mode. The initial migration uses an advisory lock and creates pgvector, tables and indexes. On a managed database, extension installation may require a separate privileged infrastructure step.

The application uses schema version 1. Startup checks but does not change production schema. Future schema changes require reviewed, versioned expand/contract migrations; do not treat `create_all` as an automatic migration engine for changed columns. For a very large corpus, plan concurrent/online index maintenance separately rather than repeatedly rebuilding large indexes in a serving deployment.

Install and configure a TLS ingress controller before adapting `ingress.example.yaml`. Set timeouts longer than the configured complete-chat deadline; allow sufficient request size for multipart overhead. The example ingress network policy must be adapted to your real ingress/monitoring namespace labels and CNI behavior. It intentionally is not applied by the base Kustomization. Egress restrictions to DNS, PostgreSQL, Redis and the model provider require your cluster-specific network policy or egress gateway. Do not assume a generic Kubernetes policy can safely whitelist a changing provider hostname by IP.

Topology spread requires correctly labeled multi-zone nodes. CPU HPAs require the metrics API. Workers are mainly constrained by model I/O and queue age, so CPU alone may not trigger useful scaling; replace or supplement the example with measured queue-age/backlog signals. The PDBs protect voluntary disruption, not every node or zone failure.

## Failure behavior

| Failure | Response / recovery |
|---|---|
| One API process exits | A retry reaches another replica. A completed request replays; abandoned work is reclaimed after its lease. |
| Worker exits mid-indexing | Another worker reclaims its expired lease. Old owners cannot publish. |
| Embedding/provider transient error | Bounded retries and delayed job retry, or a safe chat 503. Model circuit breakers reduce repeated generation calls. |
| Answer cannot be verified | One repair attempt; then abstention without exposing the draft. |
| PostgreSQL unavailable | Readiness fails; authoritative writes/reads do not silently move to a weaker store. |
| Redis unavailable | Readiness and authenticated admission fail closed. Optional retrieval-cache failures alone can fall back to database search. |
| Source deleted during chat | Final version/source check refuses publication; retry on current knowledge. |
| Duplicate upload/review/request | Scoped deduplication/idempotency prevents duplicate committed knowledge/answers. |

No API response can guarantee an already-started external model request was not billed. A client timeout is not proof of non-completion. Retry the identical body with the same idempotency key; use a new key for a genuinely new request. A 409 `request_in_progress` or `conversation_busy` should back off; a 409 `idempotency_conflict` needs a corrected client request, not blind retries. A `stale_answer` requires a new key to answer against current sources.

## Monitoring and recovery drills

Scrape `/metrics` with a token carrying **operator**, not merely tenant admin. Metric families are HTTP counts/latency and completed answer deliveries by status/mode. Delivery counts include idempotent replays; they are not counts of unique model generations. Per-answer usage records appear in the chat response and stored turn. There are no fabricated monthly cost, dollar, retrieval-quality or GPU metrics.

Track HTTP 429/503/504 rates, latency distributions, abstention rates, worker liveness, oldest queued work, dead letters, database saturation, Redis health and provider account spend. Avoid putting tenant/user IDs into unrestricted Prometheus labels. Audit records contain tenant-scoped administrative action metadata; define their retention separately from conversation storage.

Run the included smoke test after rollout, then a domain evaluation. Before claiming an availability target, practice worker termination, API termination, database/Redis failover, restore from backup, provider timeout, secret rotation and source revocation during an in-flight answer. The included load harness has no measured capacity result; establish a baseline on your actual infrastructure and provider limits.

## Backend-specific constraints

The Redis adapter targets a single logical primary endpoint with managed failover; Redis Cluster routing and Sentinel discovery are not implemented. Use a compatible endpoint rather than a sharded-cluster connection URL. Synchronize host clocks: database job/chat leases use application wall-clock timestamps. Fencing tokens still prevent replaced owners from publishing, but clock skew can cause early reclaims or delayed recovery.
