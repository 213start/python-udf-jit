# CinderX 候选源码来源

标量主线候选镜像使用基于上游提交
`ac09c68527153b43cc8b4f16f36d9245cb861d12` 的 openEuler CinderX
源码快照，并按 `patches/manifest.json` 中的顺序应用确定性补丁系列。

第一个补丁包含 UDF 内建指令依赖的平台修复和基础运行时接缝，第二个补丁实现
五种物理标量类型、空值语义、分支执行及相应的 HIR/LIR 和测试。构建输出、
虚拟环境、IDE 状态、仅用于 CI 的文件、生成的 egg 元数据和文档不参与运行时
源码树身份计算；具体排除项及应用补丁前后的源码树摘要均记录在清单中。

从匹配的 CinderX 基线源码根目录按顺序应用补丁：

```text
patch --batch -p1 < 0001-runtime-candidate.patch
patch --batch -p1 < 0002-primitive-data-intrinsics.patch
```

正式验收逐个校验补丁 SHA-256，并按清单顺序拼接补丁原始字节后计算补丁系列
SHA-256。规范化源码树摘要、补丁系列摘要会同时写入候选镜像标签和 CinderX
测试证据；任何源码树、补丁顺序或补丁内容不一致的 wheel 和镜像都会被拒绝。
