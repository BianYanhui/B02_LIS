"""Tier 0: Preflight — verify all 8 instances healthy, generation works.

Saves to results/preflight.json
"""
from __future__ import annotations
import json, time, sys, os
import requests

sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
sys.path.insert(0, os.path.expanduser("~/B02/tradeoff_experiments/scripts"))
from workloads import MODEL_ID

OUT = os.path.expanduser("~/B02/full_experiment/results")
N = 8
URLS = [f"http://127.0.0.1:{8000+i}" for i in range(N)]


def main():
    out = {"tier0": {}, "start_ts": time.time()}
    print(f"[Tier0] checking /health on {N} instances")
    health_ok = []
    for i, url in enumerate(URLS):
        try:
            r = requests.get(f"{url}/health", timeout=5)
            health_ok.append(r.status_code == 200)
            out["tier0"][f"instance_{i}"] = {
                "url": url, "health": r.status_code == 200,
                "status_code": r.status_code,
            }
            print(f"  instance_{i} {url}: {'OK' if r.status_code == 200 else 'FAIL'}")
        except Exception as e:
            out["tier0"][f"instance_{i}"] = {"url": url, "error": str(e)}
            health_ok.append(False)
            print(f"  instance_{i} {url}: ERROR {e}")
    out["all_healthy"] = all(health_ok)

    if not all(health_ok):
        out["abort_reason"] = "some instances unhealthy"
        print("[ABORT]", out["abort_reason"])
        with open(f"{OUT}/preflight.json", "w") as f:
            json.dump(out, f, indent=2)
        sys.exit(1)

    print(f"[Tier0] all healthy, probing each with 1 generation call")
    gen_ok = []
    test_prompt = [{"role": "user", "content": "Reply in 3 words: ping OK"}]
    for i, url in enumerate(URLS):
        try:
            r = requests.post(
                f"{url}/v1/chat/completions",
                json={"model": MODEL_ID, "messages": test_prompt,
                      "max_tokens": 16, "temperature": 0.0},
                timeout=30,
            )
            j = r.json()
            out["tier0"][f"instance_{i}"]["gen_status"] = r.status_code
            out["tier0"][f"instance_{i}"]["gen_ok"] = (
                r.status_code == 200 and "choices" in j
            )
            out["tier0"][f"instance_{i}"]["gen_sample"] = (
                j.get("choices", [{}])[0].get("message", {}).get("content", "")[:60]
            )
            gen_ok.append(out["tier0"][f"instance_{i}"]["gen_ok"])
            print(f"  instance_{i} gen: {out['tier0'][f'instance_{i}']['gen_sample']!r}")
        except Exception as e:
            out["tier0"][f"instance_{i}"]["gen_error"] = str(e)
            gen_ok.append(False)
            print(f"  instance_{i} gen ERROR: {e}")
    out["all_gen_ok"] = all(gen_ok)
    out["end_ts"] = time.time()
    out["duration_s"] = out["end_ts"] - out["start_ts"]
    with open(f"{OUT}/preflight.json", "w") as f:
        json.dump(out, f, indent=2)
    if not all(gen_ok):
        print("[WARN] some generations failed; check instance logs")
        sys.exit(1)
    print(f"[Tier0] PASS in {out['duration_s']:.1f}s")


if __name__ == "__main__":
    main()