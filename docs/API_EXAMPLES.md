# API examples

All authenticated paths require a signed bearer token. Identity and document permissions are derived from that token. Use the same body/key for a retry, not a newly generated key for every network attempt.

```bash
BASE=http://localhost:8000
export RAG_TOKEN="$(docker compose exec -T api python -m app.cli token --user demo)"
```

## Add a source

```bash
curl -sS "$BASE/v1/documents" \
  -H "Authorization: Bearer $RAG_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"Refund policy","text":"Customers can request a refund within 30 days of purchase.","visibility":"tenant","source_uri":"https://example.com/policy"}'
```

This queues ingestion and returns a document ID. The citation URI is not fetched or independently verified. Poll `GET /v1/documents/{id}` until `state=ready`; inspect `/v1/documents/{id}/chunks` to obtain source IDs and text. The owner or a tenant administrator can delete a shared document; an editor cannot delete another user's document without admin permission. Other administrators cannot access private documents through the ordinary document API.

## Ask a question

```bash
REQUEST_KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl -sS "$BASE/v1/chat" \
  -H "Authorization: Bearer $RAG_TOKEN" \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is the refund policy?"}'
```

The response includes `answer_id`, `conversation_id`, `status`, `answer`, numbered `sources`, `verified`, `demo_mode`, `knowledge_version`, `agent_steps` and provider usage counters. Responses are complete JSON, not a stream of unverified tokens. Add the returned `conversation_id` to a later request to continue that conversation; the conversation must belong to the same tenant and user.

## Feedback and consent

```text
POST /v1/feedback
{
  "answer_id": "the-returned-answer-uuid",
  "rating": -1,
  "correction": "Customers can request a refund within 30 days of purchase.",
  "consent_to_learning": true,
  "learning_scope": "private"
}
```

The answer UUID above is a value from your actual response, not a literal identifier to submit. Consent permits a tenant administrator to review the question and correction. It does not instantly make the correction searchable. Private scope restricts the eventual learned document to the feedback author.

With a **different administrator's** token, get `/v1/learning/proposals`, then submit:

```text
POST /v1/learning/proposals/{proposal_id}/review
{
  "action": "approve",
  "reason": "Checked the original refund policy.",
  "evidence": [
    {
      "chunk_id": "an-actual-shared-original-source-chunk-id",
      "quote": "Customers can request a refund within 30 days of purchase."
    }
  ]
}
```

Reject with `action=reject`, a reason, and `evidence=[]`. After approval, check the learned document's ingestion status. Withdraw consent as the author with `POST /v1/feedback/{id}/withdraw`; revoke approved learning as an administrator with `POST /v1/learning/proposals/{id}/revoke`.

## Route permissions

| Routes | Minimum access |
|---|---|
| `/v1/chat`, own conversation history, permitted documents/chunks | Authenticated principal |
| Create/upload documents, retry/delete owned documents | Editor (admin implies editor) |
| Review queue, review decisions, learning revocation, tenant audit/queue | Tenant admin |
| `/metrics` | Explicit operator role |
| `/health/live`, `/health/ready`, browser assets, API schema/docs | Public unless your ingress restricts them |

Pagination uses `limit` and `offset` on collection endpoints. The server bounds both; there is no unlimited list-all request. Every collection is scoped to the authenticated tenant, with additional owner filtering where required.

Error responses expose a stable `error.code` and safe `error.message`. Typical statuses are 401 invalid identity, 403 insufficient role, 404 unavailable/inaccessible resource, 409 conflicting request or lease, 413 input too large, 422 invalid/unsupported content, 429 rate-limited, 503 dependency/capacity/knowledge-change failure, and 504 the complete-chat deadline. Do not blindly retry every 4xx.
