# Python UDF JIT

Python UDF JIT 为 Daft/Ray 作业提供透明的标量 Python UDF 编译路径。当前代码已经实现 RFC-001～RFC-008 的标量阶段：用户保持原有 UDF 与 `where`、`select`、`with_columns` 调用方式，驱动节点生成可移植制品，工作节点完成严格校验、标量物理化、CinderX 编译、守卫式多变体执行和冻结治理。

本仓库交付的是第一个正式实现，不读取穿刺期制品，也不承担任何旧格式或未来格式的兼容责任。向量、Arrow 和批处理执行仅保留扩展边界，本期没有实现；RFC-009～RFC-012 始终关闭。

## 当前状态

截至 2026-07-29，提交 `eb97ee8a806f676cda8e1c9f78e6dedb5b501aca` 已在 blue-98 的 Python 3.14.3/CinderX 环境完成单物理宿主三容器正式验收：

| 项目 | 结果 |
|---|---|
| 单元测试 | 297/297，通过，零跳过 |
| 集成测试 | 59/59，通过，零跳过 |
| 实时系统测试 | 22/22，通过，零跳过 |
| RFC-001～RFC-008 | 8/8 标量契约通过 |
| Ray 拓扑 | 1 个 Head/Driver 容器和 2 个 Worker 容器 |
| 自然工作节点覆盖 | 2/2 |
| 数据面隔离 | Head/Driver 数据面事件为 0 |
| 清理 | 临时容器、网络、令牌和 firewalld 运行时绑定全部移除 |

两个 Worker 都实际执行了生产路径。每个 Worker 分别产生 1 次编译、60 次缓存命中和 60 次语义执行；这不是仅以容器存活或定向探针代替真实执行。Worker-2 重启后，旧阶段证据因进程代际变化被判为无效，并由新进程重新建立证据。

这次运行的已执行门禁为 56/56 通过，但完整发布契约共有 57 项。尚缺的是一个 Head/Driver 与两个物理隔离 Worker 组成的真实多节点外部证据，因此机器报告保持：

- `unit_completion_status=incomplete`
- `release_ready=false`
- 缺失门禁：`prerequisite.multi_node_environment`

三容器结果不能冒充真实三物理节点结果。生产目标版本是 Python 3.11.6；当前解码器和 CinderX 补丁先在 Python 3.14.3 上开发、测试和验证，Python 3.11.6 适配与资格验证仍待 CinderX 支持就绪后完成。

## 已实现的标量主线

| RFC | 已实现能力 |
|---|---|
| RFC-001 | Daft 0.7.2 精确兼容检查、显式启动引导、候选登记、操作定稿、完整 UDF 选项保留和工作节点载体 |
| RFC-002 | CPython 3.14 版本化字节码解码、控制流图、抽象解释、源码映射、图中断、依赖身份和有界捕获缓存 |
| RFC-003 | 语义核心 IR、类型/空值/效应/异常分析、区域划分、验证器、参考执行和精确 Python 区域 |
| RFC-004 | 首个正式制品格式 1.0、固定字段与分段、内容哈希、内联和 Ray `ObjectRef` 双载体、工作节点重新校验 |
| RFC-005 | `bool/int32/int64/float32/float64` 标量槽位、可空有效位、能力句柄、进程代际、所有权、原子发布和向量扩展拒绝点 |
| RFC-006 | 五种标量类型、可空值、算术、比较、分支、CinderX 数据内建函数、W^X 代码、标量执行与解释续体 |
| RFC-007 | 分层守卫、策略绑定的变体键、异步单次编译、多变体缓存、负缓存、熔断、精确侧退出、活动引用和硬资源预算 |
| RFC-008 | `off/observe/auto`、冻结策略和策略哈希、作业/租户隔离、解释信息、有限原因码、异步遥测、命令行工具和验收聚合 |

治理没有远程凭据分发或运行中控制通道，也没有紧急停用通道。模式和策略在作业提交时冻结；回滚通过新作业使用 `off` 或部署回滚完成，不中断已经进入执行的区域。

## 性能口径

性能与功能状态分开管理。当前仅保留方向性观测，不把单次数据写成正式性能结论，也不要求本期一次达到 `1.15x`。

- 本次三容器正式验收的小规模 `off/auto` 观测结果语义哈希一致；3 个样本的中位数分别为 726,960 ns 和 707,860 ns。该数据只用于确认测量链路。
- FineWeb 200K 实际文本负载的单次逐算子 A/B 中，`off` 为 559.683 s，`auto` 为 574.469 s，方向信号为 `auto` 慢 2.64%。输入 200,000 行，输出均为 199,765 行，数据模式和无序多重集哈希一致。
- FineWeb 使用文本和布尔值替代算子，不属于当前标量 JIT 支持域，因此只能证明不支持路径无感并提供开销方向，不能证明标量 JIT 收益或正式回归。

后续每个优化变更继续执行同环境 A/B。只有另行声明性能资格时，才使用稳定性统计和累计 `1.15x` 目标。

## 文档入口

- [主线完成计划](docs/plans/2026-07-26-001-feat-mainline-production-completion-plan.md)
- [RFC 索引与实现状态](docs/rfcs/README.md)
- [标量主线正式验收报告](docs/reports/2026-07-29-mainline-scalar-acceptance.md)
- [部署、灰度与回滚手册](docs/operations/mainline-deployment-and-rollback.md)
- [架构设计](docs/design/2026-07-13-python-udf-jit-architecture.md)
- [穿刺期复盘](docs/solutions/architecture-patterns/scalar-mainline-vertical-slice.md)

## 验收证据

blue-98 运行目录：

```text
/root/python-udf-jit/runs/u17-runtime-eb97ee8/acceptance
```

关键白名单证据：

| 文件 | SHA-256 |
|---|---|
| `RUN_SUMMARY.json` | `2044be5a395b28dd0fa342268b6bc04589ef02fcdf3acc5dc752ad7ff2889e70` |
| `evidence/formal-acceptance-report.json` | `0da15ceb327540eccb1b28dd6dcd8c5a1dbf0ace58f0e037a0743fd6b481938d` |
| `evidence/base-report.json` | `020c29ed7bcf4b1b45f88d86b697592b50273d27b68755aceb8f088b7ccf6e19` |
| `evidence/cleanup-proof.json` | `33461492ed291ddf64673146796677aa27e96e12ad0c755aa280e111e0ca59bc` |
| `evidence/measurement.json` | `e1ea239689bebb48b0164cd6611305e05931920fe66a557ae61797b084ab70c4` |

报告只保留 `0600` 白名单文件。原始事件和临时令牌已删除；报告不包含源码、常量、可调用对象表示、异常消息、业务值或认证令牌。
