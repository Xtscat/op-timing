# 系统设计

本文档阐述 op-timing 的系统架构设计、对 MLIR 运行时设施的依赖机制、指标度量模型以及有效性约束条件。使用指南参见 `usage.md`，数据传输协议参见 `trace-format.md`，实测评估与参考基准参见 `measurement.md`。

---

## 1. 背景与设计动机

在 MLIR 编译管线性能调优过程中，定位瓶颈通常需要明确两个维度的信息：**产生耗时的 Pass** 以及**触发该耗时的具体 Operation 实例及其源文件位置**。

MLIR 上游原生提供的性能分析工具有其粒度局限：

| 工具 | 采集粒度 | 局限性说明 |
| --- | --- | --- |
| `-mlir-timing` | Pass 级聚合 | 仅统计 Pass 级别的累计耗时，未提供任何 Operation 级别的执行信息。 |
| `--profile-actions-to` | Pass 及单次 Pattern 尝试 | 导出的描述字段（`desc`）仅包含 Operation 类型名称（如 `func.func`），缺少具体 Operation 实例标识、符号名及源位置。 |

当原生分析工具显示某一 Pass（例如 `Canonicalizer`）耗时较高时，开发人员无法进一步下钻到具体是哪一个函数的哪一行 IR 导致了性能衰减。

MLIR 内建的 `Action` 框架（`mlir::tracing::Action`）在执行动作时保存有关联的 IR 单元（通过 `Action::getContextIRUnits()` 暴露）。`PassExecutionAction` 与 `ApplyPatternAction` 均会将当前处理的 Operation 注册到该上下文中。本模块的设计目标即在于提取该上下文中的 Operation 实例、符号名与源码位置，将其与动作的起止时间戳同步输出为符合 Chrome Trace Event 规范的结构化追踪数据，并提供完备的离线聚合分析工具。

### 覆盖范围

本模块监控范围严格限定于 `mlir::PassManager` 的调度生命周期内（自 IR 文本或字节码解析完成开始，至最后一个 MLIR Pass 执行结束）。

**代码生成后端不在度量范围内**：MLIR 不为目标机代码生成后端（LLVM CodeGen 及各类专用硬件后端）派发 Action。此外，后端实现随目标架构而变，因此本模块不对后端提供测量支持，以确保分析时间线的纯净性与可复现性。

---

## 2. 数据源：MLIR Action 框架

在 MLIR 上游实现中，`MLIRContext::executeAction` 涵盖以下三类核心动作来源：

| Tag 标识 | Action 类型 | 源码派发位置 | 携带的 IR 单元（Context IR Units） |
| --- | --- | --- | --- |
| `pass-execution` | `PassExecutionAction` | `mlir/lib/Pass/Pass.cpp` | 当前 Pass 作用的根 Operation（如 `func.func @main`） |
| `apply-pattern` | `ApplyPatternAction` | `mlir/lib/Rewrite/PatternApplicator.cpp` | 当前匹配并尝试改写的目标 Operation |
| `GreedyPatternRewriteIteration` | `GreedyPatternRewriteIteration` | `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp` | 贪心驱动循环所在 Region 的父 Operation |

Action 处理器签名定义为 `MLIRContext::HandlerTy`：

```cpp
std::function<void(function_ref<void()>, const tracing::Action &)>
```

Handler 接收 `tracing::Action` 基类引用。采集器基于多态接口进行通用处理，无需针对具体 Action 类型显式注册回调。上述第三类贪心重写驱动动作可自动被捕获并纳入统计。

`apply-pattern` 动作由 `PatternApplicator::matchAndRewrite` 在遍历改写候选时逐一触发，因此能够捕获单个 Pattern 在具体 Operation 实例上的单次尝试开销与频次。

---

## 3. 采集器架构与实现机制

`OpTimingProfiler::operator()` 的事件拦截逻辑如下：

```cpp
const auto begin = steady_clock::now();
{
  std::lock_guard<std::mutex> guard(mutex);
  printEvent(action, "B", begin);
}
transform();
const auto end = steady_clock::now();
{
  std::lock_guard<std::mutex> guard(mutex);
  printEvent(action, "E", end);
}
```

关键设计考量包括：

1. **Operation 标识提取的时序敏感性**：改写操作执行成功后，原始 Operation 实例可能已被释放或替换。因此，Operation 的类型名称、符号属性及源码位置必须在调用 `transform()` 之前读取并记录；结束事件（`E`）仅记录对应的时间戳。
2. **并发编译性能隔离（细粒度锁）**：时间戳获取（`steady_clock::now()`）及编译器实际转换逻辑（`transform()`）均在互斥锁保护区外执行，互斥锁仅用于串行化底层输出流的写出操作，避免采集器因锁争用对多线程编译过程造成人为串行化偏差。
3. **线程级事件归属**：调用 `llvm::get_threadid()` 获取操作系统级线程 ID，调用栈区间的开始与结束事件配对严格在相同线程内完成。
4. **RAII 生命周期保障**：利用函数局部静态对象的析构机制在进程正常终止时自动输出 JSON 数组闭合标识符 `]`，宿主驱动无需注册额外的清理钩子即可保证输出 JSON 文件的完整性。
5. **统一相对时间基准**：所有时间戳均以采集器初始化时刻为基准原点，以微秒为单位记录浮点偏移量，确保全生命周期事件在统一时间轴上对齐。

### 只读性保障

Handler 仅调用只读查询接口：

- `Action::getTag()`
- `Action::print()`（生成 `desc` 字段）
- `Operation::getName()`、`Operation::getAttrOfType<StringAttr>("sym_name")`、`Operation::getLoc()`
- `PassExecutionAction::getPass()` 及其 `getName()` / `getArgument()`

采集器不创建、修改、替换或删除任何 IR 节点，亦不使用 `ExecutionContext` 提供的跳过能力，从架构上杜绝影响编译器语义输出的可能性。

---

## 4. 挂载机制与生命周期

采集器提供两种接入途径，均不破坏宿主原有的驱动逻辑与构建配置。

### 途径一：基于 Dialect Registry 扩展（推荐）

适用于复用 `MlirOptMain` 或在初始化阶段加载 Registry 扩展的驱动：

```
MlirOptMain 构造 MLIRContext
  └─ MLIRContext 构造函数调用 registry.applyExtensions(this)
       └─ optiming::installOpTimingOnContext    ← 注册 Action Handler
```

在此模式下，Handler 在 IR 解析及任何 Pass 执行之前完成注入，无需修改核心调用流程。

### 途径二：直接显式注入

针对完全自定义驱动逻辑的场景，可直接在构造完成的 `MLIRContext` 实例上调用：

```cpp
optiming::installOpTimingIfRequested(*context);
```

### Handler 独占性与安全保护

`MLIRContext` 内部仅支持单一 Action Handler 实例。采集器在注入前会显式调用 `hasActionHandler()`：

- 若已存在注册的 Handler（例如宿主通过参数启用了 `--profile-actions-to`），采集器将主动终止安装并输出警告信息，避免覆盖宿主既有配置导致外部工具异常。
- 注入逻辑具备幂等性：同一 `MLIRContext` 实例重复触发注册将直接返回，不会引发重复包装。

### 环境变量控制与零开销设计

当未设置 `OP_TIMING` 环境变量时，`installOpTimingIfRequested` 直接返回 `false`，不调用 `registerActionHandler`。`MLIRContext::hasActionHandler()` 保持为 `false`，底层 `MLIRContext::executeAction` 持续命中 `LLVM_UNLIKELY(hasActionHandler())` 的零开销分支，运行时行为与未引入该模块时完全一致。

---

## 5. 离线聚合与派生指标模型

`tools/op-timing-report.py` 负责离线后处理分析：按线程 ID（`tid`）维护调用栈，将 `B`（Begin）与 `E`（End）事件配对为执行区间（Span），并依据调用嵌套关系计算各项耗时指标。

系统提供三个正交的聚合视图：

| 视图 | 聚合维度 | 分析目标 |
| --- | --- | --- |
| `per_pass` | Pass 名称 | 分析 Pass 级性能分布；`outside` 指标反映 Pass 后的框架验证与分析失效代价 |
| `per_operation` | Operation 类型 + 符号名 + 源码位置 | 分析改写类动作在具体 Operation 实例上的开销分布 |
| `per_scope` | 动作标识 × Operation 实例 × 源码位置 | 全集视图，包含 Pass 执行、贪心重写驱动循环及具体改写动作 |

### 指标定义

- **自耗时（`self`）**：当前区间的总耗时扣除其内部所有直接子动作区间耗时后的净值。**系统瓶颈分析以该指标为核心依据**。
- **包含耗时（`incl`）**：当前区间自开始至结束的累计耗时，包含所有内部嵌套子动作。**定位跨层级耗时扩散时参考该指标**（例如某 Pass 自身逻辑耗时较低，但包含耗时较高，表明其主要开销由其触发的子改写驱动产生）。
- **执行次数（`count`）**：动作被触发的累计频次（在 Pattern 视图下即为候选匹配尝试次数）。
- **单次最大耗时（`worst`）**：该聚合项单次调用的最大耗时。
- **框架非区间耗时（`outside`）**：`pass-execution` 的 `E` 事件至同线程下一事件之间的墙钟间隔，用于隔离框架级验证器（`mlir::verify`）与分析失效（`AnalysisManager::invalidate`）的开销。推导原理参见 `measurement.md`。

---

## 6. 覆盖边界与有效性约束

### 1. 非 Action 路径的不可观测性

系统仅能观测通过 `MLIRContext::executeAction` 派发的动作。因此：

- **未通过 Pattern 机制实现的 Pass 内部遍历无法细分到 Operation**：例如 `OneShotBufferize` 的接口分析逻辑不产生 Operation 级 Action，这部分开销完全归入该 Pass 的 `self` 耗时。细分该类逻辑需侵入 Pass 内部修改源码，违背无侵入观测原则。
- **DialectConversion 改写阶段完全可观测**：由于其内部通过 `PatternApplicator` 执行改写，`convert-*-to-llvm` 类 Pass 的改写过程具备完整的 Operation 归因能力；仅类型转换、类型具体化（Materialization）及操作数重映射等辅助簿记逻辑计入 Pass 的 `self` 耗时。
- **初始化与 I/O 阶段不污染归因**：文本解析、IR 打印、上下文及 Dialect 初始化均处于 Trace 跨度之外，不会计入 Pass 或 Operation 的耗时统计。

### 2. 临时 Operation 的源位置缺失

在常量折叠（Constant Folding）或规范化过程中临时构造的 Operation 缺乏物理源码位置信息（`loc` 为空）。此类 Operation 会按类型聚合为统一记录。对于由源码文件直接解析生成的 IR，解析器附带 `FileLineColLoc`，行号信息天然构成实例的区分标识。

### 3. 多线程模式下的指标可加性约束

`wall_ms` 为墙钟物理时间，各动作的 `self` 耗时为所有线程 CPU 时间的离散累加，两者的比值在并发场景下可超出 100%。当后处理脚本检测到多线程事件时将输出提示信息。若需要严格可加的性能统计指标，需启用 `-mlir-disable-threading` 单线程模式。在多线程模式下，`outside` 亦会受线程调度等待延迟影响。

### 4. 嵌套 Pipeline 的自耗时校准

`OpToOpPassAdaptor` 的 Span 需等待其包含的所有子 Pass 结束方可关闭。子 Pass 执行完毕后触发的验证逻辑处于其自身的 `E` 之后、但仍处于 Adaptor 的 Span 内部，因此该部分验证开销会被计入 Adaptor 的 `self` 耗时。对此类 Adaptor 行进行校准时，需扣除其包含的所有子 Pass 的 `outside` 之和。

---

## 7. 维护不变量

对本模块进行迭代维护时，必须保证以下不变量：

1. **只读性约束**：新增逻辑仅能读取上下文状态，严禁修改 IR 结构或干预 `transform()` 的控制流。
2. **默认零开销**：当未配置 `OP_TIMING` 时，严禁注册 Handler、严禁分配内部缓冲区或执行文件 I/O。
3. **公共 API 依赖**：仅依赖 MLIR 公共头文件接口（`MLIRContext::registerActionHandler`、`tracing::Action`、`PassExecutionAction` 等），杜绝依赖私有实现或对 LLVM/MLIR 上游打补丁。
4. **格式向前兼容**：追踪文件新增字段必须设计为可选字段；仅在打破现有解析契约时升级协议版本号（Schema Version）。
5. **结构化字段优先**：后处理解析逻辑优先读取结构化字段（`pass`、`arg`、`op`、`loc`），文本描述字段（`desc`）仅作为后备降级手段。

---

## 8. 局限与演进方向

- **验证器细粒度归因受限**：`outside` 指标目前提供 Pass 维度的验证与失效总和，无法进一步细分至具体的 Region 或 Operation。下钻细分需要修改上游 `mlir::verify` 内部实现。
- **回归比对阈值整定**：当前 `--baseline` 采用固定比率（1.20 倍）与固定截断下限（200 µs），后续需支持基于实际测试环境噪声方差动态整定阈值。
- **并发分析模型演进**：后续计划分离“累计 CPU 消耗”与“关键路径（Critical Path）延迟”两种分析视图。
- **上游生态跟踪**：若未来 MLIR 上游在 `Action::print()` 中原生补全 Operation 实例与源码位置信息，离线报告脚本可直接对接 `--profile-actions-to` 生成的数据流。
