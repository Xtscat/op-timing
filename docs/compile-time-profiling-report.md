# 编译耗时统计：现状与实施方案报告

> 本报告的实测数据取自 MLIR-NorthStar 仓库的 `NS-opt15` driver（17-pass 的
> linalg-on-tensors → LLVM dialect 链路），作为参考样例。文中出现的
> `15-lowing_to_llvm/` 等路径属于该仓库，本模块不依赖它们。


本文记录 MLIR-NorthStar 中"统计 pass 级与 op 级编译耗时"这件事的**当前实现状态、验证数据、覆盖边界和待细化项**。代码已落地并实测通过，本报告用于在此基础上规范化。

---

## 1. 结论

| 能力 | 是否支持 | 实现方式 |
| --- | --- | --- |
| pass 级耗时 | 是 | 上游 `-mlir-timing`，零代码 |
| 每个改写动作（pattern）的耗时与调用次数 | 是 | 上游 `--profile-actions-to`，零代码 |
| **op 级耗时（哪一个 op、哪一行）** | **是** | 本项目新增 `op-timing/include/OpTimingProfiler.h` + `tools/op-timing-report.py` |
| 时间线 / 多线程重叠 | 是 | 两套 Chrome Trace 均可（Perfetto / speedscope） |
| 验证器（verifier）耗时 | 是 | 从 action 间隙派生，按 pass 单列在 `per_pass[].outside`，见 5.1 |

覆盖范围：**MLIR PassManager 之内**，从解析完成到最后一个 MLIR pass 结束。代码生成后端（LLVM DAG / ISel / regalloc，或任何其它目标架构）不在范围内，见 6.5。

关键点：**上游两个工具都不携带"这个动作作用在哪个 op 上"**——`ActionProfiler` 的 `desc` 只有 op 类型，`-mlir-timing` 连类型都没有。MLIR 的 `Action` 框架本身已经把这个关系存好了（每个 action 带 IRUnits），缺的只是**把它打印出来**这一半。新增代码补的正是这一半。

---

## 2. 三个硬性约束及其满足方式

需求方给定三条约束：稳定可行、不引入任何对算子的操作及 bug、容易维护。

### 2.1 不碰算子（不引入 bug）

监听体的全部内容是：

```cpp
const auto begin = std::chrono::steady_clock::now();
{ std::lock_guard<std::mutex> guard(mutex); printEvent(action, "B", begin); }
transform();                       // 原封不动执行
const auto end = std::chrono::steady_clock::now();
{ std::lock_guard<std::mutex> guard(mutex); printEvent(action, "E", end); }
```

只读取 `action.getTag()`、`action.print(stream)`、`op->getName()`、`op->getAttrOfType<StringAttr>("sym_name")`、`op->getLoc()`。不创建、不修改、不替换、不跳过任何 op，不使用 `ExecutionContext` 的 skip 能力，因此不可能改变编译器输出。

op 身份在 `transform()` **之前**读取：改写成功后原 op 可能已失效。

### 2.2 默认零开销

`OP_TIMING` 未设置时 `installOpTimingIfRequested` 立即返回，`registerActionHandler` 不会被调用，`MLIRContext::hasActionHandler()` 保持 false，于是 `MLIRContext::executeAction` 走 `LLVM_UNLIKELY(hasActionHandler)` 的零开销分支。未开测量的运行与没有这段代码完全等价。

### 2.3 容易维护

只依赖两个公开 API：

- `MLIRContext::registerActionHandler`
  (`HandlerTy = std::function<void(function_ref<void()>, const tracing::Action &)>`)
- `mlir::tracing::Action`：`getTag()` / `print()` / `getContextIRUnits()`

**不需要给 `third_party/llvm-project` 打补丁**，升级 LLVM 子模块时不需要重新 apply。Python 侧只依赖 Chrome Trace 的四个字段 `ph` / `ts` / `tid` / `name`（外加 `args`），不受上游实现变化影响。

---

## 3. 架构

### 3.1 数据来源：MLIR Action 框架

上游已 instrument 三种动作，都会自动触发：

| tag | 类 | 位置 | 携带的 IRUnit |
| --- | --- | --- | --- |
| `pass-execution` | `PassExecutionAction` | `mlir/include/mlir/Pass/Pass.h:492` | pass 的根 op（如 `func.func @main`） |
| `apply-pattern` | `ApplyPatternAction` | `mlir/include/mlir/Rewrite/PatternApplicator.h:30` | 被尝试改写的那个 op |
| `GreedyPatternRewriteIteration` | `GreedyPatternRewriteIteration` | `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:810` | 驱动循环所在 region 的父 op |

上游生产代码里 `executeAction` 只有三个调用点（`Pass.cpp:521`、`PatternApplicator.cpp:195`、`GreedyPatternRewriteDriver.cpp:872`）。采集器对 `tracing::Action` 泛化处理，因此三种 action 全部被记录，不需要按类型登记；`op-timing-report.py:56-57` 把第三个 tag 显示为 `greedy-iteration`。

`pattern` 事件在 `PatternApplicator::matchAndRewrite` 内逐候选触发（`mlir/lib/Rewrite/PatternApplicator.cpp:195`），因此能看到单个 pattern 在单个 op 上的单次尝试——这是 `-mlir-timing` 结构上无法提供的信息。

### 3.2 挂载点：dialect registry 扩展

不修改 `MlirOptMain` 的任何配置：

```
MlirOptMain 构造 MLIRContext            (mlir/lib/Tools/mlir-opt/MlirOptMain.cpp:453)
  └─ MLIRContext 构造函数执行 registry.applyExtensions(this)
                                          (mlir/lib/IR/MLIRContext.cpp:419)
       └─ optiming::installOpTimingOnContext    ← 在这里注册 action handler
```

于是 handler 在**解析之前、任何 pass 之前**就已就位，不需要在 driver 里手工传 context，也不依赖 `--profile-actions-to`。

对应 driver 中的一行（`src/Tools/NS-opt/NS-opt.cpp:69`）：

```cpp
registry.addExtension(optiming::installOpTimingOnContext);
```

### 3.3 输出：Chrome Trace Event JSON

每个动作写出 `B` / `E` 一对事件：

```json
{"name":"apply-pattern","cat":"op-timing","ph":"B","pid":0,"tid":453487,
 "ts":12345.678,"args":{"desc":"`apply-pattern` pattern: LinalgRewritePattern<mlir::scf::ForOp>",
                        "op":"scf.for","loc":"m.mlir:120:9"}}

{"name":"pass-execution","cat":"op-timing","ph":"B","pid":0,"tid":453487,
 "ts":12300.011,"args":{"desc":"`pass-execution` running `Canonicalizer` on Operation `builtin.module`",
                        "pass":"Canonicalizer","arg":"canonicalize",
                        "op":"builtin.module","loc":"m.mlir:1:1"}}
```

多线程安全：`steady_clock` 时间戳取在锁外，只有字符串写出持锁，`transform()` 在锁外执行，因此不会串行化编译；`tid` 用 `llvm::get_threadid()` 区分 worker。文件在进程退出时由静态对象的析构函数补上结尾 `]`。

文件格式是有版本的，不是随手拼的 JSON：数组首元素是元数据事件 `{"ph":"M","name":"optiming_format","args":{"schema":"op-timing","version":1}}`，`args` 里 `pass` / `arg` / `op` / `sym` / `loc` 是结构化身份字段，`desc` 只供人读。完整规范（字段要求、嵌套与成对约束、间隙语义、版本演进规则、最小实现示例）见 `docs/trace-format.md`——按它写文件的任何采集器都能被同一个脚本读取，这也是本组件对非 MLIR 编译器的接入点。

---

## 4. 使用方式

```bash
# 1. 构建
ninja -C build NS-opt15

# 2. 带测量运行（OP_TIMING 指向 trace 输出路径，"-" 表示 stderr）
#    验证器时间由脚本自动单列，不需要额外开关；想让它彻底归零可加
#    --verify-each=false（上游 mlir-opt 参数，见 5.1）
OP_TIMING=/tmp/op_trace.json \
  build/15-lowing_to_llvm/src/Tools/NS-opt/NS-opt15 input.mlir -o /dev/null \
  --pass-pipeline="$(cat /tmp/pipeline.txt)"

# 3. 聚合成 JSON（默认只写 JSON，终端两行摘要）
python3 op-timing/tools/op-timing-report.py /tmp/op_trace.json
# -> /tmp/op_trace.json.report.json

# 可选：额外打印三张人读表格
python3 op-timing/tools/op-timing-report.py /tmp/op_trace.json --table --top 20

# 可选：与基线比较，超过 1.2 倍退出码为 1（供 CI 使用）
python3 op-timing/tools/op-timing-report.py new.json --baseline base.report.json
```

原始 trace 直接拖进 `ui.perfetto.dev` 或 `speedscope.app` 可得火焰图与 Bottom-up 聚合。

### 4.1 两个产物，职责不同

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `<trace>.json` | 原始 `B`/`E` 事件流，**无** self time、**无**调用次数 | 火焰图、时间线、多线程并发视图 |
| `<trace>.report.json` | 聚合结果：self / incl / count / worst | 回归对比、CI 基线、程序化查询 |

### 4.2 `report.json` 结构

```json
{
 "schema_version": 1,
 "trace": "<输入路径>",
 "wall_ms": 1817.38,             // 首个 span 起点到最后一个 span 终点
 "inside_actions_ms": 1111.24,   // 所有 span self time 之和
 "outside_actions_ms": 778.85,   // 验证器 + analysis 失效，按 pass 归因到 outside
 "threads": 1,
 "unit": "us (self, incl, worst, outside) / ms (wall_ms, inside_actions_ms, outside_actions_ms)",
 "per_operation": [ {"op","sym","loc","count","self","incl","worst"} ],
 "per_scope":     [ {"scope","op","loc","count","self","incl","worst"} ],
 "per_pass":      [ {"scope","arg","count","self","incl","worst","outside"} ]
}
```

语义：

- `self`：已扣掉内部嵌套子动作的时间。**判断瓶颈看这一列。**
- `incl`：含内部嵌套的总时间。**定位问题看这一列**（例如 `ConvertLinalgToLoopsPass` self 只有 16.21 ms，但 incl 达 210.59 ms）。
- `per_operation` 只聚合改写类动作（其 IRUnit 就是被改写的 op），因此它回答"哪个 op 被改写得慢"，不含 pass 自身逻辑。
- `per_scope` 是 `(动作名 × op 实例 × 源位置)` 的全集，`greedy-iteration`、`pass-execution` 都在这里。
- `arg`：pipeline 选项名（如 `canonicalize`），把 pass 行映射回命令行参数。
- `outside`：该动作 `E` 之后、同一线程下一个事件之前的时间，即验证器（`Pass.cpp:557`）与分析失效（`:534`）。pass 行的这个字段单独给出，因此不需要另跑一遍去测验证器。
- 多线程下 `wall` 百分比不可相加，且 `outside` 会包含线程等待，脚本会打印提示。

---

## 5. 实测数据

环境：Debug + assertions，16 核，单线程（trace 内 `threads: 1`）。输入 = MobileNetV2（155 个 ONNX 节点 → 1589 行 linalg-on-tensors，`m.mlir`，111 KB）。

pipeline（`/tmp/onnxdemo/full_pipeline.txt`）：

```
builtin.module(func.func(linalg-fuse-elementwise-ops),canonicalize,cse,inline,
 one-shot-bufferize{bufferize-function-boundaries=true},
 func.func(convert-linalg-to-loops),convert-scf-to-cf,lower-affine,
 loop-invariant-code-motion,canonicalize,cse,convert-arith-to-llvm,
 convert-index-to-llvm,finalize-memref-to-llvm,convert-cf-to-llvm,
 convert-func-to-llvm,reconcile-unrealized-casts)
```

规模：输出 15661 行 / 1.03 MB LLVM dialect；trace 14.48 MB / 45033 个 span / 2083 个不同 op 行 / 7153 个 scope×op 行 / 17 个 pass 行。

本节数字由 `-mlir-timing`（进程预算）与 `op-timing-report.py`（pass / op 内部归属）在同一机器、同一输入上重新测得。Debug 构建下重复运行的波动可达 ±20%，跨次比较请用比例与相对排名，不要直接比毫秒数。

### 5.1 验证器：43% 的墙钟，自动单列

`--verify-each` 是上游 `mlir-opt` 的开关（`MlirOptMain.cpp:150`，默认 `true`），
`mlir::verify` 在 `executeAction`（`Pass.cpp:521`）**之后**执行（`:557`），所以它不进任何 action span，也就不可能出现在 `self` 里。但它落在 `pass-execution` 的 `E` 与同一线程的下一个事件之间，**这个间隔就是它的时长**；`op-timing-report.py` 据此把验证器（外加 `am.invalidate`）算成每个 pass 的 `outside`。

同一份 trace，两种配置：

| 配置 | trace wall | action 内合计 | Σ outside | 其中落在 span 外 |
| --- | --- | --- | --- | --- |
| `--verify-each` 默认 | 1817.38 ms | 1111.24 ms | **778.85 ms（42.9%）** | 706.15 ms |
| `--verify-each=false` | 1107.79 ms | 1107.23 ms | 6.21 ms（0.6%） | 0.56 ms |

两列差值的含义：`Σ outside` 是验证器总量；`wall - inside_actions` 只是**最外层可见**的那部分，差额 72.70 ms 发生在嵌套 pipeline adaptor 的 span 内部（见下）。因此 `wall - inside_actions` 不是验证器的全量，`Σ outside` 才是。

验证器在 pass 之间的分布（`per_pass[].outside`，前 6）：

| outside ms | pass |
| --- | --- |
| 133.96 | `ConvertFuncToLLVMPass` |
| 108.43 | `ConvertControlFlowToLLVMPass` |
| 101.54 | `FinalizeMemRefToLLVMConversionPass` |
| 84.75 | `Canonicalizer`（3 次合计） |
| 47.55 | `ConvertAffineToStandard` |
| 46.57 | `ConvertIndexToLLVMPass` |

规律很明确：**验证代价 ≈ 该 pass 结束时整个模块的规模**。`ConvertFuncToLLVMPass` 之后模块已是完整 LLVM dialect，验证最贵；`LinalgElementwiseOpFusionPass` 在链路最前面，模块还是 linalg，只有 19.93 ms。

比值：变换本身 1111.24 ms，验证 778.85 ms —— **每 1 秒变换要额外付 0.70 秒验证**。这也是为什么 pass 级归因必须先把它隔离出来。

端到端墙钟（best of 5，均未开测量）：

| 配置 | 墙钟 |
| --- | --- |
| `--verify-each=false` | 1280 ms |
| `--verify-each` 默认 | 2103 ms |

**嵌套 pass 的验证会污染 adaptor 的 self time**，这是唯一一处需要手工修正的归因：嵌套 pass 的 `E` 之后紧接着验证，而 adaptor 的 span 要等所有嵌套 pass 结束才关闭，所以这段验证被算进 adaptor 的 `self`。默认配置下 `pass: <nested pipeline>` 的 self 是 49.46 ms、关掉验证器后是 0.16 ms；而它两个子 pass 的 `outside` 是 19.93 + 29.41 = **49.34 ms**，与 49.30 ms 的差值吻合——该行的 self 完全由子 pass 的验证构成，与 pipeline 调度无关。

因此口径有两条，都在模块内部完成，不需要操作者记住开关：

1. 看 pass 归因用 `self` 列，验证器看 `outside` 列——两者的和不重叠，加 `--verify-each=false` 只是为了让 `outside` 归零并把最后一点噪声去掉。
2. 读 `<nested pipeline>` 这类 adaptor 行的 `self` 时，减去其子 pass 的 `outside`。

### 5.2 进程预算（`--verify-each=false`，`-mlir-timing`）

多次测量的区间（`Total` 随负载在 1.26–1.34 s 之间波动，各阶段的比例稳定）：

| 阶段 | 时间 | 占比 |
| --- | --- | --- |
| `Parser`（解析 111 KB / 1595 行） | 67–73 ms | 5.3–5.5% |
| MLIR pass 与 pattern | 856–922 ms | 67.9–68.8% |
| `Output`（打印 1 MB / 15661 行文本） | 333–343 ms | 25.5–26.4% |
| `Rest` | 2–3 ms | 0.2% |
| `Total` | 1.26–1.34 s | 100.0% |

`Parser` 与 `Output` 是 driver 建立的合成 TimingScope（`MlirOptMain.cpp:382`、`:420`），`Output` 覆盖 `AsmState` 构造加 `op->print()`（`MlirOptMain.cpp:433-437`）。加 `--emit-bytecode` 后 `Output` 从 333 ms 降到 **84 ms**、`Total` 从 1.26 s 降到 1.03 s —— 即 1 MB 文本的格式化成本约 250 ms，与 pass 无关。

这两项与 pipeline 无关，是常数级基础设施开销：它们不进 trace 跨度（构造上如此），因此不污染 pass 归因，只影响墙钟分母。

### 5.3 pass 与 op 的归因（`--verify-each=false`，分母 = 该次 trace 跨度）

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

### 5.4 测量开销

| 配置 | 墙钟 | trace 大小 |
| --- | --- | --- |
| `OP_TIMING` 未设置，`--verify-each=false` | 1280 ms | — |
| `OP_TIMING=<file>`，`--verify-each=false` | 1377 ms | 14.48 MB |
| `OP_TIMING` 未设置，verify 默认 | 2103 ms | — |
| `OP_TIMING=<file>`，verify 默认 | 2231 ms | 14.48 MB |

开销 **+6~8%**，来源是 45033×2 条 JSON 事件行与每条一次加锁。上游 `--profile-actions-to` 在同输入下实测 +4~5%。

### 5.5 `outside` 列的交叉验证

`outside` 是派生量，因此需要一次独立测量来确认它没有系统性偏差。两遍对照，不写任何代码：

```bash
# 基准：不含验证器
OP_TIMING=/tmp/t_off.json NS-opt15 m.mlir -o /dev/null --verify-each=false \
  --pass-pipeline="$(cat full_pipeline.txt)"
# 对照：含验证器
OP_TIMING=/tmp/t_on.json  NS-opt15 m.mlir -o /dev/null \
  --pass-pipeline="$(cat full_pipeline.txt)"
```

| 量 | 含验证器 | 不含验证器 |
| --- | --- | --- |
| `Σ outside`（派生） | 778.85 ms | 6.21 ms |
| 进程墙钟差（独立） | — | 823 ms |

两者差 44 ms（5.3%），有两个来源，量级都在这个范围里：**最后一次 pass 之后的验证**在最后一个 span 结束之后才执行，落在 trace 跨度之外，任何从 trace 派生的量都看不到它；以及 Debug 构建的 run-to-run 波动。因此 `outside` 列可以作为验证器开销使用，只需要记住它天然截断在最后一个 span。

## 6. 覆盖边界（重要）

### 6.1 有 action 才有时长

MLIR 只对"pass 执行"、"pattern 应用"和"greedy 驱动迭代"三种动作发 action（见 3.1）。**pass 内部不经过 pattern 的遍历没有 op 级事件**，例如 `one-shot-bufferize` 的分析过程；这部分只体现为该 pass 的 self time，无法再细分（实测 `OneShotBufferize` self 86.92 ms 全部属于这一类）。要细分必须在那个 pass 内部插入 `executeAction`，即修改 pass 实现——与"不碰算子"约束冲突，当前明确不做。

### 6.2 无 `loc` 的 op 无法区分实例

折叠/构造过程中产生的 op（如 `arith.mulf`）没有源位置，同类 op 会合并成一行。前端若要在 op 级区分"哪个前端节点慢"，需要在 IR 里带 loc；MLIR 文本解析器默认给每个解析出的 op 挂 `FileLineColLoc`，所以从 `.mlir` 输入直接测量时，源码行本身就是天然实例标识。

### 6.3 多线程下百分比不可加

多线程时 `wall` 是墙钟，`self` 之和是各线程 CPU 时间之和，二者比值可以超过 100%（实测 8 函数并行 canonicalize 时为 767.5%）。脚本检测到多线程会打印提示；需要可加数字时加 `-mlir-disable-threading`。

### 6.4 action 之外的成分

`wall_ms - inside_actions_ms` 这个缺口只有一个来源：**验证器**，且只包含最外层可见的那部分。全量由 `outside_actions_ms` 给出：默认配置 778.85 ms，关掉 `--verify-each` 后 6.21 ms（5.1、5.5）。因此不能用这个缺口去推断解析或打印的开销——它们根本不在这份 trace 的跨度里。

落在 trace 跨度之外、与 pipeline 无关的常数级基础设施开销另算：

| 项 | 实测 |
| --- | --- |
| 进程启动 + context + dialect 初始化 | ~11 ms |
| 解析（111 KB / 1595 行） | 67 ms |
| 打印（1 MB / 15661 行文本） | 333 ms；`--emit-bytecode` 时 84 ms |

这三项都来自 `mlir-opt` driver（`MlirOptMain.cpp:382`、`:420` 的合成 TimingScope），与本模块无关，也不做优化；记录它们只为了让墙钟分母不被误读：MLIR pass 与 pattern 占进程预算的约 68%，不是 100%。

### 6.5 代码生成后端不在范围

本工具覆盖从解析完成到最后一个 MLIR pass 结束。**后端不在范围内**，有两个理由：MLIR 不为后端发 action，且后端随目标架构更换（LLVM 是 CPU 路径的后端，NPU 等目标各有自己的后端）。因此不对后端的耗时做任何承诺，也不通过 `llvm::TimeTraceProfiler` 之类的机制把不同目标的时间线混进来。

参考数据取 `convert-*-to-llvm` 这条 CPU 路径，原因只是它是本仓库当前唯一存在的端到端 pipeline。

---

## 7. 仓库改动清单

本功能是独立组件，不是教程章节，也不带教程编号：代码放在仓库根的 `op-timing/`，
目录自包含（header + 脚本 + 格式规范 + 独立 CMake），可整体拷到别的编译器仓库。

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `op-timing/CMakeLists.txt` | 新增 | 接口目标 `op-timing`（别名 `optiming::op-timing`）；在本仓库作为子目录引入，也可独立 configure |
| `op-timing/include/OpTimingProfiler.h` | 新增 321 行 | 只读 action handler + Chrome Trace 写出；`installOpTimingIfRequested` / `installOpTimingOnContext` |
| `op-timing/tools/op-timing-report.py` | 新增 355 行 | B/E 配对 → self/count/worst 聚合 → JSON，含 `--table` / `--baseline` |
| `op-timing/README.md` | 新增 | 接入方式、独立构建、与其它编译器的关系 |
| `op-timing/docs/trace-format.md` | 新增 112 行 | 文件格式规范（schema `op-timing` v1），第三方采集器的接入契约 |
| `op-timing/tests/syntax-check.cpp` | 新增 | 独立 configure 时编译本文件，验证 header 不依赖本仓库任何头 |
| `op-timing/docs/compile-time-profiling-report.md` | 新增 | 本报告 |
| `CMakeLists.txt` | +1 | `add_subdirectory(op-timing)` |
| `15-lowing_to_llvm/src/Tools/NS-opt/NS-opt.cpp` | +40/-2 | `#include "OpTimingProfiler.h"` + `registry.addExtension(optiming::installOpTimingOnContext)`；补齐上游 pass 注册与 dialect（见下） |
| `15-lowing_to_llvm/src/Tools/NS-opt/CMakeLists.txt` | +16 | 补齐链接库，并 `PRIVATE op-timing` |

接入方式（任何 driver 通用）：

```cpp
#include "OpTimingProfiler.h"          // 由 op-timing 接口目标的 include 目录提供
...
registry.addExtension(optiming::installOpTimingOnContext);
```

`NS-opt15` 原本只注册 NorthStar 自己的 pass，命令行不认 `one-shot-bufferize` / `convert-linalg-to-loops` 等标准 pass，因此无法通过 `--pass-pipeline` 跑完整降级链路。为跑通本次测量，`NS-opt.cpp` 补齐了：

- dialect：`scf::SCFDialect`、`LLVM::LLVMDialect`、`bufferization::BufferizationDialect`（第 61–63 行）
- 外部模型：`tensor::registerInferTypeOpInterfaceExternalModels`（第 79 行）——缺它 `tensor.collapse_shape` 会以 `ReifyRankedShapedTypeOpInterface ... never implemented` 直接 abort
- pass 家族：`registerTransformsPasses` / `registerLinalgPasses` / `bufferization::registerBufferizationPasses` / `registerSCFPasses` / `memref::registerMemRefPasses`，加 8 个具体 conversion pass（第 80 行起）

这属于**为测量而必需的 driver 能力补齐**，与测量本身可分离，见 8.1。

---

## 8. 待细化项

以下是留给后续规范化的事项，按影响排序。

### 8.1 driver 改动的组织方式

当前上游 pass/dialect 注册直接堆在 `NS-opt.cpp` 的 `main` 里。可选方向：

- 抽成 `registerStandardPipelinePasses(registry)` 之类的单独函数/文件，与测量代码解耦；
- 或者只保留 NorthStar pass，把完整链路放到专用 pipeline 注册中，用 `--north-star-full-pipeline` 这类名字调用，而不是文本 `--pass-pipeline`。

### 8.2 验证器只做到"单列"，没做到"精确到 op"

`outside` 列解决了"验证器污染 pass 归因"这件事，但它只能给出**每个 pass 一份总数**，不能回答"哪一段 IR 的验证贵"。要精确到 op / region，需要在 `verify(op, verifyRecursively)` 内部插桩，那要么改上游 `Pass.cpp`，要么按 region 递归自己调 `DominanceInfo` 复算一遍（成本翻倍且不再是观测）。当前不做。

同一条边界下的另一个限制：多线程时 `outside` 会包含线程等待时间，因为它是墙钟间隔。需要可加数字时加 `-mlir-disable-threading`。

### 8.3 CI 基线

`--baseline` 已能对比 `report.json`，但阈值（当前 `REGRESSION_RATIO = 1.20`、`REGRESSION_FLOOR_US = 200`）是拍的，需要实测噪声分布后重新定；另外需要决定基线存哪、按什么粒度（输入模型 × pipeline）存。

### 8.4 多线程语义

多线程下需要给出"CPU 投资"与"关键路径"两种口径的明确输出，而不是只提示百分比不可加。当前 `per_pass` 的 `self` 在多线程下是各线程之和，需要注明。

### 8.5 与上游工具的取舍

上游 `--profile-actions-to` 已具备事件采集与写出能力，本项目自研的理由只有一条：它的 `desc` 不含 op 身份与源位置。若上游后续在 `PassExecutionAction::print()` / `ApplyPatternAction::print()` 中补上 op 名与 loc，本项目可整体退役，改用上游开关 + 本 `op-timing-report.py`。这条应作为长期跟踪项。

---

## 9. 复现材料

输入与 pipeline 放在 `/tmp/onnxdemo/`（临时目录，不入库）：

| 文件 | 内容 |
| --- | --- |
| `m.mlir` | 输入（MobileNetV2 → linalg-on-tensors，111 KB / 1595 行） |
| `full_pipeline.txt` | pipeline 原文 |
| `onnx2mlir.py` | ONNX → linalg-on-tensors 转换脚本（临时工具，入库前需重写） |

重新生成本节全部数字：

```bash
B=build/15-lowing_to_llvm/src/Tools/NS-opt/NS-opt15
P="$(cat /tmp/onnxdemo/full_pipeline.txt)"
R=op-timing/tools/op-timing-report.py

# 1. 进程预算（Parser / Output / Rest / pass 合计）
$B /tmp/onnxdemo/m.mlir -o /dev/null --pass-pipeline="$P" \
   --verify-each=false -mlir-timing | grep -E "Total|Parser|Output|Rest"

# 2. pass / op 归因（验证器自动单列在 outside 列）
OP_TIMING=/tmp/t_on.json $B /tmp/onnxdemo/m.mlir -o /dev/null \
   --pass-pipeline="$P"
python3 $R /tmp/t_on.json --table --top 20

# 3. 交叉验证 outside 列（关掉验证器后它应降到 1 ms 量级）
OP_TIMING=/tmp/t_off.json $B /tmp/onnxdemo/m.mlir -o /dev/null \
   --pass-pipeline="$P" --verify-each=false
python3 $R /tmp/t_off.json

# 4. 测量开销（对比 2 与不带 OP_TIMING 的同一命令）
```

原始 trace 直接拖进 `ui.perfetto.dev` 或 `speedscope.app` 可得火焰图与 Bottom-up 聚合。
