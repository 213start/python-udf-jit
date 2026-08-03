# 通用循环与类型特化：实现和穿刺验证

## 结论

P0 已按“行为模式 × 类型特化”实现，不再把 FineWeb 算子或某个 UDF 写法固化为语义 opcode。
同一个前端把 generator reduction、显式 `for` reduction 和数值 reduction 归一为 typed CFG/SSA；
后端仅在完整证明“精确 `str`、规范迭代、步长 1、整数计数归约、Unicode 属性谓词”后，选择可复用的
`UnicodeCountProperty` 物理能力。

aarch64-lab-host 的同进程相邻 A→B 中，组合优化相对原始 CinderX JIT UDF 为 **10.81x**。两端使用
同一个 CinderX 生产构建、CPU 32、输入和 7 轮测量；A/B 时两个诊断开关均关闭。该数字包含最终的
运行时依赖 Guard 和 Worker 正缓存路径，是 alphanumeric UDF 的穿刺结果，不是完整 FineWeb pipeline
的发布级收益。

## 实现内容

1. Semantic IR v2 使用 block argument、typed CFG、control edge、通用 operation、Effect/异常顺序和
   canonical hash；Verifier 在 Worker 重新验证类型、支配、边参数和回边。
2. Behavior Profile、Type Evidence、Pattern Analysis 三者分离。Behavior 只做准入/成本分类，Pattern
   从 IR 推导 loop/reduction，Type 单独描述 exactness、元素类型、boxed layout 和 Guard。
3. 通用前端支持 generator/显式循环及文本/数值序列，输出相同的 iterator-loop/reduction 骨架；未知
   Effect、动态调用和不支持的控制流 fail closed。
4. Worker 先经过 call/deopt ROI gate，再重算分析、生成规范 Python Bytecode，并按数据流和类型选择
   backend specialization。Driver hint 仅供比较，不被信任。
5. CinderX 生产补丁新增通用 `UnicodeCountProperty(exact str, property id) -> int64` HIR，覆盖
   alnum、alpha、decimal、digit、numeric、space。Simplify 只认精确 C helper identity 和编译期常量
   property id，LIR 直接调用 native runtime helper。
6. native helper 直接读取 CPython Unicode storage；属性分派在循环外，每个 code point 不再创建 Python
   字符对象、执行方法查找、generator yield 或 boxed integer update。
7. typed full diagnostics 在 CinderX 编译前绑定 generated-code hash，编译后导出结构化 HIR、LIR、机器
   区间，并将每个语义 operation 映射到通用/物理源码行、Bytecode offset、HIR/LIR/Machine ID。
8. 默认参数、closure、primitive global 和精确 builtin identity 都进入可移植依赖摘要；Worker 编译前及
   每次调用前重查 Guard，变化时 fail closed。成功编译结果进入有界 Worker-local LRU，避免重复 lowering
   和 JIT，同时 deopt gate 始终先于正缓存。

## “通用”如何被证明

通用性不等于“任何循环都自动变快”，而是优化准入只依赖语言级结构、数据流和类型，命中同一条件的
任意业务 UDF 都复用同一实现。

| 维度 | 编译器读取的事实 | 明确不读取的内容 |
| --- | --- | --- |
| 行为 | loop/backedge、branch、reduction、Effect、风险和 ROI | pipeline、算子、模块、函数名 |
| 类型 | exact `str`、`unicode.scalar`、`int64` accumulator、Guard | FineWeb schema 或 Data-Juicer 类型名 |
| Pattern | iterator loop、`+1` induction、`binary.add` reduction、predicate dataflow | generator 或显式循环的源码拼写 |
| 后端 | exact helper identity、常量 property id、精确 Unicode Guard | alphanumeric filter 的业务阈值和算子身份 |

测试从三面约束这一点：generator 与显式循环共享物理化器；alnum/alpha/space 共享同一个 HIR/runtime
operation；数值 list reduction 进入同一 IR/Pattern 框架但不会误入 Unicode specialization。另有畸形
IR 负例把 induction step 从 1 改为 0，Verifier 允许其作为合法程序，但 backend matcher 必须拒绝，
证明物理化依赖完整语义数据流而非“看见几个相似 opcode”。

## 诊断证据链

full diagnostic worker 生成了 28 个内容寻址产物，Bundle 状态为 `complete`，CLI 校验结果为
`valid` 且 `executed_content=false`。

| 层 | 关键证据 |
| --- | --- |
| Source / 原 Bytecode | source ranges、原始 Bytecode JSON/disassembly 均 available |
| Typed Semantic | 17 个通用 operation，1 个自然循环和 1 个 `binary.add` reduction |
| Behavior / Type / Pattern | `numeric_loop`、exact `str`、`unicode.scalar`、`iterator_loop` 独立记录 |
| 通用 Lowering | 完整规范循环、exact-type guard 和所有 operation→generated line 映射 |
| 物理 Lowering | 循环折叠为一次 `unicode.count_property`，后续阈值计算仍为通用表达式 |
| 原始/生成 Bytecode | 17/17 operation 同时有原始和生成 Bytecode offset |
| CinderX HIR | bytecode offset 202 对应唯一 `UnicodeCountProperty`（HIR 51） |
| CinderX LIR | HIR 51 对应 Move 244、Move 245、Call 98 |
| Machine | 对应 machine range 99/100/101，大小分别为 4/4/8 bytes |

17 个语义 operation 中 16 个可直接关联到 HIR、LIR 和机器区间；剩余 entry argument 在 CinderX
优化/合成 origin 后没有一一机器归属，但它仍有生成 Bytecode 定位。结构化后端共导出 97 个 HIR
node、249 个 LIR node、237 个 machine range，JIT code size 为 2520 bytes。ranges 隐私策略下对阈值
和输入样本做了全 bundle canary 扫描，未发现明文泄漏。

独立文本 dump 和 native symbol 反汇编与结构化证据一致：最终 HIR 是
`GuardType<UnicodeExact> -> UnicodeCountProperty -> PrimitiveBox<CInt64>`；AArch64 JIT code 直接
`bl JITRT_UnicodeCountProperty`，helper 中先选择 property 和 Unicode storage kind，再进入紧凑扫描循环。

## 性能结果

两端都固定 CPU 32、7 rounds、每轮 400 calls / 4,172,800 characters，checksum 均为 400，
`UDFJIT_DIAGNOSTICS=off` 且 `PYTHONJITUDFDIAGNOSTICS=0`。

| 测量 | Median | 相对候选 typed |
| --- | ---: | ---: |
| 原始 CinderX JIT UDF | 460.06 ms | 10.81x |
| UDF JIT + `UnicodeCountProperty` CinderX HIR | 42.56 ms | 1.00x |

高收益来自跨层配合，而不是一条“神奇业务规则”：UDF JIT 恢复循环/归约/类型事实并选择物理实现；
CinderX 对 helper call 去虚拟化，保留 exact-type Guard，将计数结果保持为 primitive，最后直接调用 native
Unicode scan。原 UDF 在 CinderX 中仍隔着 generator/method dispatch，因此拿不到该收益。

## 诊断隔离

- 生产 patch series 只包含运行能力；结构化 origin export 仍是单独 diagnostic overlay。
- `diagnostics=off` 时 `TypedRegionCompiler` 直接调用普通 backend，不探测 diagnostic-aware 方法；backend
  不绑定 code hash，正常 Worker 不导入 `diagnostics.worker_runtime`。
- 一个全新 subprocess 已实际编译并执行 typed region，同时断言该诊断模块不在 `sys.modules`。
- 性能 A/B 未应用诊断 overlay；full Bundle 的耗时没有进入任何性能比值。

## 验证范围与后续工作

远端最终代码已通过 74 项 Python typed/diagnostic 测试、CinderX 4 项 Python 语义/HIR 测试和 2 项
RuntimeTests；本地全量回归为 492 passed、20 skipped 和 434 个 subtest passed。生产 patch 已从
upstream 重新应用并通过规范化 tree hash 校验，full bundle 通过只读 CLI 校验且
`executed_content=false`。
精确字符串子类、未知 property、非 Unicode loop、畸形 induction 和无关 helper 均有拒绝/回退覆盖。

仍需完成的下一层验证是：将该 typed path 接入真实 `pipeline_text_fineweb_full_min` Worker 生命周期，
记录 guard hit/deopt/compile amortization，并在 CPU 频率受控环境做交替 ABBA 和完整 pipeline wall-clock。
在此之前，本报告只证明通用机制、完整诊断链和明确的 stage-level 性能潜力。

原始结构化数据见
`docs/reports/evidence/2026-08-03-generic-typed-loop-specialization-ab.json`。
