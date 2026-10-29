# op-timing

面向 MLIR 编译器工具链的操作级（Operation-level）编译耗时采集与分析工具。

MLIR 原生工具中，`-mlir-timing` 仅提供 Pass 级别的耗时聚合，未涵盖具体的 Operation 信息；`--profile-actions-to` 在描述信息中仅记录 Operation 的类型名称，未关联具体的 Operation 实例、符号名与源码位置。本模块利用 MLIR `Action` 框架内建的 IR 单元关联机制（`getContextIRUnits`），采集动作执行耗时及其目标 Operation、符号名称（`sym_name`）与源码物理位置（`FileLineColLoc`），输出符合 Chrome Trace Event 规范的追踪数据，并配套提供离线聚合与分析脚本。

**覆盖范围**：涵盖 `mlir::PassManager` 调度周期（从 IR 解析完成至最后一个 MLIR Pass 结束）。代码生成后端（LLVM CodeGen 及各类专用硬件后端）不在度量范围内。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `include/OpTimingProfiler.h` | Header-only 采集器：实现只读 Action Handler 并流式写出 Chrome Trace 数据 |
| `tools/op-timing-report.py` | 离线分析脚本：执行区间配对并计算 `self`、`incl`、`count`、`worst` 与 `outside` 指标，支持性能基线比对 |
| `tests/syntax-check.cpp` | 语法检查测试：在独立构建模式下验证头文件对外部宿主环境的零依赖性 |
| `docs/design.md` | 系统设计：Action 框架原理、挂载机制、只读性约束与维护不变量 |
| `docs/usage.md` | 接入与使用指南：宿主接入流程、运行指令、聚合报告字段说明与参数开关 |
| `docs/measurement.md` | 度量口径与测试数据：度量口径定义、基准性能数据、开销评估与复现流程 |
| `docs/trace-format.md` | 追踪格式规范（Schema `op-timing` v1）：协议规范与第三方采集器实现参考 |

## 快速接入

### 1. CMake 依赖配置

```cmake
add_subdirectory(<path-to-op-timing>)
target_link_libraries(<your-tool> PRIVATE op-timing)
```

### 2. 宿主驱动注册

在 Dialect 注册阶段添加扩展回调：

```cpp
#include "OpTimingProfiler.h"

registry.addExtension(optiming::installOpTimingOnContext);
```

### 3. 运行与分析

```bash
# 执行编译并导出追踪文件
OP_TIMING=/tmp/op_trace.json <your-tool> input.mlir -o /dev/null

# 生成聚合报告并输出格式化表格
python3 tools/op-timing-report.py /tmp/op_trace.json --table
```

当环境变量 `OP_TIMING` 未配置时，采集器不会注册任何 Handler，`MLIRContext::hasActionHandler()` 保持为 `false`，编译执行路径与未引入本模块时完全一致，运行时开销为零。

## 跨编译器适配

- **基于 `mlir::PassManager` 的标准编译器**：通过注册扩展即可自动捕获 Pass 执行、Pattern 改写及贪心重写驱动等动作。
- **自定义 Pass 调度的 MLIR 编译器**：在自定义调度点调用 `context.executeAction<YourAction>(...)`，具体实现参见 `docs/usage.md`。
- **非 MLIR 体系编译器**：前端采集器不适用，但只要第三方采集程序按照 `docs/trace-format.md` 规范输出事件，即可直接使用 `tools/op-timing-report.py` 完成聚合与对比分析。

`docs/measurement.md` 中记录的基准测试数据来自包含 17 个 Pass 的 Linalg-on-Tensors 至 LLVM Dialect 降级链路，作为工具分析能力的参考基准，本模块代码独立于该测试环境。
