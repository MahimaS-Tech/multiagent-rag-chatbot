"""Small release smoke gate, not a statistical estimate of real-world answer accuracy."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4
from smoke import request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default=os.environ.get("RAG_TOKEN", ""))
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent.parent / "evals/questions.jsonl")
    parser.add_argument("--output", type=Path, default=Path("evaluation-results.json"))
    args = parser.parse_args()
    if not args.token:
        parser.error("Set RAG_TOKEN or pass --token")
    rows = []
    for line in args.dataset.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        answer = request(args.base_url, args.token, "POST", "/v1/chat", {"message":case["question"]}, str(uuid4()))
        checks = {"status":answer["status"] == case["expected_status"],
                  "required_text":all(word.lower() in answer["answer"].lower() for word in case["must_include"]),
                  "citations":bool(answer["sources"]) == case["requires_sources"]}
        rows.append({"id":case["id"], "passed":all(checks.values()), "checks":checks, "demo_mode":answer["demo_mode"]})
    result = {"passed":sum(row["passed"] for row in rows), "total":len(rows), "cases":rows,
              "scope":"Synthetic smoke gate only; not a measured production accuracy rate."}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key:value for key,value in result.items() if key != "cases"}))
    raise SystemExit(0 if result["passed"] == result["total"] else 1)


if __name__ == "__main__":
    main()
