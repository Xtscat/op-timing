# Trace 格式规范（schema `op-timing`，version 1）

本文件定义 `OpTimingProfiler.h` 输出的文件格式，以及 `op-timing-report.py` 读取它时依赖的全部约定。目的只有一个：**任何采集器**（不限于本仓库的 header）只要按本规范写文件，就能被同一个分析脚本读取，不需要改脚本。

读取方是 `tools/op-timing-report.py`；写入方的参考实现是 `include/OpTimingProfiler.h`。

---

## 1. 容器

一个 JSON 数组，元素是事件对象。也接受 Chrome Trace 的包装形式 `{"traceEvents": [...]}`，读取方会自动展开。

第一条元素是**元数据事件**：

```json
{"ph":"M","name":"optiming_format","pid":0,"args":{"schema":"op-timing","version":1}}
```

- `version` 是整数，当前为 `1`。
- 缺失该事件时，读取方按 `version: 1` 处理（兼容早期无标记的文件）。
- `ph: "M"` 是 Chrome Trace 的元数据类型，图形化查看器会忽略它，不影响 `chrome://tracing` / Perfetto / speedscope 的加载。

## 2. 事件字段

| 字段 | 要求 | 说明 |
| --- | --- | --- |
| `ph` | 必需 | `"B"`（开始）或 `"E"`（结束）。`"X"`（自带 `dur` 的完整事件）也接受，自带 `self = dur`，不参与配对。`"M"` 仅用于元数据。 |
| `name` | 必需 | 动作 tag，决定归因视图，见第 3 节。 |
| `ts` | 必需 | 开始时刻，**微秒**，浮点。原点任意，只要同一文件内一致。 |
| `tid` | 必需 | 线程 id，整数。配对只在同一 `tid` 内进行。 |
| `pid` | 必需（固定 `0`） | 保留字段。当前实现恒为 `0`，读取方不使用。 |
| `cat` | 可选（固定 `"op-timing"`） | 保留字段，供查看器分组。读取方不使用，因此改名不影响分析结果。 |
| `args` | `B` 事件必需，`E` 事件无 | 见第 4 节。 |

## 3. `name` 的取值与归因

| `name` | 含义 | 分析脚本的处理 |
| --- | --- | --- |
| `pass-execution` | 一次 pass 执行 | 进入 `per_pass`；`args.pass` 生成标签 `pass: <name>`；`args.pass` 以 `OpToOpPassAdaptor` 结尾时折叠为 `pass: <nested pipeline>` |
| `apply-pattern` | 一次 pattern 尝试 | 进入 `per_operation`（`self` 记为被改写 op 的耗时） |
| 其它任意 tag | 自定义动作 | 原样进入 `per_scope`；不进入 `per_pass`，除非 tag 为 `pass-execution` |

`GreedyPatternRewriteIteration` 是上游 MLIR 的第三个 tag，脚本显示为 `greedy-iteration`。

## 4. `args` 字段

只出现在 `B` 事件上。全部为字符串，全部可选；读取方对缺失字段回退到空值。

| 字段 | 含义 | 缺失时 |
| --- | --- | --- |
| `desc` | 人类可读描述，自由文本 | 标签退化为 `name` 本身 |
| `op` | 该动作作用在哪个 op 上，如 `linalg.generic` | `per_operation` 中该行 `op` 为空 |
| `sym` | op 的符号名（`sym_name`），如 `main` | `per_operation` 中该行 `sym` 为空 |
| `loc` | 源位置，`file:line:col` 或符号位置名 | `per_operation` 中该行 `loc` 为空 |
| `pass` | pass 名（`Pass::getName()`） | 回退到从 `desc` 解析 |
| `arg` | pipeline 选项名（如 `canonicalize`），仅 `pass-execution` 有意义 | `per_pass` 中该行 `arg` 为空 |

`desc` 与 `pass` 并存是刻意的：前者供人读，后者是结构化身份。新采集器**至少**应填 `pass`（若发 `pass-execution`）与 `op`/`loc`（若希望 op 级归因成立），`desc` 可以留空。

## 5. 语义要求

写入方必须满足以下五条，否则分析的数值会错（而不是报错）：

1. **严格嵌套**：同一 `tid` 上 `B`/`E` 必须后进先出。跨线程嵌套不允许。
2. **成对**：每个 `B` 都要有对应 `E`。进程退出时仍未闭合的 span 会被丢弃。
3. **不重复**：一个时间区间只能发一对 `B`/`E`。`self` 由读取方按 `dur − 子 span 之和` 计算，重复发放会让父 span 的 `self` 偏小。
4. **`ts` 单调**：同一 `tid` 上按时间递增写出。
5. **间隙归属**：`pass-execution` 的 `E` 到下一条同线程事件之间的间隔，会被读取方记为**框架开销**（`per_pass[].outside`），语义是"该 pass 结束后、下一个动作开始前，不在任何 span 内的耗时"。参考实现里这段就是 MLIR 的 after-pass 验证器与分析失效（`mlir/lib/Pass/Pass.cpp:534-557`）。若宿主的 pass 执行框架在这段间隔里还有别的工作，它会被一起算进 `outside`——这是本格式唯一的语义约定，采集器无法通过字段澄清，只能在文档里说明。

## 6. 版本演进

- 增加可选字段：`version` 不变。读取方必须忽略不认识的字段。
- 改变字段含义、删除字段、改变配对/间隙语义：`version` 加一，并在本文件记录差异。

## 7. 最小实现示例

C++（任意编译器，不必是 MLIR）：

```cpp
std::chrono::steady_clock::time_point origin = std::chrono::steady_clock::now();
auto micros = [&] {
  return std::chrono::duration<double, std::micro>(
             std::chrono::steady_clock::now() - origin).count();
};
out << R"([{"ph":"M","name":"optiming_format","pid":0,)"
       R"("args":{"schema":"op-timing","version":1}})";
out << "\n" << R"({"name":"pass-execution","cat":"op-timing","ph":"B","pid":0,"tid":)"
    << tid << R"(,"ts":)" << micros()
    << R"(,"args":{"pass":"MyPass","arg":"my-pass","op":")" << opName << "\"}";
runMyPass();
out << "\n" << R"({"name":"pass-execution","cat":"op-timing","ph":"E","pid":0,"tid":)"
    << tid << R"(,"ts":)" << micros() << "}]";
```

Python：

```python
events = [{"ph": "M", "name": "optiming_format", "pid": 0,
           "args": {"schema": "op-timing", "version": 1}}]
t0 = time.perf_counter()
events.append({"name": "pass-execution", "cat": "op-timing", "ph": "B",
               "pid": 0, "tid": threading.get_ident(),
               "ts": (time.perf_counter() - t0) * 1e6,
               "args": {"pass": "MyPass", "op": "my.op"}})
run_my_pass()
events.append({"name": "pass-execution", "cat": "op-timing", "ph": "E",
               "pid": 0, "tid": threading.get_ident(),
               "ts": (time.perf_counter() - t0) * 1e6})
json.dump(events, open("trace.json", "w"))
```

两者都可以直接用 `op-timing-report.py trace.json --table` 读取。
