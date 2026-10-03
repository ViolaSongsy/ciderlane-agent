import unittest
from unittest.mock import patch

import requests

from src.return_label_provider import ReturnLabelProvider, ReturnLabelProviderError


class TestReturnLabelProvider(unittest.TestCase):
    def setUp(self):
        self.provider = ReturnLabelProvider('https://carrier.example.test/labels', 'test-token', 1.0)
        post_patch = patch('src.return_label_provider.requests.post')
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        # Exercise real JSON parsing without making a network call.
        self.response = requests.Response()
        self.response.status_code = 201
        self.response._content = b'{"label_url": "https://carrier.example.test/label.pdf"}'
        self.response._content_consumed = True  # The fake body is already downloaded.
        self.post.return_value = self.response

    def call_provider(self):
        return self.provider.create_label(order_id='CL1002', item_id='it00004', idempotency_key='saved-key')

    def test_success_posts_trusted_ids_with_application_credentials_and_key(self):
        self.assertEqual(self.call_provider(), 'https://carrier.example.test/label.pdf')
        self.post.assert_called_once_with(
            self.provider.url,
            json={'order_id': 'CL1002', 'item_id': 'it00004'},
            headers={'Authorization': 'Bearer test-token', 'Idempotency-Key': 'saved-key',
                     'Accept': 'application/json'},
            timeout=1.0,
            allow_redirects=False,
        )

    def test_malformed_success_is_rejected(self):
        for body in (b'not json', b'', b'[]', b'{}', b'{"label_url": null}',
                     b'{"label_url": "relative/path"}', b'{"label_url": "javascript:bad"}',
                     b'{"label_url": "https://carrier.example.test/\\u0000"}'):
            with self.subTest(body=body):
                self.response._content = body
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_invalid_response$'):
                    self.call_provider()

    def test_http_failures_are_not_success_and_expose_no_provider_body(self):
        for status in (302, 401, 500):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.response.status_code = status
                self.response._content = b'private provider details'
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_http_error$'):
                    self.call_provider()
                self.post.assert_called_once()

    def test_timeout_is_reported_without_retry(self):
        self.post.side_effect = requests.Timeout('private connection details')
        with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_unavailable$'):
            self.call_provider()
        self.post.assert_called_once()

    def test_invalid_application_configuration_fails_before_http(self):
        for url, token, timeout in (
            ('', 'token', 1), ('https://carrier.example.test', '', 1),
            ('https://carrier.example.test', 'bad\r\nheader', 1),
            ('https://carrier.example.test', 'token', 0),
        ):
            with self.subTest(url=url, timeout=timeout):
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_not_configured$'):
                    ReturnLabelProvider(url, token, timeout)
        self.post.assert_not_called()
