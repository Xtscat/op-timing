# 测量口径与参考数据

本文说明怎样读这些数字、哪些开关会改变它们，以及参考环境下的实测值。机制说明见 `design.md`。

---

## 1. 口径

### 1.1 验证器自动单列

`--verify-each` 是上游 `mlir-opt` 的开关（默认 `true`），`mlir::verify` 在 `executeAction` **之后**执行（`mlir/lib/Pass/Pass.cpp:557`），因此它不进任何 action span，也就不可能出现在 `self` 里。但它必然落在 `pass-execution` 的 `E` 与同一线程下一个事件之间，**这个间隔就是它的时长**。报告脚本据此把验证器（外加 `am.invalidate`）算成每个 pass 的 `outside`。

结论：**不需要开关，也不需要跑两遍**就能看到验证代价。

### 1.2 两条读表规则

1. pass 归因看 `self`，验证代价看 `outside`，两者不重叠。加 `--verify-each=false` 只是让 `outside` 归零、去掉最后一点噪声。
2. 读 `<nested pipeline>` 这类 adaptor 行的 `self` 时，减去其子 pass 的 `outside` 之和——那部分是子 pass 的验证，被算进了 adaptor 的 span（见 2.3）。

### 1.3 不要把 action 外的缺口当解析或打印

`wall_ms - inside_actions_ms` 只包含**最外层可见**的验证器时间；解析、打印、context 初始化根本不在这份 trace 的跨度里。要看它们的代价用 `-mlir-timing`（见 2.2）。

### 1.4 多线程下百分比不可加

`wall` 是墙钟，`self` 之和是各线程 CPU 时间之和，比值可能超过 100%（实测 8 个函数并行 canonicalize 时为 767.5%）。需要可加数字时加 `-mlir-disable-threading`；此时 `outside` 也不再包含线程等待。

### 1.5 别直接比毫秒数

Debug + assertions 构建下重复运行的波动可达 ±20%。跨版本比较用比例与相对排名。

---

## 2. 参考数据

环境：Debug + assertions，16 核，单线程（trace 内 `threads: 1`）。输入为 MobileNetV2（155 个 ONNX 节点 → 1589 行 linalg-on-tensors，`m.mlir`，111 KB）；输出 15661 行 / 1.03 MB LLVM dialect；trace 14.48 MB / 45033 个 span / 2083 个不同 op 行 / 7153 个 scope×op 行 / 17 个 pass 行。

pipeline：

```
builtin.module(func.func(linalg-fuse-elementwise-ops),canonicalize,cse,inline,
 one-shot-bufferize{bufferize-function-boundaries=true},
 func.func(convert-linalg-to-loops),convert-scf-to-cf,lower-affine,
 loop-invariant-code-motion,canonicalize,cse,convert-arith-to-llvm,
 convert-index-to-llvm,finalize-memref-to-llvm,convert-cf-to-llvm,
 convert-func-to-llvm,reconcile-unrealized-casts)
```

### 2.1 验证器：43% 的墙钟

同一份 trace，两种配置：

| 配置 | trace wall | action 内合计 | Σ outside | 其中落在 span 外 |
| --- | --- | --- | --- | --- |
| `--verify-each` 默认 | 1817.38 ms | 1111.24 ms | **778.85 ms（42.9%）** | 706.15 ms |
| `--verify-each=false` | 1107.79 ms | 1107.23 ms | 6.21 ms（0.6%） | 0.56 ms |

`Σ outside` 是验证器总量；`wall - inside_actions` 只是最外层可见的那部分，差额 72.70 ms 发生在嵌套 adaptor 的 span 内部。因此要总量就用 `outside_actions_ms`，不能用缺口。

验证器在 pass 之间的分布（`per_pass[].outside`，前 6）：

| outside ms | pass |
| --- | --- |
| 133.96 | `ConvertFuncToLLVMPass` |
| 108.43 | `ConvertControlFlowToLLVMPass` |
| 101.54 | `FinalizeMemRefToLLVMConversionPass` |
| 84.75 | `Canonicalizer`（3 次合计） |
| 47.55 | `ConvertAffineToStandard` |
| 46.57 | `ConvertIndexToLLVMPass` |

规律：**验证代价 ≈ 该 pass 结束时模块的规模**。`ConvertFuncToLLVMPass` 之后模块已是完整 LLVM dialect，验证最贵；`LinalgElementwiseOpFusionPass` 在链路最前，模块还是 linalg，只有 19.93 ms。

比值：变换 1111.24 ms，验证 778.85 ms —— 每 1 秒变换额外付 0.70 秒验证。这就是 pass 级归因必须先把它隔离出来的原因。

端到端墙钟（best of 5，均未开测量）：`--verify-each=false` 1280 ms，默认 2103 ms。

### 2.2 进程预算（`--verify-each=false`，`-mlir-timing`）

| 阶段 | 时间 | 占比 |
| --- | --- | --- |
| `Parser`（解析 111 KB / 1595 行） | 67–73 ms | 5.3–5.5% |
| MLIR pass 与 pattern | 856–922 ms | 67.9–68.8% |
| `Output`（打印 1 MB / 15661 行文本） | 333–343 ms | 25.5–26.4% |
| `Rest` | 2–3 ms | 0.2% |
| `Total` | 1.26–1.34 s | 100.0% |

`Parser` 与 `Output` 是 driver 建立的合成 TimingScope（`mlir/lib/Tools/mlir-opt/MlirOptMain.cpp:382`、`:420`），`Output` 覆盖 `AsmState` 构造加 `op->print()`。加 `--emit-bytecode` 后 `Output` 从 333 ms 降到 **84 ms**、`Total` 从 1.26 s 降到 1.03 s —— 1 MB 文本的格式化成本约 250 ms，与 pass 无关。

注意：`-o /dev/null` **不会**跳过格式化，它只丢弃字节。要省掉这项只有 `--emit-bytecode` 或自写 driver 不打印。

### 2.3 嵌套 adaptor 的 `self` 修正

嵌套 pass 的 `E` 之后紧接着验证，而 adaptor 的 span 要等所有子 pass 结束才关闭，所以这段验证被算进 adaptor 的 `self`。默认配置下 `pass: <nested pipeline>` 的 self 是 49.46 ms、关掉验证器后 0.16 ms；它两个子 pass 的 `outside` 是 19.93 + 29.41 = **49.34 ms**，与 49.30 ms 的差值吻合——该行的 `self` 完全由子 pass 的验证构成，与 pipeline 调度无关。

### 2.4 pass 与 op 归因（`--verify-each=false`，分母 = 该次 trace 跨度）

| 花在哪 | self | 占比 |
| --- | --- | --- |
| 具体某次改写（落到单个 op） | 398.79 ms | 40.6% |
| 重写驱动迭代（`greedy-iteration`） | 261.81 ms | 26.7% |
| pass 自身逻辑 | 320.28 ms | 32.6% |
| pipeline adaptor | 0.15 ms | 0.0% |
| 合计（action 内） | 981.04 ms | 100.0% |

最慢 pass（self / incl）：

| 次数 | self ms | incl ms | pass |
| --- | --- | --- | --- |
| 8 | 261.81 | 475.31 | `greedy-iteration`（canonicalize / cse / inline 内部的驱动循环） |
| 1 | 86.92 | 86.92 | `OneShotBufferize` |
| 1 | 45.62 | 76.45 | `ConvertFuncToLLVMPass` |
| 2 | 33.83 | 33.83 | `CSE` |
| 1 | 33.50 | 67.43 | `ArithToLLVMConversionPass` |
| 1 | 25.29 | 98.82 | `FinalizeMemRefToLLVMConversionPass` |
| 1 | 16.21 | 210.59 | `ConvertLinalgToLoopsPass` |
| 1 | 16.07 | 34.53 | `ConvertControlFlowToLLVMPass` |
| 1 | 13.14 | 13.14 | `ConvertIndexToLLVMPass` |
| 1 | 11.36 | 38.62 | `SCFToControlFlow` |
| 1 | 10.44 | 23.71 | `Inliner` |
| 1 | 9.89 | 11.17 | `ConvertAffineToStandard` |
| 1 | 7.69 | 7.69 | `ReconcileUnrealizedCasts` |
| 3 | 6.78 | 236.79 | `Canonicalizer` |
| 1 | 2.22 | 53.13 | `LinalgElementwiseOpFusionPass` |
| 1 | 1.34 | 1.34 | `LoopInvariantCodeMotion` |
| 2 | 0.15 | 263.87 | `<nested pipeline>` |

`ConvertLinalgToLoopsPass` 这行是读表方法的例子：`self` 只有 16.21 ms，`incl` 达 210.59 ms —— 它自己几乎不花时间，时间花在它触发的 pattern 上。`OneShotBufferize` 相反：86.92 ms 全部是自己，因为它的分析不经过 pattern，没有 op 级事件可分。

op 级（`per_operation`，只含改写类动作，前 6 行）：

| 次数 | self ms | op | sym | loc |
| --- | --- | --- | --- | --- |
| 1 | 11.83 | `func.func` | `main` | `m.mlir:2:3` |
| 432 | 2.55 | `arith.addf` | — | — |
| 378 | 2.52 | `arith.mulf` | — | — |
| 32 | 1.02 | `linalg.generic` | — | `m.mlir:1356:13` |
| 32 | 1.00 | `linalg.generic` | — | `m.mlir:463:13` |
| 32 | 1.00 | `linalg.generic` | — | `m.mlir:29:11` |

`arith.mulf` / `arith.addf` 没有 `loc`，是 fold 过程中临时构造的 op，本来就没有源位置；带 `loc` 的行全部指向具体源码行列，且同类 op 的不同实例（1356 行 vs 463 行）是分开的行。

### 2.5 测量开销

| 配置 | 墙钟 | trace 大小 |
| --- | --- | --- |
| `OP_TIMING` 未设置，`--verify-each=false` | 1280 ms | — |
| `OP_TIMING=<file>`，`--verify-each=false` | 1377 ms | 14.48 MB |
| `OP_TIMING` 未设置，verify 默认 | 2103 ms | — |
| `OP_TIMING=<file>`，verify 默认 | 2231 ms | 14.48 MB |

开销 **+6~8%**，来源是 45033×2 条 JSON 事件行与每条一次加锁。上游 `--profile-actions-to` 在同输入下实测 +4~5%。

### 2.6 `outside` 的交叉验证

`outside` 是派生量，需要独立测量确认没有系统性偏差：

| 量 | 含验证器 | 不含验证器 |
| --- | --- | --- |
| `Σ outside`（派生） | 778.85 ms | 6.21 ms |
| 进程墙钟差（独立） | — | 823 ms |

两者差 44 ms（5.3%），来源有两个：**最后一次 pass 之后的验证**在最后一个 span 结束之后才执行，落在 trace 跨度之外，任何从 trace 派生的量都看不到；以及 Debug 构建的 run-to-run 波动。因此 `outside` 可以直接当验证器开销使用，只需记住它天然截断在最后一个 span。

---

## 3. 复现

```bash
B=<path-to-tool>
P="$(cat pipeline.txt)"
R=tools/op-timing-report.py

# 1. 进程预算（Parser / Output / Rest / pass 合计）
$B input.mlir -o /dev/null --pass-pipeline="$P" --verify-each=false -mlir-timing \
  | grep -E "Total|Parser|Output|Rest"

# 2. pass / op 归因（验证器自动单列在 outside 列）
OP_TIMING=/tmp/t_on.json $B input.mlir -o /dev/null --pass-pipeline="$P"
python3 $R /tmp/t_on.json --table --top 20

# 3. 交叉验证 outside 列（关掉验证器后应降到 1 ms 量级）
OP_TIMING=/tmp/t_off.json $B input.mlir -o /dev/null --pass-pipeline="$P" \
  --verify-each=false
python3 $R /tmp/t_off.json

# 4. 测量开销：把 2 的 OP_TIMING 去掉，用 -mlir-timing 的 Total 做对比
```

---

## 4. 参考环境的附注

上表的数字取自 MLIR-NorthStar 仓库的 `NS-opt15` driver，它原本只注册 NorthStar 自己的 pass，命令行不认 `one-shot-bufferize`、`convert-linalg-to-loops` 等标准 pass。为跑通完整降级链路，该 driver 补齐了：

- dialect：`scf::SCFDialect`、`LLVM::LLVMDialect`、`bufferization::BufferizationDialect`；
- 外部模型：`tensor::registerInferTypeOpInterfaceExternalModels`（缺它 `tensor.collapse_shape` 会以 `ReifyRankedShapedTypeOpInterface ... never implemented` 直接 abort）；
- pass 家族：`registerTransformsPasses` / `registerLinalgPasses` / `bufferization::registerBufferizationPasses` / `registerSCFPasses` / `memref::registerMemRefPasses`，加 8 个具体 conversion pass。

这些属于"为测量而必需的 driver 能力补齐"，与测量本身可分离：任何已经能在命令行跑标准 pipeline 的 driver 都不需要这一步。
