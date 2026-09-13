# Primary implementation references

These references informed the adapter and deployment design. They are not evidence that the included external services or live-model paths were executed in the build environment.

1. OpenAI, Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs
   The Responses adapter requests a strict JSON schema. Schema conformance is separate from factual correctness.
2. OpenAI, Responses API reference: https://developers.openai.com/api/reference/resources/responses/methods/create
   The adapter uses model instructions, input data, bounded output, `store=false`, and structured text output.
3. OpenAI, embeddings guide: https://developers.openai.com/api/docs/guides/embeddings
   The application fixes its embedding dimension at 1536 and keeps different model spaces separate.
4. pgvector project documentation: https://github.com/pgvector/pgvector
   PostgreSQL vector storage, cosine distance, HNSW indexes, filtering and iterative scans.
5. PostgreSQL SELECT documentation: https://www.postgresql.org/docs/current/sql-select.html
   Row locking and `SKIP LOCKED` for concurrent queue consumers.
6. LangGraph Graph API: https://docs.langchain.com/oss/python/langgraph/graph-api
   Optional state graph, nodes and conditional transitions. The default runner does not require this dependency.
7. FastAPI documentation: https://fastapi.tiangolo.com/
   Application lifespan, dependencies, uploads, validation and testing interfaces.
8. Redis scripting documentation: https://redis.io/docs/latest/develop/programmability/eval-intro/
   Shared atomic request-admission counter.
9. Kubernetes Deployments: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
   Deployment rollout and replica configuration used in the editable manifests.

Model names and top-level Python package versions are explicit release choices, not claims about the latest available versions. Resolve and test transitive dependencies and image digests in the target release environment.
