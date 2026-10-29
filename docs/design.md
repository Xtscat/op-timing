# 设计

本文说明 op-timing 为什么这样实现、依赖 MLIR 的哪些设施、以及在什么条件下给出的数字是有效的。使用方式见 `usage.md`，文件格式契约见 `trace-format.md`，实测数据见 `measurement.md`。

---

## 1. 目标与缺口

目标是回答两个问题：**哪个 pass 慢**，以及**慢在哪个 op 的哪一行**。

MLIR 上游已有两个工具，各缺一半：

| 工具 | 粒度 | 缺什么 |
| --- | --- | --- |
| `-mlir-timing` | pass | 没有 op 概念，甚至连 op 类型都没有 |
| `--profile-actions-to` | pass + 单次 pattern 尝试 | `desc` 里只有 op **类型**（`func.func`），没有 op 实例、没有符号名、没有源位置 |

所以看到"Canonicalizer 用了 314 ms"之后，无法追问"是哪个函数的哪一行导致的"。

MLIR 的 `Action` 框架其实已经把这个关系存好了：每个 action 携带它作用的 IR unit（`Action::getContextIRUnits()`），`PassExecutionAction` 和 `ApplyPatternAction` 都会把它填成正在处理的 op。缺的只是**把它读出来并打印在时间戳旁边**这一半，本模块补的就是这一半。

### 覆盖范围

从解析完成到最后一个 MLIR pass 结束。**代码生成后端不在范围内**：MLIR 不为后端发 action，且后端随目标架构更换（LLVM 是 CPU 路径的后端，NPU 等目标各有自己的后端），因此不对后端做任何测量承诺，也不把别的时间线混进来。

## 2. 数据来源：MLIR Action 框架

上游生产代码中 `MLIRContext::executeAction` 只有三个调用点，因此只有三种 action：

| tag | 类 | 发出位置 | 携带的 IR unit |
| --- | --- | --- | --- |
| `pass-execution` | `PassExecutionAction` | `mlir/lib/Pass/Pass.cpp:521` | pass 的根 op（如 `func.func @main`） |
| `apply-pattern` | `ApplyPatternAction` | `mlir/lib/Rewrite/PatternApplicator.cpp:195` | 被尝试改写的那个 op |
| `GreedyPatternRewriteIteration` | `GreedyPatternRewriteIteration` | `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:872` | 驱动循环所在 region 的父 op |

handler 的类型是 `MLIRContext::HandlerTy`：

```cpp
std::function<void(function_ref<void()>, const tracing::Action &)>
```

它拿到的是基类引用，因此采集器对 `tracing::Action` 泛化处理即可，**不需要按 action 类型登记**。第三种 action 因此是自动获得的，不需要额外代码。

`apply-pattern` 在 `PatternApplicator::matchAndRewrite` 内逐候选触发，所以能看到单个 pattern 在单个 op 上的单次尝试——这是 `-mlir-timing` 结构上无法提供的信息。

## 3. 采集器实现

`OpTimingProfiler::operator()` 的全部内容是：

```cpp
const auto begin = steady_clock::now();
{ lock; printEvent(action, "B", begin); }
transform();                        // 原封不动执行
const auto end = steady_clock::now();
{ lock; printEvent(action, "E", end); }
```

几个不是随意选择的细节：

- **op 身份必须在 `transform()` 之前读**。改写成功后原 op 可能已被擦除，`E` 事件只写时间戳，不写身份。
- **时间戳取在锁外，只有字符串写出持锁**，`transform()` 也在锁外。否则多线程编译会被采集器串行化，测出来的时间就是错的。
- **`tid` 用 `llvm::get_threadid()`**，配对只在同一 `tid` 内进行。
- **文件由函数内 static 对象的析构函数补结尾 `]`**，因此不需要在 driver 里手工收尾；进程正常退出即可得到合法 JSON。
- **`ts` 是相对 profiler 起点的微秒浮点数**，同一文件内所有事件共用一个时间原点。

### 只读性

handler 读取的全部内容：

- `Action::getTag()`
- `Action::print()`（写成 `desc`）
- `op->getName()`、`getAttrOfType<StringAttr>("sym_name")`、`getLoc()`
- `PassExecutionAction::getPass()` 的 `getName()` / `getArgument()`

不创建、不修改、不替换、不跳过任何 op，也不使用 `ExecutionContext` 的 skip 能力，因此不可能改变编译器输出。

## 4. 挂载点

安装只有两种途径，都不需要修改宿主 driver 的既有配置。

**途径一：dialect registry 扩展**（推荐，宿主只需一行）

```
MlirOptMain 构造 MLIRContext            (mlir/lib/Tools/mlir-opt/MlirOptMain.cpp:453)
  └─ MLIRContext 构造函数执行 registry.applyExtensions(this)
                                          (mlir/lib/IR/MLIRContext.cpp:419)
       └─ optiming::installOpTimingOnContext    ← 在这里注册 action handler
```

handler 因此在**解析之前、任何 pass 之前**就位，不需要在 driver 里手工传 context。

**途径二：直接在 context 上调用** `optiming::installOpTimingIfRequested(*context)`，用于不复用 `MlirOptMain` 的 driver。

### 与既有 handler 的关系

`MLIRContext` 只有一个 action handler 槽位。因此安装前先检查 `hasActionHandler()`：

- 若已有 handler（例如宿主传了 `--profile-actions-to`），**不安装**并输出一行提示，而不是静默覆盖——覆盖会让宿主的工具突然失效且没有痕迹。
- 幂等：同一 context 重复安装直接返回，不会注册两次。

### 为什么用环境变量而不是命令行

`OP_TIMING` 未设置时 `installOpTimingIfRequested` 立即返回，`registerActionHandler` 不被调用，`hasActionHandler()` 保持 false，于是 `MLIRContext::executeAction` 走 `LLVM_UNLIKELY(hasActionHandler())` 的零开销分支。未开测量的运行与没有这段代码完全等价，且不需要给宿主加命令行选项、不需要改 `MlirOptMain` 的配置路径。

## 5. 聚合与派生量

`tools/op-timing-report.py` 是纯离线后处理：按 `tid` 维护 span 栈，把 `B` / `E` 配对成区间，再按"父区间减子区间"算 `self`。

三个视图，各自回答一个问题：

| 视图 | 键 | 回答 |
| --- | --- | --- |
| `per_pass` | pass 名 | 哪个 pass 慢；`outside` 给出它的验证代价 |
| `per_operation` | op 类型 + 符号名 + 源位置 | 哪个 op 被改写得慢（只含改写类动作） |
| `per_scope` | 动作名 × op 实例 × 源位置 | 全集，包括 `greedy-iteration` 与 pass 自身 |

`self` 与 `incl` 的区别：`incl` 含内部嵌套子动作，`self` 已扣掉。判断瓶颈看 `self`，定位问题看 `incl`（例如某个 pass `self` 只有 16 ms，但 `incl` 达 210 ms，说明它触发了大量嵌套改写）。

`outside` 是从时间线几何关系派生出来的量，用于把验证器从 pass 归因中剥离：`mlir::verify` 跑在 pass action 的 `E` 之后，因此它必然落在"该 `E` 到同线程下一个事件之间"。推导与交叉验证见 `measurement.md`。

## 6. 覆盖边界

### 没有 action 就没有时长

MLIR 只对第 2 节那三种动作发 action。因此：

- **pass 内部不经过 pattern 的遍历没有 op 级事件**，例如 one-shot-bufferize 的接口分析。这部分只体现为该 pass 的 `self`，无法再细分。要细分必须在那个 pass 内部插 `executeAction`，即修改 pass 实现，与被观测对象不可分离，因此不做。
- **DialectConversion 的改写部分有 `apply-pattern`**（它内部构造 `PatternApplicator`，见 `mlir/lib/Conversion/DialectConversion.cpp:1802`、`:2001`），所以 `convert-*-to-llvm` 这类 pass 是可归因的；只有它周围的类型转换、materialization、操作数重映射等记账没有事件，落在 pass `self` 里。
- 解析、打印、context 与 dialect 初始化、pass manager 组装、分析失效**都不在 trace 跨度内**，不会污染归因（打印与解析的实测代价见 `measurement.md`）。

### 无 `loc` 的 op 无法区分实例

折叠/构造过程中临时产生的 op 没有源位置，同类 op 会合并成一行。从 `.mlir` 文本输入时不构成问题：解析器给每个 op 挂 `FileLineColLoc`，源码行本身就是实例标识。前端若要区分"哪个前端节点慢"，需要在生成的 IR 里带 loc。

### 多线程下百分比不可加

`wall` 是墙钟，`self` 之和是各线程 CPU 时间之和，比值可以超过 100%。脚本检测到多线程会打印提示；需要可加数字时加 `-mlir-disable-threading`。多线程下 `outside` 还会包含线程等待时间，因为它是一个墙钟间隔。

### 嵌套 pipeline 的 `self` 含子 pass 的验证时间

`OpToOpPassAdaptor` 的 span 要等所有子 pass 结束才关闭，而子 pass 的验证发生在其 `E` 之后、adaptor 关闭之前，所以这段验证被算进 adaptor 的 `self`。修正方法是减去子 pass 的 `outside` 之和（实测两者闭合），细节见 `measurement.md`。

## 7. 维护不变量

改动本模块时，以下性质不能被破坏：

1. **只读**：新增的读取只能是 observation，不得影响 `transform()` 的执行或 IR 内容。
2. **默认零开销**：未设置 `OP_TIMING` 时不得注册任何 handler、不得分配缓冲、不得打开文件。
3. **只用公开 API**：当前依赖 `MLIRContext::registerActionHandler`、`tracing::Action`、`PassExecutionAction`、`Pass::getName/getArgument`。不修改 `third_party/llvm-project`，这样升级子模块时无需重新打补丁。
4. **格式向后兼容**：新增字段只能是可选的，读取方必须忽略不认识的字段；改变语义才升 `version`（见 `trace-format.md`）。
5. **分析侧不依赖 MLIR 的类型名或英文描述**：结构化字段（`pass` / `arg` / `op` / `loc`）优先，`desc` 解析只作回退。

## 8. 已知限制与后续

- **验证器只做到单列，没做到精确到 op**。`outside` 给出每个 pass 一份总数，回答不了"哪一段 IR 的验证贵"。要精确到 region 需要在 `verify` 内部插桩（改上游）或自己递归复算 `DominanceInfo`（成本翻倍且不再是观测），当前不做。
- **CI 阈值是经验值**。`--baseline` 已可用，但 1.20 倍与 200 µs 下限需要按实测噪声分布重定，基线存放位置与粒度（输入模型 × pipeline）也待定。
- **多线程口径未成形**。目前只提示百分比不可加，没有给出"CPU 投资"与"关键路径"两种独立输出。
- **若上游补全 `desc`**（在 `PassExecutionAction::print()` / `ApplyPatternAction::print()` 里带 op 名与 loc），采集器可整体退役，改用 `--profile-actions-to` 加本仓库的 `tools/op-timing-report.py`。这是长期跟踪项。
