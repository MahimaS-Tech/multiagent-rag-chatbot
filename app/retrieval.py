import asyncio
import math
from app.security import digest
from app.text import lexical_score


def reciprocal_rank_fusion(results, min_similarity=.2):
    """Fuse rankings rather than adding incomparable cosine/full-text raw scores."""
    scores, hits = {}, {}
    for name in ("vector", "lexical"):
        for rank, hit in enumerate(results.get(name, []), 1):
            if name == "vector" and hit.get("similarity", 0) < min_similarity:
                continue
            key = hit["id"]
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            hits[key] = {**hits.get(key, {}), **hit}
    return [{**hits[key], "score": scores[key]} for key in sorted(scores, key=lambda key: (-scores[key], key))]


class RetrievalAgent:
    def __init__(self, db, repo, provider, controls, settings):
        self.db, self.repo, self.provider, self.controls, self.settings = db, repo, provider, controls, settings

    async def search_one(self, principal, query, version, budget):
        key = digest("retrieval-v1", principal.tenant_id, principal.user_id, sorted(principal.roles),
                     self.settings.embedding_space, version, query, self.settings.retrieval_candidates,
                     self.settings.min_vector_similarity)
        cached = await self.controls.get(key)
        if isinstance(cached, list) and len(cached) <= self.settings.retrieval_candidates * 2 and all(
            isinstance(item, dict) and isinstance(item.get("id"), str)
            and isinstance(item.get("score"), (float, int)) and math.isfinite(item["score"]) for item in cached):
            current = await self.db.call(self.repo.get_chunks, principal,
                                        [item["id"] for item in cached], self.settings.embedding_space)
            by_id = {hit["id"]: hit for hit in current}
            if len(by_id) == len(cached):
                return [{**by_id[item["id"]], "score": item["score"]} for item in cached]
        vector = (await self.provider.embed([query], budget))[0]
        result = await self.db.call(self.repo.search, principal, query, vector,
                                   self.settings.embedding_space, self.settings.retrieval_candidates)
        fused = reciprocal_rank_fusion(result, self.settings.min_vector_similarity)
        await self.controls.set(key, [{"id": hit["id"], "score": hit["score"]} for hit in fused],
                                self.settings.retrieval_cache_seconds)
        return fused

    async def run(self, principal, question, queries, version, budget):
        # TaskGroup cancels sibling searches if one query fails, bounding abandoned work.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self.search_one(principal, query, version, budget))
                     for query in list(dict.fromkeys(queries))[:3]]
        merged = {}
        for task in tasks:
            for hit in task.result():
                previous = merged.get(hit["id"])
                merged[hit["id"]] = {**hit, "score": hit["score"] + (previous["score"] if previous else 0)}
        ranked = sorted(merged.values(), key=lambda hit: (
            -hit["score"] * (1 + .2 * lexical_score(question, hit["content"])), hit["id"]))
        result, seen = [], set()
        for hit in ranked:
            fingerprint = digest(hit["content"])
            if fingerprint not in seen:
                seen.add(fingerprint)
                result.append(hit)
            if len(result) == self.settings.retrieval_k:
                break
        return result
