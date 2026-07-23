# B02 AI Experiment Master Table

Workbook: `B02_AI_Experiment_Master_Table_20260715.xlsx`

Use `Evidence_Scorecard` first. It tells a paper-writing AI which claims are strong, which are conditional, and which sheets contain the supporting numbers.

Recommended experiment-section framing:

1. Metadata cost: Rich/exact state is 8.9x-22.3x larger than coarse.
2. Dispatch quality: coarse misses prefix affinity under locality; Sketch recovers much of the benefit.
3. Bounded K: top-K bounds metadata; K should be presented as an interface budget knob.
4. Freshness: event-driven updates and owner validation bound traffic and preserve correctness under stale metadata.
5. Scope: benefits depend on prefix locality and prefill cost; current T4 small-model TTFT is supporting, not definitive.
