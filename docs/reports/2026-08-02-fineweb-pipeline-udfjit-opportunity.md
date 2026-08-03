# FineWeb text pipeline × UDF JIT 优化机会诊断

> 2026-08-03 后端复核已完成。本文保留 2026-08-02 的历史 fusion 与 admission 证据；当前优先级、
> 新 Driver Bundle、CinderX HIR/LIR 根因和 intrinsic A/B 以
> `docs/reports/2026-08-03-fineweb-udfjit-cinderx-backend-opportunity.md` 为准。

**日期：** 2026-08-02

**状态：** 历史热点审计、UDF JIT 诊断取证、最小 fusion A/B 完成

**目标 workload：** `pipeline_text_fineweb_full_min`

**UDF JIT revision：** `90a60c66dcca1ecc9297b97adb6bf8e8eed2a52f`

**workload revision：** `56d3b6856895427a0519cbaa437d55443fcb578b`

## 结论

当前 FineWeb pipeline **没有进入 UDF JIT**。13 个逻辑 stage 中，12 个 Python stage 都是
`string -> string` 或 `string -> bool`，而当前 Daft admission 只对逻辑 schema 含 `float64`
的候选发起 Capture；剩余的 document dedup 是 Daft `distinct` 数据流，不是 Python UDF。
因此本次诊断得到 0 个 admitted artifact、0 个 `scalar_cinderx` region，HIR、LIR 和机器码不存在。
这不是 CinderX 对动态类型优化得不好，而是执行链在 CinderX 之前就被 schema、闭包和签名挡住了。

第一优先级应是**融合前 7 个连续 mapper，减少 Daft/Ray 的 UDF 边界和中间列**。在冻结 10K
fixture、真实 fastText + SentencePiece/KenLM、固定 4 CPU/NUMA、诊断关闭的单次 A→B 中，物理
execution group 从 13 降到 7，端到端从 `67.022 s` 降到 `64.697 s`，即 `1.0359x`、耗时下降
`3.47%`。这是一次趋势穿刺，没有重复样本或置信区间，不能作为正式性能发布数字。

32-row 正确性穿刺显示输出行数、值、顺序、`String` dtype 和内容哈希完全一致；但末端内部列名
由 `_text_chunk_mapper_12_out` 变成 `_text_chunk_mapper_6_out`。所以当前候选**不能直接默认开启**：
应先让公共输出列名不依赖 fusion 后的 group index，或在出口显式 alias 到稳定名称，再重复 A/B。

长期 UDF JIT 方向不是一次性支持整条动态字符串 wrapper，而是先选择有整链价值的 typed leaf。
历史 200K 数据中，`alphanumeric_filter` 占 E2E `12.19%`、perf period 的 `96.20%` 位于 CPython，
是最有希望的第一片字符串叶子；即使它单 stage 加速 2 倍，整链投影也只有约 `1.0649x`，因此必须
以 UTF-8/string-view ABI、ASCII fast path + Unicode fallback 或预计算 typed 计数列的形式控制范围，
并在 compile/hit 大于零后才重开 HIR/LIR/机器码分析。

另一个独立热点是模型生命周期：本次 baseline 和 candidate 各记录 8 次 SentencePiece 与 8 次
KenLM 加载日志。两边次数一致，不解释 fusion 的差值，但说明模型预加载/Worker 复用值得单独 A/B；
它属于执行框架和原生模型优化，不应冒充 UDF JIT 收益。

## Pipeline 结构与候选边界

原 pipeline 共 13 stage：

1. clean HTML；
2. clean links；
3. clean email；
4. clean copyright；
5. fix Unicode；
6. punctuation normalization；
7. whitespace normalization；
8. text length filter；
9. alphanumeric filter；
10. language-id score filter；
11. perplexity filter；
12. document dedup；
13. text chunk。

前 7 个 stage 都是连续 mapper，fusion 只把它们放进同一个 UDF wrapper，函数集合和执行顺序不变；
四个 filter、dedup 和末端 chunk 仍保持独立边界。因此最小候选没有删除业务步骤，也没有绕过过滤逻辑。

当前 task 文案仍把 perplexity 描述为 fake/stub，但 registry 默认在
`VOLC_REAL_TEXT_OPS != 0` 时使用真实 SentencePiece/KenLM。本次 A/B 没有设置
`VOLC_REAL_TEXT_OPS=0`，运行日志确认真实模型加载。历史 UDF JIT off/auto A/B 使用的则是 stand-in
路径；两类证据不能混为同一 workload 语义。应修正 task 元数据，使诊断报告能直接区分 real/stub。

## UDF JIT 诊断链路

可提交摘要位于
`docs/reports/evidence/2026-08-02-fineweb-pipeline-diagnostics-summary.json`。离线 Bundle：

```text
diagnostic-fineweb-l0-20260802-03-13124b3b3373426d
manifest sha256: 13124b3b3373426d492bd83ea1bb559bd6f6cf76c61146c75bc891681139f763
validation: valid
bundle status: partial
artifact count: 31
executed content: false
```

`partial` 是预期状态：原始源码身份、范围、字节码/disassembly、admission matrix 和 rejection 原因
可读可定位；但 12 个 Python stage 均未准入，所以 semantic IR、HIR、LIR、机器码只能明确标为
`unavailable`，不能拿普通 CinderX 符号替代 UDF JIT 机器码。

### 逐层结论

| 层级 | 证据 | 结论 |
|---|---|---|
| Python/source | stage matrix、函数身份、源码范围 | 12 个字符串/布尔 Python stage；1 个 Daft dataflow |
| CPython bytecode | 12 份 original bytecode/disassembly | 可读、可回源；wrapper 含闭包、调用、字符串/regex/模型依赖 |
| Adapter admission | `logical_schema` 与 admission matrix | 当前只准入含 `float64` 的 schema，FineWeb 0/12 准入 |
| Capture | forced probes、typed-leaf matrix | dependency、closure、signature 与 verification rejection |
| Semantic IR/HIR | chain status | 不存在：Capture 前/中被拒绝，不是采集失败 |
| LIR/machine | chain status | 不存在：没有 UDF JIT region，也没有 compile/hit |
| Native/perf | 历史 operator perf + 当前 wall A/B | 可定位框架、CPython 与模型原生热点，但不能归因到 UDF JIT codegen |

当前远端 pipeline image 只有旧 UDF JIT revision，未部署本 revision 的 full diagnostics，所以 Bundle
是离线 qualification 证据；真实 pipeline A/B 则在同一固定源码上单独运行，且显式设置
`UDFJIT_DIAGNOSTICS=off` / `PYTHONJITUDFDIAGNOSTICS=0`。本报告不声称已在真实 Worker 内获得当前
full source-to-machine Bundle。

### 诊断系统暴露的缺口

- `registry.py` 只在 schema 含 `float64` 时调用 Capture。FineWeb 的字符串候选在 Driver 侧被跳过，
  正常不会留下 rejection Bundle；full 模式应在 admission gate 就记录源码、字节码、原因以及后续层
  `unavailable_reason`。
- 即使放宽 schema gate，Daft mapper/filter wrapper 仍闭包捕获算子函数；仅添加 `str` dtype 不会自动
  通过 Capture。
- `try_capture()` 只捕获 `CaptureRejected`。`fix_unicode` 和 punctuation forced probe 的
  `CaptureVerificationError` 会逸出，随后被 registry 的 broad catch 静默丢弃；应先把它规范化成结构化
  rejection，再扩字符串能力。
- 真实 fastText/SentencePiece/KenLM 只完成静态签名和运行日志覆盖；远端未部署 current diagnostics，
  因而真实模型路径还没有完整 source-to-machine 诊断物。

## 历史热点证据

200K ARM 历史运行的 E2E 为 `1199.458 s`，operator chain 为 `1182.497 s`，占 `98.59%`：

| stage | wall time | E2E 占比 | perf period 主体 | 判断 |
|---|---:|---:|---|---|
| perplexity | 400.899 s | 33.42% | operator native 58.25%，CPython 20.39% | 优先查模型预加载、批处理和复用 |
| language id | 163.992 s | 13.67% | libc 49.64%，CPython 49.05% | 模型/native 与 Python 边界混合 |
| alphanumeric | 146.173 s | 12.19% | CPython 96.20% | 最有价值的 typed string leaf |
| punctuation | 118.077 s | 9.84% | CPython 94.20% | fusion/native expression 候选 |
| email | 112.307 s | 9.36% | CPython 94.27% | fusion/native expression 候选 |
| whitespace | 75.504 s | 6.29% | CPython 65.24% | native path 先验证 strip 语义 |
| copyright | 56.684 s | 4.73% | CPython 91.00% | fusion 候选 |
| links | 46.173 s | 3.85% | CPython 90.04% | fusion/native expression 候选 |
| text chunk | 31.135 s | 2.60% | CPython 87.19% | regex/分配；整链优先级较低 |
| fix Unicode | 13.446 s | 1.12% | — | fusion 候选 |
| clean HTML | 7.083 s | 0.59% | — | fusion 候选 |
| document dedup | 6.800 s | 0.57% | — | Daft dataflow，不是 UDF JIT 候选 |
| text length | 4.226 s | 0.35% | — | typed leaf，但整链价值低 |

前 7 个可融合 mapper 合计 `429.274 s`，占历史 E2E `35.789%`。它们无限加速的 Amdahl 上限为
`1.5574x`；整体加速 2 倍的投影为 `1.2179x`。历史 2M、5-op micro-pipeline 曾从 `131.858 s`
降到 `50.584 s`（`2.607x`），但该候选同时启用了 native length filter，且没有完整内容 multiset
hash，不能机械外推成 full FineWeb 的实测收益。按 2.607x 机械代入，full pipeline 投影约
`1.2830x`，这里只把它作为上界方向信号。

历史 UDF JIT off/auto ABBA 则是明确的负证据：stand-in pipeline 的 off median `274.245 s`，auto
`280.232 s`，auto 慢约 `2.18%`；输出 schema/multiset hash 一致，但 compile/hit 为零，字符串/布尔
schema 均走 unsupported fallback。这只说明当前无命中时存在 fallback 管理开销，不代表 CinderX
代码生成质量。

## 最小 A/B 穿刺

原始事实固化在
`docs/reports/evidence/2026-08-02-fineweb-fusion-ab.json`。

| 项目 | A：不融合 | B：融合前 7 mapper | 结果 |
|---|---:|---:|---|
| 10K E2E | 67.022 s | 64.697 s | `1.0359x`，下降 `3.47%` |
| 物理 execution group | 13 | 7 | 减少 6 个 UDF 边界 |
| 输入/输出行数 | 10,000 / 9,992 | 10,000 / 9,992 | 一致 |
| 32-row 内容 multiset SHA-256 | `204fc6...8ac0` | `204fc6...8ac0` | 一致 |
| 32-row ordered SHA-256 | `f761a6...1b6e` | `f761a6...1b6e` | 一致 |
| 末端 dtype | String | String | 一致 |
| 末端内部列名 | `_text_chunk_mapper_12_out` | `_text_chunk_mapper_6_out` | **不一致** |

运行约束：同一镜像和源码、同一冻结 10K/4-fragment 输入、CPU `4-7`、NUMA node 0、Ray 4 CPU、
BLAS/OpenMP 线程均锁为 1、每个样本重启 Ray、warm-cache 口径、真实 text operators，且 diagnostics
完全关闭。perf lock 没有 violation，但 CPU governor/turbo 状态无法确认且存在 16 GiB swap，因此标记
`warn`。按用户要求仅执行一次 A→B，存在顺序/cache 偏差且无 CI，结论仅为趋势。

模型加载日志中，两边都出现 8 次 SentencePiece 和 8 次 KenLM load message；这对 A/B 对称，
但为下一轮模型生命周期实验提供了直接证据。

## E1–E9 证据闭环

| 段 | 状态 | 证据与判定 |
|---|---|---|
| E1 性能基线 | `trend_only` | 200K 历史 stage/perf + 当前 10K 单次 A/B；无重复样本和 CI |
| E2 用例画像 | `closed_for_candidate_selection` | 13-stage 结构、字节码、历史 stage wall/perf、real-model 运行日志已对齐 |
| E3 HIR 分布 | `not_applicable_no_jit_hit` | 0/12 Python stage admitted；HIR 不存在，不能伪造 |
| E4 LIR + wall | `not_applicable_no_jit_hit` | 有 pipeline/stage wall 证据，无 UDF JIT LIR/machine |
| E5 codegen 差异 | `not_applicable_no_jit_hit` | baseline/candidate 都没有 UDF JIT 指令序列 |
| E6 根因下钻 | `closed_at_admission_and_framework_layers` | schema gate、wrapper closure/signature、UDF boundary 和模型加载均有证据；CinderX codegen 不是当前根因 |
| E7 优化方向 | `candidate_selected` | P0 mapper fusion；P1 native expressions；P2 typed string leaf；P3 model lifetime |
| E8 穿刺验证 | `trend_closed_with_output_schema_caveat` | 3.47% 趋势；内容/顺序/dtype 一致，但内部输出列名变化 |
| E9 价值判定 | `hold_for_stable_output_schema` | 不默认开启；先固定出口 schema，再重复 A/B |

## 优化方案

### P0：稳定输出 schema 后开启 mapper fusion

先把最终公开输出列 alias 到不依赖 `group_idx` 的稳定名称，补充内容 hash、顺序 hash、dtype 和完整
schema-name A/B。随后再用冻结 10K 至少执行 BA/AB 或 ABBA；若收益仍为正且没有 perf-lock violation，
才把 `fuse_mappers=true` 作为 FineWeb 默认值。当前 3.47% 是可实现趋势，不是发布承诺。

### P1：逐算子验证 native expression

从 email、punctuation、links 开始，一次只替换一个算子，并比较完整 Unicode fixture 的内容/顺序
hash。whitespace 暂后：历史 native 路径没有证明与 Python `strip` 的首尾空白语义一致。2M micro
结果说明 native expression 方向潜力大，但混合了 fusion/native-length-filter，不能直接进入 full
pipeline 默认配置。

### P2：窄化字符串 UDF JIT，而非接管完整 wrapper

优先选择 alphanumeric 叶子，设计显式 UTF-8/string-view 输入与 typed 输出，例如
`(alnum_count, length) -> bool`，ASCII fast path + Unicode fallback。另一种低风险方式是在 native
表达式层先生成计数列，再让现有 bool/int/float scalar ABI 处理阈值逻辑。进入性能 A/B 的硬 gate：

- Driver full diagnostics 留下 source/bytecode/rejection 或 admitted artifact；
- Capture、semantic IR、CinderX HIR、LIR、机器码逐层可定位；
- compile count 和 hit count 大于零；
- Unicode/空串/非 ASCII/异常输入与原实现逐项 hash 等价；
- 诊断与 timing 分进程，timing 必须 `UDFJIT_DIAGNOSTICS=off`。

按历史占比，alphanumeric 单 stage 加速 2 倍的 E2E 投影约为 `1.0649x`；若实测 typed-leaf 成本
不足以覆盖转换/边界开销，应停止继续扩大字符串 ABI。

### P3：模型预加载与批处理专项

对 perplexity/language-id 单独验证每 Worker 初始化一次模型、actor 生命周期复用和 batch API。候选
必须记录 Worker/partition 数、实际模型加载次数、RSS、首批与稳态耗时，并保持相同过滤结果 hash。
历史 perplexity 占 E2E `33.42%`，优先级高于无证据的通用字符串 codegen 扩展，但这属于 framework/
native model 优化。

### P4：修复诊断与 workload 元数据

先让 `CaptureVerificationError` 进入结构化 rejection，并在 Driver schema gate 生成 full-mode rejection
Bundle；正常运行和性能计时保持零诊断 I/O。同步修正 FineWeb task 对 fake/real perplexity 的描述，
把 `VOLC_REAL_TEXT_OPS` 与模型 SHA/路径写入环境指纹，避免后续比较不同语义的结果。

## 推荐执行顺序

1. 修复 fusion 后公共输出列名稳定性；
2. 对 `fuse_mappers=true` 做一次带完整 schema/content hash 的 ABBA；
3. 单独 A/B SentencePiece/KenLM Worker 预加载与复用；
4. 修复 admission/rejection 诊断缺口；
5. 以 alphanumeric typed leaf 做第一个真正 compile/hit 的字符串方向原型；
6. 只有 E3–E5 出现真实 HIR/LIR/machine 证据后，才讨论 CinderX 后端 codegen 调优。

本轮没有修改 workload 逻辑或默认配置；证据显示 fusion 有收益趋势，但出口 schema 稳定性尚未过 gate。
