# 度量口径与基准实验

本文档阐述 op-timing 各项指标的计算口径、相关编译开关对度量结果的影响机制，并提供标准基准环境下的实测数据与开销评估。

---

## 1. 度量口径与分析规范

### 1.1 框架验证开销的离线分离（`outside` 指标）

MLIR 上游驱动默认启用 `--verify-each` 机制。在 Pass 执行流程中，`mlir::verify` 与 `AnalysisManager::invalidate` 均在 `PassExecutionAction` 触发**之后**执行（`mlir/lib/Pass/Pass.cpp`）。因此，该验证逻辑不会进入任何 Action 的 Span 区间，亦不会被计入 Pass 的 `self` 自耗时。

然而，验证逻辑必定位于当前 `pass-execution` 的结束事件（`E`）与同线程后续首个事件之间。后处理脚本通过计算该时间间隙，将验证与分析失效开销归因至对应的 Pass，记为 `outside` 指标。该机制使得用户无需多次执行编译或开关验证选项，即可在单次追踪中离线分离 IR 验证所占用的物理时间。

### 1.2 指标解读与校准规范

1. **Pass 执行耗时与验证开销正交隔离**：Pass 自身的改写与优化逻辑通过 `self` 反映，框架层面的 IR 验证与分析失效代价通过 `outside` 反映，两者在统计上严格正交。指定 `--verify-each=false` 参数将使 `outside` 趋近于零。
2. **嵌套适配器（Adaptor）的自耗时校准**：对于 `<nested pipeline>` 这类代表 `OpToOpPassAdaptor` 的聚合行，其 Span 生命周期贯穿所有子 Pass 的执行。子 Pass 执行完毕后触发的验证逻辑位于该子 Pass 的 `E` 之后，但仍处于父级 Adaptor 的 Span 闭合之前，因此会被直接计入 Adaptor 的 `self`。校准 Adaptor 净调度耗时时，需减去其包含的所有子 Pass 的 `outside` 之和。

### 1.3 时间线跨度与进程生命周期边界

报告中 `wall_ms - inside_actions_ms` 仅反映最外层可见的 Pass 间隙耗时。IR 文本解析、目标代码格式化输出及上下文环境初始化均发生在该追踪时间线之前或之后。若需评估编译全周期的资源消耗，应结合 `-mlir-timing` 进行对照分析。

### 1.4 并发执行下的统计可加性约束

`wall_ms` 为物理墙钟时间，而各动作的 `self` 耗时为多线程 CPU 时间的累计和。在高度并发的编译场景下，`self` 累加值与 `wall_ms` 的比值可以超出 100%。当后处理脚本检测到多线程事件时将输出提示信息。若需严格满足百分比可加性，需配置 `-mlir-disable-threading` 单线程模式；在此模式下，`outside` 指标亦排除了线程调度与等待延迟的干扰。

### 1.5 性能回归分析的基准度量原则

在包含调试信息与断言校验（Debug + Assertions）的构建版本中，物理执行时间在多次运行间可能存在约 ±20% 的测量方差。在进行跨版本性能回归对比时，应优先比对耗时百分比分布与相对排序，而非绝对物理毫秒数。

---

## 2. 基准实验数据

**测试环境**：16 核心平台，Debug + Assertions 构建，启用单线程模式（`threads: 1`）。输入模型为 MobileNetV2（155 个 ONNX 节点转换生成的 1589 行 Linalg-on-Tensors IR，文件大小 111 KB）；编译降级输出为 15661 行 / 1.03 MB 的 LLVM Dialect IR。追踪记录共包含 14.48 MB 原始数据、45033 个 Span 区间、2083 个独立 Operation 实例行、7153 个 Scope-Operation 组合行以及 17 个 Pass 阶段。

编译管线配置：

```
builtin.module(func.func(linalg-fuse-elementwise-ops),canonicalize,cse,inline,
 one-shot-bufferize{bufferize-function-boundaries=true},
 func.func(convert-linalg-to-loops),convert-scf-to-cf,lower-affine,
 loop-invariant-code-motion,canonicalize,cse,convert-arith-to-llvm,
 convert-index-to-llvm,finalize-memref-to-llvm,convert-cf-to-llvm,
 convert-func-to-llvm,reconcile-unrealized-casts)
```

### 2.1 框架验证开销分析

对比验证机制启用与关闭时的全局时间分布：

| 配置模式 | 追踪墙钟耗时（`wall_ms`） | Action 累计耗时（`inside_actions_ms`） | 框架验证耗时（`Σ outside`） | 顶层可见间隙耗时 |
| --- | --- | --- | --- | --- |
| 默认（启用 `--verify-each`） | 1817.38 ms | 1111.24 ms | **778.85 ms（42.9%）** | 706.15 ms |
| 禁用（`--verify-each=false`） | 1107.79 ms | 1107.23 ms | 6.21 ms（0.6%） | 0.56 ms |

`Σ outside` 代表验证逻辑的全局累计耗时；`wall_ms - inside_actions_ms` 仅反映最外层未被嵌套 Span 覆盖的部分，差额 72.70 ms 发生在嵌套 Adaptor 的 Span 内部。评估验证器开销时应以 `outside_actions_ms` 为准。

框架验证在各 Pass 阶段的耗时分布（按 `outside` 降序排列，取前 6 项）：

| 验证耗时（`outside`） | Pass 名称 |
| --- | --- |
| 133.96 ms | `ConvertFuncToLLVMPass` |
| 108.43 ms | `ConvertControlFlowToLLVMPass` |
| 101.54 ms | `FinalizeMemRefToLLVMConversionPass` |
| 84.75 ms | `Canonicalizer`（3 次执行累计） |
| 47.55 ms | `ConvertAffineToStandard` |
| 46.57 ms | `ConvertIndexToLLVMPass` |

数据表明：**Pass 执行后的验证耗时与其结束时 IR 模块的节点规模成正相关**。在管线末期的 `ConvertFuncToLLVMPass` 阶段，IR 已经完全展开为庞大的 LLVM Dialect，验证耗时达到峰值；而在管线初期的 `LinalgElementwiseOpFusionPass` 阶段，IR 规模较小，验证耗时仅为 19.93 ms。

在当前测试用例中，核心变换耗时 1111.24 ms，框架验证耗时 778.85 ms，验证开销占核心变换耗时的 70.1%。

进程端到端物理墙钟耗时（5 次运行取最优值）：默认模式为 2103 ms，禁用验证后为 1280 ms。

### 2.2 编译器生命周期预算分解

在禁用验证模式（`--verify-each=false`）下，结合 `-mlir-timing` 输出的进程全局耗时分解：

| 执行阶段 | 耗时区间 | 全局占比 |
| --- | --- | --- |
| `Parser`（文本解析 111 KB / 1595 行） | 67–73 ms | 5.3–5.5% |
| MLIR Pass 与 Pattern 改写 | 856–922 ms | 67.9–68.8% |
| `Output`（文本格式化输出 1 MB / 15661 行） | 333–343 ms | 25.5–26.4% |
| `Rest`（其他框架开销） | 2–3 ms | 0.2% |
| 全流程总计（`Total`） | 1.26–1.34 s | 100.0% |

`Parser` 与 `Output` 是宿主驱动构造的计时作用域。其中 `Output` 涵盖 `AsmState` 构造与 `Operation::print()` 过程。当启用 `--emit-bytecode` 采用字节码输出时，`Output` 阶段耗时从 333 ms 下降至 **84 ms**，全流程总时间自 1.26 s 降至 1.03 s。

需要说明的是，重定向输出至 `/dev/null`（`-o /dev/null`）并不会跳过文本格式化逻辑，仅丢弃了写出字节。消除格式化开销必须显式启用 `--emit-bytecode` 或由宿主驱动跳过打印逻辑。

### 2.3 嵌套适配器耗时校准验证

嵌套 Pass 执行完毕后触发的验证逻辑位于该子 Pass 的 Span 外部，但仍处于父级 Adaptor 的 Span 闭合之前，因此该验证耗时会被直接计入 Adaptor 的 `self`。

基准测试中，在默认配置下 `pass: <nested pipeline>` 的 `self` 耗时为 49.46 ms；禁用验证器后该值下降至 0.16 ms。其包含的两个子 Pass 的 `outside` 耗时分别为 19.93 ms 与 29.41 ms，累计为 **49.34 ms**，与 49.30 ms 的差额高度吻合。这证明 Adaptor 行的 `self` 耗时主要由子 Pass 的验证开销构成，其自身的调度开销极低。

### 2.4 Pass 与 Operation 级耗时分解

在禁用验证模式下，以追踪时间跨度为基准的耗时分布：

| 耗时组成分类 | 自耗时（`self`） | 相对占比 |
| --- | --- | --- |
| 具体改写动作（归因至具体 Operation 实例） | 398.79 ms | 40.6% |
| 贪心重写驱动循环（`greedy-iteration`） | 261.81 ms | 26.7% |
| Pass 固有执行逻辑 | 320.28 ms | 32.6% |
| 管线适配器调度（Adaptor） | 0.15 ms | 0.0% |
| Action 监控区间合计 | 981.04 ms | 100.0% |

Pass 级耗时排名（按 `self` 降序排列）：

| 调用次数 | 自耗时（`self`） | 包含耗时（`incl`） | Pass 名称 |
| --- | --- | --- | --- |
| 8 | 261.81 ms | 475.31 ms | `greedy-iteration`（Canonicalize / CSE / Inline 内部驱动循环） |
| 1 | 86.92 ms | 86.92 ms | `OneShotBufferize` |
| 1 | 45.62 ms | 76.45 ms | `ConvertFuncToLLVMPass` |
| 2 | 33.83 ms | 33.83 ms | `CSE` |
| 1 | 33.50 ms | 67.43 ms | `ArithToLLVMConversionPass` |
| 1 | 25.29 ms | 98.82 ms | `FinalizeMemRefToLLVMConversionPass` |
| 1 | 16.21 ms | 210.59 ms | `ConvertLinalgToLoopsPass` |
| 1 | 16.07 ms | 34.53 ms | `ConvertControlFlowToLLVMPass` |
| 1 | 13.14 ms | 13.14 ms | `ConvertIndexToLLVMPass` |
| 1 | 11.36 ms | 38.62 ms | `SCFToControlFlow` |
| 1 | 10.44 ms | 23.71 ms | `Inliner` |
| 1 | 9.89 ms | 11.17 ms | `ConvertAffineToStandard` |
| 1 | 7.69 ms | 7.69 ms | `ReconcileUnrealizedCasts` |
| 3 | 6.78 ms | 236.79 ms | `Canonicalizer` |
| 1 | 2.22 ms | 53.13 ms | `LinalgElementwiseOpFusionPass` |
| 1 | 1.34 ms | 1.34 ms | `LoopInvariantCodeMotion` |
| 2 | 0.15 ms | 263.87 ms | `<nested pipeline>` |

上述数据体现了 `self` 与 `incl` 指标的典型特征：`ConvertLinalgToLoopsPass` 的 `self` 耗时仅为 16.21 ms，但 `incl` 耗时高达 210.59 ms，表明其自身调度逻辑开销极小，绝大部分时间耗费在其触发的底层 Pattern 改写；与此相对，`OneShotBufferize` 的 `self` 与 `incl` 均为 86.92 ms，原因在于其接口遍历分析并不派发 Pattern 改写动作，全部归属于 Pass 自身内部开销。

Operation 级耗时排名（`per_operation` 视图，仅包含改写类动作，展示前 6 项）：

| 触发次数 | 自耗时（`self`） | Operation 类型 | 符号名（`sym`） | 源码位置（`loc`） |
| --- | --- | --- | --- | --- |
| 1 | 11.83 ms | `func.func` | `main` | `m.mlir:2:3` |
| 432 | 2.55 ms | `arith.addf` | — | — |
| 378 | 2.52 ms | `arith.mulf` | — | — |
| 32 | 1.02 ms | `linalg.generic` | — | `m.mlir:1356:13` |
| 32 | 1.00 ms | `linalg.generic` | — | `m.mlir:463:13` |
| 32 | 1.00 ms | `linalg.generic` | — | `m.mlir:29:11` |

其中 `arith.mulf` 与 `arith.addf` 缺乏源位置（`loc` 为空），属于改写过程中动态折叠生成的临时中间节点；附带 `loc` 的记录均精确绑定至源文件行列号，相同类型的不同 Operation 实例（如第 1356 行与第 463 行的 `linalg.generic`）均被独立记录与聚合。

### 2.5 观测开销（Profiling Overhead）评估

| 运行配置 | 端到端物理墙钟 | 追踪文件体积 |
| --- | --- | --- |
| 未配置 `OP_TIMING`，禁用验证（`--verify-each=false`） | 1280 ms | — |
| 启用 `OP_TIMING`，禁用验证（`--verify-each=false`） | 1377 ms | 14.48 MB |
| 未配置 `OP_TIMING`，默认启用验证 | 2103 ms | — |
| 启用 `OP_TIMING`，默认启用验证 | 2231 ms | 14.48 MB |

在基准负载下，采集器引入的端到端耗时开销为 **+6% 至 +8%**。该开销主要来源于 45033 对开始与结束事件的 JSON 格式化与文件 I/O 锁争用。作为对比，MLIR 原生 `--profile-actions-to` 在相同测试用例下的观测开销约为 +4% 至 +5%。

### 2.6 `outside` 指标的一致性与交叉验证

为验证 `outside` 指标与物理真实开销的一致性，将其与独立进程墙钟耗时差值进行比对：

| 度量维度 | 启用验证配置 | 禁用验证配置 | 差值 |
| --- | --- | --- | --- |
| 追踪派生验证耗时（`Σ outside`） | 778.85 ms | 6.21 ms | 772.64 ms |
| 进程级物理墙钟耗时（端到端） | 2103 ms | 1280 ms | 823.00 ms |

两者绝对偏差为 50.36 ms（相对偏差约 6.1%）。偏差原因主要包含两点：管线最后一个 Pass 执行完毕后触发的终态验证位于追踪时间线最后一个 Span 关闭之后，脱离了追踪记录跨度；此外，Debug 构建模式下的物理测量方差亦引入了一定波动。这验证了 `outside` 指标能够高度可靠地反映框架验证开销。

---

## 3. 实验复现流程

```bash
TOOL_BIN=<path-to-tool>
PIPELINE="$(cat pipeline.txt)"
REPORT_SCRIPT=tools/op-timing-report.py

# 1. 采集进程级生命周期预算（Parser / Output / Rest / Pass）
$TOOL_BIN input.mlir -o /dev/null --pass-pipeline="$PIPELINE" --verify-each=false -mlir-timing \
  | grep -E "Total|Parser|Output|Rest"

# 2. 导出细粒度追踪并聚合生成报表（验证器开销独立归纳至 outside 列）
OP_TIMING=/tmp/trace_verify_on.json $TOOL_BIN input.mlir -o /dev/null --pass-pipeline="$PIPELINE"
python3 $REPORT_SCRIPT /tmp/trace_verify_on.json --table --top 20

# 3. 交叉验证：禁用验证后重新采集，比对 outside 列降幅
OP_TIMING=/tmp/trace_verify_off.json $TOOL_BIN input.mlir -o /dev/null --pass-pipeline="$PIPELINE" \
  --verify-each=false
python3 $REPORT_SCRIPT /tmp/trace_verify_off.json

# 4. 观测开销评测：移除 OP_TIMING 环境变量，比对 -mlir-timing 的 Total 物理总时间
```

---

## 4. 基准测试环境驱动配置说明

上述实验数据基于 MLIR-NorthStar 项目的 `NS-opt15` 驱动程序生成。为了执行完整的 Linalg-on-Tensors 至 LLVM Dialect 降级流程，测试驱动加载了以下核心依赖组件：

- **Dialect 支持**：`scf::SCFDialect`、`LLVM::LLVMDialect` 以及 `bufferization::BufferizationDialect`；
- **接口外部模型（External Model）**：`tensor::registerInferTypeOpInterfaceExternalModels`（确保 `tensor.collapse_shape` 等算子具备类型推导能力）；
- **Pass 族注册**：`registerTransformsPasses`、`registerLinalgPasses`、`bufferization::registerBufferizationPasses`、`registerSCFPasses`、`memref::registerMemRefPasses` 及相关 Dialect 转换 Pass。

上述注册属于标准降级管线所需的基础运行环境，与性能追踪工具本身解耦；任何已完整支持上述 Pass 家族的标准 MLIR 驱动环境均可直接复现上述测试结果。
