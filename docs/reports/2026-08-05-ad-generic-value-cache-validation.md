# AD pipeline 通用 CinderX value-cache 验证

## 结论

在不重排算子、不修改 AD 算子逻辑、也不按 pipeline/operator/module/function 名称匹配的前提下，`pipeline_ad_nuscenes_min` 的独立 5M 冻结集正式 A/B 达到：

- pipeline execute：`234.087s → 177.045s`，`1.322×`，耗时下降 `24.37%`；
- 含 Ray 初始化总耗时：`242.876s → 185.578s`，`1.309×`；
- off/auto 均输出 5,000,000 行、10,000 个唯一结果，schema 与 multiset 完全一致。

完整机器可读证据见 [2026-08-05-ad-generic-value-cache-ab.json](evidence/2026-08-05-ad-generic-value-cache-ab.json)。该结果是一次锁定环境的串行点估计。

## 实现边界

UDF JIT 只负责输出行为、类型、依赖与薄包装绑定证明：

1. 输入和结果都是 exact Unicode；
2. 重复输入允许使用有容量上限的值复用；
3. 固定进程状态由 identity/type/function-code watcher 保护；
4. 动态外部状态由声明式 `decoder + key path + observer + snapshot path` 描述；
5. 未建模调用、可变全局状态、异常形状或签名一律拒绝准入。

CinderX HIR/runtime 才实施优化：在函数入口插入 lookup 分支，命中时校验固定依赖和每条 entry 的外部状态，成功后返回缓存的不可变字符串；未命中或守卫失败时执行原 HIR，并只在正常 exact-Unicode return 前更新有界表。异常不缓存，非默认调用形状走原 HIR。

Ray 集成层只修复了一个对象一致性问题：结构证明通过后，调用被路由到 CinderX 实际编译的同一个函数对象，并持续检查薄包装的 live binding。它不执行缓存或业务计算。

## 诊断证据

第一版虽然报告编译成功，但独立运行时计数显示真实 Ray 路径的 lookup 为 0：包装链最终调用的对象不是 CinderX 编译的对象。对象一致性修复后，500K 诊断运行出现 499,990 次 lookup、489,968 次 hit、10,000 次 insert，且没有依赖失效。

只覆盖第一个 align mapper 时，500K execute 仅为 `26.013s → 24.941s`，约 `1.043×`。第二个 index mapper 包含 `json.loads/dumps` 和逐行文件状态读取，仍是主要成本。新增通用 per-entry 外部状态守卫后，500K 校准提升到 `1.283×`，才允许进入 5M 正式轮。

诊断计数器只用于独立诊断二进制；正式候选中已删除该代码。所有 timing arm 均设置 `UDFJIT_DIAGNOSTICS=off` 与 `PYTHONJITUDFDIAGNOSTICS=0`。

## 冻结规模与验证

- blue-98 功能集：`nuscenes_cam_front_10k`，off/auto 按行顺序逐值相同；
- blue-53 校准集：`nuscenes_cam_front_500k_calibration`，500K 行、10K 唯一值；
- blue-53 正式集：`nuscenes_cam_front_5m`，5M 行、10K 唯一值。

AD 使用自己的冻结规模，没有复用 FineWeb 的 10K/200K 定义。并行分区会改变 Lance 行顺序，因此大规模门使用 schema、行数和 order-insensitive multiset 摘要；5M 两臂摘要均为 `7e818d03debdd633032186759372b496c05ba4cf34caada85beb4981baf13b87`。
