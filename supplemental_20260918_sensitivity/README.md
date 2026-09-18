# Sensitivity supplement (2026-09-18)

Isolated fork of the frozen 4xT4 live harness. Does not modify
`experiments/4t4/` or `analysis/formal4t4/`.

## Figure

`figures/fig_sensitivity_two_panel.pdf`

- (a) C0 x theta heatmap of paired Adaptive TTFT saving vs StaticSemantic at rho=1.2
- (b) single-length reusable context {1024,2048,4096,8192} with RateFIFO / StaticSemantic / Adaptive

## Run

```bash
cd /home/byh/B02
supplemental_20260918_sensitivity/net/setup_net.sh --rebuild
experiments/4t4/restart_4t4.sh 0.40 6144
# KV_CACHE_TOKENS=...
supplemental_20260918_sensitivity/run_smoke.sh "$KV"
supplemental_20260918_sensitivity/run_panel_a.sh "$KV"
supplemental_20260918_sensitivity/run_panel_b.sh "$KV" 1024
supplemental_20260918_sensitivity/run_panel_b.sh "$KV" 2048
supplemental_20260918_sensitivity/run_panel_b.sh "$KV" 4096
experiments/4t4/restart_4t4.sh 0.40 9216
supplemental_20260918_sensitivity/run_panel_b.sh "$KV8" 8192 panel_b_L8192
poc/.venv/bin/python supplemental_20260918_sensitivity/analyze_sensitivity.py
```
