#!/usr/bin/env python3
"""Aggregate an op-timing trace into a per-operation compile-time report.

Input: a Chrome Trace file written by OpTimingProfiler.h, specified in
docs/trace-format.md.  Nothing about MLIR is assumed here, so a collector for
any other compiler can be read as long as it emits the same fields.

Usage:
    op-timing-report.py trace.json

The script has no options.  Every run prints the three tables and writes two
artifacts beside the trace: ``<trace>.report.json`` for automated consumption
and ``<trace>.report.xlsx`` for reading.  Trace timestamps are microseconds;
every reported time is seconds.  Field definitions: docs/usage.md; reading the
numbers: docs/measurement.md.
"""

import json
import re
import sys
import zipfile
from xml.sax.saxutils import escape

# Trace timestamps are microseconds (docs/trace-format.md); every report value
# is converted to seconds at the serialization boundary.
US_PER_S = 1_000_000.0

# Rows printed per console table.
_TOP = 20


def read_events(path):
    """Yield trace events one at a time.

    The collector writes one event per line, so the file is streamed: a trace
    with millions of events needs no multi-gigabyte ``json.load``.  Array
    framing and ``,`` separators are skipped.  A file that holds the whole
    array on one line (``json.dump`` of a list) is read as a document instead.
    """
    produced = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            start = line.find("{")
            if start < 0:
                continue
            end = line.rfind("}")
            if end <= start:
                continue
            try:
                event = json.loads(line[start:end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                produced += 1
                yield event
    if produced:
        return
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # An empty file holds no events; anything else is malformed. Either way
        # the caller reports that no complete intervals were found.
        return
    events = data["traceEvents"] if isinstance(data, dict) else data
    for event in events if isinstance(events, list) else ():
        if isinstance(event, dict):
            yield event


def scope_name(event):
    """Human name of a trace scope.

    Structured ``args.pass`` first (docs/trace-format.md), free-form
    ``args.desc`` as a fallback so older traces keep working.
    """
    name = event.get("name", "")
    args = event.get("args", {})
    desc = args.get("desc", "")
    if name == "apply-pattern":
        if "pattern:" in desc:
            pname = desc.split("pattern:", 1)[1].strip()
            if pname:
                return strip_namespace(pname)
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


_ANONYMOUS = re.compile(r"(?:\(anonymous namespace\)|\{anonymous\})::")
_TEMPLATE = re.compile(r"<[^<>]*>")


def strip_namespace(name):
    """Drop the anonymous-namespace marker a C++ pattern name carries.

    Real traces spell it ``{anonymous}``; ``(anonymous namespace)`` is the
    other form MLIR prints.  Only the marker goes: the enclosing namespace
    still tells which dialect or library owns the pattern.
    """
    return _ANONYMOUS.sub("", name)


def short_name(name):
    """Last identifier of a qualified C++ name, without template arguments.

    ``mlir::surge::{anonymous}::AdjustSubFuncArguments`` becomes
    ``AdjustSubFuncArguments``; the full name stays in the report, this is what
    makes a 39-character average column scannable.
    """
    text = name[6:] if name.startswith("pass: ") else name
    text = strip_namespace(text)
    while True:
        stripped = _TEMPLATE.sub("", text)
        if stripped == text:
            break
        text = stripped
    base = text.rsplit("::", 1)[-1].strip()
    if base:
        return base
    if name.startswith("pass: "):
        return name[6:].strip() or name
    return name


def scope_kind(scope):
    """Which kind of action a per-scope row describes."""
    if scope == "greedy-iteration":
        return "贪心"
    if scope == "pass: <nested pipeline>":
        return "嵌套管线汇总"
    if scope.startswith("pass: "):
        return "Pass"
    return "Pattern"


def split_loc(loc):
    """``file:line:column`` to (file, line); a symbolic loc has no line."""
    if not loc:
        return "", ""
    parts = loc.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0], parts[1]
    return loc, ""


def _loc_file(loc):
    return (split_loc(loc)[0] or "-", TEXT)


def _loc_line(loc):
    """Line number as a number, so the column sorts numerically."""
    line = split_loc(loc)[1]
    return (int(line), INT) if line.isdigit() else ("-", TEXT)


def _frame(event, tid):
    args = event.get("args", {})
    return {
        "tag": event.get("name", ""),
        "scope": scope_name(event),
        "op": args.get("op", ""),
        "sym": args.get("sym", ""),
        "loc": args.get("loc", ""),
        "arg": args.get("arg", ""),
        "tid": tid,
        "start": event.get("ts", 0.0),
        "child": 0.0,
    }


def _account(table, key, self_us, incl_us):
    row = table.get(key)
    if row is None:
        row = table[key] = {"count": 0, "self": 0.0, "incl": 0.0, "worst": 0.0}
    row["count"] += 1
    row["self"] += self_us
    row["incl"] += incl_us
    if self_us > row["worst"]:
        row["worst"] = self_us


def analyse(path):
    """Read the trace once, pairing events and aggregating as it goes.

    Nothing per-event is retained: the tables, the per-pass gaps and the
    uncovered-time segments are the only state, so memory is bounded by the
    number of distinct rows rather than by the event count.

    Coverage comes from the nesting depth of the event stream: between two
    events a thread is either inside an action or not, so the intervals with no
    action on the clock can be recorded without keeping every interval.
    """
    op_table = {}
    scope_table = {}
    pass_table = {}
    scope_args = {}
    gaps = {}
    gap_segments = {}
    stacks = {}
    pending = {}
    labels = {}
    depth = {}
    cursor = {}
    uncovered = {}
    threads = set()
    accounted = 0.0
    gap_total = 0.0
    scopes = 0
    open_spans = 0
    orphan_ends = 0
    first_ts = None
    last_ts = 0.0

    for event in read_events(path):
        phase = event.get("ph")
        if phase == "M":
            continue
        ts = event.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        tid = event.get("tid", 0)
        threads.add(tid)

        previous = cursor.get(tid)
        nested = depth.get(tid, 0)
        # The interval since this thread last appeared on the clock had no
        # action in it exactly when that thread was not inside a span.
        if previous is not None and nested == 0 and ts > previous:
            uncovered.setdefault(tid, []).append(
                [previous, ts, labels.get(tid, ""), scope_name(event)])
        # The previous event on this thread ends any gap still owed to a pass.
        for scope, end in pending.pop(tid, ()):
            gap = ts - end
            if gap > 0:
                gaps[scope] = gaps.get(scope, 0.0) + gap
                gap_total += gap
                gap_segments.setdefault(tid, []).append((end, ts))

        if phase == "X":
            duration = float(event.get("dur", 0.0))
            frame = _frame(event, tid)
            frame["dur"] = duration
            frame["self"] = duration
            # A complete event covers its own interval, and the part of it
            # that overlaps a span is subtracted from that span, so a pass's
            # self time stays self instead of counting the nested interval
            # twice.
            for parent in stacks.get(tid, ()):
                begin = max(ts, parent["start"])
                if ts + duration > begin:
                    parent["child"] += ts + duration - begin
            accounted += duration
            scopes += 1
            _record(frame, duration, duration, op_table, scope_table, pass_table,
                    scope_args)
            cursor[tid] = ts + duration
            labels[tid] = frame["scope"]
            last_ts = max(last_ts, ts + duration)
        elif phase == "B":
            frame = _frame(event, tid)
            stacks.setdefault(tid, []).append(frame)
            depth[tid] = nested + 1
            cursor[tid] = ts
            labels[tid] = frame["scope"]
            last_ts = max(last_ts, ts)
        elif phase == "E":
            stack = stacks.get(tid)
            if not stack:
                orphan_ends += 1
                cursor[tid] = ts
                last_ts = max(last_ts, ts)
                continue
            frame = stack.pop()
            depth[tid] = nested - 1
            duration = ts - frame["start"]
            self_us = max(duration - frame["child"], 0.0)
            accounted += self_us
            scopes += 1
            _record(frame, self_us, duration, op_table, scope_table, pass_table,
                    scope_args)
            if stack:
                stack[-1]["child"] += duration
            cursor[tid] = ts
            labels[tid] = frame["scope"]
            last_ts = max(last_ts, ts)
            if frame["scope"].startswith("pass: "):
                pending.setdefault(tid, []).append((frame["scope"], ts))
        else:
            continue
        if first_ts is None or ts < first_ts:
            first_ts = ts

    # A span still open at exit has no closing event: count it, keep it out of
    # the totals, and let the caller say so.
    for stack in stacks.values():
        open_spans += len(stack)

    # A post-pass gap is also time with no action on the clock, so it has to be
    # taken out of the idle time before the two can be shown side by side:
    # only then do action + gap + other idle add up to the elapsed clock.
    idle_total = 0.0
    gap_in_idle = 0.0
    for tid, segments in uncovered.items():
        idle = _merge(segments)
        idle_total += sum(segment[1] - segment[0] for segment in idle)
        gap_in_idle += _overlap(_merge(gap_segments.get(tid, ())), idle)
    idle_union = _merge(segment for segments in uncovered.values()
                        for segment in segments)

    return {
        "op_table": op_table,
        "scope_table": scope_table,
        "pass_table": pass_table,
        "scope_args": scope_args,
        "gaps": gaps,
        "gap_total": gap_total,
        "idle_total": idle_total,
        "gap_in_idle": gap_in_idle,
        "uncovered_union": sum(segment[1] - segment[0] for segment in idle_union),
        "uncovered_merged": idle_union,
        "accounted": accounted,
        "scopes": scopes,
        "threads": threads,
        "clock": last_ts - (first_ts if first_ts is not None else 0.0),
        "uncovered": uncovered,
        "open_spans": open_spans,
        "orphan_ends": orphan_ends,
    }


def _record(frame, self_us, incl_us, op_table, scope_table, pass_table,
            scope_args):
    """Add one closed action to the three aggregation tables."""
    scope = frame["scope"]
    if frame["tag"] == "apply-pattern":
        key = (frame["op"], frame["sym"], frame["loc"])
        if key != ("", "", ""):
            _account(op_table, key, self_us, incl_us)
    _account(scope_table, (frame["op"], scope), self_us, incl_us)
    if scope.startswith("pass: ") or scope == "greedy-iteration":
        _account(pass_table, (scope,), self_us, incl_us)
    if frame["arg"]:
        scope_args.setdefault(scope, frame["arg"])


def rows(table, key_names, gaps=None, scope_args=None, group_by=None):
    """Turn one aggregation table into report rows, ordered for reading."""
    if group_by is None:
        order = sorted(table.items(), key=lambda item: -item[1]["self"])
    else:
        # Rows sharing one `group_by` value stay adjacent: groups are ordered
        # by their total self, rows inside a group by self.
        position = key_names.index(group_by)
        totals = {}
        for key, row in table.items():
            totals[key[position]] = totals.get(key[position], 0.0) + row["self"]
        order = sorted(table.items(), key=lambda item: (
            -totals[item[0][position]], item[0][position],
            -item[1]["self"], item[0]))
    result = []
    for key, row in order:
        entry = dict(zip(key_names, key))
        entry.update(count=row["count"], self=row["self"] / US_PER_S,
                     incl=row["incl"] / US_PER_S,
                     worst=row["worst"] / US_PER_S)
        if gaps is not None:
            entry["outside"] = gaps.get(key[0], 0.0) / US_PER_S
            entry["arg"] = scope_args.get(key[0], "")
        result.append(entry)
    return result


def fmt_s(seconds):
    """Seconds with trailing zeros trimmed, and never a real value as 0."""
    if not seconds:
        return "0"
    if abs(seconds) < 1e-6:
        return f"{seconds:.3g}"
    return f"{seconds:.6f}".rstrip("0").rstrip(".")


# --- console ----------------------------------------------------------------

def _console(value, kind):
    if kind == "time":
        return fmt_s(value)
    if kind == "pct":
        return f"{value * 100:.2f}%"
    if kind == "int":
        return str(value)
    text = "-" if value in ("", None) else str(value)
    return text


def print_table(title, rows_, columns, share_of):
    """Print one table with the rank column that the workbook also carries."""
    shown = rows_[:_TOP]
    print(f"\n---- {title} (top {len(shown)} of {len(rows_)}) ----")
    kinds = ["int"] + [kind for _, _, kind in columns]
    lines = [["#"] + [header for _, header, _ in columns]]
    for rank, row in enumerate(shown, start=1):
        cells = [str(rank)]
        for key, _, kind in columns:
            value = row.get(key)
            if kind == "pct":
                value = share_of(value)
            cells.append(_console(value, kind))
        lines.append(cells)
    widths = [max(len(line[column]) for line in lines)
              for column in range(len(kinds))]
    for line in lines:
        cells = []
        for column, text in enumerate(line):
            if kinds[column] in ("int", "time", "pct"):
                cells.append(text.rjust(widths[column]))
            else:
                cells.append(text.ljust(widths[column]))
        print("  " + "  ".join(cells).rstrip())


# --- Excel export -----------------------------------------------------------
# A workbook is a zip of small XML parts; the templates below are the fixed
# parts, so the export needs nothing beyond the standard library.

# Cell styles: 0 plain text, 1 bold header, 2 seconds, 3 integer, 4 percentage.
TEXT, HEAD, TIME, INT, PERCENT = 0, 1, 2, 3, 4

_XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{overrides}</Types>
"""

_XLSX_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

_XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>
"""

_XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}</Relationships>
"""

# Number formats, so a time column reads as "0.001234" instead of "1.234E-03":
# 6 decimals is exactly the 1 us resolution of the trace, and percentages are
# stored as fractions and shown with a sign, never pre-multiplied.
_XLSX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3"><numFmt numFmtId="164" formatCode="0.000000"/><numFmt numFmtId="165" formatCode="#,##0"/><numFmt numFmtId="166" formatCode="0.00%"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""

# XML 1.0 text content cannot hold most control characters.
_XML_ILLEGAL = {code: None for code in range(0x20) if code not in (9, 10, 13)}


def _column_letter(index):
    """1-based column index to spreadsheet letters (1 -> A, 27 -> AA)."""
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _text(value):
    return escape(str(value).translate(_XML_ILLEGAL))


def _cell(ref, value, style=TEXT):
    attribute = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{attribute}><v>{_number(value)}</v></c>'
    return (f'<c r="{ref}"{attribute} t="inlineStr">'
            f'<is><t xml:space="preserve">{_text(value)}</t></is></c>')


def _number(value):
    """Plain decimal, so a value of 9 us is written as 0.000009."""
    if isinstance(value, int):
        return str(value)
    return f"{value:.12f}".rstrip("0").rstrip(".") or "0"


def _display(value, style):
    """Rendered width of a cell, so time columns get a usable width."""
    if style == TIME and isinstance(value, (int, float)):
        return f"{value:.6f}"
    if style == INT and isinstance(value, (int, float)):
        return f"{value:,}"
    if style == PERCENT and isinstance(value, (int, float)):
        return f"{value * 100:.1f}%"
    return str(value)


def _worksheet(rows, filterable=False):
    """One sheet: frozen header, a width per column, optional autofilter.

    A row is a list of (value, style) pairs; rows of length one are section
    headings, and an empty list is a blank separator line.
    """
    last_column = max([len(row) for row in rows] + [1])
    widths = []
    for column in range(last_column):
        widest = 0
        for row in rows:
            if column < len(row):
                value, style = row[column]
                widest = max(widest, len(_display(value, style)))
        widths.append(min(max(widest + 2, 9), 48))
    last_row = max(len(rows), 1)
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">',
             f'<dimension ref="A1:{_column_letter(last_column)}{last_row}"/>',
             '<sheetViews><sheetView workbookViewId="0">']
    if filterable:
        parts.append('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                     'state="frozen"/>')
    parts.append('</sheetView></sheetViews><cols>')
    for index, width in enumerate(widths, start=1):
        parts.append(f'<col min="{index}" max="{index}" width="{width}" '
                     'customWidth="1"/>')
    parts.append("</cols><sheetData>")
    for number, row in enumerate(rows, start=1):
        if not row:
            parts.append(f'<row r="{number}"/>')
            continue
        parts.append(f'<row r="{number}">')
        for index, (value, style) in enumerate(row, start=1):
            parts.append(_cell(f"{_column_letter(index)}{number}", value, style))
        parts.append("</row>")
    parts.append("</sheetData>")
    if filterable:
        parts.append(f'<autoFilter ref="A1:{_column_letter(last_column)}{last_row}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


def _header_row(labels):
    return [(label, HEAD) for label in labels]


def _ranked(rows_, columns):
    """Rows plus the rank column that makes "first row, slowest row" obvious."""
    table = [_header_row(["#"] + [label for label, _ in columns])]
    for rank, row in enumerate(rows_, start=1):
        line = [(rank, INT)]
        for _, reader in columns:
            line.append(reader(row))
        table.append(line)
    return table


def _share(part, total):
    return part / total if total else 0.0


def share_denominator(payload):
    """One denominator for every percentage.

    `self` values add up across threads, so the total time is only a valid
    denominator on a single thread; on several, action time is used instead.
    """
    if payload["threads"] <= 1:
        return payload["total_s"]
    return payload["inside_actions_s"]


# A worksheet holds at most 1048576 rows, and a million-row table is not
# readable anyway: the detail sheets stop here and point at report.json.
_MAX_SHEET_ROWS = 100_000


def _capped(rows_, columns, section):
    """Detail sheet, truncated with a visible note when the table is huge."""
    table = _ranked(rows_[:_MAX_SHEET_ROWS], columns)
    if len(rows_) > _MAX_SHEET_ROWS:
        table.append([])
        table.append([(f"已截断：仅列出前 {_MAX_SHEET_ROWS:,} 行"
                       f"（共 {len(rows_):,} 行），完整数据见 report.json 的 "
                       f"{section} 段", TEXT)])
    return table


def _overview_sheet(payload, share_of):
    """Total time, then the five slowest passes and the five slowest ops."""
    def label(text):
        return [(text, HEAD)]

    rows = [
        label("总耗时"),
        [("总耗时 (s)", TEXT), (payload["total_s"], TIME)],
        label("最慢的 5 个 Pass"),
    ]
    rows += _ranked(payload["per_pass"][:5], [
        ("Pass", lambda r: (r["scope"], TEXT)),
        ("次数", lambda r: (r["count"], INT)),
        ("自耗时 (s)", lambda r: (r["self"], TIME)),
        ("占比", lambda r: (share_of(r["self"]), PERCENT)),
    ])
    rows.append([])
    rows.append(label("最慢的 5 个算子"))
    rows += _ranked(payload["per_operation"][:5], [
        ("算子", lambda r: (r["op"], TEXT)),
        ("次数", lambda r: (r["count"], INT)),
        ("自耗时 (s)", lambda r: (r["self"], TIME)),
        ("占比", lambda r: (share_of(r["self"]), PERCENT)),
    ])
    return rows


def _pass_sheet(payload, share_of):
    return _capped(payload["per_pass"], [
        ("Pass", lambda r: (r["scope"], TEXT)),
        ("选项", lambda r: (r["arg"] or "-", TEXT)),
        ("类型", lambda r: (scope_kind(r["scope"]), TEXT)),
        ("次数", lambda r: (r["count"], INT)),
        ("自耗时 (s)", lambda r: (r["self"], TIME)),
        ("占比", lambda r: (share_of(r["self"]), PERCENT)),
        ("含子区间 (s)", lambda r: (r["incl"], TIME)),
        ("单次最大 (s)", lambda r: (r["worst"], TIME)),
        ("平均 (s)", lambda r: (_share(r["self"], r["count"]), TIME)),
        ("动作外 (s)", lambda r: (r["outside"], TIME)),
    ], "per_pass")


def _operation_sheet(payload, share_of):
    return _capped(payload["per_operation"], [
        ("算子", lambda r: (r["op"], TEXT)),
        ("符号名", lambda r: (r["sym"] or "-", TEXT)),
        ("文件", lambda r: _loc_file(r["loc"])),
        ("行", lambda r: _loc_line(r["loc"])),
        ("次数", lambda r: (r["count"], INT)),
        ("自耗时 (s)", lambda r: (r["self"], TIME)),
        ("占比", lambda r: (share_of(r["self"]), PERCENT)),
        ("含子区间 (s)", lambda r: (r["incl"], TIME)),
        ("单次最大 (s)", lambda r: (r["worst"], TIME)),
        ("平均 (s)", lambda r: (_share(r["self"], r["count"]), TIME)),
    ], "per_operation")


def _scope_sheet(payload, share_of):
    totals = {}
    for row in payload["per_scope"]:
        totals[row["op"]] = totals.get(row["op"], 0.0) + row["self"]
    return _capped(payload["per_scope"], [
        ("算子", lambda r: (r["op"] or "-", TEXT)),
        ("算子合计 (s)", lambda r: (totals.get(r["op"], 0.0), TIME)),
        ("动作类型", lambda r: (scope_kind(r["scope"]), TEXT)),
        ("动作", lambda r: (short_name(r["scope"]), TEXT)),
        ("次数", lambda r: (r["count"], INT)),
        ("自耗时 (s)", lambda r: (r["self"], TIME)),
        ("占比", lambda r: (share_of(r["self"]), PERCENT)),
        ("含子区间 (s)", lambda r: (r["incl"], TIME)),
        ("单次最大 (s)", lambda r: (r["worst"], TIME)),
        ("平均 (s)", lambda r: (_share(r["self"], r["count"]), TIME)),
    ], "per_scope")


_LEGEND = [
    ("列名", "含义", "单位", "说明"),
    ("self 自耗时", "动作区间长度减去直接子区间的净时长", "s",
     "评估算子或 Pass 本身的执行瓶颈，支持跨行累加"),
    ("incl 含子区间", "动作区间总长度，包含嵌套子区间", "s",
     "评估调用链总体开销，包含子动作耗时，不可跨行相加"),
    ("worst 单次最大", "该聚合项所有调用实例中单次最大自耗时", "s",
     "甄别性能瓶颈源于高频调用还是极端长耗时实例"),
    ("平均", "单次平均耗时（自耗时 / 次数）", "s",
     "对比不同实例规模动作的执行效率"),
    ("次数", "动作被触发的累计频次", "次",
     "结合平均耗时排查高频小开销或低频大开销动作"),
    ("outside 动作外",
     "Pass 结束至同线程下一事件的间隔（验证器与分析失效）", "s",
     "仅 Pass 表有效；多线程下含线程等待，不可跨行相加"),
    ("占比", "该行自耗时占基准分母的比例", "%",
     "单线程分母为总耗时，多线程分母为动作内合计"),
    ("动作类型", "Pass / 嵌套管线汇总 / Pattern / 贪心", "-",
     "Pattern 属于局部改写动作，与 Pass 处于不同抽象层级"),
    ("动作", "简名：去除命名空间、匿名作用域与模板实参", "-",
     "用于快速浏览与排查，完整名称保留在 report.json 的 per_scope 字段"),
    ("算子合计", "同一算子在全部动作下的自耗时总和", "s",
     "评估该算子在所有改写与处理阶段下的综合耗时代价"),
    ("时间列小数位", "固定 6 位小数，与 1 µs 采集分辨率保持一致", "s",
     "亚微秒级微小数值显示为 0.000000"),
    ("空值 -", "采集器未捕获该属性（如顶层 Pass 无归属算子）", "-",
     "表示该属性在该上下文中不适用或未定义"),
    ("截断提示", "明细数据超过 100,000 行时仅导出前 100,000 行", "行",
     "避免超出工作表上限，完整数据保留在 report.json"),
]


def write_excel(path, payload):
    """Write the report as a five-sheet workbook, standard library only."""
    denominator = share_denominator(payload)

    def share_of(part):
        return _share(part, denominator)

    legend = [list(_header_row(["列名", "含义", "单位", "说明"]))]
    legend += [[(cell, TEXT) for cell in row] for row in _LEGEND[1:]]
    sheets = [
        ("总览", _overview_sheet(payload, share_of), False),
        ("Pass", _pass_sheet(payload, share_of), True),
        ("算子", _operation_sheet(payload, share_of), True),
        ("算子×动作", _scope_sheet(payload, share_of), True),
        ("说明", legend, False),
    ]
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
        'worksheet+xml"/>'
        for index in range(1, len(sheets) + 1))
    body = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _, _) in enumerate(sheets, start=1))
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.'
        'org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1))
    rels += (f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.'
             'openxmlformats.org/officeDocument/2006/relationships/styles" '
             'Target="styles.xml"/>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml",
                      _XLSX_CONTENT_TYPES.format(overrides=overrides))
        book.writestr("_rels/.rels", _XLSX_ROOT_RELS)
        book.writestr("xl/workbook.xml", _XLSX_WORKBOOK.format(sheets=body))
        book.writestr("xl/_rels/workbook.xml.rels",
                      _XLSX_WORKBOOK_RELS.format(rels=rels))
        book.writestr("xl/styles.xml", _XLSX_STYLES)
        for index, (_, rows, filterable) in enumerate(sheets, start=1):
            book.writestr(f"xl/worksheets/sheet{index}.xml",
                          _worksheet(rows, filterable=filterable))


# --- coverage ---------------------------------------------------------------

def _merge(segments):
    """Union of intervals, keeping the labels of the segment that opened it."""
    merged = []
    for segment in sorted(segments, key=lambda item: (item[0], item[1])):
        if merged and segment[0] <= merged[-1][1]:
            if segment[1] > merged[-1][1]:
                merged[-1][1] = segment[1]
        else:
            merged.append(list(segment))
    return merged


def _overlap(left, right):
    """Length two lists of merged intervals have in common."""
    total = 0.0
    i = j = 0
    while i < len(left) and j < len(right):
        low = max(left[i][0], right[j][0])
        high = min(left[i][1], right[j][1])
        if high > low:
            total += high - low
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total


def clock_split(info):
    """Three disjoint slices of the clock, in microseconds.

    Two overlaps have to be removed for the slices to be disjoint.  A pass's
    `outside` runs from its end to the next event on that thread and that time
    has no action on the clock, so it must be taken out of the idle time.
    When the pass sits inside another action, that action keeps running through
    the gap, so the same interval is already counted in that action's `self`:
    the gap is kept out of the slice (reported separately as the overlap)
    instead of being removed from the action work.  Afterwards the slices add
    up to the clock the threads actually held: action self plus idle time.
    """
    accounted = info["accounted"]
    idle = info["idle_total"]
    gaps = info["gap_total"]
    gap_in_idle = info["gap_in_idle"]
    overlap = max(gaps - gap_in_idle, 0.0)
    return {
        "single": len(info["threads"]) <= 1,
        "inside": accounted,
        "outside": gaps,
        "outside_net": min(gaps, gap_in_idle),
        "overlap": overlap,
        "other": max(idle - gap_in_idle, 0.0),
        "idle": idle,
        "union": info["uncovered_union"],
        "segments": len(info["uncovered_merged"]),
        "total": accounted + idle,
        "clock": info["clock"],
    }


def report_clock(split):
    """Show the clock as three non-overlapping slices plus their total."""
    basis = split["total"] if split["total"] else 1.0

    def line(name, value):
        print(f"  {name:<14}{fmt_s(value / US_PER_S):>12} s"
              f"{100.0 * value / basis:>8.1f}%")

    scope = "各线程合计" if not split["single"] else "占合计"
    print(f"耗时构成（三段互不重叠，占比列分母为{scope}）：")
    line("动作内", split["inside"])
    line("动作外（无动作）", split["outside_net"])
    line("其余空闲", split["other"])
    line("合计", split["total"])
    if split["overlap"]:
        print(f"  注：outside 合计 {fmt_s(split['outside'] / US_PER_S)} s，其中 "
              f"{fmt_s(split['overlap'] / US_PER_S)} s 发生在另一动作区间内部，"
              "该部分已计入那个动作的 self，不重复列入动作外")
    if not split["single"]:
        print(f"  总耗时 {fmt_s(split['clock'] / US_PER_S)} s；线程并行，"
              f"各线程合计是其 {split['total'] / split['clock']:.2f} 倍")


def report_coverage(payload, info):
    """Print what the total time held besides actions, per thread."""
    merged = info["uncovered_merged"]
    unobserved = info["uncovered_union"]
    clock = info["clock"]
    if not merged:
        print("  无动作区间（各线程并集）：无（所有线程的时间都落在动作区间内）")
        return
    share = 100.0 * unobserved / clock if clock else 0.0
    print(f"  无动作区间（各线程并集） {fmt_s(unobserved / US_PER_S)} s "
          f"(占总耗时 {share:.1f}%) 分布在 {len(merged)} 段")
    longest = max(merged, key=lambda segment: segment[1] - segment[0])
    print(f"  最长一段 {fmt_s((longest[1] - longest[0]) / US_PER_S)} s: "
          f"{longest[2] or '-'} 结束 -> {longest[3] or 'trace 结束'}")
    if len(info["threads"]) <= 1:
        return
    gaps = sorted(((sum(segment[1] - segment[0] for segment in segments), tid)
                   for tid, segments in info["uncovered"].items()),
                  reverse=True)
    joined = " / ".join(f"tid {tid} {fmt_s(total / US_PER_S)} s"
                        for total, tid in gaps[:4])
    print(f"  逐线程未覆盖：{joined}")
    print("  多线程下 outside 是各线程未覆盖时间的算术和：包含线程阻塞与空闲"
          "等待，不可跨行相加，也不等于验证器开销；"
          "需要可加口径请用 -mlir-disable-threading 重新采集。")


def main(argv):
    if len(argv) != 2:
        print("usage: op-timing-report.py <trace.json>  (no options: the "
              "report and the workbook are written beside the trace)",
              file=sys.stderr)
        return 2
    path = argv[1]
    base = path[:-5] if path.endswith(".json") else path
    report_path = base + ".report.json"
    excel_path = base + ".report.xlsx"

    info = analyse(path)
    # Say what was dropped before anything else: a collector that exits inside a
    # pass leaves the last span unclosed, and silence would look like data loss.
    if info["open_spans"] or info["orphan_ends"]:
        parts = []
        if info["open_spans"]:
            parts.append(f"{info['open_spans']} span(s) never closed")
        if info["orphan_ends"]:
            parts.append(f"{info['orphan_ends']} closing event(s) without an "
                         f"opening event")
        print("warning: " + " and ".join(parts) +
              "; excluded from the totals", file=sys.stderr)
    if not info["scopes"]:
        print(f"{path}: no complete B/E pairs", file=sys.stderr)
        return 1

    total_us = info["clock"]
    payload = {
        "trace": path,
        "total_s": total_us / US_PER_S,
        "inside_actions_s": info["accounted"] / US_PER_S,
        "outside_actions_s": info["gap_total"] / US_PER_S,
        "threads": len(info["threads"]),
        "scopes": info["scopes"],
        "unit": "s",
        "per_operation": rows(info["op_table"], ("op", "sym", "loc")),
        "per_scope": rows(info["scope_table"], ("op", "scope"),
                          group_by="op"),
        "per_pass": rows(info["pass_table"], ("scope",), info["gaps"],
                         info["scope_args"]),
    }
    with open(report_path, "w") as handle:
        json.dump(payload, handle, indent=1)
    write_excel(excel_path, payload)

    # The console and the workbook must agree on the denominator they used.
    denominator = share_denominator(payload)

    def share_of(part):
        return part / denominator if denominator else 0.0

    print(f"{info['scopes']} scopes, {payload['threads']} threads -> "
          f"{report_path}")
    print(f"excel -> {excel_path}")
    report_clock(clock_split(info))
    report_coverage(payload, info)

    print_table("per pass", payload["per_pass"], [
        ("scope", "scope", "text"), ("count", "count", "int"),
        ("self", "self s", "time"), ("self", "share %", "pct"),
        ("incl", "incl s", "time"), ("worst", "max s", "time"),
        ("outside", "outside s", "time")], share_of)
    print_table("per operation, rewrite actions only", payload["per_operation"], [
        ("op", "op", "text"), ("sym", "sym", "text"), ("loc", "loc", "text"),
        ("count", "count", "int"), ("self", "self s", "time"),
        ("self", "share %", "pct"), ("incl", "incl s", "time"),
        ("worst", "max s", "time")], share_of)
    print_table("per op x scope", payload["per_scope"], [
        ("op", "op", "text"), ("scope", "scope", "text"),
        ("count", "count", "int"), ("self", "self s", "time"),
        ("self", "share %", "pct"), ("incl", "incl s", "time"),
        ("worst", "max s", "time")], share_of)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
