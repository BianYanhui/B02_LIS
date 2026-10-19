# F6-B1 routing effect

Three different counts are kept apart.

## A. Stale-positive affinity

The dispatcher still advertises worker 0 at 4096 while the capture has already recorded that worker's reusable coverage as gone. This is true at the controlled FIFO probe in all three formal seeds and in the pilot: `tomb_applied_before_probe = 0`, advertised coverage 4096, physical coverage 0. It is false at the controlled priority probe and at every natural probe, because the tombstone had already been applied and worker 0's entry was gone. Worker 1 remains at 4096 in all of those views.

## B and C. Decision-level invalid route

A causal invalid decision requires the live frozen `choose` to select worker 0, the index to still advertise that worker, the capture loss to be real, and a counterfactual `choose` with that entry removed to select someone else. The counterfactual is attribution. It is not a second request.

At load 0 the counterfactual selects worker 1 in every primary cell. The live call selects worker 0 only when the stale entry is still present. Native least-loaded would also be worker 0, but without the stale entry the guard moves the request to worker 1. The stale advertisement is what suppresses that override. Selecting worker 0 is therefore not "native happened to land on a dead worker" with the same choice either way.

Read-only probe, formal seeds, one `choose` per cell, offset 2.371638536453247 s after enqueue:

| Condition | FIFO | Priority |
| --- | ---: | ---: |
| Natural | 0 / 3 | 0 / 3 |
| Controlled | 3 / 3 | 0 / 3 |

The pilot official pair matches: controlled FIFO 1/1, controlled priority 0/1, natural 0/1 and 0/1. These probes did not send a prompt. Label: `READ_ONLY_ROUTING_DECISION`.

## Executed requests

Label: `EXECUTED_VLLM_REQUEST`. Four requests, each in its own rebuilt world. They are not the 12 relay cells, and they are not a FIFO-versus-priority serving pair. The stale world applies both upserts, resets worker 0, and calls `choose` before any tombstone. The cleared world applies the tombstone first. One request ends that world.

| World | Selected | Override | Advertised | `cached_tokens` | Prompt tokens | Client TTFT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pilot stale | 0 | no | 4096 | 0 | 4128 | 1946.73 ms |
| Pilot cleared | 1 | yes | 4096 | 4096 | 4128 | 1547.65 ms |
| 2026101920 stale | 0 | no | 4096 | 0 | 4128 | 1908.23 ms |
| 2026101920 cleared | 1 | yes | 4096 | 4096 | 4128 | 124.57 ms |

Loads were `[0,0,0,0]` and the engines were idle before every `choose`. HTTP 200. First streamed piece `unt`. Both stale executions are causal invalid routes with a real miss: the selected owner returned no cached tokens for a 4128-token prompt whose advertisement was 4096. Both cleared executions hit 4096 cached tokens on worker 1.

Owner-side validation was not called. That result is `NOT_MEASURED`. A cached-token miss is the prefill cost, not a wrong completion.

TTFT is not a Fig. 6(b) result. The two cleared hits are 1548 ms and 125 ms with the same cached-token count, so the delay is not stable, and these four requests were never run as a priority pair.

## What is not claimed

The decision-level rate is the routing result of the priority flag. The executed rows show that the stale decision and the cleared decision correspond to cached tokens 0 and 4096 when a request is actually sent. They do not by themselves measure how often priority changes an in-service request.
