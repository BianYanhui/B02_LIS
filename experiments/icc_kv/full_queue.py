"""Finish the ICC matrix after the running 4-GPU end-to-end job.

Scale and burst use the same unique short noise and 4096-token prefixes as
the end-to-end run. Ablation uses that end-to-end path at ultrahigh and burst.
Does not start overnight.py and does not restart vLLM. Capacity stays 13000.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path("/home/byh/B02")
PY = ROOT / "poc/.venv/bin/python"
OUT = ROOT / "analysis/icc_kv"
E2E_PID = OUT / "e2e_full.pid"
E2E_SUMMARY = OUT / "e2e_full" / "e2e_summary.json"
LOG = OUT / "full_queue.log"
C = "13000"


def log(message: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with LOG.open("a") as handle:
        handle.write(line + "\n")


def pid_alive(pid: int) -> bool:
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return False
    text = cmdline.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    return "experiments.icc_kv.e2e" in text and "e2e_full" in text


def wait_for_e2e() -> None:
    pid = int(E2E_PID.read_text().strip())
    log(f"waiting for e2e_full pid {pid}")
    while pid_alive(pid):
        time.sleep(60)
    log(f"e2e_full pid {pid} has exited")


def run_stage(name: str, args: list[str]) -> None:
    log(f"start {name}: {' '.join(args)}")
    completed = subprocess.run(args, cwd=ROOT, check=False)
    log(f"done {name}: exit {completed.returncode}")


def e2e_args(scenarios: str, methods: str, out_dir: Path) -> list[str]:
    return [
        str(PY), "-u", "-m", "experiments.icc_kv.e2e",
        "--capacity", C,
        "--scenarios", scenarios,
        "--methods", methods,
        "--seeds", "5",
        "--requests", "350",
        "--concurrency", "4",
        "--kv-cache-tokens", "104544",
        "--out-dir", str(out_dir),
    ]


def main() -> None:
    log("full queue starting; C=13000; noise workload; vLLM is left running")
    wait_for_e2e()
    run_stage("e2e-resume", e2e_args(
        "near,high,xhigh,ultrahigh,burst",
        "FullSync,StaticSemantic,Adaptive,Ideal",
        OUT / "e2e_full",
    ))
    run_stage("e2e-normal", e2e_args(
        "normal",
        "FullSync,StaticSemantic,Adaptive,Ideal",
        OUT / "e2e_full",
    ))
    scale_methods = "FullSync,RateFIFO,StaticSemantic,Adaptive"
    run_stage("scale", [
        str(PY), "-u", "-m", "experiments.icc_kv.replay",
        "--capacity", C,
        "--rhos", "0.5,0.9,1.2,1.5,2.0",
        "--methods", scale_methods,
        "--seeds", "5",
        "--seconds", "120",
        "--out-dir", str(OUT / "scale_c13000"),
    ])
    run_stage("burst", [
        str(PY), "-u", "-m", "experiments.icc_kv.burst",
        "--capacity", C,
        "--methods", scale_methods,
        "--seeds", "5",
        "--out-dir", str(OUT / "burst_c13000"),
    ])
    run_stage("ablation", e2e_args(
        "ultrahigh,burst",
        "FullSync,StaticSemantic,AdaptiveNoPriority,Adaptive",
        OUT / "ablation_c13000",
    ))
    log("full queue finished")


if __name__ == "__main__":
    main()
