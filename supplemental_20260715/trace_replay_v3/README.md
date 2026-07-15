# Same-trace replay v2

`frozen` rows are dispatcher-only fixed-snapshot evidence. `closed_loop` rows send actual requests to four T4 vLLM endpoints. Internal vLLM cache counters are unavailable; observed reuse is tracked at the dispatcher.
