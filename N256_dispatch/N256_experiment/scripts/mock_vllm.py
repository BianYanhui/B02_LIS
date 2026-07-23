"""Mock vLLM instance for N=256 dispatch scalability test.

Simulates a vLLM OpenAI-compatible server with:
  - /health endpoint
  - /metrics Prometheus endpoint (vllm:* metrics)
  - /v1/chat/completions (streaming and non-streaming)

Failure modes (configurable via env or CLI):
  - SLOW: each request takes 100-500ms (configurable)
  - FAIL_5XX: 5% of requests return 500
  - TIMEOUT: 1% of requests hang forever
  - DEAD: returns 503 on every request

This is a CPU-only mock; no GPU required.
"""
from __future__ import annotations
import argparse
import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--instance-id", type=str, default=None,
                    help="defaults to mock_<port>")
    ap.add_argument("--mode", type=str, default="normal",
                    choices=["normal", "slow", "fail_5xx", "timeout", "dead"],
                    help="Failure mode")
    ap.add_argument("--base-latency-ms", type=float, default=80,
                    help="Base inference latency in ms")
    ap.add_argument("--jitter-ms", type=float, default=40,
                    help="Random +/- jitter")
    ap.add_argument("--failure-rate", type=float, default=0.0,
                    help="Probability of returning 5xx (per request)")
    return ap.parse_args()


class MockVLLM:
    def __init__(self, args):
        self.port = args.port
        self.instance_id = args.instance_id or f"mock_{args.port}"
        self.mode = args.mode
        self.base_latency = args.base_latency_ms / 1000.0
        self.jitter = args.jitter_ms / 1000.0
        self.failure_rate = args.failure_rate
        self.request_count = 0
        self.fail_count = 0
        # running state
        self.running = random.randint(0, 5)
        self.waiting = random.randint(0, 8)

    def metrics_text(self) -> str:
        """Return Prometheus-format metrics that vllm would expose."""
        # vllm uses gauge/counter names like vllm:num_requests_running
        lines = [
            f'vllm:num_requests_running{{model_name="Qwen2.5-1.5B-Instruct"}} {self.running}',
            f'vllm:num_requests_waiting{{model_name="Qwen2.5-1.5B-Instruct"}} {self.waiting}',
            f'vllm:kv_cache_usage_perc{{model_name="Qwen2.5-1.5B-Instruct"}} {random.uniform(0.1, 0.7):.4f}',
            f'vllm:gpu_cache_usage_perc{{model_name="Qwen2.5-1.5B-Instruct"}} {random.uniform(0.1, 0.7):.4f}',
            f'vllm:prefix_cache_hits_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(1000, 100000)}',
            f'vllm:prefix_cache_queries_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(1000, 100000)}',
            f'vllm:prompt_tokens_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(10000, 1000000)}',
            f'vllm:generation_tokens_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(10000, 1000000)}',
            f'vllm:request_success_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(100, 10000)}',
            f'vllm:num_preemptions_total{{model_name="Qwen2.5-1.5B-Instruct"}} {random.randint(0, 10)}',
            f'vllm:e2e_request_latency_seconds_bucket{{le="0.1"}} {random.randint(0, 100)}',
            f'vllm:e2e_request_latency_seconds_bucket{{le="0.5"}} {random.randint(100, 1000)}',
            f'vllm:e2e_request_latency_seconds_bucket{{le="1.0"}} {random.randint(1000, 5000)}',
            f'vllm:e2e_request_latency_seconds_bucket{{le="+Inf"}} 10000',
            f'vllm:time_to_first_token_seconds_bucket{{le="0.05"}} {random.randint(100, 1000)}',
            f'vllm:time_to_first_token_seconds_bucket{{le="0.1"}} {random.randint(1000, 5000)}',
            f'vllm:time_to_first_token_seconds_bucket{{le="+Inf"}} 10000',
        ]
        return "\n".join(lines) + "\n"

    def handle_chat(self, body: dict) -> tuple:
        """Return (status_code, body_dict, streaming_chunks_or_None)."""
        self.request_count += 1
        # Apply failure modes
        if self.mode == "dead":
            return (503, {"error": "service unavailable"}, None)
        if self.mode == "fail_5xx" or random.random() < self.failure_rate:
            self.fail_count += 1
            return (500, {"error": "internal mock error"}, None)
        if self.mode == "timeout":
            time.sleep(120)  # hang
            return (200, {}, None)

        # Increment running (simulate queue)
        self.running += 1
        try:
            streaming = body.get("stream", False)
            max_tokens = body.get("max_tokens", 32)
            # Latency
            latency = self.base_latency + random.uniform(-self.jitter, self.jitter)
            latency = max(0.01, latency)
            time.sleep(latency)
            # Build response
            n_chunks = min(max_tokens, random.randint(5, max_tokens))
            content = " ".join([f"tok{i}" for i in range(n_chunks)])
            if streaming:
                chunks = []
                for i in range(n_chunks):
                    chunks.append({
                        "id": f"chatcmpl-{self.port}-{self.request_count}",
                        "object": "chat.completion.chunk",
                        "choices": [{"delta": {"content": f"tok{i} "}, "index": 0}],
                    })
                chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                return (200, chunks, "streaming")
            else:
                return (200, {
                    "id": f"chatcmpl-{self.port}-{self.request_count}",
                    "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": n_chunks,
                              "total_tokens": 50 + n_chunks},
                }, None)
        finally:
            self.running = max(0, self.running - 1)
            # Slowly leak waiting into running
            if self.waiting > 0 and random.random() < 0.3:
                self.waiting -= 1
                self.running += 1


def make_handler(mock: MockVLLM):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress logs

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"")
            elif self.path == "/metrics":
                body = mock.metrics_text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/v1/models"):
                body = json.dumps({"data": [{"id": "Qwen2.5-1.5B-Instruct"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/v1/chat/completions":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except Exception:
                    body = {}
                status, resp, streaming = mock.handle_chat(body)
                if streaming == "streaming":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in resp:
                        line = f"data: {json.dumps(chunk)}\n\n".encode()
                        self.wfile.write(line)
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    body_bytes = json.dumps(resp).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def main():
    args = parse_args()
    mock = MockVLLM(args)
    handler = make_handler(mock)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"[mock-vllm] {args.instance_id} mode={args.mode} on :{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()