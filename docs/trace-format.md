# Trace 数据协议规范（Schema `op-timing`，Version 1）

本文档定义 `OpTimingProfiler.h` 导出的追踪数据格式规范，以及 `op-timing-report.py` 解析追踪流时遵循的数据协议契约。遵循本规范的任何第三方采集器输出的追踪文件，均可直接被本仓库提供的分析脚本解析与聚合。

- **写入参考实现**：`include/OpTimingProfiler.h`
- **解析参考实现**：`tools/op-timing-report.py`

---

## 1. 顶层容器与元数据事件

追踪数据顶层为 JSON 数组结构，内部元素由按时序记录的事件对象组成。解析器同时兼容 Chrome Trace 的顶层封装格式（即 `{"traceEvents": [...]}` 对象）。

数据流的首个元素为**格式元数据事件**：

```json
{
  "ph": "M",
  "name": "optiming_format",
  "pid": 0,
  "args": {
    "schema": "op-timing",
    "version": 1
  }
}
```

- `version`：协议版本标识，当前固定为整数 `1`。
- 若缺少该元数据事件，解析器默认按 `version: 1` 协议向下兼容处理。
- `ph: "M"`：遵循 Chrome Trace Event 的元数据定义标准，第三方可视化工具（Perfetto、Speedscope、`chrome://tracing`）加载时将自动忽略该事件，不影响时间轴渲染。

---

## 2. 事件对象字段规范

| 字段 | 约束 | 数据类型 | 说明 |
| --- | --- | --- | --- |
| `ph` | 必选 | 字符串 | 事件阶段类型：`"B"`（Begin，区间开始）或 `"E"`（End，区间结束）。支持 `"X"`（Complete，自带持续时间 `dur` 的独立区间事件，其 `self` 与 `dur` 相等且不参与栈配对）。`"M"` 仅用于文件元数据。 |
| `name` | 必选 | 字符串 | 动作标识（Action Tag），决定聚合报告中的归因视图（参见第 3 节）。 |
| `ts` | 必选 | 浮点数 | 事件起始时间戳，单位为**微秒（µs）**。时间原点可任意指定，但同文件内必须保持全局统一基准。 |
| `tid` | 必选 | 整数 | 线程 ID。调用栈配对与嵌套追踪严格限制在同一 `tid` 内部进行。 |
| `pid` | 必选 | 整数 | 进程 ID 保留字段。当前实现固定填 `0`，解析器不用于聚合计算。 |
| `cat` | 可选 | 字符串 | 事件类别分类标识，当前实现固定为 `"op-timing"`，用于可视化查看器分类过滤。 |
| `args` | 条件必选 | 对象 | 事件属性参数。`B` 与 `X` 事件必须提供，`E` 事件无需提供（参见第 4 节）。 |

---

## 3. 动作标识分类与视图映射

解析器根据 `name` 字段将事件分流至不同的聚合统计视图：

| `name` 标识值 | 对应业务语义 | 分析脚本映射规则 |
| --- | --- | --- |
| `pass-execution` | 单次 Pass 执行生命周期 | 映射至 `per_pass` 视图。提取 `args.pass` 作为标识标签（格式为 `pass: <name>`）；若 `args.pass` 以 `OpToOpPassAdaptor` 结尾，则自动折叠合并为 `pass: <nested pipeline>`。 |
| `apply-pattern` | 单次 Pattern 改写尝试 | 映射至 `per_operation` 视图。该区间的 `self` 耗时归因至被改写的 Operation 实例。 |
| 其他自定义 Tag | 自定义动作作用域 | 原样映射至 `per_scope` 视图。除非 Tag 显式命名为 `pass-execution`，否则不进入 `per_pass` 视图。 |

注：MLIR 原生派发的第三类动作标识 `GreedyPatternRewriteIteration` 在报告中将格式化显示为 `greedy-iteration`。

---

## 4. 属性参数（`args`）规范

属性对象挂载于开始事件（`ph: "B"`）或完整事件（`ph: "X"`）中，所有子字段均为字符串类型且均为可选字段。当字段缺失时，解析器将回退至空值或降级规则：

| 参数字段 | 字段语义 | 缺失时的降级行为 |
| --- | --- | --- |
| `desc` | 动作的文本描述信息 | 聚合标签回退至 `name` 字段本身 |
| `op` | 动作作用的目标 Operation 类型（如 `linalg.generic`） | `per_operation` 视图中对应记录的 `op` 留空 |
| `sym` | Operation 绑定的符号名称（`sym_name` 属性，如 `main`） | `per_operation` 视图中对应记录的 `sym` 留空 |
| `loc` | 源码物理位置（`file:line:col` 或符号位置名） | `per_operation` 视图中对应记录的 `loc` 留空 |
| `pass` | Pass 结构化类名（由 `Pass::getName()` 导出） | 尝试从 `desc` 文本描述中提取；提取失败则回退至 `pass-execution` |
| `arg` | Pass 命令行选项名称（由 `Pass::getArgument()` 导出） | `per_pass` 视图中对应记录的 `arg` 留空 |

规范说明：`desc` 供第三方可视化工具展示人类可读文本，`pass` 与 `op` 提供离线分析的结构化分类标识。第三方采集器在派发 `pass-execution` 时应至少提供 `pass` 字段；在派发改写动作时应至少提供 `op` 与 `loc` 字段，以确保 Operation 级的统计与归因有效。

---

## 5. 协议语义约束与不变式

第三方写入程序必须满足以下语义约束，否则会导致聚合指标产生计算偏差：

1. **调用栈严格后进先出（LIFO）嵌套**：相同 `tid` 内的 `B` 与 `E` 事件必须满足调用栈的严格嵌套。严禁跨线程交叠配对。
2. **区间闭合完整性**：每一个 `B` 事件必须存在相同线程内的对应 `E` 事件。未闭合的 Span 区间将在离线后处理时被直接丢弃。
3. **区间单次发射约束**：同一段业务逻辑的执行时间只能由单一的 `B`/`E` 区间对包围。自耗时（`self`）由解析器通过 `当前区间总耗时 − 直接子区间耗时之和` 计算，重复发射嵌套区间会导致父区间的自耗时被错误扣除。
4. **时间戳单调递增性**：同一线程内写出的事件时间戳必须单调不减。
5. **事件间隙的框架开销归属**：`pass-execution` 的 `E` 事件至同线程后续首个事件之间的未覆盖物理时间，解析器将归因于对应 Pass 的框架非区间耗时（`outside`）。该间隙在 MLIR 参考实现中对应 IR 验证器（`mlir::verify`）与分析失效（`AnalysisManager::invalidate`）。若第三方编译器在此间隙内调度了其他工作，相关耗时亦将累加至 `outside` 指标中。

---

## 6. 协议演进规则

- **向前兼容扩展**：新增可选属性字段或新增 `name` 动作标识时，保持 `version: 1` 不变。解析器必须主动忽略未识别的未知字段。
- **破坏性变更升级**：修改现有字段的数据语义、删除核心字段或变更区间配对与间隙归属规则时，协议版本号必须自增（如升级为 `version: 2`），并在本规范中详细记录变更差异。

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
    out << R"([{"ph":"M","name":"optiming_format","pid":0,)"
        << R"("args":{"schema":"op-timing","version":1}})";
  }

  ~SimpleTraceWriter() {
    out << "\n]\n";
  }

  void tracePass(const char *passName, const char *arg, auto &&fn) {
    emitEvent("pass-execution", "B", passName, arg);
    fn();
    emitEvent("pass-execution", "E", nullptr, nullptr);
  }

private:
  double nowUs() {
    return std::chrono::duration<double, std::micro>(
        std::chrono::steady_clock::now() - origin).count();
  }

  void emitEvent(const char *name, const char *ph, const char *pass, const char *arg) {
    out << ",\n"
        << R"({"name":")" << name << R"(","cat":"op-timing","ph":")" << ph
        << R"(","pid":0,"tid":1,"ts":)" << nowUs();
    if (ph[0] == 'B') {
      out << R"(,"args":{"pass":")" << pass << R"(","arg":")" << arg << R"("})";
    }
    out << "}";
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
        self.events = [{
            "ph": "M",
            "name": "optiming_format",
            "pid": 0,
            "args": {"schema": "op-timing", "version": 1}
        }]
        self.origin = time.perf_counter()

    def record_pass(self, pass_name, arg_name, func):
        tid = threading.get_ident()
        ts_begin = (time.perf_counter() - self.origin) * 1e6
        self.events.append({
            "name": "pass-execution",
            "cat": "op-timing",
            "ph": "B",
            "pid": 0,
            "tid": tid,
            "ts": ts_begin,
            "args": {"pass": pass_name, "arg": arg_name}
        })

        try:
            return func()
        finally:
            ts_end = (time.perf_counter() - self.origin) * 1e6
            self.events.append({
                "name": "pass-execution",
                "cat": "op-timing",
                "ph": "E",
                "pid": 0,
                "tid": tid,
                "ts": ts_end
            })

    def export(self, filepath):
        with open(filepath, "w") as f:
            json.dump(self.events, f, indent=2)
```

两类实现生成的追踪文件均可直接通过 `python3 tools/op-timing-report.py <trace-file> --table` 进行聚合与展示。
