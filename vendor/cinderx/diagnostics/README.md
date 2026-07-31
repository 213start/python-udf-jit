# CinderX 结构化诊断 Overlay

本目录是正常候选 CinderX patch series 之后的诊断专用 overlay。它不加入
`vendor/cinderx/patches/manifest.json`，因此不会改变生产候选 wheel、源码树
身份或 `diagnostics=off` 的运行路径。只有 RFC-013 的 dedicated diagnostic
worker 镜像可以应用本 overlay。

构建诊断 wheel 时，先按 `../../patches/manifest.json` 应用正常候选补丁，
再按本目录 `manifest.json` 的顺序应用：

```text
patch --batch -p1 < patches/0001-structured-origin-export.patch
```

启动进程前设置 `PYTHONJITUDFDIAGNOSTICS=1`。该开关只在 JIT 初始化时读取；
关闭时不保留 HIR/LIR 节点和机器地址区间，也不暴露结构化查询结果。Full
诊断 worker 还应把编译器生成的 `code_hash` 绑定到目标函数属性
`__udfjit_generated_code_hash__`，然后调用：

```text
cinderx.jit.get_udfjit_compilation_diagnostics(
    function,
    compile_instance_id,
)
```

返回值只包含 opcode、generated-bytecode offset、HIR/LIR origin、机器地址
区间、代码/栈大小和阶段时长，不导出 IR operand、常量、对象 `repr` 或输入
样本。CinderX 返回的原始符号名只在进程内短暂存在；Python bridge 在写入
Bundle 前将其转换为 SHA-256。

源码级门禁：

```text
git apply --check \
  vendor/cinderx/diagnostics/patches/0001-structured-origin-export.patch
```

交付诊断镜像前还必须在匹配的候选 CinderX 源码树执行构建，并以
`PYTHONJITUDFDIAGNOSTICS=1` 运行
`test_cinderx.test_udf_jit_diagnostics`。普通候选验收不应用本 overlay。
