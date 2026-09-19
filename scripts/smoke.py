"""Exercise a running API using only the standard library. No credentials are printed."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from uuid import uuid4


def request(base, token, method, path, data=None, key=None):
    headers = {"Authorization": "Bearer " + token}
    if key:
        headers["Idempotency-Key"] = key
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Avoid dumping provider errors, payloads, or token-bearing request objects.
        raise RuntimeError(f"HTTP {error.code} for {method} {path}") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default=os.environ.get("RAG_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        parser.error("Set RAG_TOKEN or pass --token")
    body = "Customers can request a refund within 30 days of purchase."
    doc = request(args.base_url, args.token, "POST", "/v1/documents", {
        "title": "Smoke test " + str(uuid4()), "text": body, "visibility": "private"})
    try:
        deadline = time.monotonic() + 120
        while True:
            status = request(args.base_url, args.token, "GET", "/v1/documents/" + doc["id"])
            if status["state"] == "ready":
                break
            if status["state"] == "failed" or time.monotonic() > deadline:
                raise RuntimeError("Ingestion failed or did not complete before the smoke-test deadline")
            time.sleep(1)
        key = str(uuid4())
        question = {"message": "What is the refund policy?"}
        answer = request(args.base_url, args.token, "POST", "/v1/chat", question, key)
        assert answer["status"] == "answered" and answer["verified"] and answer["sources"], answer["status"]
        assert "30" in answer["answer"]
        replay = request(args.base_url, args.token, "POST", "/v1/chat", question, key)
        assert replay == answer
        print(json.dumps({"smoke_test":"passed", "demo_mode":answer["demo_mode"], "agent_steps":answer["agent_steps"]}))
    finally:
        request(args.base_url, args.token, "DELETE", "/v1/documents/" + doc["id"])


if __name__ == "__main__":
    main()
