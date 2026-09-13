# Reviewed learning

## What changes

Approved corrections become new indexed documents. The model's weights, prompts and deployment settings do not change automatically. Raw chats, thumbs-up ratings and unreviewed corrections are never used as trusted retrieval knowledge.

A negative rating with a correction can create a proposal only when `consent_to_learning=true`. The user selects `learning_scope=private` or explicitly chooses `tenant`. Private is the default. Without consent, feedback is recorded for the owning user's answer but is not listed in the review queue or indexed.

## Approval gate

A different administrator in the same tenant must review the proposal. The reviewer supplies an exact quote and chunk ID from an active, shared, original document in the current embedding space. Private documents and previously learned documents cannot be used as approval evidence. This avoids implicit sharing of another user's private source and prevents chains of unsupported learned facts.

The verification agent checks the correction's factual support and relevance. A real quote with a contradictory correction is insufficient. Immediately before committing approval, the repository revalidates source state, source permissions, quote existence, consent and proposal state. Approval, the learned document, its provenance dependencies and its ingestion job are written atomically. It becomes searchable only after worker indexing succeeds.

In mock mode, approval verification is deliberately extractive: the correction must occur in the supporting quote. Use a real model and a domain evaluation set before assessing paraphrased corrections.

## State transitions

```text
recorded                         feedback without learning consent
pending -> rejected              reviewer declines
pending -> approved -> published approved and then indexed
approved/published -> revoked    administrator or dependency revocation
pending/approved/published -> withdrawn  owner withdraws consent
```

Approval can remain `approved` while ingestion is queued, retrying or dead-lettered. Inspect its learned document and job status; use the document retry endpoint after fixing the cause. A repeated identical review by the same reviewer is idempotent. A different review of an already decided proposal is a conflict.

## Revocation

The feedback owner can withdraw consent; this clears the correction field, revokes any derived active document and removes it from the consented review queue. An administrator can explicitly revoke a promoted correction. Deleting an original supporting document also revokes dependent learned documents. Provenance depth is one because learned documents cannot serve as approval evidence.

These operations remove active retrieval content and block dependent response replay. They are not a complete privacy-erasure implementation: retained turn records, review evidence, infrastructure backups and operational retention policies must be handled separately.

## Improving quality over time

The included evaluation script is a small release smoke gate, not a proof that every approved correction improves quality. Before promotion at scale, establish a representative, versioned domain evaluation set with correct source IDs, expected abstentions, conflicting policies, negation, dates, multilingual content and cross-tenant adversarial cases. Compare retrieval recall, supported-answer precision, appropriate abstention and response latency before and after batches of reviewed changes. Include human adjudication of disputed claims. Do not treat a single model's self-reported confidence as calibrated accuracy.
