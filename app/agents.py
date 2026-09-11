from typing import TypedDict, Any
from app.providers import Budget
from app.schemas import Plan, Draft, Verdict, ChatOutput, Source
from app.security import suspicious
from app.text import normalized

ABSTENTION = "I don't have enough reliable evidence in the available documents to answer that."


def source_payload(sources):
    return [{"id": source["id"], "title": source["title"], "content": source["content"]} for source in sources]


def evidence_issues(claims, sources):
    by_id, issues = {source["id"]: source for source in sources}, []
    if not claims:
        return ["No factual claims were provided."]
    for claim in claims:
        if suspicious(claim["text"]):
            issues.append("A claim contains an instruction-override attempt.")
        if not claim.get("evidence"):
            issues.append("A claim has no citations.")
        for ref in claim.get("evidence", []):
            source = by_id.get(ref["chunk_id"])
            if not source:
                issues.append("A citation points outside the retrieved evidence.")
            elif normalized(ref["quote"]) not in normalized(source["content"]):
                issues.append("A cited quote does not occur in its source.")
            elif suspicious(ref["quote"]):
                issues.append("A quote contains an instruction-override attempt.")
    return list(dict.fromkeys(issues))[:8]


class VerificationAgent:
    def __init__(self, provider):
        self.provider = provider

    async def run(self, question, claims, sources, budget, original_question=None, history=None):
        issues = evidence_issues(claims, sources)
        if issues:
            return Verdict(supported=False, answers_question=False, contradiction=False, issues=issues)
        return await self.provider.generate("verify", {"question": question, "claims": claims,
            "sources": source_payload(sources), "original_question": original_question or question,
            "history": (history or [])[-6:]}, Verdict, budget)


class State(TypedDict, total=False):
    principal: Any
    question: str
    resolved_question: str
    history: list[str]
    version: int
    reservation: dict
    budget: Budget
    plan: Plan
    sources: list[dict]
    draft: Draft
    verdict: Verdict
    attempts: int
    steps: list[str]
    result: ChatOutput


class AgentWorkflow:
    """Same bounded agent nodes run natively or through the optional LangGraph adapter."""
    def __init__(self, provider, retriever, settings):
        self.provider, self.retriever, self.settings = provider, retriever, settings
        self.verifier = VerificationAgent(provider)
        self.graph = self._build_langgraph() if settings.orchestrator == "langgraph" else None

    async def plan(self, state):
        question = state["question"]
        if suspicious(question):
            plan = Plan(standalone_question=question[:1600], queries=[question[:1200]], route="block")
        else:
            plan = await self.provider.generate("plan", {"question": question,
                "history": [q[:600] for q in state.get("history", [])]}, Plan, state["budget"])
        return {"plan": plan, "resolved_question": plan.standalone_question,
                "steps": state["steps"] + ["planner"]}

    async def retrieve(self, state):
        sources = await self.retriever.run(state["principal"], state["resolved_question"], state["plan"].queries,
                                          state["version"], state["budget"])
        return {"sources": sources, "steps": state["steps"] + ["retriever"]}

    async def generate(self, state):
        previous = state.get("verdict")
        draft = await self.provider.generate("answer", {"question": state["resolved_question"],
            "sources": source_payload(state["sources"]), "issues": previous.issues if previous else []}, Draft, state["budget"])
        return {"draft": draft, "attempts": state["attempts"] + 1,
                "steps": state["steps"] + ["answerer" if state["attempts"] == 0 else "answer_repair"]}

    async def verify(self, state):
        draft = state["draft"]
        if draft.abstain:
            verdict = Verdict(supported=False, answers_question=False, contradiction=False, issues=["Answerer abstained."])
        else:
            verdict = await self.verifier.run(state["resolved_question"], [claim.model_dump() for claim in draft.claims],
                                              state["sources"], state["budget"], original_question=state["question"],
                                              history=state.get("history", []))
        return {"verdict": verdict, "steps": state["steps"] + ["verifier"]}

    @staticmethod
    def verified(state):
        verdict = state.get("verdict")
        return bool(verdict and verdict.supported and verdict.answers_question and not verdict.contradiction
                    and state.get("draft") and not state["draft"].abstain and state["draft"].claims)

    def after_verify(self, state):
        return "finalize" if self.verified(state) or state["attempts"] >= 2 or state["draft"].abstain else "generate"

    async def finalize(self, state):
        route, sources, answer, status = state["plan"].route, [], ABSTENTION, "abstained"
        verified = self.verified(state)
        if route == "block":
            answer, status = "I can answer document questions, but cannot bypass instructions or expose secrets.", "blocked"
        elif route == "clarify":
            answer, status = "Please add more detail about what you want to know from the documents.", "clarification"
        elif verified:
            status, lines, references = "answered", [], {}
            by_id = {source["id"]: source for source in state["sources"]}
            for claim in state["draft"].claims:
                numbers = []
                for ref in claim.evidence:
                    key = (ref.chunk_id, normalized(ref.quote))
                    if key not in references:
                        source = by_id[ref.chunk_id]
                        references[key] = len(sources) + 1
                        sources.append(Source(number=references[key], chunk_id=ref.chunk_id,
                            document_id=source["document_id"], title=source["title"], source_uri=source["source_uri"], quote=ref.quote))
                    numbers.append(references[key])
                lines.append(claim.text + " " + " ".join(f"[{number}]" for number in sorted(set(numbers))))
            answer = "\n\n".join(lines)
        reservation = state["reservation"]
        return {"result": ChatOutput(answer_id=reservation["id"], conversation_id=reservation["conversation_id"],
            status=status, answer=answer, sources=sources, verified=verified, demo_mode=self.settings.provider == "mock",
            knowledge_version=state["version"], agent_steps=state["steps"], usage=state["budget"].export())}

    async def run(self, initial):
        state = {**initial, "steps": [], "sources": [], "attempts": 0}
        if self.graph is not None:
            output = await self.graph.ainvoke(state, config={"recursion_limit": 16})
            return output["result"]
        state.update(await self.plan(state))
        if state["plan"].route == "retrieve":
            state.update(await self.retrieve(state))
            if state["sources"]:
                while state["attempts"] < 2:
                    state.update(await self.generate(state))
                    state.update(await self.verify(state))
                    if self.after_verify(state) == "finalize":
                        break
        state.update(await self.finalize(state))
        return state["result"]

    def _build_langgraph(self):
        try:
            from langgraph.graph import StateGraph, START, END
        except ImportError:
            raise RuntimeError("Install requirements-langgraph.txt for ORCHESTRATOR=langgraph") from None
        builder = StateGraph(State)
        for name in ("plan", "retrieve", "generate", "verify", "finalize"):
            builder.add_node("node_" + name, getattr(self, name))
        builder.add_edge(START, "node_plan")
        builder.add_conditional_edges("node_plan", lambda s: "node_retrieve" if s["plan"].route == "retrieve" else "node_finalize")
        builder.add_conditional_edges("node_retrieve", lambda s: "node_generate" if s["sources"] else "node_finalize")
        builder.add_edge("node_generate", "node_verify")
        builder.add_conditional_edges("node_verify", lambda s: "node_" + self.after_verify(s))
        builder.add_edge("node_finalize", END)
        return builder.compile()
