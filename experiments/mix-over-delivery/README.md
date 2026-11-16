# Mix over delivery

Mix scenarios run with the harness against logos-delivery master `39b5f4ee` and
the logos-delivery#4647 stack (`69f3ff21`), for
logos-co/logos-delivery-module#183. This directory exists only on the branch
`experiment/mix-over-delivery`; the harness itself is on master.

- [`REPORT.html`](REPORT.html): state of master and the PR stack, what issue 183
  requires, the feature matrix, the harness setup and the recorded runs.
- [`manifests/`](manifests/): every recorded case, pinned to the logos-delivery
  it ran on.

## Running

Each manifest names its own build, image and duration:

```bash
./harness.sh suite --build experiments/mix-over-delivery/manifests/mix-pool-short.json \
  experiments/mix-over-delivery/manifests/mix-churn-pr4647.json
```

The static smokes on master (`examples/mix-static-smoke.json`,
`examples/mix-rln-smoke.json`) are part of the harness and belong to this set.

## Cases

"Recorded on" is the logos-delivery the manifest pins; the verdict is what that
build gives.

| Manifest | Nodes | Recorded on | Duration | Shows | Verdict |
|---|---|---|---|---|---|
| `mix-pool-short` | 7 | master `39b5f4ee` | 200 s | pool below 4: `Required` fails every send, `Preferred` falls back | PASS |
| `mix-oversize` | 11 | master `39b5f4ee` | 200 s | payload above the mix limit: `Required` fails, `Preferred` goes out in the clear | FAIL: the size cases behave as expected, but 4 smaller messages miss their exit node |
| `mix-churn` | 11 | master `39b5f4ee` | 400 s | 3 senders, a mix node stopped at 210 s | FAIL: a third of the sends report an error although delivered |
| `mix-disco-smoke` | 8 | master `39b5f4ee` | 28 min | pool fed by discovery only (usable after about 16 min) | not judged |
| `mix-static-smoke-pr4647` | 8 | #4647 stack | 240 s | the static smoke on the PR stack | PASS |
| `mix-churn-pr4647` | 11 | #4647 stack | 400 s | the churn case on the PR stack | PASS |
| `mixnet-46-master` | 46 | master `39b5f4ee` | 62 min | 25 mix nodes, 8 senders, 5,600 sends | FAIL: 1,150 messages miss their exit node |
| `mixnet-46-pr4647` | 46 | #4647 stack | 62 min | the same on the PR stack | PASS |
| `mixnet-46-pr4647-churn` | 46 | #4647 stack | 62 min | 5 of 25 mix nodes stopped at 1400 s | PASS |
| `mixnet-23-pr4647-rln` | 23 | #4647 stack + RLN | 62 min | Mix RLN: 12 module mix nodes, 4 senders | PASS |
| `mixnet-23-train4-rln` | 23 | fourth train `27b59e17` + RLN | 62 min | run 5: the `mixnet-23-pr4647-rln` network on what replaces the #4647 stack | FAIL: 2,750/2,750 propagated and confirmed, p50 1.0 s, but every message is missing at its own exit node |

The #4647 stack is obsolete (REPORT.html, section 05). `mixnet-23-train4-rln`
builds the tip of the fourth delivery train from the fork
`fcecin/logos-delivery` (PRs 401, 402, 403, 405, 406; 401 is upstream draft
#4729). Its `nimble.lock` pins nim-libp2p-mix `fe620629`, the 2026-11-06 head of
logos-co/nim-libp2p-mix#87, so the build has the mix stack #82, #85, #86, #87 as
it was then, not its current heads.

The `mixnet-*` manifests come from `lib/gen_mixnet.py`. In the recorded churn
run the five stopped nodes were brought back by hand at T0+2643 s:
`./harness.sh start mix21 mix22 mix23 mix24 mix25`.

## What to know when reading a result

- **Exit-node miss on master `39b5f4ee`**: a delivery-module node that mounts
  mix and is picked as the exit of a message gets no `messageReceived` for it.
  The #4647 stack delivers it.
- **False send errors after a mix node stops** on master `39b5f4ee`; addressed
  by the #4647 stack.
- **The PR stack refuses private addresses for mix**, so its mix nodes and
  senders set `mix-allow-all-addresses` (`--mix-allow-all-addresses=true`,
  `MIX_ALLOW_ALL=1`), an option master does not know.
- **A discovery-fed pool takes about 16.5 minutes to become usable**: mix is a
  node's second advertised service and registrars make it wait.
- **RLN scale**: on a 10-core docker VM, 22 RLN nodes with Mix RLN at 0.8
  messages per second fit; 45 at 1.6 per second saturate the host.
