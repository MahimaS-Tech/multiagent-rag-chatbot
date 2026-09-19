// Run against a dedicated test tenant after seeding. Raise the tenant request limit deliberately.
// This is a harness, not a claim of measured throughput. Real-provider runs incur charges.
import http from "k6/http";
import { check, sleep } from "k6";
export const options = {vus: Number(__ENV.VUS || 2), duration: __ENV.DURATION || "30s",
  thresholds: {http_req_failed: ["rate<0.01"], checks: ["rate>0.99"]}};
export default function () {
  if (!__ENV.RAG_TOKEN) throw new Error("RAG_TOKEN is required");
  const result = http.post((__ENV.BASE_URL || "http://localhost:8000") + "/v1/chat",
    JSON.stringify({message: "What is the refund policy?"}),
    {headers: {Authorization: `Bearer ${__ENV.RAG_TOKEN}`, "Content-Type":"application/json",
      "Idempotency-Key":`load-${__VU}-${__ITER}-${Date.now()}`}, timeout:"120s"});
  check(result, {"successful response": r => r.status === 200,
    "answered with sources": r => r.status === 200 && r.json("status") === "answered" && r.json("sources").length > 0});
  sleep(3);
}
