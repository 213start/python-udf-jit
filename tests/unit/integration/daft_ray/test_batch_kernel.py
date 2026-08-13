from __future__ import annotations

import importlib.util
import re
import unittest

from python_udf_jit.integration.daft_ray.batch_kernel import (
    RegexSubBatchKernel,
    build_batch_kernel,
)


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_COPYRIGHT_RE = re.compile(
    r"(?i)(copyright\s*\(?c\)?|©|\(c\)|all rights reserved)[^\n.]*\.?"
)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_url(value):
    return _URL_RE.sub("", value)


def clean_html(value):
    return _HTML_RE.sub(" ", value)


def clean_email(value):
    return _EMAIL_RE.sub("", value)


def clean_copyright(value):
    return _COPYRIGHT_RE.sub("", value)


def normalize_whitespace(value):
    return _WS_RE.sub(" ", value).strip()


def transparent_wrapper(function):
    def udf(value):
        return function(value)

    return udf


class BatchKernelTest(unittest.TestCase):
    def test_builds_validated_regex_through_transparent_closure_chain(self):
        wrapped = transparent_wrapper(transparent_wrapper(clean_url))

        kernel = build_batch_kernel(wrapped)

        self.assertEqual(
            kernel,
            RegexSubBatchKernel(_URL_RE.pattern, "", True),
        )

    def test_rejects_unvalidated_regex_descriptor(self):
        self.assertIsNone(build_batch_kernel(clean_html))

    def test_builds_all_validated_regex_descriptors(self):
        for function in (clean_url, clean_email, clean_copyright):
            with self.subTest(function=function.__name__):
                self.assertIsNotNone(build_batch_kernel(function))

    def test_rejects_regex_plus_additional_scalar_operation(self):
        self.assertIsNone(build_batch_kernel(normalize_whitespace))

    @unittest.skipUnless(
        importlib.util.find_spec("pyarrow") is not None,
        "pyarrow not installed",
    )
    def test_arrow_kernel_preserves_null_and_regex_semantics(self):
        kernel = RegexSubBatchKernel(_URL_RE.pattern, "", True)

        self.assertEqual(
            kernel.invoke(["A HTTP://EXAMPLE.COM B", None, "plain"]),
            ["A  B", None, "plain"],
        )


if __name__ == "__main__":
    unittest.main()
