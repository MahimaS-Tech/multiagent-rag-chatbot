# Security boundaries and remaining work

## Implemented controls

JWT verification pins the allowed algorithm, issuer and audience and requires expiry, issue time, subject and tenant claims. Tenant/user identity is never accepted from document or chat payloads. Production requires RS256 public-key validation; development tokens are generated only by the local CLI. Roles distinguish normal users, document editors, tenant administrators and global telemetry operators. A tenant administrator does not gain access to another user's private documents or conversations.

All repository reads that return user data are explicitly tenant-scoped. Private visibility requires ownership. Source access is rechecked on retrieval cache hits, final response commit, history reads and idempotent replay. SQL is parameterized. **Database row-level security is not implemented**; the repository's application-level filters are the isolation boundary. Use separate databases/schemas or properly engineered RLS where a stronger isolation boundary is required, and test it independently.

Request bodies, document sizes, chunk counts, query counts, retrieved context and output sizes are bounded. Both Content-Length and chunked bodies are limited before parsing. The application does not parse PDFs or archives, execute model-selected code, browse model-selected URLs, or expose shell/database tools to agents. HTTP model clients disable redirects.

Source contents are explicitly untrusted data in model instructions. A heuristic filter catches a narrow family of instruction-override patterns; it is neither a complete prompt-injection defense nor an authoritative classifier. Models may still follow attacks that this filter misses. Capability restriction, scoped retrieval, output validation and refusal/abstention provide separate controls.

Email, phone-like numbers and common API-key patterns are redacted before storing source bodies/user questions or sending them to the model. This regex-based filter can miss sensitive information and can redact legitimate numbers. Citation URLs, filenames and unusual identifiers can also contain personal data. It is not a DLP product or a regulatory compliance guarantee.

The main UI uses DOM text nodes, not raw HTML insertion, and keeps bearer tokens in memory instead of browser storage. It applies a self-hosted CSP. Only the built-in documentation pages permit their required CDN and inline bootstrap. The app uses bearer headers, not cookie authentication. CORS is disabled unless specific origins are configured.

Error responses never include raw provider response bodies, database exceptions or user-submitted invalid values. Request logs contain generated request IDs, normalized route names, status and duration—not prompts, source content, authorization headers or model outputs. Audit events store action metadata. Metrics avoid user/tenant label cardinality and require the distinct `operator` role.

## Required production decisions

Use an established identity provider to issue short-lived tokens containing `sub`, `tid`, `roles`, `iss`, `aud`, `iat`, and `exp`. This project validates a configured public key; it does not implement a login service, refresh-token flow, JWKS discovery, automatic key rollover or immediate per-token revocation. Plan key rotation, expiry and account/role revocation with that provider.

Terminate TLS at a controlled ingress. Use verified TLS for PostgreSQL, Redis and the model endpoint. Keep secrets in a managed secret store or protected Kubernetes Secrets with appropriate encryption and access policy. Do not grant the API the database migration role. The Kubernetes examples use separate runtime and migration secrets; the migration job never starts an HTTP service.

Add network-layer connection limits, a reverse-proxy/WAF policy, upload/storage quotas, account onboarding limits and IP-based unauthenticated abuse protection. The shared per-user request limiter is not a monthly spending budget or a storage quota. Configure provider project limits and operational alerts. Avoid exposing health, OpenAPI or telemetry surfaces more broadly than your policy allows.

Establish original-document trust, document expiry/version ownership, backups, restore drills, retention and deletion procedures. Source deletion scrubs active document/chunk data and withholds revoked answers; it does not erase all conversation records, review evidence or backups. Consent withdrawal removes promoted retrieval content but is not a complete legal erasure workflow.

Full-context third-party tracing is not enabled. Do not enable framework or provider tracing that uploads prompts, source excerpts, principal objects or embeddings without a separate privacy/security review and redaction policy. The application sets `store=false` on Responses requests, but this setting alone is not a contractual promise about provider logging, retention or training. Review your provider agreement and project settings.

Finally, resolve and pin transitive dependencies and image digests in your own release pipeline, scan dependencies and images, verify the deployment against your threat model, and run cross-tenant and model-adversarial evaluations. Top-level pins in this package are not a signed supply-chain attestation or a complete dependency lock.
