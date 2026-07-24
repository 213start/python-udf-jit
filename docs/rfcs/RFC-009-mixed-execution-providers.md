# RFC-009：混合 Execution Provider

**状态 (Status):** Draft

**作者 (Authors):** Python UDF JIT 项目组

**创建日期 (Created):** 2026-07-17

**更新日期 (Updated):** 2026-07-17

**相关 Issue/PR:** 本地方案评审阶段，无外部 Issue/PR

**类别:** 可选特性

**工作量估算:** 8 人周

**上游 RFC:** [RFC-005：数据布局特化](RFC-005-data-layout-specialization.md)、[RFC-007：守卫式执行](RFC-007-guarded-execution.md)

---

# 1. 概述

## 1.1 简介

本提案定义在同一个 UDF Region Graph 中组合多个 Execution Provider 的能力。当前 Provider 只分为两个执行域：Scalar Python Provider 内含 CinderX JIT 与 CPython Interpreter 两级执行方式；Host Columnar/Vector Provider 内含 Arrow/Native Kernel 与 NumPy/Pandas Batch Adapter。Region Partitioner 根据能力和总成本选择 Provider，并在边界显式插入 Materialize、Box/Unbox、Layout Convert、GIL 和 Side Exit Contract。

CPython Interpreter 不作为第三个后端；CinderX Deopt、Graph Break 和普通不透明 Python/C Extension 调用都留在 Scalar Python Provider。GPU 等具有独立设备内存、传输和 Stream 的目标，未来才可作为第三类 Accelerator Provider。

## 1.2 动机

一个真实 UDF 可能同时包含可向量化算术、NumPy 批量调用、动态 Python 分支和 Opaque Call。全函数只选择一个后端会导致两种极端：要么因少量动态逻辑放弃所有列式收益，要么为了进入 Native 路径引入高成本全量转换。

混合 Provider 的价值不只是提高覆盖率，而是在保持 Python 语义的前提下改变局部执行模型，并把每一次跨域转换纳入成本与 Explain。RFC-010 可以不依赖本 RFC，把整个合法 Region 交给 Host Columnar Provider；本 RFC 进一步允许同一个 UDF 内同时存在 Scalar 与 Columnar Region，因此不属于本期主线 `1.15x` 或高阶阶段的强制前置范围。

## 1.3 目标

### 目标

1. 扩展架构基础 Execution Provider Capability/Compile/Execute 契约，使其支持同一 Region DAG 的多 Provider Assignment。
2. Driver 生成合法 Provider 候选和布局约束，Worker 结合实际 Target/Layout/Profile 形成 `BoundRegionPlan`。
3. 在 Scalar Python 与 Host Columnar/Vector 之间显式建模 Value/Layout/Effect 和转换边。
4. 总成本同时考虑计算、编译、Copy、Materialize、Box/Unbox、GIL、Region 切换和回退风险。
5. 支持 Provider 部分拒绝、失败隔离和 Scalar Interpreter Continuation。
6. 保留 Accelerator Provider 扩展，但不进入首期实现/交付。

### 非目标

- 不把每个库名称定义为一个 Provider。
- 不实现具体 Arrow/Native Kernel；RFC-010 负责 Host Columnar 执行。
- 不实现批内稀疏退出；RFC-011 负责 Lane 级恢复。
- 不跨 Effect/Exception Barrier 融合或重排。
- 不改变 Daft/Ray 的分区、调度、重试和 Object Store 语义。

# 2. 用例分析

```python
def score(row):
    base = row.price * row.quantity       # columnar candidate
    adjusted = np.clip(base, 0, 1000)     # batch library candidate
    if audit_enabled:                     # scalar/global guard
        audit(adjusted)                   # opaque Python effect
    return adjusted * 0.9                 # columnar candidate
```

| 子区域 | Provider/执行方式 |
|---|---|
| primitive element-wise arithmetic | Host Columnar/Arrow-Native（若 RFC-010 可用） |
| `np.clip` 且输入已是 ndarray/Arrow-compatible | Host Columnar/NumPy Adapter |
| `audit_enabled/audit` | Scalar Python/CinderX 或 CPython Interpreter |
| Guard/Deopt | Scalar Python Provider 内部转移 |

若跨域 Materialize/Copy 成本大于计算收益，Target Binder 可以选择整个 Region 留在 Scalar Python Provider；“支持”不等于“必须选择”。

# 3. 方案设计

## 3.1 总体方案

```mermaid
flowchart LR
    CORE["SemanticRegionGraph"] --> CAND["Provider Capability Query"]
    CAND --> PLAN["CandidateRegionPlan<br/>legal candidates + cost envelope"]
    PLAN --> BIND["Worker Target Plan Binder<br/>layout + ABI + CPU + profile"]
    BIND --> BOUND["BoundRegionPlan"]
    BOUND --> S["Scalar Python Provider<br/>CinderX ⇄ Interpreter"]
    BOUND --> H["Host Columnar/Vector Provider<br/>Arrow/Native · NumPy/Pandas"]
    S <-->|"Materialize · Box/Unbox<br/>SideExit Contract"| H
```

### 两阶段分区

1. Driver 只查询 Provider-independent 能力模型，生成合法候选、所需布局、Guard 和成本包络；不固化 Worker Provider 版本。
2. Worker Target Binder 使用真实 Descriptor、CPU、库 ABI 和运行画像选择最终 Provider Assignment，插入转换边并再次 Verify。

### 总成本

```text
total_cost = compute_cost
           + compile_cost_amortized
           + layout_conversion_cost
           + copy_and_materialization_cost
           + box_unbox_and_gil_cost
           + region_switch_cost
           + guard_side_exit_risk_cost
```

首期使用规则与线性成本估计；只有热点候选允许有限 A/B 测量，不做全局自动调优搜索。

## 3.2 技术选型

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| ONNX Runtime 式 Capability Partition + Worker Binding | Provider 解耦；允许部分覆盖和目标晚绑定 | 转换边/成本模型复杂 | 采用 |
| 每个 Provider 独立遍历并抢占节点 | 实现局部简单 | 顺序依赖、全局转换成本不可控 | 不采用 |
| 全 UDF 固定单 Provider | 执行简单 | 小量动态逻辑污染整图，性能上限低 | 作为成本模型候选保留 |
| 将 Arrow/NumPy/CPython 各列为 Provider | 表面清晰 | 混淆库与执行域；CinderX/CPython重复 | 不采用 |

## 3.3 功能与性能设计

### Capability Contract

```text
CapabilityResult {
  provider_id,
  supported_operations,
  type_null_effect_constraints,
  required_input_layouts,
  produced_output_layout,
  required_guards,
  side_exit_capabilities,
  estimated_compute_cost,
  estimated_compile_cost,
  estimated_conversion_cost
}
```

### Value/Layout Contract

跨 Region Value 必须声明 LogicalType、Null、Representation、Ownership、Mutability、Lifetime 和 Error/SideExit Contract。转换节点也是 Region DAG 的显式节点，必须计时、计字节和可回退，不能藏在 Provider 内部。

### Provider 失效

- Capability 拒绝：重新求解其他合法 Assignment，最终可全 Scalar。
- Compile 失败：该 Provider/Region/Key 进入负缓存，当前执行走 Scalar Interpreter。
- Execute 内部故障：未提交输出前 Side Exit；已产生不可逆副作用的 Region 禁止投机混合执行。
- Python 业务异常：按原顺序传播，不视为 Provider 故障。

### 性能与验收

- 功能 Benchmark 使用包含两段纯数值计算和一段 Opaque Python Call 的固定 UDF，验证 Region DAG、转换边、结果/异常和 Explain。
- A：在 RFC-010 Host Columnar 实现可用时，仍强制整个 UDF 使用 Scalar Python；B：额外启用本 RFC，允许同一 UDF 混合 Provider。
- B 的端到端中位数必须优于 A，若成本模型选择全 Scalar 则允许相等但 Explain 必须给出转换成本拒绝原因；任何支持场景不得低于 A 的 `0.98x`。
- 本 RFC 为可选扩展，不计入本期 `1.15x`；高阶最终 `1.30x` 由 RFC-010～012 的组合作业判定。

## 3.4 安全隐私与DFX设计

- Provider 只接收 Verified Physical Region/Descriptor，不读取框架私有对象或其他 Provider 内部状态。
- 转换节点验证 Bounds、Ownership、Null 和输出容量；跨 Provider Borrowed Buffer 必须持有 Keepalive。
- Provider Crash/Timeout 按 Provider/Region/Job 熔断，不关闭 Scalar Interpreter 正确性路径。
- Capability/Cost/版本和 Assignment 写入 Artifact/Variant Explain，支持复现。
- Provider Plugin 由 Manifest/签名白名单加载，不从用户可写路径动态装载。
- Profile 不包含业务值；高基数 Key 采样和限流。

## 3.5 编程与调用设计

### 3.5.1 编程模型基本设计

普通用户不指定 Provider。Provider 开发者实现版本化 SPI、Capability Schema、Compile/Execute 和 Contract Tests；框架 Adapter 不依赖具体 Provider。

### 3.5.2 接口定义与设计

#### 3.5.2.1 `ExecutionProvider` SPI

```text
provider_manifest() -> ProviderManifest
query_capability(region, available_layouts, target) -> CapabilityResult
compile(region, compile_context) -> ExecutableRegion | Reject
execute(executable_or_handle, inputs, runtime_context) -> RegionResult
```

- **异常处理：** 内部错误返回结构化 `InternalFailure`；不得直接修改框架异常状态后继续。
- **约束说明：** Capability 必须保守；未声明的类型、Effect 或 Layout 一律不支持。

#### 3.5.2.2 `IF-TARGET-BIND-API`

- **接口描述：** 从 Candidate Plan 和真实 Worker Capability 生成 Bound Region Plan。
- **输入：** CandidateRegionPlan、LayoutDescriptorSet、Provider Manifests、Policy/Profile。
- **输出：** Provider Assignment、转换 Region、Guard 和总成本。
- **变更说明：** 新 Provider 不改变 Core UDF IR；只扩展 Provider Manifest/Physical/Provider IR。

### 3.5.3 编程手册设计

新增《Execution Provider 开发指南》，包括 SPI、能力保守性、Value/Layout Contract、转换记账、异常/Side Exit、Contract Test、ABI/签名和性能验收。

# 4. 缺点和风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 分区/转换复杂度 | 维护成本与错误面增加 | 两阶段 Verify、显式 Contract、有限模式起步 |
| Provider 过多 | 搜索空间/编译延迟膨胀 | 按执行域而非库划分，候选/预算上限 |
| 成本模型不准 | 选择慢路径 | 保守门禁、Profile 校准、可回到全 Scalar |
| Side Effect 跨域 | 重放/异常顺序错误 | Effect Barrier、Commit Contract、禁止投机 |
| ABI 组合爆炸 | 交付/缓存复杂 | Provider Manifest、独立插件和 Compatibility Key |

# 5. 现有技术

- ONNX Runtime Execution Provider 使用 Capability Partition 将图分配给硬件/库 Provider；本提案额外处理 PythonRegion、GIL、Boxing 和 Interpreter Continuation。
- TensorFlow PluggableDevice、PyTorch Backend 和 IREE HAL 展示了执行域插件化；本提案当前只保留 Scalar Python 与 Host Columnar 两个 CPU 执行域。
- 数据库异构执行器表明转换成本可能主导收益，因此 Provider Assignment 必须考虑 Materialization 和数据布局。

# 6. 未解决问题

本 RFC 无阻塞性未决问题。Accelerator Provider 的 Device Memory、Stream 和远程编译契约留待独立 RFC，不进入当前实现。

---

## 附录 A：参考资料

- [RFC-005：数据布局特化](RFC-005-data-layout-specialization.md)
- [RFC-007：守卫式执行](RFC-007-guarded-execution.md)
- [ONNX Runtime Execution Providers](https://onnxruntime.ai/docs/execution-providers/)

## 附录 B：术语

| 术语 | 定义 |
|---|---|
| Execution Provider | 按执行模型、数据表示和资源域划分的可插拔执行实现 |
| CandidateRegionPlan | Driver 侧合法 Provider 候选与成本包络，非 Daft/Ray 概念 |
| BoundRegionPlan | Worker 选定 Provider 并绑定真实 Layout 后的 Region DAG |

## 附录 C：文档更新计划

Provider SPI、成本字段、转换 Contract 或新执行域变化时更新；Host Columnar 具体能力变化同步 RFC-010。
