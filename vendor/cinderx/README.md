# CinderX 候选源码来源

标量主线候选镜像使用基于上游提交
`ac09c68527153b43cc8b4f16f36d9245cb861d12` 的 openEuler CinderX
源码快照，并按 `patches/manifest.json` 中的顺序应用确定性补丁系列。

第一个补丁包含 UDF 内建指令依赖的平台修复和基础运行时接缝；第二个补丁实现
五种物理标量类型、空值语义、分支执行及相应的 HIR/LIR 和测试；第三个补丁
实现续体载荷构造、区域代码对象内的原生去优化范围校验，以及活跃值类型、
物化状态、源码身份和提交状态的拒绝矩阵。跨代码对象恢复始终返回 Python
`InterpreterContinuation`，提交后不允许整函数重放。
第四个补丁将普通代码分配器和大页代码分配器统一改为 RX/RW 双映射，代码
执行只使用 RX 别名，生成和热修补只使用 RW 别名；无法解析到独立写别名的
热修补地址会被直接拒绝。fork 后的子进程不会继续写入父进程继承的共享
分配块，新生成代码会切换到当前进程自己的分配代际。候选容器同时通过
seccomp 拒绝请求 W+X 权限的 `mmap`、`mprotect` 和 `pkey_mprotect`，因此
第三方库也不能重新引入可写且可执行的映射。

构建输出、虚拟环境、IDE 状态、仅用于 CI 的文件、生成的 egg 元数据和文档
不参与运行时源码树身份计算；具体排除项、续体 ABI 及应用补丁前后的源码树
摘要均记录在清单中。

从匹配的 CinderX 基线源码根目录按顺序应用补丁：

```text
patch --batch -p1 < 0001-runtime-candidate.patch
patch --batch -p1 < 0002-primitive-data-intrinsics.patch
patch --batch -p1 < 0003-continuation-deopt.patch
patch --batch -p1 < 0004-wx-dual-mapping.patch
```

正式验收逐个校验补丁 SHA-256，并按清单顺序拼接补丁原始字节后计算补丁系列
SHA-256。规范化源码树摘要、补丁系列摘要会同时写入候选镜像标签和 CinderX
测试证据；任何源码树、补丁顺序或补丁内容不一致的 wheel 和镜像都会被拒绝。
