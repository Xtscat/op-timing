# 接入与使用

## 1. 构建与接入

### 1.1 作为子目录引入

```cmake
add_subdirectory(<path-to-op-timing>)
target_link_libraries(<your-tool> PRIVATE op-timing)
```

接口目标名 `op-timing`，别名 `optiming::op-timing`。它只导出 include 目录并链接 `MLIRIR` / `MLIRSupport`，本身不含编译产物。

### 1.2 driver 侧改动

若宿主使用 `MlirOptMain`（或任何在构造 `MLIRContext` 时应用 registry 扩展的 driver），注册一行扩展即可：

```cpp
#include "OpTimingProfiler.h"
...
registry.addExtension(optiming::installOpTimingOnContext);
```

若宿主自己构造 `MLIRContext`，直接调用：

```cpp
optiming::installOpTimingIfRequested(*context);   // 未设置 OP_TIMING 时返回 false
```

两种方式都是幂等的；同一 context 已经装有 action handler 时（例如传了 `--profile-actions-to`）不会安装，只输出一行提示。

### 1.3 三类宿主

| 宿主 | 采集器是否可用 | 需要做什么 |
| --- | --- | --- |
| 使用 `mlir::PassManager` | 可用，自动覆盖三种 action | 注册扩展，一行 |
| 使用 `MLIRContext` 但自写 pass 调度 | 可用，需要在自写的调度处发 action | 见 1.4 |
| 完全不复用 MLIR | 不适用 | 按 `trace-format.md` 自行发出事件，分析脚本不变 |

### 1.4 自写 pass 调度的接入

宿主自己的调度点需要显式调用 `executeAction`，动作类型按 `mlir::tracing::ActionImpl` 定义：

```cpp
struct MyPassAction : mlir::tracing::ActionImpl<MyPassAction> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MyPassAction)
  using ActionImpl::ActionImpl;
  static constexpr llvm::StringLiteral tag = "pass-execution";
};

context.executeAction<MyPassAction>([&] { pass.runOnOperation(op); }, {op});
```

`ActionImpl` 只要求派生类提供 `tag`；`getContextIRUnits()` 返回构造时传入的 `{op}`。tag 用 `pass-execution` 时该动作进入 `per_pass` 视图，用其它值时原样显示在 `per_scope` 里。

## 2. 运行

```bash
# 采集：OP_TIMING 指向输出文件，"-" 表示 stderr
OP_TIMING=/tmp/op_trace.json <your-tool> input.mlir -o /dev/null

# 聚合：默认只写 JSON，终端两行摘要
python3 tools/op-timing-report.py /tmp/op_trace.json
# -> /tmp/op_trace.json.report.json

# 额外打印三张人读表格
python3 tools/op-timing-report.py /tmp/op_trace.json --table --top 20

# 与基线比较：任一行 self 超过基线的 1.2 倍时退出码为 1
python3 tools/op-timing-report.py new.json --baseline base.report.json
```

原始 trace 可以直接拖进 `ui.perfetto.dev`、`chrome://tracing` 或 `speedscope.app`，得到火焰图、时间线与多线程并发视图。

### 两个产物，职责不同

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `<trace>.json` | 原始 `B`/`E` 事件流，不含 self time、不含调用次数 | 图形化时间线、并发视图、第三方工具 |
| `<trace>.report.json` | 聚合结果：self / incl / count / worst / outside | 回归对比、CI 基线、程序化查询 |

## 3. `report.json` 字段

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

| 字段 | 含义 |
| --- | --- |
| `self` | 已扣掉内部嵌套子动作的时间。**判断瓶颈看这一列。** |
| `incl` | 含内部嵌套的总时间。**定位问题看这一列。** |
| `count` | 该动作被触发次数（pattern 视图里就是候选尝试次数） |
| `worst` | 单次最大耗时 |
| `outside` | `pass-execution` 的 `E` 之后、同线程下一个事件之前的墙钟间隔，即验证器与分析失效，见 `measurement.md` |
| `arg` | pipeline 选项名（如 `canonicalize`），把 pass 行映射回命令行参数 |

`per_operation` 只聚合改写类动作（其 IR unit 就是被改写的 op），因此回答"哪个 op 被改写得慢"，不含 pass 自身逻辑；`per_scope` 是"动作名 × op 实例 × 源位置"的全集。

如何解读这些数字、哪些开关会影响它们，见 `measurement.md`。

## 4. 独立构建

```bash
cmake -S . -B /tmp/build-op-timing -DMLIR_DIR=<MLIR cmake 目录>
cmake --build /tmp/build-op-timing
```

`MLIR_DIR` 形如 `<llvm 安装目录>/lib/cmake/mlir`。独立构建只编译 `tests/syntax-check.cpp`，作用是证明本模块不依赖任何宿主仓库的头文件；没有可执行产物。

## 5. 开关一览

| 名字 | 归属 | 作用 |
| --- | --- | --- |
| `OP_TIMING=<path>` | 本模块 | 采集开关与输出路径；未设置时零开销 |
| `--verify-each=false` | 上游 `mlir-opt` | 关掉 pass 之后的全量验证。只影响墙钟与 `outside` 列，不影响 pass `self` |
| `--emit-bytecode` | 上游 `mlir-opt` | 用二进制输出替代文本打印，省掉约 250 ms/1 MB 的格式化成本 |
| `-o <file>` | 上游 `mlir-opt` | 输出路径，默认 `-`（stdout）；`-o /dev/null` 仍然会格式化输出 |
| `-mlir-timing` | 上游 PassTiming | 输出 Parser / pass / Output / Rest 的进程预算分解，可与此工具并行使用 |
| `-mlir-disable-threading` | 上游 | 让 `self` 之和与墙钟可加，多线程分析时使用 |
