# FineWeb 全 Pipeline 的 UDF JIT × CinderX 集成验证

**日期：** 2026-08-03  
**workload：** `pipeline_text_fineweb_full_min`  
**结论：** Worker 集成、语义一致性和全层诊断通过；端到端性能目标未通过

## 结论

通用循环与类型特化已经真实接入 Daft/Ray Worker，不再只是独立 benchmark。128 行诊断运行中，系统从
Daft receiver trampoline 和两层 closure wrapper 解析到叶子 UDF，绑定 `min_ratio=0.2`，第 8 次调用只编译
一次，随后实际命中 CinderX 机器码 16 次。29 个诊断产物组成的 Bundle 为 `complete`，CLI 校验为
`valid`；Source ranges、原 Bytecode、Typed Semantic、Behavior、Type、Pattern、generic/physical
lowering、生成 Bytecode、HIR、LIR 和 machine ranges 全部可读可定位。

但 10K 全 pipeline 的单次 A/B 只得到：

| 口径 | off | auto | speedup |
| --- | ---: | ---: | ---: |
| runner 总时间（含 Ray 启动） | 33.7048 s | 33.1756 s | 1.016x |
| 13 阶段执行段 | 26.4222 s | 26.2828 s | **1.005x** |

因此本轮是“集成通过、性能未达标”，不能宣称已实现高于 10% 的组合收益。样本每臂只有一次，1.57% 的
总时间改善还混有 Ray 启动差异；更可信的执行段改善只有 0.53%。

## 这次补齐的真实 Worker 能力

生产路径没有加入 FineWeb、Data-Juicer、函数名或业务阈值规则。它只读取 Python 语言结构和运行时事实：

1. 递归识别通用薄包装，穿过 Daft receiver trampoline、`udf(x) -> fn(x)` 和 closure wrapper；
2. 将默认参数、closure primitive 和关键 callable identity 纳入可移植依赖摘要及 Guard；
3. 将 `if not sequence: return ...` 变成机器码入口前的通用 entry guard；
4. Worker 达到调用阈值后懒编译，成功 variant 在进程内复用；
5. 永久不支持的 UDF 只记录一次终态，此后直接调用原函数，避免逐行 fallback 事件开销；
6. full diagnostics 可用 `udf:<code-hash>` 精确选择叶子 UDF，并记录 compile/hit 运行摘要；
7. 新增无 Lance 的完整值物化 harness，性能 runner 与正确性采集保持隔离。

目标相关代码只存在于 `benchmarks/fineweb_pipeline_integration.py`，用于调用目标框架自己的 task parser、
operator resolver、UDF factory 和 execution grouping；compiler/provider 生产目录没有业务特判。

## 诊断链证据

最终 Worker Bundle：

```text
artifact://fineweb-pipeline-integration-20260803/
  diagnostics/bundle-566cdbc42aa93a3b/
```

关键运行证据：

| 项目 | 结果 |
| --- | --- |
| wrapper depth | 3 |
| compile attempts / successes | 1 / 1 |
| compile 前 fallback | 7 |
| guard misses | 0 |
| machine-code hits | 16 |
| execution mode | `cinderx_unicode_property_hir` |
| HIR 特化 | `UnicodeCountProperty × 1` |
| semantic hash | `c8ae4645facfc874fe4bdf9956e128f1dd188d3f3e280a04b618fcc258a656b2` |

性能运行固定 `UDFJIT_DIAGNOSTICS=off`、`PYTHONJITUDFDIAGNOSTICS=0`；full Bundle 的耗时没有进入 A/B。

## 为什么独立穿刺很快，而全链只快 0.53%

同一 10K 输入只保留原 `alphanumeric_filter` 做归因时，真实 Daft Worker 执行段为：

| 口径 | off | auto | speedup |
| --- | ---: | ---: | ---: |
| alphanumeric 执行段 | 4.4847 s | 3.0413 s | **1.475x** |
| 含 Ray 启动 | 11.7902 s | 9.9421 s | 1.186x |

这证明 CinderX intrinsic 在 Worker 中有效，也说明先前 10.81x 的纯 UDF 穿刺不能直接外推到 Daft
stage：调度、Arrow/Series 转换、filter/project 和 wrapper Guard 都是不可消失的固定成本。更重要的是，
当前生产实现只覆盖 Unicode 属性计数这一种通用循环；其余 mapper/filter 仍走原路径。一个 stage 的节省
被 12 个未优化阶段按 Amdahl 定律稀释后，不可能支撑“大幅超过 10%”的全链目标。

集成初版甚至让执行段轻微回退。证据显示不支持的 UDF 在终态拒绝后仍逐行记录 fallback；加入
Worker-local terminal bypass 后，最终执行段才从回退变为 0.53% 正收益。这是通用集成开销修复，不改变
任何业务逻辑。

## 正确性

性能 A/B 两边均为 10,000 输入、9,992 输出、13 个业务阶段和相同最终 String 列。另起诊断关闭的完整
值物化运行，得到相同的无序值集合：

```text
multiset_sha256 = 0b3c525fe120a66cf6b6bb41085e51f14df5860bfc433a9659e3c603427090de
```

`ordered_sha256` 不同。pipeline 的 `document_deduplicator` 使用 Daft `distinct()`，其输出顺序没有合同；
因此本轮可证明的是无序语义等价，不声称顺序一致。

## 运行范围

本轮保持 13 个业务 stage 不变，但受 CinderX 镜像依赖限制：

- 10K JSONL 的原始文本值通过 `synthetic_text` 输入，输入 SHA-256 为
  `7475aaa2659f66ee4ca085c6178173db37f95c9f22d02e7b9c340bf71012d8ca`；
- 镜像没有 Lance Python 包，因此移除了生成的 `write_lance` sink；
- `official_text_ops=false` 且 `VOLC_REAL_TEXT_OPS=0`，perplexity 等使用目标框架已有 stand-in；
- fastText、SentencePiece、KenLM 和真实 Data-Juicer 模型路径不在本次结论内。

所以这是当前 stand-in pipeline 的集成证据，不是 real-model 生产发布数字。

## 下一步后端优先级

下一步仍应扩后端 pattern，不做算子重排：

1. `ImmutableLookupSpecialization + SequenceBuilderLowering`，覆盖 punctuation normalization 的冻结查表和
   Unicode 输出构造；
2. Unicode whitespace classification、branch FSM 和 sequence builder，覆盖空白压缩/trim；
3. 完成上述两类后重跑相同 full-pipeline A/B；只有 stage-level profile 证明剩余 cleaner 占比足够时，才
   增加可证明等价的 constant-regex automaton；
4. 最终换到包含官方 Data-Juicer 模型和 Lance 的 Python 3.14 镜像，做 real-model ABBA/多样本验证。

结构化原始数据见
`docs/reports/evidence/2026-08-03-fineweb-full-pipeline-integration-ab.json`。

## 2026-08-04 后续结果

上述后端优先级中的 punctuation immutable lookup/builder 与 whitespace Unicode FSM/builder 已完成。
三种目标 UDF 均在真实 Worker 命中各自唯一 HIR。保持 13 个 stage、补齐全部线程锁的最终简单
A/B 将 pipeline 执行段在不含诊断 overlay 的生产 CinderX 中从 26.4197 s 降至 24.2020 s
（1.0916x，耗时下降 8.39%）。三个 Worker 在 review hardening 后仍全部命中；覆盖目标通过，
但 15% 硬目标仍未通过。
详见 `docs/reports/2026-08-04-generic-sequence-patterns-validation.md`。

## 验证

- Python 3.14 unit：442 passed，412 subtests passed；
- integration（排除本机缺失的 `cinderjit` 原生扩展用例）：51 passed，9 skipped，17 subtests passed；
- e2e/system/fuzz：13 passed，10 skipped，5 subtests passed；
- `compileall`、`git diff --check`：通过；
- 远端 128 行 smoke/full diagnostics、10K off/auto、10K 全值正确性：全部 `status=ok`。
