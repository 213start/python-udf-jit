# PDF pipeline × UDF JIT 优化机会诊断

**日期：** 2026-08-02
**状态：** 静态分析、诊断取证、最小 A/B 与 Data-Juicer 移植一致性审计完成
**目标 workload：** `pipeline_pdf_full_min`
**历史 workload revision：** `56d3b6856895427a0519cbaa437d55443fcb578b`

## 结论

当前 PDF pipeline **不能直接受益于现有标量 UDF JIT**。这不是 CinderX
“优化力度不够”，而是在更上游就没有进入 JIT：五个计算算子的输入都是 `Utf8`，输出为
`Utf8` 或 `list[float]`，而当前 Daft admission 只对含 `float64` 的逻辑 schema 发起
Capture。离线继续穿刺时，五个函数又都因为复杂/关键字签名被 Capture 判为
`unsupported_signature`。

历史 hash64 运行进一步限定了收益上界：OCR 占端到端时间的 95.96%，其中 98.28% 的
perf period 位于 Tesseract/OpenMP 原生库。即使把除 OCR 外的全部时间降为零，整链理论
上限也只有 `1.0420x`；只优化 Python 占比高的 parse/table，两者总占比 3.923%，无限
加速上限为 `1.0408x`，两者各加速 2 倍时整链约为 `1.0200x`。

冻结 hash4 fixture 的最小 A→B 穿刺验证了当前实现中跳过 parse 的性能潜力：`parse+table` 子链从
`54.180 s` 降到 `27.491 s`，即 `1.9709x`、耗时下降 `49.26%`；四个文档的输出字符数、
SHA-256 完全一致且无异常。因为只运行了一次 A→B，结果只用于趋势穿刺，不提供置信区间，
也不替代完整 OCR pipeline 的正式 E2E A/B。

后续 Data-Juicer 移植一致性审计修正了 P0 的落地判断：A/B 只证明 parse 在**当前自定义实现**
中没有可观察输出，不能证明删除它仍符合上游语义。固定版本 `py-data-juicer==1.5.2` 没有这三个
PDF mapper；原生 PDF 支持位于输入 `TextFormatter.extract_txt_from_pdf`，它一次性去除表格区域
和页尾页码并产出文本，没有 OCR 或表格输出阶段。当前 pipeline 不是这个契约的正确移植。

因此当前优先级是：

1. 先选择目标契约：Data-Juicer formatter 等价，还是带 OCR/table 的自定义业务 pipeline；
2. 若选择 Data-Juicer 等价，将三个伪 mapper 收敛为一次 PDF→text formatter，而不是简单删 parse；
3. 若选择自定义业务 pipeline，让 parse/OCR/table 各自保留可观察产物并定义合并规则，再优化
   OCR 并行度；
4. 只有在 PDF 解析结果改成 typed page/block/table 列，且数值几何/过滤内核被证明为热点
   后，才让 UDF JIT 接管这些纯数值叶子区域。

## 证据口径

历史性能数据来自：

- `python-framework-analysis-pipeline/projects/volc-operator-sim-reference/runs/`
  `2026-07-22-pdf-full-min-hash64-p4-force4-arm/arm/operators/`
- 运行环境：ARM bare metal、CPU `4-7`、NUMA node 0、Daft 0.7.2、Ray 2.55.1、
  Python 3.10.20；输入为冻结的 64 PDF fixture。
- 该运行可用于热点预诊断，但 operator-case perf 的质量等级为 C，存在
  `input_parity_failed` 和 `perf_lock_failed`，不能作为正式性能发布结论。

当前诊断系统的离线 admission Bundle 以逻辑制品集标识：

```text
artifact://pdf-pipeline-diagnostics-20260802/bundles/
```

可提交、可追溯的 Bundle 校验摘要位于
`docs/reports/evidence/2026-08-02-pdf-pipeline-diagnostics-summary.json`，包含 UDF JIT/workload
revision、函数 code/module SHA-256、Bundle ID、共同 rejection 结论和逐层可用性。完整 Bundle
保留在临时诊断目录，不把临时绝对路径当作长期事实源。

五个 Bundle 均通过 `udfjitctl diagnostics validate`，状态为 `complete/valid`，每个包含：

- `source/identity.json`
- `source/ranges.json`
- `candidate/signature.json`
- `bytecode/original.json`
- `bytecode/original.dis`
- `capture/result.json`
- `reports/chain-status.json`
- `reports/stages.json`

函数与 Bundle 对应关系：

| 函数 | Bundle |
|---|---|
| `dj_pdf_parse_real` | `diagnostic-pdf-candidate-1-30539b1bdf51a42f` |
| `dj_pdf_ocr_real` | `diagnostic-pdf-candidate-2-04f02ed3d3face37` |
| `dj_pdf_table_real` | `diagnostic-pdf-candidate-3-8318e366b0062400` |
| `dj_sentence_split` | `diagnostic-pdf-candidate-4-83c59bc1eb6b830e` |
| `dj_bge_vectorize_vec` | `diagnostic-pdf-candidate-5-0030d34863b85ce5` |

所有 `capture/result.json` 的关键结论一致：

```json
{
  "adapter_admitted": false,
  "adapter_reason_code": "logical_schema_not_float64",
  "capture_supported": false,
  "capture_reject_code": "unsupported_signature",
  "logical_schema": "Utf8"
}
```

因此 HIR、LIR 和机器码缺失是有证据的 `capture_rejected`，不是采集失败，也不能用普通
CinderX 函数级符号冒充 UDF JIT 机器码证据。

## 历史热点分解

| 算子 | Pipeline 时间 | E2E 占比 | perf period 主体 | 当前 UDF JIT 适配性 |
|---|---:|---:|---|---|
| `pdf_parse_mapper` | 411.728 s | 1.94% | CPython runtime 97.24% | 不准入；对象调用密集 |
| `pdf_ocr_mapper` | 20,333.919 s | 95.96% | native/OpenMP 98.28% | 不应交给 UDF JIT |
| `pdf_table_extract_mapper` | 419.596 s | 1.98% | CPython runtime 97.52% | 不准入；列表/表格对象密集 |
| `text_chunk_mapper` | 0.191 s | <0.01% | CPython regex 97.21% | 无整链价值 |
| `bge_vectorize_mapper` | 13.623 s | 0.06% | native/model/math 主导 | 不应交给标量 UDF JIT |

原始 3.14 字节码也与上述判断一致：parse/table 函数大量出现 `CALL`、`LOAD_ATTR`、
迭代、异常处理和容器操作；它们不是当前 `bool/int/float` 算术区域。`text_chunk` 的热点来自
正则引擎，BGE 的热点来自 GEMM 内核；扩展字符串槽位也不会自动消除这些成本。

## Amdahl 收益边界

以历史 E2E `21188.936 s` 为基准：

| 假设 | 预测候选时间 | 理论 speedup | 时间下降 |
|---|---:|---:|---:|
| 只跳过当前 output-discarding `pdf_parse` | 20,777.208 s | 1.0198x | 1.943% |
| parse/table 均加速 2x | 20,773.274 s | 1.0200x | 1.962% |
| parse/table 无限加速 | 20,357.612 s | 1.0408x | 3.923% |
| 除 OCR 外全部无限加速 | 20,333.919 s | 1.0420x | 4.035% |

这些是历史时间分解上的投影，不是实测候选收益。

## Data-Juicer 移植一致性审计

固定环境是 `py-data-juicer==1.5.2`。官方包中不存在 `pdf_parse_mapper`、`pdf_ocr_mapper`、
`pdf_table_extract_mapper`；本地 `runner/op_source_policy.py` 也把三者标记为
`dj_baseline=missing` / Daft-only。官方 PDF 支持实际位于 `data_juicer/format/text_formatter.py`
的 `extract_txt_from_pdf`，发生在 Dataset load 之前而不是 process operator 链中。

| 语义点 | Data-Juicer 1.5.2 | 当前 pipeline | 判定 |
|---|---|---|---|
| 执行位置 | `TextFormatter.load_dataset` 的输入格式化 | 三个 Daft/Ray mapper | 不同层级 |
| PDF parse 输出 | 全页文本写入缓存 `.txt` | 最多 50 页，文本计数后丢弃，返回原路径 | 不等价 |
| 表格处理 | `find_tables` + `outside_bbox`，从正文移除表格区域 | 抽正文后再追加 `extract_tables` 行 | 语义相反 |
| 页码处理 | 去掉每页末尾页码 | 不处理 | 不等价 |
| OCR | 无 | 前 5 页 Tesseract，但识别文本被丢弃 | 额外无效负载 |
| 最终数据 | 每个 PDF 一个纯文本 sample | table 阶段重新打开 PDF 后的正文+表格字符串 | 不等价 |

hash4 动态对比结果固化在
`docs/reports/evidence/2026-08-02-data-juicer-pdf-semantics-compare.json`。4/4 文档输出 SHA-256
不同；当前输出字符数相对官方分别多 30.8%、5.5%、50.8%、5.9%。远端原始 JSON SHA-256 为
`6799255c366f7479e4522559de452484770ec4918f04b82b7f6e318e0a14da53`，exit status 0。

历史也支持这一结论：`pipeline_pdf_full_min` 最初在 commit `c1bc600` 作为“所有 spec pipeline
最小跑通”的 fake/stub 链加入，并明确不具备 semantic validity；后续 commit `7b8d36b` / `3ad1bec`
把三个阶段替换成真实库负载，但没有上游 Data-Juicer PDF operator 契约可供逐算子移植。当前设计
文档则明确把 PDF 标为 Daft-only、无公平 DJ baseline。因此它是自定义业务 workload 原型，
不是 Data-Juicer 原生 PDF pipeline 的 faithful port。

## 最小 A/B 穿刺结果

本次按用户要求只做一次 A→B，不做 ABBA。原始结果固化在
`docs/reports/evidence/2026-08-02-pdf-stage-elision-ab.json`。

| 项目 | A：`parse + table` | B：`table only` | 结论 |
|---|---:|---:|---|
| hash4 wall clock | 54.180 s | 27.491 s | `1.9709x`，下降 49.26% |
| user CPU | 53.911 s | 27.401 s | 与 wall clock 趋势一致 |
| system CPU | 0.152 s | 0.036 s | 非主要成本 |
| 输出正确性 | 4/4 无异常 | 4/4 无异常 | 字符数与 SHA-256 逐文档一致 |

运行事实：

- 镜像：`volc-operator-sim-bench:56d3b685-aarch64`；源码 revision
  `56d3b6856895427a0519cbaa437d55443fcb578b`；
- Python 3.10.20，SOABI `cpython-310-aarch64-linux-gnu`，pdfplumber 0.11.10；
- Kunpeng 920，容器 cpuset 和实际 affinity 均为 CPU `4-7`，同属 NUMA node 0；
- 唯一 A/B 差异是是否先调用 `dj_pdf_parse_real`；同一进程、同一解释器、同一输入顺序；
- `UDFJIT_DIAGNOSTICS=off`，诊断 I/O 没有进入计时；
- 四个输入文件均先校验 SHA-256，与 manifest 一致；
- 远端日志 `artifact://pdf-stage-elision-ab-20260802/run.log`，容器结果
  `artifact://pdf-stage-elision-ab-20260802/result.json`，exit status 0。

局限：这是单次顺序 A→B，B 可能受 A 建立的页缓存影响，且没有重复样本/置信区间；不过 A
内部的 table 同样紧随 parse、也能利用热页缓存，四个文档的子链下降均接近一半。该结果只验证
“当前 parse 的结果未进入输出”的性能事实，不证明其上游业务语义应被删除，也不能作为正式性能发布数字。

## E1–E9 证据闭环

本表把“不适用”和“尚缺证据”显式分开。Capture 前被拒绝意味着 HIR/LIR/机器码不存在，
不能把 E3–E5 伪装成已完成；单次穿刺只关闭趋势判断，不越过 E8 的重复性 gate 发布正式收益。

| 段 | 状态 | What | Verdict / Gate |
|---|---|---|---|
| E1 性能基线 | `trend_only` | 历史 hash64 E2E/stage wall clock + hash4 单次 A→B；缺重复样本、CI | 候选趋势明显，但不满足正式显著性 gate |
| E2 用例画像 | `closed` | 五级函数形状、原始字节码、perf period 与 pipeline 占比 | PDF 对象/字符串动态流；OCR 是 native/OpenMP 主热点，形状与热点吻合 |
| E3 HIR 分布 | `not_applicable_yet` | Bundle 明确 `capture_rejected`，无 semantic IR/HIR | 当前不是 HIR 优化不足，而是 schema/signature admission 未通过 |
| E4 LIR + wall clock | `not_applicable_yet` | 无 UDF JIT LIR/机器码；已有 stage wall clock | 不能做 LIR 对齐；只有 typed leaf kernel 进入 Capture 后重开 |
| E5 差异点 | `not_applicable_yet` | 无 baseline/candidate JIT 指令序列 | 不能声称 codegen 差异 |
| E6 根因下钻 | `evidence_gap` | admission Bundle + 历史 perf top；缺同口径 perf stat/PMU | 已定位两个独立表层根因：UDF JIT 准入为零、OCR barrier；硬件级根因尚未闭环 |
| E7 优化方向 | `contract_blocked` | parse/OCR 结果被丢弃；DJ formatter 契约与当前输出相反；typed IR 边界 | 先选 DJ-equivalent 或 custom contract；契约冻结后再谈 stage-elision/OCR/UDF JIT |
| E8 穿刺数据 | `trend_closed` | hash4 diagnostics-off A→B：54.180 s → 27.491 s；4/4 当前输出哈希一致 | 跳过当前 output-discarding parse 可消除约一半子链时间；不证明上游契约允许删除 |
| E9 价值判定 | `hold_P0_for_contract` | P0 子链收益 49.26%，但 4/4 与 DJ formatter 输出不等价；P2 无热点证据 | 暂不改逻辑；先修复/确认移植契约，直接扩字符串 JIT 仍不进入当前清单 |

## 优化方案

### P0：暂停直接删除，先冻结 PDF 语义

当前 `dj_pdf_parse_real` 提取正文、只累计 `chars`，最后仍返回原始路径；
`dj_pdf_table_real` 随后重新 `pdfplumber.open(path)`，再次调用 `extract_text()`，并把正文和表格
装配成真正下游输出。对成功样本，第一阶段没有可观察的数据产物。

最小穿刺满足“当前实现 A/B 输出一致”，并减少 49.26% 的 parse/table 子链时间；但这只能证明
当前 parse 是死负载。移植一致性审计表明当前最终输出本身不符合 Data-Juicer formatter，所以
不能先删 parse 再把现状固化为契约。

- 若目标是 **Data-Juicer 1.5.2 等价**：实现单一 PDF formatter，保留全页处理、去表格区域、
  去页尾页码、一个 PDF→一个 text sample；移除独立 OCR/table 阶段。
- 若目标是 **自定义文档 ETL**：定义 typed `DocumentRecord`，至少含 native text、OCR text、tables、
  page/source 元数据；三个阶段必须贡献可观察字段，最后显式 merge/dedup。
- 若目标只是 **跨架构负载 benchmark**：允许保留纯负载，但应重命名并标注 output-discarding，
  不再声称是 Data-Juicer 算子移植或业务全链。

只有目标契约确定后，49.26% 子链收益才能转化成代码改动建议；历史完整 E2E 投影仍约为 1.94%。

### P1：修复诊断覆盖缺口

当前 full Bundle 只在 Worker 已收到可执行 Artifact 后建立。像 PDF 这种在 schema admission
就被拒绝的候选不会自然生成 Bundle。本次使用独立诊断进程补采了 rejection Bundle，但正式系统
应在 Driver admission 层输出同样的源码范围、原始字节码、拒绝原因和后续层
`unavailable_reason`。该能力必须继续受 `UDFJIT_DIAGNOSTICS=full` 和专用进程隔离约束，正常
运行不增加序列化、计时或文件 I/O。

### P2：typed PDF 中间表示后再评估 UDF JIT

若业务需要进一步优化 parse/table，应先把对象流改成明确的 typed 列，例如 page index、
`x0/y0/x1/y1`、字符/表格计数、置信度和有效位。随后只把纯数值 bbox 归一化、面积/重叠计算、
阈值过滤等叶子函数交给 UDF JIT；文件 I/O、pdfplumber 对象遍历、字符串拼接和异常边界继续
留在 Python/native continuation。

进入实现的 gate：新数值叶子区域必须在 parse/table 自身 CPU 中占至少 10%，且能形成当前
支持的单标量或小型可扩展标量 ABI。否则扩展 UDF JIT 的成本大于整链收益。

### P3：OCR 并行度专项

OCR 的 top symbols 是 `gomp_team_barrier_wait_end` / `gomp_barrier_wait_end`，应独立检查
Tesseract/OpenMP 线程数与 Ray 四分区是否形成嵌套并行和 barrier 等待。候选轴包括固定
`OMP_NUM_THREADS=1` 配合四进程、或减少进程并提高每进程线程数。该实验与 UDF JIT 分开，
但它覆盖 95.96% 的 E2E，优先级高于任何字符串 JIT 扩展。

## 最小穿刺验证设计

### 功能 gate

使用冻结 hash4 fixture，对每个路径执行：

- baseline：`pdf_parse(path)` 后执行 `pdf_table(path)`；
- candidate：只执行 `pdf_table(path)`；
- 比较每个文档输出 SHA-256、异常类型、输出文档数和总字符数。

不修改现有 pipeline 仓库和 Volc 工作树；用临时 overlay/runner 执行，避免污染用户的 dirty
改动。

### 本次最小性能 gate

- 同一旧 PDF 容器、同一 fixture、同一 CPU `4-7` / NUMA 0；
- diagnostics 明确为 off；
- 按用户要求执行一次 A→B，不做重复样本；
- stage-elision 子链收益为 49.26%，正确性哈希一致；
- 整链收益只做 Amdahl 投影，除非另行运行完整 OCR pipeline A/B。

若需要发布正式性能数字，再升级为 ABBA/多样本并报告 CV 与 bootstrap 置信区间；本次不追加
该成本。

### 正式 UDF JIT 诊断 gate

旧 PDF 容器是 Python 3.10.20，缺少 CinderX 和当前 UDF JIT。若 typed 中间表示穿刺证明值得
继续，需另建 Python 3.14.3/CinderX 的专属 `pdf-udfjit-diag` 容器：

- 正式 timing A/B：`UDFJIT_DIAGNOSTICS=off`；
- 来源/HIR/LIR/perf：另起 `UDFJIT_DIAGNOSTICS=full` 专用 Worker；
- 不把诊断运行耗时用于性能收益结论；
- 在 baseline source、CinderX commit、SOABI/TLS 和 structured-origin patch 全部可证明前，
  不发布正式 UDF JIT A/B 结论。

## 当前证据缺口

- 目标尚未在“Data-Juicer formatter 等价”和“自定义 OCR/table 文档 ETL”之间做产品裁定；
- stage-elision 只有单次 A→B，缺重复样本和置信区间，不能作为正式 benchmark；
- 当前没有 typed PDF 数值叶子区域，因此没有 UDF JIT HIR/LIR/机器码；
- 旧 operator-case perf 输入一致性和 perf-lock 未通过，只能用于选点；
- 现有 CinderX 3.14 环境来源无 Git 事实源，`baseline_source_untrusted`；
- 未验证完整 OCR pipeline 的候选收益，不把 1.0198x 投影写成实测结果。
