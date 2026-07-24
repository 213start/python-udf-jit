# 标量主线纵向切片：四个承重接缝与证据闭环

## 结论边界

本记录只覆盖固定版本、固定三容器拓扑和一个 `float64 -> float64` Daft Row-wise Projection。两个 Worker 的定向 Readiness/资格测试证明整个 Worker Pool 可执行；自然 E2E 仍由 Ray 默认调度，使用一个 Worker 也是合法结果。最终结论必须来自同一 Run/Epoch 的白名单报告，不能把历史 U4、调试 Epoch 或单独测试结果拼入。

`u12-data-plane-fix-20260723-0840z` 的纵向切片最终状态为 **PASS**。固定三容器拓扑、双 Worker ObjectRef、双 Worker Readiness、双 Worker 生产资格、真实 `off/auto` Ray Jobs 和白名单聚合均在同一 Run/Epoch 完成。自然业务覆盖与定向资格覆盖都是 `2/2`，两个远程 PartitionTask 均唯一归因到 `FINISHED attempt_number=0`，Head 没有数据面事件。

这个 Pass 仍没有证明 Arrow/Unboxed 数据路径、Nullable、分支/Graph Break、跨 Worker Cache、弹性恢复、多物理机容灾或 `1.15x` 性能收益。

## 四个承重接缝

| 接缝 | 已验证处置 | 继续扩展前的约束 |
|---|---|---|
| Execution Carrier | 使用 Daft 0.7.2 `RaySwordfishActor`；Head 为零 CPU，同一 Plan 在两个 Worker 分别完成 compile、hit 和语义执行，生产资格为 `2/2` | 新 Carrier 或资源配置仍必须重新做双 Worker 资格，不能只依赖自然调度 |
| Scalar Slot | Worker-local capability handle 绑定 `float64` Slot；Epoch、ABI、类型、进程代际与借用期在 Data Load 前校验 | Arrow/Unboxed 只能作为下一种 Descriptor，不能绕过现有所有权/ABA Gate |
| Artifact Lowering | Worker 对 bounded Artifact 重新解码和语义验证；唯一生产 Lowering 从 Region 生成 code object，semantic/artifact/code/Variant hash 可连接 | 不接受固定手写函数替代 Artifact 算术；非法/损坏 Artifact 在 Descriptor/Compile 前拒绝 |
| CinderX Data Load | Region-derived code object 经 `force_compile/is_jit_compiled`，HIR 中恰有一个 `LoadUdfDataF64`；同进程完整 Variant Key 才能 Hit | 提交点仍是首个语义 Load/算术；以后增加类型或列式 Load 必须保留前置 Guard 支配关系 |

四个接缝都通过双 Worker 生产资格测试，才允许进入真实 E2E；真实 E2E 再负责证明默认调度、语义 A/B、Attempt 唯一性和 Fail Open。资格测试不计入 `natural_worker_coverage`。

## 真实失败方案与诊断信号

### 1. 版本号相同不等于 Daft 源码相同

早期从已有镜像重打包的 Daft 0.7.2 Wheel，其 `DataFrame.with_columns` AST 指纹和官方 v0.7.2 不同。插件正确 Fail Open，但最初只看到“没有 JIT 事件”，容易误判为 Worker 问题。处置是锁定官方源码指纹并重建 Wheel；不得把不明补丁的指纹静默加入白名单。

诊断顺序：版本 → `Func.__call__`/`with_columns` 签名 → AST 指纹 → Hook 安装结果 → Driver Registry/Capture 事件。

### 2. `internal: true` 单网络会让旧 Docker 的 Jobs 发布端口失效

Docker 18 环境中，Compose 虽声明 `127.0.0.1:8265:8265`，Head 仅连接 internal 网络时宿主可能没有实际监听/NAT。容器内 Jobs API 返回 401 只能证明服务和认证 Gate 存在，不能证明宿主回环入口可用。处置是 Head 同时连接内部数据网和独立 gateway 网，两个 Worker 仍只连接内部网；每个 Epoch 必须从宿主再次验证回环访问和非回环隔离。

### 3. Docker bridge 间流量会被宿主 firewalld 拦截

Ray 节点注册失败时，容器日志和 Docker 网络配置可能都正常，但 firewalld/nftables 仍阻断 bridge 流量。实验只对当前 Run 的 bridge 做临时 trusted 绑定，并在最终清理移除；不能写永久全局规则。

### 4. 多网络 Head 注册错误地址会伪装成 Carrier 挂起

Head 同时连接 internal 数据网和 Dashboard gateway 网时，Ray 自动选择了 gateway 地址注册 NodeManager。Worker 只连接 internal 网，因此能加入 GCS，却无法拉取 Head 持有的 ObjectRef；`RaySwordfishActor.run_plan` 最终表现为没有结果、没有异常的等待。处置是在 internal 网给 Head 设置稳定 alias，并把解析后的地址显式传给 `ray start --node-ip-address`。

修复后，Head 注册地址与 internal alias 一致；新增 2 MiB Head-owned ObjectRef 门禁要求两个定向 Worker 都在 30 秒内拉取并校验。这道门禁位于 Readiness 和 Carrier 资格之前，可以把网络数据面故障与 CinderX/Daft 故障分开。

### 5. NativeExecutor 中重复读取 Ray Context 不是安全的归因方案

Daft 的 task display/name 可以重复，同一 Actor 也会连续执行多个 `run_plan`。把两行、两个输出分区或 task name 当成两次远程任务会制造假证据。Wrapper 反序列化并创建进程本地 Adapter 时只捕获一次运行身份，事件发射不再从 NativeExecutor/UDF 线程反复读取 Ray Context。

不同容器的 PID namespace 还可能都分配相同数值 PID，所以 Worker 独立性使用随机 `process_generation`，不能比较裸 PID。

### 6. Daft 嵌套 task ID 与 Ray State task ID 不一定相同

Daft 0.7.2 UDF 内 `ray.get_runtime_context().get_task_id()` 返回的嵌套 ID，在 Ray 2.55 State API 中可能没有同 ID 记录。State 仍提供唯一的物理计划任务，其 `actor_id`、`node_id` 和 `worker_pid` 与事件一致。归因先尝试 task ID 精确连接；不存在时只接受该三元组唯一匹配且状态为 `FINISHED attempt_number=0` 的记录。多个候选、retry 或任一字段缺失都保持 Inconclusive。

### 7. Driver/Worker 内存事件不能直接当 Run 报告

Driver DecisionEvent 和 Worker RuntimeEvent 都是进程本地。验收使用同 Carrier 内的只读诊断 UDF带回 value-free 事件，外部 Harness 再写入每 Run 的 `0700` 临时目录。原始 JSONL 和最终报告从创建时就是 `0600`；成功、失败和 Inconclusive 聚合后都删除原始事件。

## 验收结构

1. 每阶段实采 Container ID/StartedAt 派生的 Boot ID、Ray Node/角色映射和 Candidate Manifest。
2. 两个 Worker 各运行 Readiness；两个 Worker 各用同一 Artifact 运行生产资格测试。
3. 分别提交真实 `UDFJIT_MODE=off` 与 `auto` Ray Job；Driver 必须位于 Head。
4. 使用多个 Parquet 文件形成自然调度的 Daft Projection；从 Worker RuntimeEvent 的 Ray task ID 连接 State Attempt。
5. Supported、Guard Miss、Unsupported、指纹失配、损坏 Artifact 和 zero-row 分别做结果/调用/副作用与事件断言。
6. 白名单聚合器给出 `pass`、`fail`、`stop` 或 `inconclusive`；没有“带警告通过”。

U12 完整执行了第 1～6 步。最终报告为 `/root/python-udf-jit/runs/u12-data-plane-fix-20260723-0840z/scalar-mainline-piercing-report.json`，SHA-256 为 `e8038b4813755958e84f717c8f7487d41ec21643e5b42278643217801af55a42`。聚合器的 11 项检查均为 Pass，`reason_codes=[]`，自然与资格 Worker 覆盖均为 `2/2`。

## 后续扩展次序

只有本切片最终报告为 Pass，才按以下顺序扩展：

1. Arrow Primitive Descriptor，同时复用 Scalar Slot 的 capability/ownership/epoch Gate。
2. Unboxed Lane 与更多非空标量类型，逐类型增加 Artifact/Guard/CinderX Load 证据。
3. Nullable 和分支，再定义局部 Side Exit/Deopt 的精确提交点。
4. 多 Variant、Singleflight、预算和跨 Worker Cache。
5. 弹性、Retry/恢复、多物理机与正式性能 Harness。

任何接缝在新布局或新类型上不成立，应先重构该接缝，而不是靠更多 Fixture 或耗时指标掩盖。
