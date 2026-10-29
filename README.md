# op-timing

面向 MLIR 系编译器的 op 级编译耗时采集与分析。

上游 `-mlir-timing` 只给 pass 级耗时，`--profile-actions-to` 的 `desc` 里只有 op **类型**；两者都无法回答"慢在哪个 op 的哪一行"。本模块读取 MLIR `Action` 框架已有的 IR unit 关联，把**动作耗时 + 它作用的 op + 符号名 + 源位置**写成带版本标记的 Chrome Trace，再用一个只认事件字段的脚本聚合。

覆盖范围：MLIR PassManager 之内，从解析完成到最后一个 MLIR pass 结束。代码生成后端不在范围内。

## 文件

| 路径 | 内容 |
| --- | --- |
| `include/OpTimingProfiler.h` | header-only 采集器：只读 action handler，写 Chrome Trace |
| `tools/op-timing-report.py` | 聚合脚本：B/E 配对 → `self` / `incl` / `count` / `worst` / `outside`，支持基线比对 |
| `tests/syntax-check.cpp` | 仅在独立 configure 时编译，验证 header 不依赖宿主仓库 |
| `docs/design.md` | 为什么这样实现：Action 框架、挂载点、只读性与边界、维护不变量 |
| `docs/usage.md` | 接入（三类宿主）、运行、`report.json` 字段、开关一览 |
| `docs/measurement.md` | 测量口径、参考数据、复现命令 |
| `docs/trace-format.md` | 文件格式契约（schema `op-timing` v1），第三方采集器的接入点 |

## 接入

```cmake
add_subdirectory(<path-to-op-timing>)
target_link_libraries(<your-tool> PRIVATE op-timing)
```

```cpp
#include "OpTimingProfiler.h"
...
registry.addExtension(optiming::installOpTimingOnContext);   // 一行
```

运行：

```bash
OP_TIMING=/tmp/op_trace.json <your-tool> input.mlir -o /dev/null
python3 tools/op-timing-report.py /tmp/op_trace.json --table
```

`OP_TIMING` 未设置时不注册任何 handler，`MLIRContext::hasActionHandler()` 保持 false，未开测量的运行与没有这段代码完全等价。

## 其它编译器

- 用 `mlir::PassManager` 的编译器：一行扩展，自动覆盖 pass / pattern / greedy 三类动作。
- 用 `MLIRContext` 但自写 pass 调度的编译器：在自写调度处调用 `context.executeAction<YourAction>(fn, {op})`，见 `docs/usage.md` 1.4。
- 不复用 MLIR 的编译器：header 不适用，但按 `docs/trace-format.md` 输出同名字段后，`tools/op-timing-report.py` 无需改动即可读取。

`docs/measurement.md` 的实测数据取自 MLIR-NorthStar 的 `NS-opt15` driver（17-pass 的 linalg-on-tensors → LLVM dialect 链路），作为该工具的参考样例，本模块不依赖该仓库。
