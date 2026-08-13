from __future__ import annotations

import pickle
import re
import unittest
import unicodedata

from python_udf_jit.integration.daft_ray.batch_kernel import (
    CallableBatchKernel,
    LengthFilterBatchKernel,
    NormalizeBatchKernel,
    RegexSubBatchKernel,
    TranslateBatchKernel,
    build_batch_kernel,
    register_batch_kernel,
)
from python_udf_jit.integration.daft_ray.carrier import ProductionCarrierState
from python_udf_jit.integration.daft_ray.wrapper import (
    BatchExecutionWrapper,
    FallbackOnlyWrapper,
)


class FakeSeries:
    def __init__(self, values):
        self.values = values

    def to_pylist(self):
        return list(self.values)


def scalar(value: int) -> int:
    return value * 2


def batch(values: list[int]) -> list[int]:
    return [value * 2 for value in values]


def bad_length(_values: list[int]) -> list[int]:
    return []


# ---- 形态映射族：模块级定义，与 volc_operator_sim 真实业务同构 ----

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")


def clean_url(value):
    return _URL_RE.sub("", value)


def clean_html(value):
    return _HTML_RE.sub(" ", value)


def fix_unicode(value):
    return unicodedata.normalize("NFKC", value)


def punctuation_normalize(value):
    trans = str.maketrans(
        {
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
        }
    )
    return value.translate(trans)


def make_length_filter(min_len, max_len):
    def length_ok(value):
        n = len(value)
        return min_len <= n <= max_len

    return length_ok


class BatchKernelTest(unittest.TestCase):
    def make_scalar_wrapper(self, function=scalar):
        return FallbackOnlyWrapper(
            candidate_id="candidate-batch",
            original_callable=function,
            carrier=ProductionCarrierState.placeholder(
                "candidate-batch",
                "a" * 64,
            ),
        )

    def test_batch_wrapper_invokes_kernel_once_for_all_rows(self):
        calls = []

        def recording(values):
            calls.append(list(values))
            return [value * 2 for value in values]

        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_vectorized", recording),
        )

        self.assertEqual(wrapper(None, FakeSeries([1, 2, 3])), [2, 4, 6])
        self.assertEqual(calls, [[1, 2, 3]])

    def test_explicit_kernel_failure_is_not_silently_replayed_rowwise(self):
        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_bad", bad_length),
        )

        with self.assertRaisesRegex(ValueError, "output_length_mismatch"):
            wrapper(None, FakeSeries([1, 2, 3]))

    def test_batch_wrapper_and_explicit_kernel_are_pickle_stable(self):
        wrapper = BatchExecutionWrapper(
            "candidate-batch",
            self.make_scalar_wrapper(),
            CallableBatchKernel("test_vectorized", batch),
        )

        restored = pickle.loads(pickle.dumps(wrapper))

        self.assertEqual(restored(None, FakeSeries([4, 5])), [8, 10])
        self.assertEqual(restored.batch_kernel.kind, "test_vectorized")

    # ---- 形态映射族：按函数字节码形态自动识别 ----

    def test_regex_sub_shape_is_recognized_without_whitelist(self):
        url_kernel = build_batch_kernel(clean_url)
        html_kernel = build_batch_kernel(clean_html)

        self.assertIsInstance(url_kernel, RegexSubBatchKernel)
        self.assertEqual(url_kernel.kind, "arrow_regex_sub")
        # 非白名单 pattern（HTML）也按形态自动识别
        self.assertIsInstance(html_kernel, RegexSubBatchKernel)
        self.assertEqual(
            html_kernel.invoke(["<p>hi</p>", "no tags"]),
            [" hi ", "no tags"],
        )

    def test_unicode_normalize_shape_is_recognized(self):
        kernel = build_batch_kernel(fix_unicode)

        self.assertIsInstance(kernel, NormalizeBatchKernel)
        self.assertEqual(kernel.kind, "arrow_utf8_normalize")
        self.assertEqual(kernel.form, "NFKC")
        self.assertEqual(kernel.invoke(["ａｂｃ", "①②"]), ["abc", "12"])

    def test_maketrans_translate_shape_is_recognized(self):
        kernel = build_batch_kernel(punctuation_normalize)

        self.assertIsInstance(kernel, TranslateBatchKernel)
        self.assertEqual(kernel.kind, "arrow_replace_all")
        self.assertEqual(
            kernel.invoke(["“hello”", "‘x’"]),
            ['"hello"', "'x'"],
        )

    def test_length_filter_closure_shape_is_recognized(self):
        kernel = build_batch_kernel(make_length_filter(5, 10))

        self.assertIsInstance(kernel, LengthFilterBatchKernel)
        self.assertEqual(kernel.kind, "arrow_utf8_length_range")
        self.assertEqual(kernel.min_length, 5)
        self.assertEqual(kernel.max_length, 10)
        self.assertEqual(kernel.invoke(["abc", "12345", "12345678901"]), [False, True, False])

    def test_unmatched_shape_returns_none(self):
        def arithmetic(value: float) -> float:
            return value * 2.0 + 3.0

        self.assertIsNone(build_batch_kernel(arithmetic))


if __name__ == "__main__":
    unittest.main()
