from __future__ import annotations

import unittest
import urllib.request
from unittest import mock

from tests.system.loopback_http import (
    LoopbackRedirectError,
    _RejectRedirects,
    loopback_urlopen,
    require_numeric_loopback,
)


class LoopbackHttpTests(unittest.TestCase):
    def test_only_numeric_loopback_hosts_are_accepted(self) -> None:
        for url in (
            "http://127.0.0.1:8265/api/jobs",
            "https://[::1]:8265/api/jobs",
        ):
            with self.subTest(url=url):
                require_numeric_loopback(url)

        for url in (
            "http://localhost:8265/api/jobs",
            "http://192.168.41.98:8265/api/jobs",
            "ftp://127.0.0.1/resource",
            "http://user:secret@127.0.0.1:8265/api/jobs",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_numeric_loopback(url)

    def test_request_disables_proxies_and_installs_redirect_rejection(self) -> None:
        response = object()
        opener = mock.Mock()
        opener.open.return_value = response
        request = urllib.request.Request("http://127.0.0.1:8265/api/jobs")

        with mock.patch(
            "tests.system.loopback_http.urllib.request.build_opener",
            return_value=opener,
        ) as build_opener:
            actual = loopback_urlopen(request, timeout=7.5)

        self.assertIs(actual, response)
        handlers = build_opener.call_args.args
        proxy = next(
            handler
            for handler in handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        )
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(
            any(isinstance(handler, _RejectRedirects) for handler in handlers)
        )
        opener.open.assert_called_once_with(request, timeout=7.5)

    def test_redirects_are_rejected_before_following_location(self) -> None:
        handler = _RejectRedirects()
        request = urllib.request.Request("http://127.0.0.1:8265/api/jobs")

        with self.assertRaisesRegex(LoopbackRedirectError, "redirect_refused:302"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1:9000/capture",
            )


if __name__ == "__main__":
    unittest.main()
