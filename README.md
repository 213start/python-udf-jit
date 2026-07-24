# Python UDF JIT

Python UDF JIT 是面向数据工程框架的 Python UDF 编译与运行实验。本仓库当前交付的是一个固定拓扑纵向切片：Daft 0.7.2、Ray 2.55.0、PyArrow 22.x 与 CPython 3.14/CinderX，在一个 Head/Driver 容器和两个 Worker 容器中验证单基本块 `float64 -> float64` Projection。它不是 RFC-001～RFC-008 的完整实现，也不是生产性能发布。

## 文档入口

- [架构设计](docs/design/2026-07-13-python-udf-jit-architecture.md)
- [RFC 索引与依赖关系](docs/rfcs/README.md)
- [标量主线纵向切片复盘](docs/solutions/architecture-patterns/scalar-mainline-vertical-slice.md)
- [RFC 模板](docs/templates/提案%20技术方案（RFC）模版.md)

## 当前证据边界

最近一次完整验证 `u12-data-plane-fix-20260723-0840z` 的结论为 **PASS**。在 blue-98 上，候选镜像 `sha256:273f269c…b7108` 通过了固定三容器拓扑、回环 token 鉴权、Head 零 CPU、双 Worker ObjectRef 数据面、双 Worker CinderX Readiness、双 Worker `RaySwordfishActor` 生产资格以及真实 `off/auto` Ray Jobs。最终聚合的 11 项检查全部通过，原因码为空；自然业务覆盖和定向资格覆盖均为 `2/2`，远程 PartitionTask 为 2，Head 无数据面事件。脱敏报告位于 `/root/python-udf-jit/runs/u12-data-plane-fix-20260723-0840z/scalar-mainline-piercing-report.json`，SHA-256 为 `e8038b4813755958e84f717c8f7487d41ec21643e5b42278643217801af55a42`。

- 两个 Worker 的 Readiness 与生产 Artifact 资格测试必须分别达到 `2/2`；这是部署资格，不是自然业务覆盖。
- 真实业务 E2E 通过 Ray Jobs 启动 Driver，并由 Daft/Ray 默认调度。允许自然覆盖 `1/2` 或 `2/2` Worker，但 Head 数据面执行为失败。
- Supported、Guard Miss、Unsupported、`mode=off`、Daft 指纹失配、损坏 Artifact 和零行输入都必须与 `off` 语义闭环。
- 每个远程 PartitionTask 优先以 Worker 事件中的 Ray `task_id` 连接 State；Daft 0.7.2 暴露的嵌套 task ID 不在 Ray 2.55 State 中时，只接受 `actor_id + node_id + worker_pid` 唯一匹配的 `FINISHED attempt_number=0`。缺失、歧义、重试或跨阶段身份漂移一律为 `Inconclusive`。
- 每次运行只保留 `0600` 白名单报告；原始事件在 `0700` 临时目录中聚合后删除。报告不包含 Source、常量、Callable repr、异常消息、业务值或认证令牌。

`benchmarks/scalar_piercing/run.py` 只记录验证规模的冷启动/编译窗口和稳态样本，不应用 `1.15x` Gate，也不支持发布性能结论。U12 的 3 样本结果保持语义等价；`off`/`auto` 稳态中位数约为 0.822/0.836 ms，只能作为环境记录。

## 设计边界

- 用户保持现有 Daft UDF 源码和调用方式。
- 不修改 Daft、Ray 或 Lance 源码；Daft 0.7.2 采用版本专用 Python Runtime Instrumentation。
- CinderX JIT 与 CPython Interpreter 是同一 Scalar Python Execution Provider 内的两级执行方式，不作为两个后端。
- 当前只支持 `arg.load`、`const.f64`、`add/sub/mul.f64` 和 `return`；分支、调用、Nullable、Arrow/Unboxed、跨 Worker Cache、弹性和故障恢复均延期。
- 最终状态只由同 Run/Epoch 的 `scalar-mainline-piercing-report.json` 判定；单元测试、Readiness 或资格测试不能单独宣称纵向切片通过。
