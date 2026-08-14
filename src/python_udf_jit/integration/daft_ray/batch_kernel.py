from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import re
from types import FunctionType
from typing import Any, Callable, Protocol, runtime_checkable


_BATCH_KERNEL_ATTRIBUTE = "__python_udf_jit_batch_kernel__"
_VALIDATED_REGEX_SUBSTITUTIONS = frozenset(
    {
        (r"https?://\S+|www\.\S+", True, ""),
        (r"[\w.+-]+@[\w.-]+\.\w+", False, ""),
        (
            r"(?i)(copyright\s*\(?c\)?|©|\(c\)|all rights reserved)"
            r"[^\n.]*\.?",
            True,
            "",
        ),
    }
)


@runtime_checkable
class BatchKernel(Protocol):
    kind: str
    fallback_on_error: bool

    def invoke(self, values: list[Any]) -> list[Any]: ...


@dataclass(frozen=True)
class CallableBatchKernel:
    """Explicit, serializable batch implementation supplied by an integration."""

    kind: str
    callable: Callable[[list[Any]], list[Any]]
    fallback_on_error: bool = False

    def invoke(self, values: list[Any]) -> list[Any]:
        output = self.callable(values)
        return output if type(output) is list else list(output)


@dataclass(frozen=True)
class RegexSubBatchKernel:
    """Arrow regex substitution proven equivalent for a specific descriptor."""

    pattern: str
    replacement: str
    ignore_case: bool
    kind: str = "arrow_regex_sub"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        pattern = self.pattern
        if self.ignore_case and not pattern.startswith("(?i)"):
            pattern = "(?i)" + pattern
        output = pc.replace_substring_regex(
            pa.array(values),
            pattern,
            self.replacement,
        )
        return output.to_pylist()


@dataclass(frozen=True)
class NormalizeBatchKernel:
    """Arrow Unicode normalization (NFC/NFKC/NFD/NFKD) for scalar normalize()."""

    form: str
    kind: str = "arrow_utf8_normalize"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        return pc.utf8_normalize(pa.array(values), self.form).to_pylist()


@dataclass(frozen=True)
class TranslateBatchKernel:
    """Arrow replace_all for a str.maketrans mapping (one replace per pair)."""

    mapping: tuple[tuple[str, str], ...]
    kind: str = "arrow_replace_all"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        column = pa.array(values)
        for source, target in self.mapping:
            column = pc.replace_substring(column, source, target)
        return column.to_pylist()


@dataclass(frozen=True)
class LengthFilterBatchKernel:
    """Arrow utf8_length + range predicate for a min <= len(s) <= max filter."""

    min_length: int
    max_length: int
    kind: str = "arrow_utf8_length_range"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        lengths = pc.utf8_length(pa.array(values))
        keep = pc.and_(
            pc.greater_equal(lengths, self.min_length),
            pc.less_equal(lengths, self.max_length),
        )
        return keep.to_pylist()


@dataclass(frozen=True)
class WhitespaceBatchKernel:
    r"""Arrow 空白标准化：`\s+` → 单空格 + 首尾 trim（对应 sub+strip 复合形态）。

    对应 `_WS_RE.sub(" ", s).strip()`：先整批 replace_substring_regex(\s+ → " "),
    再 utf8_trim_whitespace 收尾——两段 Arrow 批计算替代 Python 逐行 sub+strip。
    """

    pattern: str
    kind: str = "arrow_whitespace_normalize"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        column = pc.replace_substring_regex(
            pa.array(values),
            self.pattern,
            " ",
        )
        return pc.utf8_trim_whitespace(column).to_pylist()


def register_batch_kernel(
    function: Callable[..., Any],
    batch_callable: Callable[[list[Any]], list[Any]],
    *,
    kind: str,
) -> Callable[..., Any]:
    """Attach an explicit batch contract without wrapping the scalar callable."""

    if not callable(function) or not callable(batch_callable):
        raise TypeError("batch kernel registration requires callables")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("batch kernel kind must not be empty")
    setattr(
        function,
        _BATCH_KERNEL_ATTRIBUTE,
        CallableBatchKernel(kind.strip(), batch_callable),
    )
    return function


def _callable_graph(function: Callable[..., Any]) -> tuple[Callable[..., Any], ...]:
    pending = [function]
    found: list[Callable[..., Any]] = []
    visited: set[int] = set()
    while pending and len(visited) < 64:
        current = pending.pop()
        if not callable(current) or id(current) in visited:
            continue
        visited.add(id(current))
        found.append(current)
        if inspect.isfunction(current):
            pending.extend(
                cell.cell_contents
                for cell in (current.__closure__ or ())
                if callable(cell.cell_contents)
            )
            pending.extend(
                value for value in (current.__defaults__ or ()) if callable(value)
            )
            pending.extend(
                value
                for value in (current.__kwdefaults__ or {}).values()
                if callable(value)
            )
    return tuple(found)


def _explicit_batch_kernel(
    function: Callable[..., Any],
) -> BatchKernel | None:
    matches = []
    for candidate in _callable_graph(function):
        kernel = getattr(candidate, _BATCH_KERNEL_ATTRIBUTE, None)
        if isinstance(kernel, BatchKernel):
            matches.append(kernel)
    if not matches:
        return None
    kinds = {kernel.kind for kernel in matches}
    if len(kinds) != 1:
        return None
    return matches[0]


def _transparent_leaf(function: Callable[..., Any]) -> FunctionType | None:
    current = function
    visited: set[int] = set()
    for _ in range(8):
        if not inspect.isfunction(current) or id(current) in visited:
            return None
        visited.add(id(current))
        closure_functions = [
            cell.cell_contents
            for cell in (current.__closure__ or ())
            if inspect.isfunction(cell.cell_contents)
        ]
        if not closure_functions:
            return current
        if len(closure_functions) != 1:
            return None
        current = closure_functions[0]
    return None


def _regex_sub_descriptor(
    function: FunctionType,
) -> tuple[str, bool, str] | None:
    """识别 `pattern.sub(repl, s)` 形态：单参数、co_names=[pat, sub]、globals 有 re.Pattern。"""
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if len(code.co_names) != 2 or code.co_names[1] != "sub":
        return None
    pattern = function.__globals__.get(code.co_names[0])
    if not isinstance(pattern, re.Pattern):
        return None
    replacements = [
        value
        for value in code.co_consts
        if type(value) is str and value != function.__doc__
    ]
    if len(replacements) != 1:
        return None
    return (
        pattern.pattern,
        bool(pattern.flags & re.IGNORECASE),
        replacements[0],
    )


def _normalize_descriptor(
    function: FunctionType,
) -> str | None:
    """识别 `unicodedata.normalize(FORM, s)` 形态（NFC/NFKC/NFD/NFKD）。"""
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if len(code.co_names) != 2 or code.co_names[1] != "normalize":
        return None
    module = function.__globals__.get(code.co_names[0])
    if module is not None and getattr(module, "__name__", "") != "unicodedata":
        return None
    forms = [value for value in code.co_consts if value in ("NFC", "NFKC", "NFD", "NFKD")]
    if len(forms) != 1:
        return None
    return forms[0]


def _translate_descriptor(
    function: FunctionType,
) -> tuple[tuple[str, str], ...] | None:
    """识别 `str.maketrans({...})` + `s.translate(trans)` 形态。

    从指令流提取 BUILD_MAP 前的成对 LOAD_CONST（source -> target）。
    不能依赖 co_consts 成对：dict 字面量中相同的 target（如 `"`）会被
    常量去重合并为单一对象，co_consts 中不再相邻。
    """
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if "maketrans" not in code.co_names or "translate" not in code.co_names:
        return None

    import dis

    pairs: list[tuple[str, str]] = []
    pending_key: Any = None
    for instr in dis.get_instructions(function):
        opname = instr.opname
        if opname == "BUILD_MAP":
            break
        if opname == "LOAD_CONST":
            if pending_key is None:
                pending_key = instr.argval
            elif isinstance(pending_key, str) and isinstance(instr.argval, str):
                pairs.append((pending_key, instr.argval))
                pending_key = None
            else:
                pending_key = None
        elif opname not in ("RESUME", "PUSH_NULL", "CACHE", "NOP"):
            pending_key = None
    if not pairs:
        return None
    return tuple(pairs)


def _length_filter_descriptor(
    function: FunctionType,
) -> tuple[int, int] | None:
    """识别长度区间过滤形态：工厂闭包捕获 2 个 int（min/max），函数体调用单个长度谓词。

    对应 `make_text_length_filter(min_len, max_len)` 返回的 `_filter(s)`：
    co_names 恰好 1 个（调用谓词函数）、闭包恰好 2 个 int cell。
    """
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if len(code.co_names) != 1:
        return None
    closure = function.__closure__ or ()
    int_cells = [cell.cell_contents for cell in closure if isinstance(cell.cell_contents, int)]
    if len(int_cells) != 2:
        return None
    low, high = min(int_cells), max(int_cells)
    return (low, high)


def _whitespace_descriptor(function: FunctionType) -> str | None:
    r"""识别空白标准化复合形态：`_WS_RE.sub(" ", s).strip()`。

    字节码特征：单参数、co_names 恰好 (`pattern名`, "sub", "strip")、
    globals 的 pattern 是 `\s+` 的 re.Pattern、co_consts 恰好 1 个字符串 " "（替换串）。
    对应 `dj_whitespace_normalization`（连续 \\s+ → 单空格 + 首尾 trim）。
    """
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if len(code.co_names) != 3 or code.co_names[1:] != ("sub", "strip"):
        return None
    pattern = function.__globals__.get(code.co_names[0])
    if not isinstance(pattern, re.Pattern):
        return None
    if pattern.pattern != r"\s+":
        return None
    replacements = [
        value
        for value in code.co_consts
        if type(value) is str and value != function.__doc__
    ]
    if len(replacements) != 1 or replacements[0] != " ":
        return None
    return pattern.pattern


def _build_regex_kernel(descriptor: tuple[str, bool, str]) -> BatchKernel:
    pattern, ignore_case, replacement = descriptor
    return RegexSubBatchKernel(pattern, replacement, ignore_case)


# ---------------------------------------------------------------------------
# 成本门禁（静态成本表）：识别到 Kernel 后查表，Arrow 批实现比 Python 标量
# 更快（speedup >= 阈值）才替换；更慢/未维护成本数据的形态一律回退原始行式。
#
# speedup 数据来自 10k 单算子 microbench（microbench_fineweb_arrow_kernels.py）：
#   speedup = 标量耗时 / Arrow 批耗时；<1 表示 Arrow 更慢（HTML 0.223x、NFKC 0.385x）。
# 指纹 key 与 `_kernel_fingerprint` 一致（regex 用 pattern:ignore_case:replacement）。
# ---------------------------------------------------------------------------

_COST_GATE_MIN_SPEEDUP = 1.05

# 静态成本表：kernel 指纹 -> Arrow/Scalar 速度比（来自 10k microbench 实测）
_COST_SPEEDUP_TABLE: dict[str, float] = {
    "regex:https?://\\S+|www\\.\\S+:True:": 5.863,  # clean_links -> 替换
    "regex:[\\w.+-]+@[\\w.-]+\\.\\w+:False:": 10.276,  # clean_email -> 替换
    "regex:(?i)(copyright\\s*\\(?c\\)?|©|\\(c\\)|all rights reserved)[^\\n.]*\\.?:True:": 7.753,  # clean_copyright -> 替换
    "regex:<[^>]+>:False:": 0.223,  # clean_html -> Arrow 更慢，回退
    "normalize:NFKC": 0.385,  # fix_unicode -> Arrow 更慢，回退
    # punctuation（maketrans+translate）：50k 实测 5.285x（54 微测）-> 替换
    "translate:"
    + repr(
        (
            ("\u201c", '"'),
            ("\u201d", '"'),
            ("\u2018", "'"),
            ("\u2019", "'"),
            ("\u2013", "-"),
            ("\u2014", "-"),
        )
    ): 5.285,
    # whitespace（\s+ → 单空格 + trim 复合）：50k 实测 1.530x（54 微测）-> 替换
    r"whitespace:\s+": 1.530,
}


def _kernel_fingerprint(kernel: BatchKernel) -> str:
    """Kernel 稳定指纹（kind + 参数），用于成本表查找。"""
    if isinstance(kernel, RegexSubBatchKernel):
        return f"regex:{kernel.pattern}:{kernel.ignore_case}:{kernel.replacement}"
    if isinstance(kernel, NormalizeBatchKernel):
        return f"normalize:{kernel.form}"
    if isinstance(kernel, TranslateBatchKernel):
        return f"translate:{kernel.mapping}"
    if isinstance(kernel, LengthFilterBatchKernel):
        return f"length:{kernel.min_length}:{kernel.max_length}"
    if isinstance(kernel, WhitespaceBatchKernel):
        return f"whitespace:{kernel.pattern}"
    return f"callable:{kernel.kind}"


def _cost_gate_passes(
    kernel: BatchKernel,
    original_callable: Callable[..., Any] | None = None,
) -> bool:
    """查静态成本表：Arrow 批实现更快（speedup >= 阈值）才允许替换。

    成本表中无该形态数据时默认回退（不替换），避免未经验证的劣化。
    """
    fingerprint = _kernel_fingerprint(kernel)
    speedup = _COST_SPEEDUP_TABLE.get(fingerprint)
    if speedup is None:
        return False
    return speedup >= _COST_GATE_MIN_SPEEDUP


def build_batch_kernel(
    original_callable: Callable[..., Any],
) -> BatchKernel | None:
    """按函数字节码形态自动识别通用可批化操作（形态映射族）。

    不依赖算子名、不依赖显式注册、不依赖白名单；任何匹配通用形态的
    函数（regex-sub / unicode-normalize / maketrans-translate / 长度区间）
    都自动获得对应的 Arrow 批 Kernel。

    识别到 Kernel 后再过成本门禁（UDFJIT_COST_SAMPLE_PATH 样本微测）：
    若 Arrow 批实现不比 Python 标量更快（speedup < 1.05），拒绝该 Kernel
    返回 None —— 无 Kernel 即回退原始行式，不做批输入/批处理替换。
    """
    leaf = _transparent_leaf(original_callable)
    if leaf is None:
        return None
    regex = _regex_sub_descriptor(leaf)
    if regex is not None:
        kernel = _build_regex_kernel(regex)
        return kernel if _cost_gate_passes(kernel, original_callable) else None
    form = _normalize_descriptor(leaf)
    if form is not None:
        kernel = NormalizeBatchKernel(form)
        return kernel if _cost_gate_passes(kernel, original_callable) else None
    mapping = _translate_descriptor(leaf)
    if mapping is not None:
        kernel = TranslateBatchKernel(mapping)
        return kernel if _cost_gate_passes(kernel, original_callable) else None
    bounds = _length_filter_descriptor(leaf)
    if bounds is not None:
        low, high = bounds
        kernel = LengthFilterBatchKernel(low, high)
        return kernel if _cost_gate_passes(kernel, original_callable) else None
    whitespace = _whitespace_descriptor(leaf)
    if whitespace is not None:
        kernel = WhitespaceBatchKernel(whitespace)
        return kernel if _cost_gate_passes(kernel, original_callable) else None
    return None
