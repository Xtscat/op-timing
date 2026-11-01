#!/usr/bin/env python3
"""Regression tests for tools/op-timing-report.py.

Run with:  python3 tests/test_report.py

The fixtures are synthetic `op-timing` traces (docs/trace-format.md) covering
the paths real collectors exercise: nested passes that close at the same
timestamp, ``ph: "X"`` complete events, greedy rewrite iterations, custom
action tags, several threads, and a span whose closing event never arrives.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "tools", "op-timing-report.py")

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def begin(name, ts, args, tid=1):
    return {"name": name, "cat": "op-timing", "ph": "B", "pid": 0, "tid": tid,
            "ts": float(ts), "args": args}


def end(name, ts, tid=1):
    return {"name": name, "cat": "op-timing", "ph": "E", "pid": 0, "tid": tid,
            "ts": float(ts)}


def complete(name, ts, duration, args, tid=1):
    return {"name": name, "cat": "op-timing", "ph": "X", "pid": 0, "tid": tid,
            "ts": float(ts), "dur": float(duration), "args": args}


# One thread: a pass around a greedy iteration around a pattern application,
# two nested passes closing at the same timestamp, one complete ("X") pattern
# application and one custom tag.
TRACE = [
    begin("pass-execution", 0, {"desc": "pass-execution running `Canonicalizer`",
                                "pass": "Canonicalizer", "arg": "canonicalize",
                                "op": "func.func", "sym": "main",
                                "loc": "m.mlir:2:3"}),
    begin("GreedyPatternRewriteIteration", 10,
          {"desc": "GreedyPatternRewriteIteration", "op": "func.func",
           "sym": "main", "loc": "m.mlir:2:3"}),
    begin("apply-pattern", 20, {"desc": "apply-pattern pattern: (anonymous namespace)::Foo",
                                "op": "arith.addf", "loc": "m.mlir:5:7"}),
    end("apply-pattern", 30),
    end("GreedyPatternRewriteIteration", 40),
    end("pass-execution", 50),
    begin("pass-execution", 100, {"desc": "pass-execution running `Outer`",
                                  "pass": "Outer", "arg": "outer"}),
    begin("pass-execution", 110, {"desc": "pass-execution running `Inner`",
                                  "pass": "Inner", "arg": "inner"}),
    end("pass-execution", 200),
    end("pass-execution", 200),
    complete("apply-pattern", 300, 5,
             {"desc": "apply-pattern pattern: {anonymous}::Bar<mlir::arith::AddFOp, 3>",
              "op": "arith.mulf", "loc": "m.mlir:6:7"}),
    begin("custom-scope", 600, {"desc": "custom-scope", "op": "tensor.empty",
                                "loc": "m.mlir:9:3"}),
    end("custom-scope", 610),
]

# A complete event nested inside a pass: its interval must be taken out of the
# pass's self time instead of being counted twice.
TRACE_NESTED_X = [
    begin("pass-execution", 0, {"desc": "pass-execution running `CSE`",
                                "pass": "CSE", "arg": "cse",
                                "op": "func.func", "loc": "m.mlir:1:1"}),
    complete("apply-pattern", 20, 10,
             {"desc": "apply-pattern pattern: Fold", "op": "arith.subi",
              "loc": "m.mlir:4:5"}),
    end("pass-execution", 100),
]

# A span whose closing event never arrives: the process exited inside a pass.
TRACE_UNCLOSED = [
    begin("pass-execution", 0, {"desc": "pass-execution running `CSE`",
                                "pass": "CSE", "arg": "cse"}),
    begin("apply-pattern", 10, {"desc": "apply-pattern pattern: Fold",
                                "op": "arith.subi"}),
]

# Two threads: percentages of the total time are not summable here, so the
# report has to say what its denominator is and how much clock had no action.
TRACE_THREADS = [
    begin("pass-execution", 0, {"desc": "pass-execution running `A`", "pass": "A",
                                "op": "func.func"}),
    end("pass-execution", 100),
    begin("pass-execution", 200, {"desc": "pass-execution running `B`",
                                  "pass": "B", "op": "func.func"}),
    end("pass-execution", 300),
    begin("pass-execution", 50, {"desc": "pass-execution running `C`",
                                 "pass": "C", "op": "func.func"}, tid=2),
    end("pass-execution", 60, tid=2),
]


class ReportScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_report(self, events, *extra, name="trace"):
        """Run the script on `events`; return (code, payload, output)."""
        trace_path = os.path.join(self.tmp.name, name + ".json")
        report_path = os.path.join(self.tmp.name, name + ".report.json")
        with open(trace_path, "w") as handle:
            json.dump(events, handle)
        done = subprocess.run(
            [sys.executable, SCRIPT, trace_path] + list(extra),
            capture_output=True, text=True)
        payload = None
        if os.path.exists(report_path):
            with open(report_path) as handle:
                payload = json.load(handle)
        return done.returncode, payload, done.stdout + done.stderr

    def test_no_options_writes_both_artifacts(self):
        """The script takes one argument and always writes both artifacts."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        self.assertNotIn("usage:", out)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp.name, "trace.report.json")))
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp.name, "trace.report.xlsx")))
        self.assertEqual(payload["scopes"], 7)
        self.assertEqual(payload["threads"], 1)

    def test_options_are_gone(self):
        """There is no option to pass: the removed ones are rejected."""
        for option in ("--baseline", "-o", "--table", "--top", "--help"):
            code, payload, out = self.run_report(TRACE, option)
            self.assertEqual(code, 2, out)
            self.assertIn("usage:", out)
            self.assertIsNone(payload)

    def test_no_argument_is_an_error(self):
        done = subprocess.run([sys.executable, SCRIPT], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage:", done.stderr + done.stdout)

    def test_complete_events_aggregate(self):
        """`ph: "X"` events carry no B/E pair and must still aggregate."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        rows = {(row["op"], row["loc"]): row for row in payload["per_operation"]}
        self.assertEqual(rows[("arith.mulf", "m.mlir:6:7")]["self"], 0.000005)
        self.assertEqual(rows[("arith.mulf", "m.mlir:6:7")]["count"], 1)

    def test_per_operation_only_holds_rewrite_actions(self):
        """per_operation is apply-pattern only; greedy driver and custom tags
        stay in per_scope (docs/trace-format.md section 3)."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted((row["op"], row["loc"])
                                for row in payload["per_operation"]),
                         [("arith.addf", "m.mlir:5:7"),
                          ("arith.mulf", "m.mlir:6:7")])
        scopes = {row["scope"] for row in payload["per_scope"]}
        self.assertIn("greedy-iteration", scopes)
        self.assertIn("custom-scope", scopes)

    def test_outside_gap_ends_at_next_thread_event(self):
        """Inner and Outer close at the same timestamp: the gap after Inner's E
        ends at Outer's E (zero), the one after Outer's E ends at the next
        event, so a wall-clock interval is never handed to two passes."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        outside = {row["scope"]: row["outside"] for row in payload["per_pass"]}
        self.assertEqual(outside["pass: Inner"], 0.0)
        self.assertEqual(outside["pass: Outer"], 0.0001)
        self.assertEqual(outside["pass: Canonicalizer"], 0.00005)
        self.assertEqual(payload["outside_actions_s"], 0.00015)

    def test_complete_event_inside_a_pass_is_not_double_counted(self):
        """A nested X event is reported on its own and removed from the self
        time of every span it overlaps."""
        code, payload, out = self.run_report(TRACE_NESTED_X)
        self.assertEqual(code, 0, out)
        passes = {row["scope"]: row for row in payload["per_pass"]}
        self.assertEqual(passes["pass: CSE"]["self"], 0.00009)
        self.assertEqual(passes["pass: CSE"]["incl"], 0.0001)
        operations = {row["op"]: row for row in payload["per_operation"]}
        self.assertEqual(operations["arith.subi"]["self"], 0.00001)
        self.assertEqual(payload["inside_actions_s"], 0.0001)

    def test_unclosed_span_is_reported(self):
        """A pass the process never left is excluded from the totals and
        announced, instead of silently disappearing from the report."""
        code, payload, out = self.run_report(TRACE_UNCLOSED)
        self.assertEqual(code, 1, out)
        self.assertIn("never closed", out)
        self.assertIn("no complete B/E pairs", out)
        self.assertIsNone(payload)

    def test_uncovered_time_is_reported(self):
        """Time with no action on the clock is stated as such, not as outside."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        self.assertIn("无动作区间", out)

    def test_per_scope_is_grouped_by_op(self):
        """per_scope is keyed (op, scope) and grouped by op: one op's rows are
        adjacent, groups are ordered by their total self and rows inside a
        group by self, so one op being rewritten by several patterns is
        visible without summing scattered rows."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        rows = payload["per_scope"]
        self.assertNotIn("loc", rows[0])
        groups = []
        for row in rows:
            if not groups or groups[-1][0] != row["op"]:
                groups.append((row["op"], []))
            groups[-1][1].append(row)
        self.assertEqual(len(groups), len({row["op"] for row in rows}),
                         "rows of one op must stay adjacent")
        totals = [sum(member["self"] for member in group)
                  for _, group in groups]
        self.assertEqual(totals, sorted(totals, reverse=True))
        for _, group in groups:
            selves = [member["self"] for member in group]
            self.assertEqual(selves, sorted(selves, reverse=True))
        func_group = next(group for op, group in groups if op == "func.func")
        self.assertEqual({row["scope"] for row in func_group},
                         {"greedy-iteration", "pass: Canonicalizer"})

    def test_clock_splits_into_three_disjoint_slices(self):
        """Action, post-pass gap and other idle must add up to the clock.

        A pass's gap is also time with no action on the clock: shown next to the
        idle time without subtracting the overlap, the three percentages sum to
        more than 100%.
        """
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        total = payload["total_s"]
        self.assertAlmostEqual(payload["inside_actions_s"] +
                               payload["outside_actions_s"], 0.000315, places=9)
        self.assertIn("三段互不重叠", out)
        slices = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] in ("动作内", "动作外（无动作）",
                                                "其余空闲", "合计"):
                slices[parts[0]] = float(parts[1])
        self.assertEqual(sorted(slices),
                         sorted(["动作内", "动作外（无动作）", "其余空闲",
                                 "合计"]))
        self.assertAlmostEqual(slices["合计"], total, places=9)
        self.assertAlmostEqual(slices["动作内"] + slices["动作外（无动作）"] +
                               slices["其余空闲"], total, places=9)

    def test_threads_switch_the_percentage_denominator(self):
        """With several threads the share is of inside actions, not of the
        total time."""
        code, payload, out = self.run_report(TRACE_THREADS)
        self.assertEqual(code, 0, out)
        self.assertEqual(payload["threads"], 2)
        self.assertEqual(payload["total_s"], 0.0003)
        self.assertEqual(payload["inside_actions_s"], 0.00021)
        self.assertIn("逐线程未覆盖", out)
        self.assertIn("不可跨行相加", out)
        with zipfile.ZipFile(os.path.join(self.tmp.name,
                                          "trace.report.xlsx")) as book:
            cells = self.read_sheet(book, "Pass")
        shares = [float(row[6]) for row in cells[1:]]
        self.assertEqual(sorted(shares, reverse=True)[0],
                         round(0.0001 / 0.00021, 12))

    def test_overview_stays_minimal(self):
        """总览 holds the total time and two top-5 tables, nothing else."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        with zipfile.ZipFile(os.path.join(self.tmp.name,
                                          "trace.report.xlsx")) as book:
            cells = self.read_sheet(book, "总览")
        self.assertEqual(cells[0], ["总耗时"])
        self.assertEqual(cells[1][0], "总耗时 (s)")
        self.assertAlmostEqual(float(cells[1][1]), payload["total_s"], places=9)
        headings = [row[0] for row in cells if len(row) == 1]
        self.assertEqual(headings, ["总耗时", "最慢的 5 个 Pass",
                                    "最慢的 5 个算子"])
        self.assertEqual(cells[3], ["#", "Pass", "次数", "自耗时 (s)", "占比"])
        self.assertEqual(cells[10][0], "#")
        self.assertEqual(cells[10][1], "算子")

    def test_excel_workbook(self):
        """Five sheets, Chinese headers, real number formats, filterable."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        excel_path = os.path.join(self.tmp.name, "trace.report.xlsx")
        with zipfile.ZipFile(excel_path) as book:
            names = set(book.namelist())
            for part in ("[Content_Types].xml", "_rels/.rels",
                         "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                         "xl/styles.xml"):
                self.assertIn(part, names)
            for index in range(1, 6):
                self.assertIn(f"xl/worksheets/sheet{index}.xml", names)
            workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
            self.assertEqual([sheet.get("name")
                              for sheet in workbook.iter(f"{XLSX_NS}sheet")],
                             ["总览", "Pass", "算子", "算子×动作", "说明"])
            styles = book.read("xl/styles.xml").decode()
            # 6 decimals for seconds, thousands for counts, a sign for shares:
            # without these a time of 5 us reads as "5E-06" in the grid.
            for code_ in ('formatCode="0.000000"', 'formatCode="#,##0"',
                          'formatCode="0.00%"'):
                self.assertIn(code_, styles)
            pass_cells = self.read_sheet(book, "Pass")
            operation_cells = self.read_sheet(book, "算子")
            scope_cells = self.read_sheet(book, "算子×动作")
            stats = self.read_styles(book, "算子×动作")
        self.assertEqual(pass_cells[0][:11],
                         ["#", "Pass", "选项", "类型", "次数", "自耗时 (s)",
                          "占比", "含子区间 (s)", "单次最大 (s)", "平均 (s)",
                          "动作外 (s)"])
        rows = {row[1]: row for row in pass_cells[1:]}
        self.assertEqual(rows["pass: Inner"][6], "0.147540983607")
        self.assertEqual(rows["pass: Inner"][2], "inner")
        self.assertEqual(rows["pass: Inner"][3], "Pass")
        self.assertEqual(rows["greedy-iteration"][3], "贪心")
        self.assertEqual(operation_cells[0][:5],
                         ["#", "算子", "符号名", "文件", "行"])
        operations = {row[1]: row for row in operation_cells[1:]}
        self.assertEqual(operations["arith.addf"][3:5], ["m.mlir", "5"])
        self.assertEqual(operations["arith.addf"][2], "-")
        self.assertEqual(scope_cells[0][:5],
                         ["#", "算子", "算子合计 (s)", "动作类型", "动作"])
        scopes = {row[4]: row for row in scope_cells[1:]}
        # A 39-character average C++ name is reduced to its base identifier.
        self.assertIn("Bar", scopes)
        self.assertIn("Foo", scopes)
        self.assertIn("Inner", scopes)
        # Every time cell is a number with the seconds format, every count an
        # integer, every share a fraction with the percentage format.
        stylesheet = {cell: stylesheet_style for cell, stylesheet_style in
                      stats.items()}
        self.assertEqual(stylesheet[("C", 2)], "2")   # 算子合计 in seconds
        self.assertEqual(stylesheet[("F", 2)], "3")   # 次数 as an integer
        self.assertEqual(stylesheet[("G", 2)], "2")   # 自耗时 in seconds
        self.assertEqual(stylesheet[("H", 2)], "4")   # 占比 as a percentage

    def test_excel_sheets_are_filterable(self):
        """A 7000-row table is unusable without a frozen header and a filter."""
        code, payload, out = self.run_report(TRACE)
        self.assertEqual(code, 0, out)
        with zipfile.ZipFile(os.path.join(self.tmp.name,
                                          "trace.report.xlsx")) as book:
            for index in (2, 3, 4):
                sheet = ElementTree.fromstring(
                    book.read(f"xl/worksheets/sheet{index}.xml"))
                self.assertIsNotNone(sheet.find(f"{XLSX_NS}autoFilter"))
                self.assertIsNotNone(sheet.find(f".//{XLSX_NS}pane"))
            overview = ElementTree.fromstring(
                book.read("xl/worksheets/sheet1.xml"))
            self.assertIsNone(overview.find(f"{XLSX_NS}autoFilter"))

    @staticmethod
    def read_sheet(book, name):
        """Decode one worksheet, addressed by sheet name, into string rows."""
        workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
        index = None
        for position, sheet in enumerate(workbook.iter(f"{XLSX_NS}sheet"),
                                         start=1):
            if sheet.get("name") == name:
                index = position
        sheet = ElementTree.fromstring(
            book.read(f"xl/worksheets/sheet{index}.xml"))
        table = []
        for row in sheet.iter(f"{XLSX_NS}row"):
            values = []
            for cell in row.iter(f"{XLSX_NS}c"):
                text = "".join(node.text or ""
                               for node in cell.iter(f"{XLSX_NS}t"))
                if not text:
                    value = cell.find(f"{XLSX_NS}v")
                    text = value.text if value is not None else ""
                values.append(text)
            table.append(values)
        return table

    @staticmethod
    def read_styles(book, name):
        """Map ("column letter", row number) to the cell's style id."""
        workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
        index = None
        for position, sheet in enumerate(workbook.iter(f"{XLSX_NS}sheet"),
                                         start=1):
            if sheet.get("name") == name:
                index = position
        sheet = ElementTree.fromstring(
            book.read(f"xl/worksheets/sheet{index}.xml"))
        styles = {}
        for row in sheet.iter(f"{XLSX_NS}row"):
            for cell in row.iter(f"{XLSX_NS}c"):
                ref = cell.get("r")
                letters = "".join(character for character in ref
                                  if character.isalpha())
                styles[(letters, int(ref[len(letters):]))] = cell.get("s", "0")
        return styles


if __name__ == "__main__":
    unittest.main()
