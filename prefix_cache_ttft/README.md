# Isolated prefix-cache TTFT microbenchmark

This diagnostic uses one Qwen2.5-1.5B-Instruct vLLM instance only. It excludes dispatcher, gateway, `tc`, signaling, and multi-instance scheduling.

## Run

```bash
cd /home/byh/B02
GPU=0 PORT=8010 prefix_cache_ttft/run_prefix_cache_ttft.sh \\
  --prefix-lengths 512 1024 2048 4096 --repetitions 15
```

The dedicated launcher starts a new vLLM with prefix caching, prompt-token telemetry, `max-num-seqs=1`, and 12,288-token context. It refuses an occupied port and does not restart the formal shared-link cluster.

Every Hit first populates `P + suffix_A`, then measures `P + suffix_B`; every Miss has a new leading prefix and a different cache salt. Hit/Miss measurement order is fixed-seed randomized. The documented run uses 512, 1024, 2048, and 4096 content tokens; append `8192` to `--prefix-lengths` only when that extra point is required.

Before repetitions the script prints `Miss cached tokens` and `Hit cached tokens`, saves `sanity_check.json`, and aborts unless vLLM telemetry demonstrates a material Hit/Miss difference.

Outputs are under `analysis/prefix_cache_ttft/`: `raw_results.csv`, `summary.csv`, `paired_summary.csv`, `fig_prefix_cache_ttft.pdf`, `report.md`, `sanity_check.json`, `run_manifest.json`, and the dedicated server log/PID.
