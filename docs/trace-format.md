# Trace 数据格式规范

本文档定义 `OpTimingProfiler.h` 导出的追踪数据格式，以及 `op-timing-report.py` 解析追踪流时遵循的协议契约。遵循本规范的任何第三方采集器输出的追踪文件，均可直接被本仓库提供的分析脚本解析与聚合。

- **写入参考实现**：`include/OpTimingProfiler.h`
- **解析参考实现**：`tools/op-timing-report.py`

格式不携带版本号。字段语义需要变更时直接修改本文档；解析器忽略无法识别的字段，因此新增可选字段不需要同步升级任何版本标记。

---

## 1. 顶层容器

追踪数据的顶层是 JSON 数组，元素为按时序追加的事件对象。解析器同时兼容 Chrome Trace 的顶层封装格式（即 `{"traceEvents": [...]}` 对象）。

写入端**每行一个事件**（首行 `[`，末行 `]`，事件之间以 `,` 分隔）。解析器据此逐行流式读取，一份 1 GB 的追踪文件不需要整体载入内存。单行包含整个数组（常见于 `json.dump` 生成的测试数据）的文件同样可以读取，但会被整体载入。

不存在元数据事件：数组的第一个元素就是第一个业务事件。

---

## 2. 事件对象字段

| 字段 | 约束 | 数据类型 | 说明 |
| --- | --- | --- | --- |
| `ph` | 必选 | 字符串 | 事件阶段：`"B"`（区间开始）、`"E"`（区间结束）、`"X"`（完整区间，自带 `dur`）。其他取值被忽略。 |
| `name` | 必选 | 字符串 | 动作标识（Action Tag），决定聚合报告中的归因视图（第 3 节）。 |
| `ts` | 必选 | 整数 | 事件时间戳，单位为**微秒**。时间原点可任意指定，同一文件内必须统一。 |
| `tid` | 必选 | 整数 | 线程 ID。调用栈配对与嵌套判定严格限制在同一 `tid` 内部。 |
| `pid` | 可选 | 整数 | 进程 ID。当前实现固定填 `0`，解析器不使用该字段。 |
| `cat` | 可选 | 字符串 | 事件类别，当前实现固定为 `"op-timing"`，用于可视化工具分类过滤。 |
| `dur` | `X` 必选 | 数值 | 完整区间的持续时间，微秒。 |
| `args` | `B`/`X` 必选 | 对象 | 事件属性参数（第 4 节）。 |

`ts` 必须是整数微秒，不得使用浮点数：以 `double` 累加并以浮点格式写出时，输出精度由格式化器决定（LLVM `raw_ostream` 默认为 6 位小数、7 位有效数字），超过 10 秒的时间戳会被量化到 1 毫秒网格，从而把亚毫秒动作的耗时报成 0，并让同一动作在追踪前段与后段的耗时不可比。

---

## 3. 动作标识分类与视图映射

解析器根据 `name` 字段将事件分流至不同的聚合统计视图：

| `name` 标识值 | 业务语义 | 映射规则 |
| --- | --- | --- |
| `pass-execution` | 单次 Pass 执行 | 进入 `per_pass` 视图，标签取自 `args.pass`，格式为 `pass: <name>`；若以 `OpToOpPassAdaptor` 结尾，折叠为 `pass: <nested pipeline>`。 |
| `apply-pattern` | 单次 Pattern 改写尝试 | 进入 `per_operation` 视图，区间耗时归因至被改写的 Operation 实例。 |
| `GreedyPatternRewriteIteration` | 贪心重写驱动循环 | 在报告中显示为 `greedy-iteration`，与 Pass 一同进入 `per_pass` 视图。 |
| 其他自定义 Tag | 自定义动作作用域 | 原样进入 `per_scope` 视图，不进入 `per_pass` 视图。 |

`per_scope` 视图总是包含全部动作，作为「Operation 类型 × 动作名称」的完整展开。

---

## 4. 属性参数（`args`）

属性对象作为开始事件（`ph: "B"`）或完整事件（`ph: "X"`）的 `args` 字段，子字段均为字符串、均为可选。字段缺失时按下表降级：

| 字段 | 语义 | 缺失时的降级行为 |
| --- | --- | --- |
| `desc` | 动作的文本描述 | 聚合标签回退为 `name` 本身 |
| `op` | 目标 Operation 类型（如 `linalg.generic`） | 报告中该行的 `op` 留空 |
| `sym` | Operation 的符号名（`sym_name` 属性，如 `main`） | 报告中该行的 `sym` 留空 |
| `loc` | 源码位置（`file:line:col` 或符号位置名） | 报告中该行的 `loc` 留空 |
| `pass` | Pass 结构化类名（`Pass::getName()`） | 尝试从 `desc` 中提取；失败则回退为 `pass-execution` |
| `arg` | Pass 命令行选项名（`Pass::getArgument()`） | `per_pass` 视图中该行的 `arg` 留空 |

`desc` 供可视化工具展示人类可读文本，`pass` 与 `op` 提供离线分析所需的结构化标识。第三方采集器在派发 `pass-execution` 时应至少提供 `pass`，在派发改写动作时应至少提供 `op` 与 `loc`，否则 Operation 级统计与源码归因不完整。

---

## 5. 语义约束与不变式

第三方写入程序必须满足以下约束，否则聚合指标会产生偏差：

1. **调用栈严格后进先出**：同一 `tid` 内的 `B`/`E` 必须严格嵌套，禁止跨线程交叠配对。
2. **区间闭合完整性**：每个 `B` 应在同线程内存在对应 `E`。仍在栈中的区间（进程在动作执行中退出）不计入任何耗时统计，解析器在控制台报告其数量；只有 `E` 没有 `B` 的孤立事件同样被计数并排除。
3. **区间单次发射**：同一段业务逻辑的时间只能由一对 `B`/`E` 包围。`self` 由「区间总耗时 − 直接子区间耗时」得出，重复发射嵌套区间会使父区间自耗时被过度扣除。
4. **时间戳单调不减**：同一线程内 `ts` 必须单调不减。
5. **完整事件占用自身区间**：`X` 事件覆盖 `[ts, ts + dur)`，与该区间重叠的父区间会扣除重叠部分，因此 `X` 与其父区间的 `self` 不会重复计算同一段时间。
6. **间隙归因**：`pass-execution` 的 `E` 到同线程后续首个事件之间的时间，归因于该 Pass 的区间外耗时（`outside`）。在 MLIR 参考实现中这段间隙对应 IR 验证器（`mlir::verify`）与分析失效（`AnalysisManager::invalidate`）。若第三方编译器在此间隙内调度了其他工作，该耗时同样计入 `outside`。

多线程写入时，各线程的未覆盖时间会累加进 `outside`，该指标因此包含线程阻塞与空闲等待，不可跨行相加（`docs/measurement.md` 第 1.4 节）。

---

## 6. 格式演进规则

- **兼容扩展**：新增可选 `args` 字段或新增 `name` 取值不需要任何版本标记；解析器忽略未识别字段。
- **破坏性变更**：修改现有字段语义、删除核心字段或改变区间配对与间隙归属规则时，必须同步修改本文件，并在 `docs/measurement.md` 说明对既有结论的影响。

---

## 7. 采集器最小参考实现

### C++ 参考实现（独立于 MLIR）

```cpp
#include <chrono>
#include <fstream>
#include <thread>

class SimpleTraceWriter {
public:
  SimpleTraceWriter(std::ostream &os) : out(os) {
    origin = std::chrono::steady_clock::now();
    out << "[\n";
  }

  ~SimpleTraceWriter() { out << "\n]\n"; }

  void tracePass(const char *passName, const char *arg, auto &&fn) {
    emitEvent("pass-execution", "B", passName, arg);
    fn();
    emitEvent("pass-execution", "E", nullptr, nullptr);
  }

private:
  // Integer microseconds: a floating-point writer quantizes long traces to a
  // 1 ms grid, which reports sub-millisecond actions as zero.
  long long nowUs() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now() - origin)
        .count();
  }

  void emitEvent(const char *name, const char *ph, const char *pass,
                 const char *arg) {
    static bool first = true;
    out << (first ? "" : ",\n")
        << R"({"name":")" << name << R"(","cat":"op-timing","ph":")" << ph
        << R"(","pid":0,"tid":)" << std::this_thread::get_id()
        << R"(,"ts":)" << nowUs();
    if (ph[0] == 'B')
      out << R"(,"args":{"pass":")" << pass << R"(","arg":")" << arg
          << R"("})";
    out << "}";
    first = false;
  }

  std::ostream &out;
  std::chrono::steady_clock::time_point origin;
};
```

### Python 参考实现

```python
import json
import threading
import time

class SimpleTraceCollector:
    def __init__(self):
        self.events = []
        self.origin = time.perf_counter()

    def _ts(self):
        return int((time.perf_counter() - self.origin) * 1e6)

    def record_pass(self, pass_name, arg_name, func):
        tid = threading.get_ident()
        self.events.append({
            "name": "pass-execution", "cat": "op-timing", "ph": "B",
            "tid": tid, "ts": self._ts(),
            "args": {"pass": pass_name, "arg": arg_name},
        })
        try:
            return func()
        finally:
            self.events.append({
                "name": "pass-execution", "cat": "op-timing", "ph": "E",
                "tid": tid, "ts": self._ts(),
            })

    def export(self, filepath):
        with open(filepath, "w") as handle:
            handle.write("[\n")
            handle.write(",\n".join(json.dumps(event)
                                    for event in self.events))
            handle.write("\n]\n")
```

两类实现生成的追踪文件均可直接由 `python3 tools/op-timing-report.py <trace-file>` 聚合。
