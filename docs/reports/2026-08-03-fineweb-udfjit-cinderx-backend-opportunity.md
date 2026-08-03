# FineWeb UDF JIT × CinderX 后端优化重新分析

**日期：** 2026-08-03
**workload：** `pipeline_text_fineweb_full_min`
**结论状态：** 诊断缺口已补全；后端候选已完成 stage-level A/B；尚未形成 full-pipeline 正式收益

**设计订正：** 本报告最初提出的三个业务 `text-scan operation/matcher` 已撤回。后续实现以
`docs/design/2026-08-03-generic-loop-type-specialization-rfc.md` 的“Behavior Profile × Typed
Semantic IR × 通用 Pass”方案为准；本文中的三个算子只作为性能上限和通用能力验证 workload。

## 结论

这轮应停止把 mapper fusion / 算子重排作为主线。新的真实诊断证据把组合优化收敛到三层通用能力：

1. UDF JIT 把真实 callable 还原成带 block/phi、loop、type、effect 与异常边的 CFG/SSA；
2. Behavior Profile 只按循环、控制、计算、对象、dispatch、dynamic 等维度做准入和 Pass 调度，不定义
   业务语义；
3. CinderX 对闭包目标、generator/iterator、归约、不可变查表和 sequence builder 做通用 HIR 特化。

在 10K FineWeb 输入、47,307,528 个字符上，用等价 native helper 模拟三项后端 lowering：

| stage | 当前 CinderX 形态 | native scan 上限原型 | stage speedup |
|---|---:|---:|---:|
| alphanumeric | 3.8811 s | 0.3871 s | **10.03x** |
| punctuation | 1.1541 s | 0.0874 s | **13.21x** |
| whitespace | 3.0785 s | 0.1916 s | **16.07x** |

三项逐条结果 hash 与 Unicode 边界样本完全一致。按既有 200K stage share 投影，它们覆盖历史
E2E 的 `28.32%`，对应耗时下降 `25.97%`、`1.351x` E2E。这个目标显著高于单独 CinderX 的约
10%，也不依赖算子重排。

`1.351x` 是 Amdahl 投影，不是 full-pipeline 实测。当前 Python 3.14 CinderX 镜像没有官方
fastText / SentencePiece / KenLM 依赖，原型基于 stand-in 函数；进入生产前必须先与官方
Data-Juicer 算子冻结语义，再在 real-model full pipeline 上做独立 A/B。

## 诊断系统缺口已经闭合

此前 FineWeb 的 12 个 Python 候选在 Driver 的 float64 gate 前被跳过，因此没有自然 Bundle；
`CaptureVerificationError` 也可能越过结构化 rejection。当前实现已经补上：

- full 模式在 Driver gate 生成 source-to-rejection Bundle；
- Bundle 包含 source identity/ranges、candidate signature、原始 bytecode/disassembly、拒绝原因、
  chain status 和 Source→Bytecode provenance；
- semantic/artifact rejection 会保留已生成的 Capture IR 与 CFG；显式 `source=text` 才会写
  `source/source.py`；
- `CaptureVerificationError` 被规范化为 `capture_verification_failed`，不再被 broad catch 静默丢失；
- 必需产物缺失会生成 `incomplete`、递增失败计数并发出 `recording_failed` 事件，不再保留虚假的
  complete/partial 声明；
- 显式 summary/full 配置错误 fail-closed；正常 `off` 仍然 fail-open、懒加载且不写诊断文件；
- 正常 `off` 路径不解析诊断策略、不创建 Driver 上下文、不构造拒绝证据；`summary` 也不会创建
  Driver full-Bundle recorder；
- pre-admission 候选可用 `udf:<code-sha256-prefix>` 筛选，不再要求先拥有 artifact ID。

真实穿刺结果：13 个 stage 中 12 个 Python candidate 全部产生 `partial` Bundle；dedup 是 Daft
dataflow，不是 Python UDF。12/12 Bundle 经 CLI 校验有效，每份 11 个 manifest artifact，统一拒绝码
为 `logical_schema_not_float64`。下游 semantic/HIR/LIR/machine 均明确写成
`unavailable_reason=capture_rejected`，没有用空文件伪装产物。

稳定原始证据位于：

```text
artifact://fineweb-backend-diagnostics-20260803/
  driver-bundles/
  cinderx-expanded/
  alnum-probe/
```

可提交摘要为
`docs/reports/evidence/2026-08-03-fineweb-backend-diagnostics-summary.json`。

## CinderX 后端证据

### 1. wrapper 进了 JIT，但动态边界没有消失

`_make_filter_udf.<locals>.udf` 的 final HIR 核心仍是：

```text
LoadCurrentFunc
LoadField<func_closure>
LoadTupleItem<0>
LoadCellItem
CheckFreevar<"fn">
VectorCall<1>
```

也就是说 CinderX 生成了机器码，但它只优化 wrapper 框架；UDF JIT 已知的精确 callable 没有进入
HIR 类型事实，callee 不能去虚拟化或 inline。12 个 wrapper 还以不同 closure 实例重复编译相同的
632/640-byte code shape。编译共享可以减少启动成本，但稳态优先级低于消除每行 `VectorCall`。

### 2. alphanumeric 的扫描主体仍在 generator 边界之外

`dj_alphanumeric_ok` final HIR 包含：

```text
MakeFunction(<genexpr>)
GetIter
CallMethod
VectorCall(sum)
GetLength
BinaryOp<TrueDivide>
Compare<GreaterThanEqual>
```

父函数虽已生成 1304-byte 机器码，但逐字符循环没有融合进 reduction。给 genexpr 加精确 JIT list
后，HIR 能生成到 `InvokeIterNext -> LoadMethodCached(isalnum) -> CallMethod -> YieldValue`，随后
32 次都在 HIR→LIR 失败：

```text
std::get: wrong index for variant
```

没有一次成功 compile，也没有观察到失败负缓存，因此 32 行输入触发 32 次重复尝试。这是独立的
CinderX P0 bug：即使暂不做 typed-loop lowering，也应修复 LIR lowering，并对确定性编译失败做负缓存。

### 3. language stand-in 暴露了通用分配问题，但不是首个生产候选

`dj_language_id_score` 的 final HIR 仍包含 per-call dict/cell、六次 regex `findall` 结果列表、
`StoreSubscr`、lambda 创建和 `max VectorCall`，最终机器码 5872 bytes。该形态说明 CinderX 能编译
动态控制流，却没有把已冻结 pattern set 降成单次扫描。

不过生产 pipeline 的 language-id 是 fastText 模型路径，不应把 stand-in 单扫描收益外推成模型收益。
它只作为第二阶段的通用 HIR 形态证据；首个生产候选仍是 alphanumeric、punctuation、whitespace。

## 后端上限穿刺

原型不是生产 C API。它在独立 timing 进程中关闭 UDF JIT diagnostics 和所有 HIR/LIR/ASM dump，
baseline 与 candidate 都确认已被 CinderX 编译：

- alphanumeric：当前 `sum(genexpr)` 对比 ASCII 快路 + `Py_UNICODE_ISALNUM` Unicode fallback；
- punctuation：每行 `str.maketrans + str.translate` 对比冻结映射的一次 Unicode scan；
- whitespace：regex `\s+` substitution + `strip` 对比一次 Unicode whitespace scan。

正确性覆盖 10K 全输入和空串、ASCII、CJK、全角数字、combining mark、Unicode numeric、emoji 等
边界。探针将输入硬限制为 10,000 行/64 MiB，本次读取 47,880,452 bytes；原始输入和每个 stage
的输出 hash 均已固化。两次 A→B 样本只适合作为趋势穿刺，没有随机化
顺序、置信区间或 full pipeline framework/model 成本，因此不作为发布数字。

投影公式：

```text
saved = Σ stage_share × (1 - 1 / stage_speedup)
      = 0.259677
E2E speedup = 1 / (1 - saved) = 1.350761x
```

## 组合优化设计

推荐的转换链是：

```text
Python UDF
  -> UDF JIT capture：精确 callable、closure 常量、CFG、Effect、异常合同
  -> Behavior Profile：循环/控制/计算/对象/dispatch/dynamic 风险和成本提示
  -> Typed Semantic IR：block/phi、loop、typed iterator、基础计算、调用和 sequence builder
  -> CinderX HIR：closure 去虚拟化 + generator inline + 通用 loop/type Pass
  -> CinderX LIR：按 exact type 选择数据访问、unbox、Unicode/ASCII fast path 和 fallback
  -> AArch64 machine code
```

这条链要求 UDF JIT 和 CinderX 同时参与：UDF JIT 提供跨框架的静态事实与合法性证明，CinderX 负责
后端 lowering、调用约定、deopt/fallback 和机器码。只改 Python pipeline 源码可以模拟收益，但不是
最终方案；只打开 CinderX 则缺少 callable/schema/常量事实，当前 HIR 已证明会停在动态边界。

### P0-A：修复 generator 后端失败与负缓存

先为 `YieldValue`/generator LIR variant failure 建立最小 CinderX regression test，修复
`std::get` 错误；无论成功与否，同一 code object + 编译配置的确定性失败不得逐行重试。该修复应在
CinderX 仓单独提交，不与 UDF JIT ABI 改动混在一个 commit。

### P0-B：建设 Semantic Core IR v2 与类型维度

UDF JIT 先补齐 block argument/phi、循环、typed iterator、参数化 sequence/mapping/builder，以及
`effect`、`may_raise`、异常边和 source provenance。IR 不出现 FineWeb 业务 opcode；行为画像在 Driver
和 Worker 都可从 verified IR 重算，只参与准入、Pass 选择和成本判断，不进入 semantic hash，也不代替
等价性证明。

### P0-C：通用 CinderX HIR Bridge 与 Pass

第一批能力按依赖顺序建设：

1. `ClosureTargetPropagation`：把冻结 closure target 变成带 dependency Guard 的 direct call；
2. `GeneratorInline`、`TypedIteratorSpecialization`、`LoopCanonicalization`：把通用迭代恢复成规范循环；
3. `ReductionRecognition`、`BoxingAndRefcountElimination`：识别归约并消除逐元素 Python 对象开销；
4. `ImmutableLookupSpecialization`、`SequenceBuilderLowering`：特化冻结查表与可变长输出构造；
5. 后续仅对可证明等价的常量正则子集增加 `ConstantRegexAutomaton`。

HIR Builder 和 Pass 只读取 CFG/SSA、标准 CPython call model、类型、常量、effect 与异常合同。
compiler/provider 目录不得出现 Data-Juicer 名称、pipeline ID、固定业务字符表或业务阈值。

### P0-D：以 FineWeb 验证通用能力，而不是定义能力

alphanumeric 用于验证 generator/typed iterator/reduction；punctuation 用于验证 immutable lookup/builder；
whitespace 用于验证 BranchFSM/automaton/builder。每条 Pass 还必须由不同源码写法和非文本 workload
触发；若只能识别这三个函数，或依赖函数名、模块名、固定字符集合，就不通过通用性门槛。

### P1：real-model full pipeline A/B

在包含官方 Data-Juicer、fastText、SentencePiece/KenLM 的同一 Python 3.14 镜像中：

1. 冻结三个算子的官方语义和版本 hash；
2. 先跑 full diagnostics，确认三条通用能力链均有真实 Behavior/Typed IR/HIR/LIR/machine 产物；
3. 新进程关闭 diagnostics，按 A/B 或 ABBA 跑 10K/200K；
4. 比较完整 schema、行数、顺序、值 hash、异常和模型加载次数；
5. 要求 E2E 收益明显高于 10%，且单个 stage、framework、model 和 compile cost 能闭账。

### P2：算子重排 / fusion

保留既有 `3.47%` mapper fusion 趋势证据，但不作为当前主线。它依赖 pipeline 物理形状，且旧候选
还改变了内部输出列名；只有通用 backend loop/type 路径闭环后，才把 fusion 作为独立叠加项验证，不能把
它计入 UDF JIT × CinderX 的核心收益。

## 提交边界

建议分五批提交：

1. 当前仓：Driver rejection Bundle、Capture verification 结构化、fail-closed diagnostics 和文档；
2. CinderX 仓：generator LIR failure + failed-compile negative cache；
3. 当前仓：Semantic Core IR v2、Behavior Profile、通用 loop/type/effect verifier 与 reference executor；
4. CinderX/provider：Typed Region HIR Builder、通用 loop/type Pass 与 wrapper devirtualization；
5. 跨仓：多写法/多业务语料、逐层 diagnostics 和 real-model full-pipeline A/B。

每一批都能独立回滚。benchmark helper 只是性能上限探针，不链接进生产 Wheel；生产路径必须由通用
CinderX HIR/LIR Pass 产生，并独立接受 exact type、异常顺序、Deopt/fallback、CPython 3.14 与 Unicode
数据版本兼容性审查。
