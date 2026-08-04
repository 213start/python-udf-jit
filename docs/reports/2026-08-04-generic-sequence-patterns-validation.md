# 通用查表、FSM 与序列构造：三算子实现和 Pipeline 验证

**日期：** 2026-08-04
**workload：** `pipeline_text_fineweb_full_min`
**结论：** 三种后端 pattern 全部命中并取得正收益；生产构建的 post-review 锁定环境单次 pipeline A/B 为 8.39%，尚未达到 15% 目标

## 结论

本轮把原有的通用循环/类型特化从“标量归约”扩展到了“序列构造”，没有添加
punctuation、whitespace、FineWeb、算子名或 UDF 名特判。三个目标 UDF 现在分别归一为：

| 源码行为 | Typed Semantic | CinderX 物理 HIR |
| --- | --- | --- |
| Unicode 属性计数 + 标量归约 | iterator + `unicode.property` + reduction | `UnicodeCountProperty` |
| 不可变标量查表 + 序列构造 | iterator + `immutable.lookup` + builder | `UnicodeMapSequence` |
| Unicode 分类 + FSM + 序列构造 | iterator + `unicode.property` + `fsm.transition` + builder | `UnicodeFsmSequence` |

真实 FineWeb 10K 输入上的直接 UDF A/B 分别为 7.84x、2.71x、3.88x。保持原 13 个
pipeline stage、使用完全不含诊断 overlay 的生产 CinderX，并补齐全部线程锁后，post-review
执行段从 26.4197 s 降到 24.2020 s，speedup 为 1.0916x，即耗时下降 **8.39%**。覆盖目标
已经完成，但 24.2020 s 仍高于预设的 22.34 s 硬目标；
因此不能把本轮报告成“显著超过 10%”或“达到 15%”。

## 为什么它是通用能力

前端只识别可证明的 Python 语义子集：精确 `str`、常量单码点映射，或精确的 `\s+`
正则、单个 whitespace replacement 和无参数 `strip()`。随后把业务源码改写为有限、可验证的
整数表和通用 CFG。Verifier 对映射表、状态数、转移、动作、Unicode 标量范围和数据流做有界校验；
不满足条件时在 builder 产生任何可见状态之前 fail closed。

后端物理化器不读取源码函数身份。它重新证明循环从 0 开始、步长为 1、读取同一个精确字符串、
builder/state 经正确回边传递、返回值来自同一个 builder。CinderX 只接受精确 helper identity、
编译期常量描述符和 exact-str Guard。测试还用完全无关的希腊字符映射和不同 whitespace replacement
复用同一 IR/HIR，且序列化 IR 中不存在 `punctuation`。

## 后端实现

`UnicodeMapSequence` 接受最多 256 个严格递增的 Unicode scalar key 和等长 value。运行时用
边界判断与二分查找完成查表，并通过 `PyUnicodeWriter` 单遍构造规范最小宽度结果。曾尝试直接
复用输入宽度，但 CPython 要求字符串使用最小规范宽度；该版本被 Python 语义测试拒绝并撤销。
最初的线性查表也使 punctuation 回退到 0.67x；二分查表的两遍版本达到 1.99x，最终单遍 writer
进一步达到 2.71x。

`UnicodeFsmSequence` 的描述符由状态转移、builder 动作和 emission 三张有界表组成。运行时用
CPython Unicode 分类宏驱动布尔类别 FSM，单遍完成分类、转移和输出构造，避免第二次重复分类与
状态转移。当前 whitespace 只是该通用 transducer 的一个实例：leading、emitted、pending-space
三个状态。

canonical Python lowering 保留可读的逐 operation 版本用于诊断和语义参考；正常 CinderX Worker
必须成功物理化为上述单个 HIR，否则 sequence-builder pattern 直接拒绝编译并回到原 UDF，避免执行
参考 lowering 的持久 list 复制路径。

## 直接 UDF A/B

同一 aarch64 Python 3.14.3/CinderX runtime、同一冻结 10K 文本、CPU 4-7、5 轮中位数；每个 case
先验证完整输出逐值相等，再测原始 UDF 和物理化函数。CinderX 源码中不存在诊断 overlay，两个
诊断环境开关也均关闭。

| Pattern | 原始 UDF median | UDF JIT + CinderX median | Speedup | HIR |
| --- | ---: | ---: | ---: | --- |
| alphanumeric | 3.8896 s | 0.4958 s | **7.844x** | `UnicodeCountProperty × 1` |
| punctuation | 1.1540 s | 0.4251 s | **2.714x** | `UnicodeMapSequence × 1` |
| whitespace | 2.9529 s | 0.7613 s | **3.879x** | `UnicodeFsmSequence × 1` |

这是一轮简单的定向 A/B，不是多臂统计发布结果；它用于证明三个 kernel 的方向和量级。

## 完整 Pipeline A/B

任务 hash 为 `8839ef528b2fd3cabaf99bbc03cf2630d3386e2280ba64dba177dd3c4e9f0d64`，
两臂均为 10,000 输入、9,992 输出、13 个原始 stage；没有算子重排或业务逻辑修改。auto 臂显式
锁定 UDF JIT manifest。每臂只运行一次，这是用户允许的简单 A/B 范围。

| 口径 | off | auto | Speedup | 耗时下降 |
| --- | ---: | ---: | ---: | ---: |
| pipeline 执行段 | 26.4197 s | 24.2020 s | **1.0916x** | **8.39%** |
| 含 Ray 启动总时间 | 33.7068 s | 31.0873 s | 1.0843x | 7.77% |

两臂均显式固定 `OMP`、`MKL`、`OPENBLAS`、`NUMEXPR` 和 `VECLIB` 线程数为 1；perf-lock
没有 violation，仅保留 governor/turbo 不可读、swap/NUMA 等主机级 warning。三个 kernel 的直接
收益被 Daft/Ray、wrapper/Guard 和其余十个未物理化 stage 按 Amdahl 定律稀释。真实 Worker 诊断
证明三个算子在 review hardening 后仍然 compile/hit，因此 8.39% 的边界不是漏准入造成的。若继续追求
远高于 10%，下一优先级应是用同一行为×类型方法处理剩余热点，例如 language-id 的多次字符/正则
扫描；不应靠算子重排或业务专用 opcode 填数字。

## 正确性和诊断链

另起 diagnostics-off 的完整值物化运行。off/auto 均输出 9,992 行，最终无序值集合 hash 相同：

```text
0b3c525fe120a66cf6b6bb41085e51f14df5860bfc433a9659e3c603427090de
```

ordered hash 不同，原因仍是 pipeline 中 `distinct()` 没有顺序合同；本轮证明无序语义等价。

三个真实 Ray Worker Bundle 均为 `complete`、各 29 个产物，Source ranges、原 Bytecode、Typed
Semantic、Behavior/Type/Pattern、generic/physical lowering、生成 Bytecode、HIR、LIR 和 machine
ranges 全部 `available`：

| UDF | calls | compile | fallback/hit | guard miss | execution mode |
| --- | ---: | ---: | ---: | ---: | --- |
| alphanumeric | 15 | 1/1 | 7/8 | 0 | `cinderx_unicode_property_hir` |
| punctuation | 15 | 1/1 | 7/8 | 0 | `cinderx_unicode_map_hir` |
| whitespace | 15 | 1/1 | 7/8 | 0 | `cinderx_unicode_fsm_hir` |

性能运行使用完全不带 `diagnostics/` overlay 的生产源码，并固定 `UDFJIT_DIAGNOSTICS=off`、
`PYTHONJITUDFDIAGNOSTICS=0`；full diagnostics 使用单独 dedicated Worker，耗时未进入任何 A/B。
首轮 pipeline/diagnostic 脚本遗漏 manifest，auto 实际 fail-open，已明确作废；两遍构造的 r2
结果只作为优化前参考。生产单遍 writer 的 r3 pipeline 虽然达到 11.55%，但遗漏
`NUMEXPR_NUM_THREADS` 和 `VECLIB_MAXIMUM_THREADS`；r4 补齐线程锁后为 9.61%，但早于代码审查
hardening。最终结论使用同步全部审查修复、并重新证明三个真实 Worker 命中的 r5 结果。

## 验证

- 本地 Python 3.14.6 unit/integration：512 passed、10 skipped；
- CinderX Python 语义/HIR：9 passed；C++ RuntimeTests：4 passed；
- 第六号生产 patch 可精确应用于前五号候选树，规范化源码树为 1,611 个文件，hash 为
  `2dd7292f59f07a6c8cd2b0bca69f9f87dbefe6f4c320e3c34b99e95e6e7281d6`；
- 诊断 overlay 已重新基于第六号生产树生成，正式应用无 offset/`.orig`；
- `compileall`、`git diff --check` 和最终聚焦回归在提交前再次执行。

结构化数据见
`docs/reports/evidence/2026-08-04-generic-sequence-patterns-ab.json`。
