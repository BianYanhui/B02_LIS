# Control-plane delay-anchor supplement (2026-09-20)

Independent variable is logical worker fan-in `N` against control-plane ingest μ
(frames/s), not HTB kbps. Four Tesla T4s still produce live TTFT. Extra metadata
rides the existing four TCP agents. Dispatcher routing stays on instances 0-3.

- HTB parent is 100 Mbit (not the IV).
- netem 40ms ± 5ms on the signaling class (regional RTT stand-in).
- Gateway drain token-bucket is μ (default 200 frames/s).
- Fan-in: heartbeats of advertised prefixes plus unique `__fanin_*` clutter.

Frozen `experiments/4t4/` and `analysis/formal4t4/` are read-only.
