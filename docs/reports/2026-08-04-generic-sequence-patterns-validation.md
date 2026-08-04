# 通用循环与类型特化：CinderX HIR/LIR 实现和 FineWeb 验证

**日期：** 2026-08-04

**workload：** `pipeline_text_fineweb_full_min`

**结论：** UDF JIT 只传递 typed CFG/SSA、类型、Effect、Guard 和来源信息；三个目标模式均由 CinderX 通用 HIR/LIR 实施。FineWeb 200K 简单 A/B 的执行阶段耗时下降 18.36%，端到端耗时下降 18.19%。

## 最终链路

```text
Python UDF source
  -> original bytecode
  -> provider-neutral Typed Semantic CFG/SSA
  -> CinderX direct typed-region HIR builder
  -> ordinary CinderX HIR passes
  -> generic LIR
  -> machine code
```

UDF JIT 不再生成或调用整段循环 helper，也不向 CinderX 传递 punctuation、whitespace、FineWeb、
Data-Juicer 算子名或函数名。`udf_physical_lowering` 的诊断状态固定为
`not_applicable_backend_owned`，表示物理优化属于 CinderX。

| 验证行为 | Typed Semantic | CinderX 通用 HIR |
| --- | --- | --- |
| Unicode 分类 + 归约 | loop/phi + `unicode.property` + numeric ops | `UnicodeData/Kind/Read/Classify` + `IntBinaryOp` |
| 不可变查表 + 序列构造 | loop/phi + `immutable.lookup` + builder | `PrimitiveTableLookup` + `SequenceBuilder*` |
| Unicode 分类 + FSM + 序列构造 | loop/phi + `unicode.property` + `fsm.transition` + builder | `PrimitiveTableGet` + branch + `SequenceBuilder*` |

上述都是标量、控制流、表访问和 builder 原语；CinderX 中不存在三个业务复合 HIR opcode。

## CinderX 实施的优化

1. exact `str` Guard 后缓存 Unicode data、kind 和 length，循环内按 1/2/4-byte 宽度直接读取；
2. ASCII Unicode property 使用只读分类表，非 ASCII 保留 CPython Unicode 语义的慢路径；
3. 编译期不可变表物化为 CodeRuntime 持有的原生 `int64` 表，动态查表有边界检查；冻结映射使用有界平衡查找；
4. `SequenceBuilderCreate/Data/Append/Finish` 使用 `_PyUnicodeWriter`，常见 append 在 LIR 中直接写入，扩容或宽度升级走语义等价慢路径；
5. CFG 回边包含 eval-breaker / periodic-task safepoint，异常顺序、Effect 和来源 offset 在进入 HIR 前重验；
6. closure 中的精确 Python 函数目标以 `GuardIs` 保护，并交给 CinderX 既有通用 inliner，消除 wrapper 内的动态 `VectorCall`；
7. generator 的 HIR→LIR 路径已能完成目标用例；若同一 code/builtins/globals/specialization 仍出现确定性的 `std::bad_variant_access`，负缓存阻止重复编译，且每个 code 的失败变体数有上限。

## 通用性边界与新增校验

- 前端用两组不同业务名称、不同源码形态的 UDF 复用同一 typed pattern；序列化语义中不含业务标识；
- 新增 Unicode property 只改变属性参数，不修改 adapter 或后端 opcode；
- `sequence.builder.create` 现在显式接收容量 SSA value，不再暗中依赖“最近一次 sequence.length”；
- CinderX 为每个 builder SSA value 独立传递 writer state，并在 CFG edge 上同步隐藏状态；双 builder 回归用例证明两个实例不会互相覆盖；
- FSM 的 reference lowering 和 canonical lowering 从 `class_count` 属性计算索引，不再写死乘数；当前已实现的 classifier 类型是 `bool`，因此验证范围为两类 FSM；
- 当前直接 typed HIR 的签名范围仍是单个 exact `str` 输入。它是可扩展的通用子集，不等于支持任意 Python UDF。

## 诊断链与运行隔离

三类最终诊断包各有 25 个产物，状态均为 `complete`。以下各层全部为 `available`：

```text
source_ranges -> original_bytecode -> typed_semantic -> generic_lowering
-> generated_bytecode -> cinderx_hir -> cinderx_lir -> machine
```

最终 bundle：

- reduction: `/root/udfjit-generic-hir-final-20260804/diagnostics-v6/reduction/diagnostic-generic-typed-reduction-diagnostic-bd79281436960c60`
- mapping: `/root/udfjit-generic-hir-final-20260804/diagnostics-v6/mapping/diagnostic-generic-typed-mapping-diagnostic-ef14397846420d24`
- FSM: `/root/udfjit-generic-hir-final-20260804/diagnostics-v6/fsm/diagnostic-generic-typed-fsm-diagnostic-73fabb3422f80c5a`

诊断运行必须在进程初始化前设置 `PYTHONJITUDFDIAGNOSTICS=1`，并由 dedicated worker 使用
`UDFJIT_DIAGNOSTICS=full`。性能运行显式设置 `UDFJIT_DIAGNOSTICS=off` 和
`PYTHONJITUDFDIAGNOSTICS=0`；正常路径不保留结构化 HIR/LIR/machine provenance。

## 三模式 micro A/B

同一 aarch64 Python 3.14.3/CinderX 构建、固定 CPU、诊断关闭；7 轮中位数，每轮 400 次调用、
4,275,200 个字符。baseline 是原 UDF 的普通 CinderX JIT，candidate 是 UDF JIT typed region +
CinderX generic HIR。每个 case 的 checksum 完全一致。

| Pattern | baseline median | candidate median | Speedup |
| --- | ---: | ---: | ---: |
| alphanumeric | 474.730 ms | 43.129 ms | **11.007x** |
| punctuation | 132.739 ms | 42.695 ms | **3.109x** |
| whitespace | 233.745 ms | 94.507 ms | **2.473x** |

这些数字用于验证后端 kernel，不直接外推为 pipeline 收益。

## FineWeb 200K 简单 A/B

两臂使用相同 aarch64 主机、镜像、Lance fixture、CPU 4-7、4 个 Ray CPU、单线程数值库设置，
业务 stage 不变，最终 sink 不计时。唯一变量是 `UDFJIT_MODE=off` / `auto`；两臂均为
200,000 输入、199,765 输出且 `status=ok`。

| 口径 | off | auto | Speedup | 耗时下降 |
| --- | ---: | ---: | ---: | ---: |
| pipeline 执行阶段 | 522.088 s | 426.247 s | **1.2249x** | **18.36%** |
| 含 Ray 初始化总时间 | 528.841 s | 432.628 s | **1.2224x** | **18.19%** |

结果文件：

- off: `/root/udfjit-generic-hir-final-20260804/results/fineweb-200k-off-final-v2/pipeline_text_fineweb_full_min@v0__udfjit_lance_200k__daft_ray__8feb43b8-8d37-46a3-a762-1bfc21b9b598.json`
- auto: `/root/udfjit-generic-hir-final-20260804/results/fineweb-200k-auto-final-v2/pipeline_text_fineweb_full_min@v0__udfjit_lance_200k__daft_ray__332f3ad5-ce80-42ea-b3a3-b325980b3e67.json`

这是用户允许的单次简单 A/B，不是多轮统计发布结果。它证明当前组合路径在该 200K workload 上
明显超过单独 CinderX 约 10% 的预期，但不代表所有 pipeline 都有相同收益。

## 验证

- UDF JIT typed/worker/diagnostics 相关单元测试：85 passed；
- CinderX 定向 Python 测试：13 passed；跨仓诊断集成：1 passed；
- CinderX 定向 RuntimeTest：2 passed；
- clean `_cinderx` 构建、import/init/force-compile smoke：通过；
- 三类诊断 bundle：全部 complete，Source→Machine 链完整；
- `git diff --check`、Python compile/test 和 C++ 构建在提交前复核。

完整 `runtime_tests` 目标仍被候选基线中无关的
`osr_loop_header_secondary_entry_test.cpp` 重复定义阻塞；本轮使用排除该文件的已编译对象重链定向
RuntimeTest，不把完整目标记为通过。

结构化证据见
`docs/reports/evidence/2026-08-04-generic-sequence-patterns-ab.json`。
