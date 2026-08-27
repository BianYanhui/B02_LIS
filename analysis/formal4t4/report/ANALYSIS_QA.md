# Analysis QA note

Post-run visual review found that the initial Pareto plot used
`net_wire_bytes_sent`, which counts source-to-gateway ingress updates.  Since
that quantity includes updates subsequently suppressed by the gateway, it is
not the actual state volume on the constrained gateway-to-dispatcher path.

The analysis was corrected before delivery to use
`relay_forwarded * wire_bytes_per_msg_assumed` and the affected paired delta.
This is an analysis/visualization correction only: no formal configuration,
raw request/update telemetry, cell result, or statistical sample was changed
or rerun.
