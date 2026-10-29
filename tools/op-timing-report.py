#!/usr/bin/env python3
"""Aggregate an op-timing trace into a per-operation compile-time report.

Input format: docs/trace-format.md (schema ``op-timing``, version 1).  Nothing
about MLIR is assumed — any collector emitting those fields can be read here.
Chrome Trace stores no durations, self time or call counts, so all three are
derived by pairing events per thread and written as one aggregate document.

Usage:
    op-timing-report.py trace.json                  # writes trace.report.json
    op-timing-report.py trace.json -o base.json      # explicit output path
    op-timing-report.py trace.json --table           # also print the tables
    op-timing-report.py trace.json --baseline base.json  # compare, exit 1 on regression

Field definitions: docs/usage.md.  Reading the numbers: docs/measurement.md.
"""

import bisect
import json
import sys
from collections import defaultdict

# A scope row whose self time grows by more than this ratio is a regression.
REGRESSION_RATIO = 1.20
# Ignore rows below this self time when comparing baselines (us).
REGRESSION_FLOOR_US = 200.0

# Trace format version this script understands (docs/trace-format.md).
SUPPORTED_FORMAT_VERSION = 1


def load(path):
    """Read a trace file, returning only duration events.

    The leading `ph: "M"` metadata event carries the format version
    (docs/trace-format.md) and is consumed here instead of being handed to the
    pairing code, which needs `tid` / `ts` on every event.
    """
    with open(path) as handle:
        data = json.load(handle)
    events = data["traceEvents"] if isinstance(data, dict) else data
    result = []
    version = 1
    for event in events:
        if event.get("ph") == "M":
            if event.get("name") == "optiming_format":
                version = event.get("args", {}).get("version", 1)
            continue
        result.append(event)
    if version > SUPPORTED_FORMAT_VERSION:
        print(f"{path}: trace format version {version} is newer than the "
              f"supported {SUPPORTED_FORMAT_VERSION}; fields may be ignored")
    return result


def scope_name(event):
    """Human name of a trace scope.

    Structured ``args.pass`` first (see docs/trace-format.md), free-form
    ``args.desc`` as a fallback so older traces keep working.
    """
    name = event["name"]
    args = event.get("args", {})
    desc = args.get("desc", "")
    if name == "apply-pattern":
        if "pattern:" in desc:
            return desc.split("pattern:", 1)[1].strip().replace(
                "(anonymous namespace)::", "")
        return "apply-pattern"
    if name == "pass-execution":
        label = args.get("pass", "")
        if not label and "running `" in desc:
            label = desc.split("running `", 1)[1].split("`", 1)[0]
        if not label:
            return "pass-execution"
        # The nested-pipeline adaptor has no useful name of its own.
        if label.endswith("OpToOpPassAdaptor"):
            return "pass: <nested pipeline>"
        return "pass: " + label
    if name == "GreedyPatternRewriteIteration":
        return "greedy-iteration"
    return name


def pair(events):
    """Pair B/E events per thread and derive inclusive and self durations (us)."""
    stacks = defaultdict(list)
    records = []
    for event in events:
        thread = event["tid"]
        stack = stacks[thread]
        # A duration event ("X") is self contained; B/E events nest.
        if event.get("ph") == "X":
            records.append({
                "scope": scope_name(event),
                "op": event.get("args", {}).get("op", ""),
                "sym": event.get("args", {}).get("sym", ""),
                "loc": event.get("args", {}).get("loc", ""),
                "depth": 0,
                "tid": thread,
                "start": event["ts"],
                "dur": event.get("dur", 0.0),
                "self": event.get("dur", 0.0),
            })
            continue
        if event["ph"] == "B":
            args = event.get("args", {})
            stack.append({
                "scope": scope_name(event),
                "op": args.get("op", ""),
                "sym": args.get("sym", ""),
                "loc": args.get("loc", ""),
                "arg": args.get("arg", ""),
                "depth": len(stack),
                "tid": thread,
                "start": event["ts"],
                "child": 0.0,
            })
            continue
        if event["ph"] != "E" or not stack:
            continue
        frame = stack.pop()
        duration = event["ts"] - frame["start"]
        frame["dur"] = duration
        frame["self"] = max(duration - frame["child"], 0.0)
        frame["end"] = event["ts"]
        records.append(frame)
        if stack:
            stack[-1]["child"] += duration
    return records


def framework_time(records, events):
    """Time spent outside every action span, attributed to the pass that ended.

    Taken as the gap between the ``E`` of a pass action and the next event on
    the same thread; in an MLIR host that gap is the after-pass verifier plus
    analysis invalidation (docs/measurement.md).  Returns (per scope, total) in
    microseconds.
    """
    trace_ts = defaultdict(list)
    for event in events:
        trace_ts[event["tid"]].append(event["ts"])
    for timestamps in trace_ts.values():
        timestamps.sort()

    per_scope = defaultdict(float)
    total = 0.0
    for record in records:
        if not record["scope"].startswith("pass: "):
            continue
        timestamps = trace_ts.get(record["tid"], [])
        index = bisect.bisect_right(timestamps, record["end"])
        if index >= len(timestamps):
            continue
        gap = timestamps[index] - record["end"]
        per_scope[record["scope"]] += gap
        total += gap
    return per_scope, total


def aggregate(records, key_fields):
    table = defaultdict(lambda: {"count": 0, "self": 0.0, "incl": 0.0, "worst": 0.0})
    for record in records:
        key = tuple(record[field] for field in key_fields)
        row = table[key]
        row["count"] += 1
        row["self"] += record["self"]
        row["incl"] += record["dur"]
        row["worst"] = max(row["worst"], record["self"])
    return table


def print_table(title, table, key_names, top, denominator_us, gaps=None):
    rows = sorted(table.items(), key=lambda item: -item[1]["self"])[:top]
    print(f"\n---- {title} ----")
    header = (f"{'count':>7} {'self ms':>9} {'wall %':>7} {'incl ms':>9} "
              f"{'avg us':>9} {'max us':>9}  " + "  ".join(key_names))
    if gaps is not None:
        header += "  outside ms"
    print(header)
    for key, row in rows:
        avg = row["self"] / row["count"] if row["count"] else 0.0
        percent = 100.0 * row["self"] / denominator_us if denominator_us else 0.0
        fields = "  ".join(str(part) if part else "-" for part in key)
        line = (f"{row['count']:>7} {row['self'] / 1000:>9.2f} {percent:>7.2f} "
                f"{row['incl'] / 1000:>9.2f} {avg:>9.1f} {row['worst']:>9.1f}  {fields}")
        if gaps is not None:
            line += f"  {gaps.get(key[0], 0.0) / 1000:>10.2f}"
        print(line)
    return rows


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    top = 20
    out_path = path[:-5] + ".report.json" if path.endswith(".json") else path + ".report.json"
    baseline_path = None
    want_table = False
    index = 2
    while index < len(argv):
        if argv[index] == "--top":
            top = int(argv[index + 1])
            index += 2
        elif argv[index] in ("-o", "--out", "--json"):
            out_path = argv[index + 1]
            index += 2
        elif argv[index] == "--baseline":
            baseline_path = argv[index + 1]
            index += 2
        elif argv[index] == "--table":
            want_table = True
            index += 1
        else:
            print(f"unknown argument: {argv[index]}")
            return 2

    events = load(path)
    records = pair(events)
    if not records:
        print(f"{path}: no complete B/E pairs")
        return 1

    threads = {event["tid"] for event in events}
    wall = max(record["start"] + record["dur"] for record in records) - min(
        record["start"] for record in records)
    accounted = sum(record["self"] for record in records)

    # Per operation: only rewrite actions are aggregated here, because their IR
    # unit is the rewritten operation.  `self` never double counts nested
    # scopes; `incl` does and is informational only.
    rewrite_records = [record for record in records
                       if not record["scope"].startswith("pass: ")]
    op_table = aggregate(rewrite_records, ("op", "sym", "loc"))
    op_table.pop(("", "", ""), None)

    # Per scope x operation and per pass: the pass / pattern view.
    scope_table = aggregate(records, ("scope", "op", "loc"))
    pass_records = [record for record in records
                    if record["scope"].startswith("pass: ")
                    or record["scope"] == "greedy-iteration"]
    pass_table = aggregate(pass_records, ("scope",))

    # Time outside every action span (verifier + analysis invalidation),
    # attributed to the pass that just finished.
    scope_gap, framework = framework_time(records, events)

    # Pipeline option name per scope ("canonicalize", "cse", ...), so a pass row
    # can be mapped back to a command line flag.
    scope_args = {}
    for record in records:
        if record["arg"] and record["scope"] not in scope_args:
            scope_args[record["scope"]] = record["arg"]

    def rows(table, key_names, gaps=None):
        result = []
        for key, row in sorted(table.items(), key=lambda item: -item[1]["self"]):
            entry = {**dict(zip(key_names, key)), **row}
            if gaps is not None:
                entry["outside"] = gaps.get(key[0], 0.0)
                entry["arg"] = scope_args.get(key[0], "")
            result.append(entry)
        return result

    payload = {
        "schema_version": 1,
        "trace": path,
        "wall_ms": wall / 1000,
        "inside_actions_ms": accounted / 1000,
        "outside_actions_ms": framework / 1000,
        "threads": len(threads),
        "unit": ("us (self, incl, worst, outside) / "
                 "ms (wall_ms, inside_actions_ms, outside_actions_ms)"),
        "per_operation": rows(op_table, ("op", "sym", "loc")),
        "per_scope": rows(scope_table, ("scope", "op", "loc")),
        "per_pass": rows(pass_table, ("scope",), scope_gap),
    }
    with open(out_path, "w") as handle:
        json.dump(payload, handle, indent=1)

    print(f"{len(records)} scopes -> {out_path} "
          f"({len(payload['per_operation'])} ops, {len(payload['per_scope'])} "
          f"scope x op, {len(payload['per_pass'])} passes)")
    print(f"wall {wall / 1000:.2f} ms, inside actions {accounted / 1000:.2f} ms "
          f"({100.0 * accounted / wall:.1f}%), outside actions "
          f"{framework / 1000:.2f} ms ({100.0 * framework / wall:.1f}%)")
    # `--verify-each` (default on) is what lives outside the actions; make the
    # measured amount visible instead of leaving it inside the wall clock.
    print("  outside = verifier + analysis invalidation, per pass in "
          "per_pass[].outside; --verify-each=false drops it")
    if len(threads) > 1:
        print("note: with several threads percentages of wall are not summable; "
              "run with -mlir-disable-threading for additive numbers")

    if want_table:
        print_table("per pass", pass_table, ["scope"], top, wall, scope_gap)
        print_table("per operation, rewrite actions only (self time)", op_table,
                    ["op", "sym", "loc"], top, wall)
        print_table("per scope x operation", scope_table,
                    ["scope", "op", "loc"], top, wall)

    if baseline_path:
        regressions = compare(payload, baseline_path)
        if regressions:
            return 1
    return 0


def compare(payload, baseline_path):
    """Report per-operation self times that grew since the baseline."""
    with open(baseline_path) as handle:
        baseline = json.load(handle)
    previous = {(row["op"], row["sym"], row["loc"]): row
                for row in baseline.get("per_operation", [])}
    regressions = []
    for row in payload["per_operation"]:
        old = previous.get((row["op"], row["sym"], row["loc"]))
        if old is None or old["self"] < REGRESSION_FLOOR_US:
            continue
        if row["self"] > old["self"] * REGRESSION_RATIO:
            regressions.append((row, old))
    if not regressions:
        print(f"no regression above {REGRESSION_RATIO}x vs {baseline_path}")
        return []
    for row, old in regressions:
        print(f"REGRESSION {row['op']} {row['loc'] or '-'}: "
              f"{old['self'] / 1000:.2f} ms -> {row['self'] / 1000:.2f} ms "
              f"({row['self'] / old['self']:.2f}x)")
    return regressions


if __name__ == "__main__":
    sys.exit(main(sys.argv))
