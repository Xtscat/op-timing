# op-timing

MLIR 编译耗时测量工具：一个 header-only 的 op 级耗时采集器，加一个离线聚合脚本。
本目录自包含，可以整体拷到别的编译器仓库里使用。

覆盖范围：MLIR PassManager 之内，从解析完成到最后一个 MLIR pass 结束。
代码生成后端（LLVM，或 NPU 等其它目标架构）不在范围内。

| 路径 | 内容 |
| --- | --- |
| `include/OpTimingProfiler.h` | 只读 action handler，把 pass / pattern / greedy 驱动迭代的耗时连同它作用的 op 与源位置写成 Chrome Trace |
| `tools/op-timing-report.py` | 配对 B/E 事件，聚合出 `self` / `incl` / `count` / `worst` / `outside`，输出 JSON，支持与基线比对 |
| `docs/trace-format.md` | **文件格式规范（schema `op-timing` v1）**：任何采集器照它写文件就能被同一个脚本读取 |
| `docs/compile-time-profiling-report.md` | 设计、约束、实测数据、覆盖边界、待细化项 |

## 嵌入使用

CMake 里引入本目录并链接接口目标（在 MLIR-NorthStar 中根 `CMakeLists.txt` 已有
`add_subdirectory(op-timing)`；在其它仓库里自行引入即可）：

```cmake
target_link_libraries(<your-tool> PRIVATE op-timing)
```

driver 里注册 context 扩展（在 `Registry` 交给 `MlirOptMain` 之前），或在自建
`MLIRContext` 上直接调用 `optiming::installOpTimingIfRequested(*context)`：

```cpp
#include "OpTimingProfiler.h"
...
registry.addExtension(optiming::installOpTimingOnContext);
```

运行时用环境变量开启，未设置时零开销：

```bash
OP_TIMING=/tmp/op_trace.json <your-tool> input.mlir -o /dev/null \
  --pass-pipeline="$(cat pipeline.txt)"
python3 tools/op-timing-report.py /tmp/op_trace.json --table
```

`--verify-each`、`--emit-bytecode`、`-o` 都是 driver（`mlir-opt`）的上游参数，不是本模块
提供的（`MlirOptMain.cpp:150`、`:89`、`:495`）。本模块只提供一个环境变量和一个聚合脚本。

## 独立构建（脱离本仓库）

```bash
cmake -S . -B /tmp/build-op-timing -DMLIR_DIR=<MLIR cmake 目录>
cmake --build /tmp/build-op-timing     # 只编译 tests/syntax-check.cpp，验证 header 自足
```

`MLIR_DIR` 形如 `<llvm 安装或构建目录>/lib/cmake/mlir`。独立构建只做语法验证，
目标库 `op-timing` 是 INTERFACE 目标，产物仍是 header + 脚本。

## 与其它编译器的关系

- 对任何使用 `mlir::PassManager` 的编译器：一行 `addExtension` 即可，采集器自动覆盖
  `pass-execution` / `apply-pattern` / `greedy-iteration` 三类动作。
- 对使用 `MLIRContext` 但自写 pass 调度的编译器：需要在自写的调度处调用
  `context.executeAction<YourAction>(fn, {op})`，动作类型按 `mlir::tracing::ActionImpl`
  定义，见 `docs/trace-format.md` 第 7 节。
- 对完全不复用 MLIR 的编译器：header 不适用，但**格式与分析脚本适用** —— 按
  `docs/trace-format.md` 输出同名字段，`tools/op-timing-report.py` 无需改动。

`docs/compile-time-profiling-report.md` 的实测数据取自 MLIR-NorthStar 的 `NS-opt15`
driver（17-pass 的 linalg → LLVM dialect 链路），作为该工具的参考样例，本模块不依赖
该仓库。

## 测量口径

1. **验证器时间自动单列**：`mlir::verify` 跑在 pass action 之外（`Pass.cpp:557`），
   因此不在任何 span 里，但会落在 `pass-execution` 的 `E` 与下一个事件之间。
   `op-timing-report.py` 用这个间隔算出每个 pass 的 `outside`（验证器 + analysis
   失效），写在 `per_pass[].outside` 和表格的 `outside ms` 列，摘要行给出进程级
   合计。**不需要开关，不需要跑两遍。**
2. **要绝对干净的 pass 表再加 `--verify-each=false`**：`outside` 列已经把验证器
   隔离出来了，但嵌套 pipeline adaptor 的 `self` 仍然包含其子 pass 的验证时间
   （实测 49.46 ms，关掉后 0.16 ms）。用该列减去子 pass 的 `outside` 即可还原，
   不想手工算就直接关验证器。
3. **打印是常数开销**：`-o /dev/null` 仍然会格式化输出（1 MB 文本约 333 ms），
   它不影响 pass 归因（不进 trace 跨度），但会抬高墙钟；只关心 pass 归因时可加
   `--emit-bytecode`，该项降到 84 ms。
4. **别把 action 外的缺口当解析或打印**：`wall - inside_actions` 在不关验证器时
   等于最外层可见的验证器开销（706 ms），关掉后只有 0.6 ms。

详见 `docs/compile-time-profiling-report.md`。
