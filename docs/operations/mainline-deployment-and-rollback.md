# 标量主线部署、灰度与回滚手册

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 适用范围 | RFC-001～RFC-008 标量路径 |
| 当前开发验证版本 | Python 3.14.3、CinderX `ac09c68527153b43cc8b4f16f36d9245cb861d12` |
| 生产目标版本 | Python 3.11.6，CinderX 适配和资格验证待完成 |
| 框架基线 | Daft 0.7.2、Ray 2.55.0、PyArrow 22.0.0、Lance 7.0.0 |
| 首个正式制品格式 | 1.0 |
| 修订日期 | 2026-07-29 |

本手册只描述标量路径。向量、Arrow、批处理执行和 RFC-009～RFC-012 不在本期范围内。系统没有远程凭据分发、运行中策略更新或紧急停用通道。

## 2. 发布状态边界

发布状态分为三条独立轴：

| 状态轴 | 当前含义 |
|---|---|
| 标量功能状态 | 代码和 blue-98 三容器门禁已通过；真实三物理节点门禁尚未执行，因此不能标记为发布就绪 |
| 灰度状态 | 未绑定首发业务，最高为 `observe-ready` |
| 性能资格 | `unqualified`；当前只有方向性 A/B，不形成正式性能结论 |

不得用单元测试、容器存活、单次 A/B 或三容器结果替代真实多物理节点发布证据。

## 3. 不可变发布输入

每次部署必须形成新的候选清单，并锁定以下内容：

1. Python UDF JIT Git 提交、Wheel SHA-256 和候选镜像摘要。
2. CPython 版本、SOABI、CinderX 提交、源码树、补丁和 Wheel SHA-256。
3. Daft、Ray、PyArrow 和 Lance 精确版本。
4. 冻结策略全文、策略版本和策略 SHA-256。
5. 制品格式、包装器/载体结构和运行时 ABI。
6. 作业、租户、运行批次和集群代次。

驱动节点与所有工作节点必须使用同一候选镜像。提交前不完整或不一致时停止作业；工作节点在读取载体后仍要重新校验制品、运行时和策略。

## 4. 安装与启动引导

Wheel 安装完成后，显式把随包交付的 `.pth` 文件安装到选定的 `purelib` 根目录：

```bash
python -m python_udf_jit.bootstrap_install --purelib /absolute/python/purelib
```

安装器要求目标目录由当前用户拥有、不是符号链接、不可被组或其他用户写入。目标文件已存在时只接受内容和权限完全一致的幂等安装。

`.pth` 启动逻辑在 `off` 模式下不导入 Daft 或 Ray。在其他模式下，它只登记 Daft 延迟导入钩子；Daft 0.7.2 的版本、签名、私有接缝和源码指纹全部通过后才安装适配器。

## 5. 模式与冻结策略

### 5.1 模式

| 模式 | 捕获 | 影子编译 | 优化执行 |
|---|---:|---:|---:|
| `off` | 否 | 否 | 否 |
| `observe` | 是 | 仅在冻结策略明确允许时 | 否 |
| `auto` | 是 | 是 | 仅在冻结策略已授权时 |

模式解析优先级固定为：

```text
UDFJIT_DISABLE
→ UDFJIT_PLUGIN_ENABLE
→ UDFJIT_MODE
→ Daft/运行时兼容性
→ 冻结策略
```

低优先级输入只能收紧能力，不能扩大权限。建议初始环境：

```bash
export UDFJIT_PLUGIN_ENABLE=1
export UDFJIT_MODE=off
```

本期策略在作业提交时冻结并随载体分发。运行中的工作进程不会从远端拉取新策略，也不存在运行中切换开关。

### 5.2 默认资源预算

| 预算 | 默认值 |
|---|---:|
| 单命名空间变体数 | 64 |
| 单命名空间代码字节数 | 64 MiB |
| 单进程命名空间数 | 32 |
| 单进程变体数 | 256 |
| 单进程代码字节数 | 256 MiB |
| 编译并发数 | 1 |
| 待编译队列 | 32 |
| 单次编译超时 | 30 秒 |
| 负缓存条目数 | 1024 |
| 负缓存有效期 | 30 秒 |
| 熔断失败阈值 | 3 |
| 熔断复位时间 | 30 秒 |
| 空闲命名空间回收时间 | 5 分钟 |

策略只能使用封闭字段和规定范围。命名空间预算不得超过进程预算；未知提供器、未知预算或向量/Arrow/RFC-009～RFC-012 开关会被拒绝。

## 6. 灰度流程

### 6.1 `off`

1. 安装候选 Wheel 和启动引导。
2. 以 `UDFJIT_MODE=off` 启动新作业。
3. 验证结果、异常、行序和 UDF 选项与未安装插件时一致。
4. 确认没有捕获、编译、变体或优化执行事件。

### 6.2 `observe`

1. 保持 `rollout_authorized=false`。
2. 以 `UDFJIT_MODE=observe` 启动新作业。
3. 检查候选发现、捕获、制品校验和解释信息。
4. 只有策略同时设置 `observe_shadow_compile=true` 且作业明确请求时，才允许影子编译。
5. 无论是否影子编译，都不得执行优化代码。

### 6.3 受控 `auto`

进入 `auto` 前必须具备：

- 命名首发作业和稳定作业指纹；
- 目标集群和业务负责人；
- 同环境 `off` 正确性基线；
- 标量支持矩阵命中证明；
- 观察/影子编译证据；
- 变更授权和部署回滚方案。

冻结策略必须显式设置 `rollout_authorized=true`。未授权的 `auto` 请求会收紧为 `observe`，原因码为 `rollout_not_authorized`。

## 7. 回滚

本期没有紧急停用通道，回滚按新作业和部署边界执行：

1. 停止提交新的 `auto` 作业。
2. 让已进入执行的区域自然完成；不要中断、重放或在区域中途替换策略。
3. 新作业使用 `UDFJIT_MODE=off`，或设置本地 `UDFJIT_DISABLE=1`。
4. 如需部署回滚，重新部署已经批准的镜像和 Wheel，并为其建立新的候选清单。
5. 清理旧工作进程后验证不存在旧进程代际、旧变体和旧制品句柄。
6. 重跑 `off` 正确性检查、兼容性检查和清理检查。

当前制品格式 1.0 是首个正式格式。回滚不会读取穿刺期制品，也不会尝试解释未知、未来或其他版本的制品。

## 8. 诊断命令

所有输入文件必须是当前用户拥有的普通文件、权限不超过 `0600`、大小不超过 16 MiB；符号链接会被拒绝。

```bash
udfjitctl compatibility --manifest /absolute/manifest.json
udfjitctl artifact verify /absolute/artifact.bin
udfjitctl artifact inspect /absolute/artifact.bin
udfjitctl explain /absolute/explain.json
udfjitctl benchmark mainline --config /absolute/profile.json
```

命令输出使用封闭的 JSON 结构和稳定原因码。错误输出不包含本地绝对路径、异常类名或异常消息。

解释信息按以下阶段串联：

```text
适配器 → 捕获 → 语义 IR → 制品 → 物理化 → 变体 → 执行
```

每条记录只包含运行批次、作业、租户、策略哈希、源码身份摘要、制品摘要、变体摘要、有限决策和有限原因码，不包含业务值。

## 9. blue-98 预发布验收

正式运行器入口：

```bash
PYTHONPATH=src:. python3 tests/system/run_blue98_acceptance.py \
  --acceptance-profile mainline-production \
  --repository /absolute/source \
  --run-root /absolute/run-root \
  --unit-completion-status incomplete \
  [锁定的 CinderX、Daft、Ray、PyArrow 和 setuptools Wheel 参数]
```

运行器必须在隔离目录和 `tmux` 中执行。它负责：

- 构建绑定提交和依赖摘要的候选镜像；
- 创建一个 Head/Driver 和两个 Worker 容器；
- 验证 Head/Driver 不承接数据面；
- 验证两个 Worker 的准备状态、生产资格和自然执行；
- 执行候选镜像内单元测试、集成测试和实时系统测试；
- 验证重启失效、鉴权、隐私、结果等价和方向性测量；
- 删除运行范围内的容器、网络、令牌和原始事件；
- 比较运行前后的路由、firewalld 运行时/永久配置和 nftables 状态。

`--allow-runtime-firewalld-trusted` 只允许运行器把新建 Docker bridge 临时绑定到 trusted 区域。绑定必须由同一运行批次删除，禁止手工改路由或保留防火墙规则。

## 10. 通过判据

预发布验收至少要求：

- 候选源码和镜像身份一致；
- 单元、集成、实时系统测试计数与配置完全一致且零跳过；
- RFC-001～RFC-008 的单元、集成和系统契约全部通过；
- 两个 Worker 资格为 2/2，并自然观察到两个 Worker 都实际执行；
- Head/Driver 数据面事件为 0；
- `off/observe/auto` 的结果、异常和行序契约成立；
- 工作节点重启使旧证据失效，新进程重新建立证据；
- 临时资源、令牌和原始事件全部清理；
- 路由和防火墙状态与运行前完全一致。

生产发布还必须在真实三物理节点环境执行跨机 `ObjectRef`、节点加入/退出/重启、任务重试、CPU/ABI 隔离、可信私网、端口访问控制和节点身份门禁。在该外部证据完成前，`release_ready` 必须保持 `false`。
