#!/usr/bin/env python3
"""Message delivery report over a collect directory.

  msg_report.py <dir>

Reads what docker/entrypoint-module.sh leaves per node:
  <node>.send.jsonl  one line per send call: {"t": ns, "payload": "<node>-<n>", "reply": {...}}
  <node>.msg.jsonl   every delivery_module event the node saw

A message is named by its payload. The sender's reply gives the request id its
own messagePropagated / messageSent / messageError events carry; a receiver's
messageReceived carries the payload back. Every node with a .msg.jsonl other
than the sender is expected to receive every message.

What a sender's messages should come to is its group's `expect` in the manifest
(plan.json): "delivered" (the default) -- accepted, no error, received by every
other node; "error" -- every send refused or ended in messageError, and none
received anywhere; "any" -- not judged. A send still without an outcome when
the run ended (made in its last IN_FLIGHT_S seconds) is not judged either.

Exit status 0 when every judged message came to what was expected; 1 otherwise.
"""
import base64
import glob
import json
import os
import sys
from collections import Counter, defaultdict


IN_FLIGHT_S = 60
# Memory slopes are taken from this many seconds after T0 on: the start-up
# (memberships, discovery, first adverts) is not part of a node's steady growth.
WARMUP_S = 1200


def expectations(d):
    """node -> its group's `expect`, from the collect's plan.json."""
    try:
        with open(os.path.join(d, "plan.json")) as f:
            return {s: g.get("expect", "delivered") for g in json.load(f) for s in g["services"]}
    except (OSError, ValueError):
        return {}


def lines(path):
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("{"):
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


def payload_text(arg):
    """messageReceived carries the payload as {"_bytes": <base64, unpadded>}."""
    b = arg.get("_bytes", "") if isinstance(arg, dict) else ""
    try:
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode(errors="replace")
    except ValueError:
        return ""


def ms(ns):
    return f"{ns / 1e6:.0f}"


def spread(values):
    if not values:
        return "-"
    v = sorted(values)
    return f"min {ms(v[0])} / median {ms(v[len(v) // 2])} / max {ms(v[-1])} ms"


def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * q))] / 1e6 if v else float("nan")


def dist(counts):
    """min / median / max and the coefficient of variation of a load spread."""
    v = sorted(counts)
    if not v:
        return "-"
    mean = sum(v) / len(v)
    cv = (sum((x - mean) ** 2 for x in v) / len(v)) ** 0.5 / mean if mean else 0
    return f"min {v[0]} / median {v[len(v) // 2]} / max {v[-1]}, cv {cv:.2f}"


def slope(points):
    """Least-squares slope of [(x, y)], per unit of x."""
    if len(points) < 2:
        return 0.0
    mx = sum(x for x, _ in points) / len(points)
    my = sum(y for _, y in points) / len(points)
    den = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else 0.0


def long_run(d, sends, own, rx, hashes, receivers, to_prop, received):
    """What a long run adds: percentiles, the run over time, end-to-end latency,
    mix routes (mixlog.txt), per-node mix load (q_mixload.json), resources."""
    def full(s):
        return all(s[1] in received[r] for r in receivers if r != s[0])

    def prop(s):
        ev = own[s[0]].get(s[3], {}) if s[3] else {}
        return ev["messagePropagated"][0] - s[2] if "messagePropagated" in ev else None

    print(f"latency send->propagated: p50 {pct(to_prop, .5):.0f} / p90 {pct(to_prop, .9):.0f} "
          f"/ p99 {pct(to_prop, .99):.0f} ms")
    e2e = [rx[r][p] - t for _, p, t, _, _ in sends for r in receivers if p in rx[r]]
    if e2e:
        print(f"latency send->received (per receiver, local clocks): p50 {pct(e2e, .5):.0f} "
              f"/ p90 {pct(e2e, .9):.0f} / p99 {pct(e2e, .99):.0f} ms over {len(e2e)} receptions")

    t0 = min(s[2] for s in sends)
    span = max(s[2] for s in sends) - t0
    width = max(60e9, span / 12)
    buckets = defaultdict(lambda: [0, 0, 0, []])  # sends, propagated, errors, latencies
    for sender, _, t, rid, _ in sends:
        b = buckets[int((t - t0) // width)]
        ev = own[sender].get(rid, {}) if rid else {}
        b[0] += 1
        b[2] += "messageError" in ev
        if "messagePropagated" in ev:
            b[1] += 1
            b[3].append(ev["messagePropagated"][0] - t)
    print(f"over time, from the first send, {width / 60e9:.0f} min steps:")
    for i in sorted(buckets):
        n, ok, err, lat = buckets[i]
        print(f"  +{i * width / 60e9:4.0f} min: {n:5d} sends, propagated {100 * ok / n:5.1f}%, "
              f"errors {err:4d}, p50 {pct(lat, .5):6.0f} / p90 {pct(lat, .9):6.0f} ms")

    tries = {}
    path = os.path.join(d, "mixlog.txt")
    if os.path.exists(path):
        by_hash = {hashes.get((s[0], s[3])): s for s in sends}
        by_hash.pop(None, None)
        sent_hash = set(by_hash)
        exits, handled = Counter(), Counter()
        exited = defaultdict(set)  # exit node -> hashes it published
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 3 and parts[1] == "exit" and parts[2] in sent_hash:
                    exits[parts[0]] += 1
                    handled[parts[2]] += 1
                    exited[parts[0]].add(parts[2])
                elif len(parts) == 4 and parts[1] == "try":
                    tries[parts[2]] = max(tries.get(parts[2], 0), int(parts[3]))
        print(f"mix exits: {sum(exits.values())} lightpush requests for {len(handled)}/{len(sent_hash)} "
              f"messages at {len(exits)} nodes; per exit node: {dist(list(exits.values()))}")
        print("  busiest: " + ", ".join(f"{n} {c}" for n, c in exits.most_common(5))
              + "; quietest: " + ", ".join(f"{n} {c}" for n, c in exits.most_common()[-3:]))
        more = sum(1 for c in handled.values() if c > 1)
        print(f"  messages that reached more than one exit: {more}")
        tc = Counter(tries.values())
        print("  tries per message: " + ", ".join(f"{k} x{v}" for k, v in sorted(tc.items())))
        print("  per exit node: messages exited, share of them that reached all receivers, "
              "p50 send->propagated / send->received ms")
        for n, hs in sorted(exited.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            ss = [by_hash[h] for h in hs]
            lat = [x for x in map(prop, ss) if x is not None]
            e2e = [rx[r][s[1]] - s[2] for s in ss for r in receivers
                   if r != s[0] and s[1] in rx[r]]
            print(f"    {n:<10} {len(ss):5d}  {100 * sum(map(full, ss)) / len(ss):5.1f}%  "
                  f"p50 {pct(lat, .5):5.0f} / {pct(e2e, .5):5.0f}")

    # Fixed 10-minute windows by send time, counted from T0 when the collect
    # has it, so they line up with what happened at known offsets.
    try:
        with open(os.path.join(d, "t0")) as f:
            base, label = int(f.read().strip()) * 10**9, "T0"
    except (OSError, ValueError):
        base, label = t0, "first send"
    win = defaultdict(list)
    for s in sends:
        win[int((s[2] - base) // 600e9)].append(s)
    print(f"10-minute windows by send time, from {label}: sends, reached all receivers, "
          "errors, p50 / p90 send->propagated ms, mean tries")
    shares = []
    for i in sorted(win):
        ss = win[i]
        share = 100 * sum(map(full, ss)) / len(ss)
        shares.append(share)
        lat = [x for x in map(prop, ss) if x is not None]
        err = sum("messageError" in (own[s[0]].get(s[3], {}) if s[3] else {}) for s in ss)
        tr = [tries[h] for h in (hashes.get((s[0], s[3])) for s in ss) if h in tries]
        print(f"  {label}+{i * 10:3d}-{i * 10 + 10:3d} min: {len(ss):4d} sends, {share:5.1f}%, "
              f"errors {err:3d}, p50 {pct(lat, .5):5.0f} / p90 {pct(lat, .9):5.0f}, "
              f"tries {sum(tr) / len(tr) if tr else float('nan'):.2f}"
              + ("   < 97%" if share < 97 else ""))
    overall = 100 * sum(map(full, sends)) / len(sends)
    print(f"long-run criteria: reached all receivers {overall:.2f}% (target 99%), lowest "
          f"window {min(shares):.1f}% (target 97%): "
          + ("met" if overall >= 99 and min(shares) >= 97 else "NOT met"))

    path = os.path.join(d, "q_mixload.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                series = json.load(f)["data"]["result"]
        except (ValueError, KeyError):
            series = []
        load = defaultdict(dict)
        for r in series:
            m = r["metric"]
            # Split by the hop role of the mix counters (Entry, Intermediate, Exit, Reply).
            name = m["__name__"] + (f"{{type={m['type']}}}" if "type" in m else "")
            load[name][m.get("node", "?")] = load[name].get(m.get("node", "?"), 0) + float(r["value"][1])
        for name in sorted(load):
            v = [int(x) for x in load[name].values()]
            top = max(load[name].items(), key=lambda kv: kv[1])
            print(f"{name}: total {sum(v)} over {len(v)} nodes; per node: {dist(v)}; "
                  f"busiest {top[0]}")

    path = os.path.join(d, "stats.tsv")
    if os.path.exists(path):
        mem, cpu = defaultdict(float), defaultdict(list)
        total = defaultdict(float)
        samples = defaultdict(list)  # container -> [(s from T0, MiB)]
        with open(path) as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) != 4:
                    continue
                used = p[3].split("/")[0].strip()
                mib = float(used[:-3]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(used[-3:], 0)
                mem[p[1]] = max(mem[p[1]], mib)
                total[p[0]] += mib
                cpu[p[0]].append(float(p[2].rstrip("%") or 0))
                samples[p[1]].append((int(p[0]), mib))
        if total:
            cpus = [sum(v) for v in cpu.values()]
            print(f"resources: {len(mem)} containers, memory in use peak {max(total.values()) / 1024:.1f} GiB; "
                  f"CPU all containers mean {sum(cpus) / len(cpus):.0f}% / peak {max(cpus):.0f}% (100% = one core)")
            top = sorted(mem.items(), key=lambda kv: -kv[1])[:3]
            print("  largest: " + ", ".join(f"{n.split('-')[-2]} {m:.0f} MiB" for n, m in top))
            tot = sorted((int(k), v) for k, v in total.items())
            tot = [x for x in tot if x[0] >= WARMUP_S] or tot
            print(f"memory from T0+{tot[0][0]}s: all containers {tot[0][1] / 1024:.2f} -> "
                  f"{tot[-1][1] / 1024:.2f} GiB at T0+{tot[-1][0]}s, slope "
                  f"{slope(tot) * 3600:.0f} MiB/h; per container: first / last MiB, slope MiB/h")
            rows = []
            for n, v in samples.items():
                v = [x for x in v if x[0] >= WARMUP_S] or v
                rows.append((slope(v) * 3600, n.split('-')[-2], v[0][1], v[-1][1]))
            for sl, n, first, last in sorted(rows, reverse=True):
                print(f"  {n:<10} {first:6.0f} {last:6.0f}  {sl:6.1f}")


def main(d):
    node = lambda p, suffix: os.path.basename(p)[: -len(suffix)]
    received = defaultdict(Counter)  # node -> payload -> times
    sources = Counter()
    own = defaultdict(dict)  # sender -> request id -> {event: (ns, error)}
    rx = defaultdict(dict)  # node -> payload -> local ns of the first reception
    hashes = {}  # (sender, request id) -> message hash
    for path in sorted(glob.glob(os.path.join(d, "*.msg.jsonl"))):
        n = node(path, ".msg.jsonl")
        received[n]  # a node that saw no event is still a receiver
        for e in lines(path):
            a = e.get("data", {})
            ev = e.get("event")
            if ev == "messageReceived":
                text = payload_text(a.get("arg2"))
                received[n][text] += 1
                sources[a.get("arg3")] += 1
                if "rx" in e:
                    rx[n].setdefault(text, e["rx"])
            elif ev in ("messagePropagated", "messageSent"):
                own[n].setdefault(a.get("arg0"), {})[ev] = (a.get("arg2"), None)
                hashes[(n, a.get("arg0"))] = a.get("arg1")
            elif ev == "messageError":
                own[n].setdefault(a.get("arg0"), {})[ev] = (a.get("arg3"), a.get("arg2"))
                hashes[(n, a.get("arg0"))] = a.get("arg1")

    sends = []  # (sender, payload, t, request id or None, refusal)
    for path in sorted(glob.glob(os.path.join(d, "*.send.jsonl"))):
        n = node(path, ".send.jsonl")
        for s in lines(path):
            res = s.get("reply", {}).get("result") or {}
            ok = isinstance(res, dict) and res.get("success")
            sends.append((n, s["payload"], s["t"], res.get("value") if ok else None,
                          None if ok else json.dumps(s.get("reply"))[:200]))
    if not sends:
        print("no sends recorded (no *.send.jsonl): nothing to report")
        return 1

    # Sends without an outcome when the run ended: no event of their own, and
    # made within IN_FLIGHT_S of the last thing any node recorded.
    end = max([s[2] for s in sends] + [t for r in rx.values() for t in r.values()])
    in_flight = [s for s in sends if s[3] and not own[s[0]].get(s[3])
                 and end - s[2] < IN_FLIGHT_S * 1e9]
    sends = [s for s in sends if s not in in_flight]
    expect = expectations(d)

    receivers = sorted(received)
    print(f"senders: {', '.join(sorted({s[0] for s in sends}))}; "
          f"nodes with an event log: {len(receivers)}")
    print(f"messages: {len(sends)}"
          + (f" (and {len(in_flight)} in flight when the run ended, not judged)" if in_flight else ""))

    refused = [s for s in sends if s[3] is None]
    errors, to_prop, to_sent, undelivered = [], [], [], []
    n_prop = n_sent = n_full = 0
    missed_by = Counter()
    per = defaultdict(Counter)  # sender -> outcome counts
    unexpected = Counter()  # sender -> messages that did not come to its `expect`
    for sender, payload, t, rid, _ in sends:
        ev = own[sender].get(rid, {}) if rid else {}
        per[sender]["messages"] += 1
        per[sender]["sent"] += "messageSent" in ev
        per[sender]["error"] += "messageError" in ev
        per[sender]["delivered"] += all(
            payload in received[r] for r in receivers if r != sender)
        if "messageError" in ev:
            errors.append((payload, ev["messageError"][1]))
        if "messagePropagated" in ev:
            n_prop += 1
            to_prop.append(ev["messagePropagated"][0] - t)
        if "messageSent" in ev:
            n_sent += 1
            to_sent.append(ev["messageSent"][0] - t)
        missing = [r for r in receivers if r != sender and payload not in received[r]]
        want = expect.get(sender, "delivered")
        failed = rid is None or "messageError" in ev
        if want == "delivered":
            unexpected[sender] += failed or bool(missing)
        elif want == "error":
            unexpected[sender] += not failed or len(missing) < len(receivers) - (sender in receivers)
        if missing:
            undelivered.append((payload, missing))
            missed_by.update(missing)
        else:
            n_full += 1

    total = len(sends)
    print(f"  accepted by send():   {total - len(refused)}/{total}")
    print(f"  messagePropagated:    {n_prop}/{total}   send->propagated {spread(to_prop)}")
    print(f"  messageSent:          {n_sent}/{total}   send->sent {spread(to_sent)}"
          "   (needs a store node to validate against)")
    print(f"  messageError:         {len(errors)}/{total}")
    print(f"  reached all receivers: {n_full}/{total}")
    # Only the sent payloads: a node also reports what else it relays, such as
    # Mix RLN coordination frames.
    sent = {s[1] for s in sends}
    dup = sum(c - 1 for r in received.values() for p, c in r.items() if p in sent and c > 1)
    other = sum(c for r in received.values() for p, c in r.items() if p not in sent)
    print(f"  duplicate receptions: {dup}; by source: "
          + (", ".join(f"{k} {v}" for k, v in sorted(sources.items(), key=str)) or "-")
          + (f"; receptions of other messages: {other}" if other else ""))

    if len(per) > 1:
        for s, c in sorted(per.items()):
            print(f"  sender {s}: {c['messages']} messages, messageSent {c['sent']}, "
                  f"messageError {c['error']}, reached all receivers {c['delivered']}")

    for payload, reply in ((s[1], s[4]) for s in refused):
        print(f"REFUSED {payload[:24]}: {reply}")
    for reason, count in Counter(e[1] for e in errors).most_common():
        print(f"ERROR x{count}: {reason}")
    for payload, missing in undelivered[:20]:
        print(f"UNDELIVERED {payload[:24]}: missing at {len(missing)} "
              f"({', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''})")
    if len(undelivered) > 20:
        print(f"... and {len(undelivered) - 20} more undelivered")
    for r, count in missed_by.most_common(10):
        print(f"receiver {r} missed {count}/{total}")

    if total >= 50:
        long_run(d, sends, own, rx, hashes, receivers, to_prop, received)

    for s in sorted({s[0] for s in sends}):
        want = expect.get(s, "delivered")
        if want != "delivered" or unexpected[s]:
            print(f"expected of {s}: {want}" + ("" if want == "any" else
                  f"; {unexpected[s]} of {per[s]['messages']} messages came to something else"))
    ok = not sum(unexpected.values())
    print("verdict:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
