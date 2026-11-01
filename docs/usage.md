# 接入与使用指南

本文档介绍如何在宿主编译器中集成 op-timing 采集模块、执行性能追踪、解析聚合报告（`report.json`）并配置相关控制开关。

---

## 1. 构建与集成

### 1.1 CMake 依赖引入

将本仓库作为子目录引入宿主工程，并链接接口目标：

```cmake
add_subdirectory(<path-to-op-timing>)
target_link_libraries(<your-tool> PRIVATE op-timing)
```

本工程暴露 CMake 接口目标 `op-timing`（别名 `optiming::op-timing`）。该目标仅导出头文件包含路径并依赖 `MLIRIR` 与 `MLIRSupport` 组件，自身不产生二进制编译产物。

### 1.2 宿主驱动代码集成

#### 方式一：基于 Dialect Registry 扩展（推荐）

若宿主编译器采用 `MlirOptMain` 或在初始化 `MLIRContext` 时加载 Dialect Registry 扩展，仅需注册一行扩展回调：

```cpp
#include "OpTimingProfiler.h"

// 在注册 Dialect 的阶段注入扩展
registry.addExtension(optiming::installOpTimingOnContext);
```

#### 方式二：手动注入 Context 实例

若宿主驱动自行管理 `MLIRContext` 的生命周期，可在 Context 初始化完成后直接调用：

```cpp
#include "OpTimingProfiler.h"

// 尝试注入 Handler；未配置环境变量 OP_TIMING 时直接返回 false
optiming::installOpTimingIfRequested(*context);
```

上述两种方式均具备幂等性。若目标 `MLIRContext` 已经绑定了 Action Handler（例如启动参数中指定了 `--profile-actions-to`），注册逻辑将主动放弃挂载并输出提示日志，以确保宿主功能不受干扰。

### 1.3 宿主类型适配矩阵

| 宿主架构分类 | 采集器适用性 | 接入要求 |
| --- | --- | --- |
| 使用 `mlir::PassManager` 调度 | 完全支持 | 注册 Dialect Registry 扩展即可自动覆盖 Pass 执行、Pattern 应用及贪心重写驱动等动作 |
| 使用 `MLIRContext` 但自定义 Pass 调度器 | 支持 | 需在自定义调度循环中显式派发 Action（参见 1.4 节） |
| 非 MLIR 架构编译器 | 头文件不适用 | 按照 `docs/trace-format.md` 规范直接输出 JSON 事件流，分析脚本保持通用 |

### 1.4 自定义 Pass 调度的事件派发

对于自行实现 Pass 调度机制的编译器，需在调度执行点显式调用 `executeAction`。基于 `mlir::tracing::ActionImpl` 定义派生动作类型：

```cpp
#include "mlir/IR/Action.h"

struct MyPassAction : mlir::tracing::ActionImpl<MyPassAction> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MyPassAction)
  using ActionImpl::ActionImpl;
  static constexpr llvm::StringLiteral tag = "pass-execution";
};

// 执行调度并绑定目标 Operation
context.executeAction<MyPassAction>([&] {
  pass.runOnOperation(op);
}, {op});
```

`ActionImpl` 要求派生类声明静态成员 `tag`。`getContextIRUnits()` 将返回构造时传入的 Operation 集合 `{op}`。若 `tag` 配置为 `"pass-execution"`，该动作将自动归入 `per_pass` 视图；若使用其他标识符，则记录于 `per_scope` 视图。

---

## 2. 运行与分析流程

```bash
# 1. 采集：配置环境变量 OP_TIMING 指定输出路径（"-" 表示标准错误输出）
OP_TIMING=/tmp/op_trace.json <your-tool> input.mlir -o /dev/null

# 2. 离线聚合：唯一参数是追踪文件，脚本不接受任何选项
python3 tools/op-timing-report.py /tmp/op_trace.json
```

每次运行固定执行三项输出任务，无需额外开关：

1. 在控制台打印采集概况（总耗时、耗时构成三段、无动作区间）与三张排名表格（Pass、算子、算子×动作，各展示前 20 项）；
2. 在与追踪文件同目录、同基名下写出 `<trace>.report.json`；
3. 写出 `<trace>.report.xlsx`。

原始追踪文件可直接导入 Chrome 追踪查看器（`chrome://tracing`）、[Perfetto UI](https://ui.perfetto.dev) 或 [Speedscope](https://speedscope.app)，进行调用栈时序展开与线程并发分析。

### 产物职责对比

| 产物文件 | 数据特性 | 应用场景 |
| --- | --- | --- |
| `<trace>.json` | 原始 `B`/`E` 时间戳事件流，不包含聚合自耗时或统计计数值 | 图形化时间线渲染、线程并发状态分析、对接第三方追踪平台 |
| `<trace>.report.json` | 离线计算得出的结构化聚合结果（涵盖 `self`、`incl`、`count`、`worst`、`outside`） | 自动化指标提取、报表二次处理 |
| `<trace>.report.xlsx` | 随 `report.json` 一同生成的工作簿：`总览`、`Pass`、`算子`、`算子×动作`、`说明` 五个工作表，数值与 `report.json` 逐字段一致（设计说明见 `docs/excel.md`） | 人工浏览、评审汇报、表格工具内二次统计 |

---

## 3. 聚合报告（`report.json`）格式规范

全部时间字段统一为秒（s）；原始追踪事件的时间戳为整数微秒（`docs/trace-format.md`），脚本在聚合时换算。`per_scope` 聚合键为 `(op, scope)`，各行按 `op` 分组（组间按 `self` 总和降序、组内按 `self` 降序）。报告不含版本号：解析器忽略无法识别的字段，字段语义变更时直接更新本文档。

```json
{
  "trace": "<输入追踪文件路径>",
  "total_s": 1.81738,
  "inside_actions_s": 1.11124,
  "outside_actions_s": 0.77885,
  "threads": 1,
  "scopes": 45033,
  "unit": "s",
  "per_operation": [
    {
      "op": "linalg.generic",
      "sym": "",
      "loc": "model.mlir:1356:13",
      "count": 32,
      "self": 0.0010245,
      "incl": 0.0010245,
      "worst": 0.0000452
    }
  ],
  "per_scope": [
    {
      "op": "func.func",
      "scope": "pass: Canonicalizer",
      "count": 3,
      "self": 0.0067802,
      "incl": 0.2367901,
      "worst": 0.0820004
    }
  ],
  "per_pass": [
    {
      "scope": "pass: ConvertFuncToLLVMPass",
      "arg": "convert-func-to-llvm",
      "count": 1,
      "self": 0.04562,
      "incl": 0.07645,
      "worst": 0.04562,
      "outside": 0.13396
    }
  ]
}
```

### 核心指标说明

| 字段 | 计量单位 | 语义与应用方法 |
| --- | --- | --- |
| `self` | 秒（s） | **自耗时**：当前区间耗时剔除内部所有嵌套子动作后的净值。**系统瓶颈排查的核心依据**。 |
| `incl` | 秒（s） | **包含耗时**：当前区间的起止累计总耗时（包含全部子动作）。**分析跨层级耗时扩散时参考**。 |
| `count` | 次数 | 动作触发累计频次（在改写视图中对应候选 Pattern 的匹配尝试次数）。 |
| `worst` | 秒（s） | 单次触发的最大耗时值。 |
| `outside` | 秒（s） | `pass-execution` 结束事件（`E`）至同线程下一事件之间的时间间隔，对应 Pass 后的 IR 验证与分析失效开销。多线程宿主下该值包含线程阻塞等待，**不可跨行相加**，且与无动作区间存在重叠（同一段时间不计两次，见 `docs/excel.md` 第 3.1 节的耗时构成）（详见 `docs/measurement.md` 第 1.4 节）。 |
| `scopes` | 次 | 计入统计的动作区间总数（不含未闭合区间与孤立结束事件）。 |
| `arg` | 字符串 | Pass 对应的命令行选项名称（例如 `canonicalize`），用于将分析结果映射回编译管线配置。 |

- **`per_operation` 视图**：仅统计改写类动作（其 IR 单元直接对应被操作的 Operation），反映哪些 Operation 实例的改写开销显著，不包含 Pass 自身的调度逻辑。
- **`per_scope` 视图**：按 Operation 类型 × 动作名称聚合的全集展开，包含贪心重写驱动循环与 Pass 本身；同一 Operation 的各行相邻排列（组间按 `self` 总和降序、组内按 `self` 降序），用于观察同一算子被多条 Pattern 重复作用的情况。源码行级归因由 `per_operation` 视图承担。

指标更深层次的数学关联与解读规则详见 `docs/measurement.md`；Excel 工作簿的结构、列定义与阅读指南详见 `docs/excel.md`。

### 未计入统计的数据

分析脚本在控制台显式报告两类不进表的数据，避免把它们的缺失误读为零开销：

| 情形 | 控制台输出 | 处理方式 |
| --- | --- | --- |
| 进程在动作执行中退出，最后一个区间只有开始事件 | `warning: N span(s) never closed` | 计数并排除；不纳入 `scopes`、`self`、`inside_actions_s` |
| 无动作区间（该线程不处于任何动作区间内的时段） | `无动作区间（各线程并集） … 分布在 N 段` 与逐线程合计 | 单独报告。它是各线程并集，不可跨线程相加；扣除 `outside` 已覆盖的部分后，余量在「耗时构成」中作为其余空闲一行列出 |

---

## 4. 独立验证构建

在不依赖外部宿主工程源码的环境下，可通过独立构建编译语法检查测试：

```bash
cmake -S . -B /tmp/build-op-timing -DMLIR_DIR=<path-to-llvm-install>/lib/cmake/mlir
cmake --build /tmp/build-op-timing
```

该构建仅编译 `tests/syntax-check.cpp`，旨在验证头文件对宿主工程环境的解耦性。

分析脚本的回归测试无需构建环境，直接运行 `python3 tests/test_report.py` 即可；独立构建时会一并注册为 `ctest` 用例 `op-timing-report`。

---

## 5. 编译与控制参数一览

| 配置项 | 归属 | 功能说明 |
| --- | --- | --- |
| `OP_TIMING=<path>` | 本模块 | 指定追踪文件写出路径。未设置该环境变量时保持零开销。 |
| `--verify-each=false` | 上游 `mlir-opt` | 关闭每个 Pass 执行完毕后的全量 IR 验证。该参数仅影响整体物理时间和报告中的 `outside` 指标，不影响 Pass 的 `self` 自耗时。仅单线程采集时可将 `outside` 的降幅读作验证开销。 |
| `--emit-bytecode` | 上游 `mlir-opt` | 启用 MLIR 字节码输出格式替代默认文本输出，避免文本格式化产生的开销（基准测试下约可降低 0.25 秒/MB）。 |
| `-o <file>` | 上游 `mlir-opt` | 指定编译输出目标。使用 `-o /dev/null` 时编译器仍会执行 IR 格式化流程；若需规避该开销需配合 `--emit-bytecode`。 |
| `-mlir-timing` | 上游 PassTiming | 输出进程生命周期预算（Parser、Pass、Output 及 Rest），可与本工具并发启用。 |
| `-mlir-disable-threading` | 上游运行时 | 禁用 PassManager 的多线程并发调度，使各线程时间开销具备严格可加性。 |
