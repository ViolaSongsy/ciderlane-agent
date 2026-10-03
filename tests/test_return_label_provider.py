import os
import unittest
from unittest.mock import call, patch

import requests

from src.return_label_provider import ReturnLabelProvider, ReturnLabelProviderError, get_return_label_provider


class TestReturnLabelProvider(unittest.TestCase):
    def setUp(self):
        self.provider = ReturnLabelProvider('https://carrier.example.test/labels', 'test-token', 1.0)
        post_patch = patch('src.return_label_provider.requests.post')
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        sleep_patch = patch('src.return_label_provider.sleep')
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
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

    def test_forwards_caller_supplied_order_item_and_key(self):
        self.provider.create_label(
            order_id='CL2007', item_id='it00888', idempotency_key='caller-key-2007',
        )
        self.post.assert_called_once()
        request = self.post.call_args.kwargs
        self.assertEqual(request['json']['order_id'], 'CL2007')
        self.assertEqual(request['json']['item_id'], 'it00888')
        self.assertEqual(request['headers']['Idempotency-Key'], 'caller-key-2007')

    def test_malformed_success_is_rejected(self):
        for body in (b'not json', b'', b'[]', b'{}', b'{"label_url": null}',
                     b'{"label_url": "relative/path"}', b'{"label_url": "javascript:bad"}',
                     b'{"label_url": "https://carrier.example.test/\\u0000"}'):
            with self.subTest(body=body):
                self.post.reset_mock()
                self.response._content = body
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_invalid_response$'):
                    self.call_provider()
                self.post.assert_called_once()
                self.sleep.assert_not_called()

    def test_http_failures_are_not_success_and_expose_no_provider_body(self):
        for status in (302, 400, 401, 403, 409, 429):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.response.status_code = status
                self.response._content = b'private provider details'
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_http_error$'):
                    self.call_provider()
                self.post.assert_called_once()
                self.sleep.assert_not_called()

    def test_timeout_is_reported_without_retry(self):
        self.provider = ReturnLabelProvider(self.provider.url, 'test-token', 1.0, max_attempts=1)
        self.post.side_effect = requests.Timeout('private connection details')
        with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_timeout$'):
            self.call_provider()
        self.post.assert_called_once()
        self.sleep.assert_not_called()

    def assert_retry_identity(self, attempts):
        self.assertEqual(self.post.call_count, attempts)
        # Other HTTP details may change; the operation's identity must stay fixed.
        for request in self.post.call_args_list:
            self.assertEqual(request.kwargs['headers']['Idempotency-Key'], 'saved-key')
            self.assertEqual(request.kwargs['json']['order_id'], 'CL1002')
            self.assertEqual(request.kwargs['json']['item_id'], 'it00004')

    def test_timeout_and_connection_failures_exhaust_retries_without_success(self):
        for error, code in ((requests.Timeout('private details'), 'label_provider_timeout'),
                            (requests.ConnectionError('private details'), 'label_provider_unavailable')):
            with self.subTest(code=code):
                self.post.reset_mock()
                self.sleep.reset_mock()
                self.post.side_effect = error
                with self.assertRaisesRegex(ReturnLabelProviderError, f'^{code}$'):
                    self.call_provider()
                self.assert_retry_identity(3)
                self.assertEqual(self.sleep.call_args_list, [call(0.1), call(0.2)])

    def test_5xx_exhausts_retries_without_success(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.sleep.reset_mock()
                self.response.status_code = status
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_unavailable$'):
                    self.call_provider()
                self.assert_retry_identity(3)
                self.assertEqual(self.sleep.call_args_list, [call(0.1), call(0.2)])

    def test_transient_failure_then_success_preserves_identity(self):
        unavailable = requests.Response()
        unavailable.status_code = 503
        unavailable._content = b'private provider details'
        unavailable._content_consumed = True
        for failure in (requests.Timeout(), requests.ConnectionError(), unavailable):
            with self.subTest(failure=type(failure).__name__):
                self.post.reset_mock()
                self.sleep.reset_mock()
                self.post.side_effect = [failure, self.response]
                self.assertEqual(self.call_provider(), 'https://carrier.example.test/label.pdf')
                self.assert_retry_identity(2)
                self.sleep.assert_called_once_with(0.1)

    def test_invalid_attempt_limits_fail_before_http(self):
        for attempts in (0, 6, 1.5):
            with self.subTest(attempts=attempts):
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_not_configured$'):
                    ReturnLabelProvider(self.provider.url, 'test-token', max_attempts=attempts)
        self.post.assert_not_called()

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

    def test_provider_reads_application_environment(self):
        settings = {'CIDERLANE_LABEL_PROVIDER_URL': 'https://configured.example.test/labels',
                    'CIDERLANE_LABEL_PROVIDER_TOKEN': 'configured-token',
                    'CIDERLANE_LABEL_TIMEOUT_SECONDS': '2.5', 'CIDERLANE_LABEL_MAX_ATTEMPTS': '2'}
        with patch.dict(os.environ, settings, clear=True):
            provider = get_return_label_provider()
            self.assertEqual(provider.max_attempts, 2)
            provider.create_label(
                order_id='CL2007', item_id='it00888', idempotency_key='persisted-key',
            )
        request = self.post.call_args
        self.assertEqual(request.args[0], settings['CIDERLANE_LABEL_PROVIDER_URL'])
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer configured-token')
        self.assertEqual(request.kwargs['timeout'], 2.5)

    def test_missing_or_malformed_environment_fails_before_http(self):
        valid = {'CIDERLANE_LABEL_PROVIDER_URL': self.provider.url,
                 'CIDERLANE_LABEL_PROVIDER_TOKEN': 'configured-token'}
        for settings in ({}, {**valid, 'CIDERLANE_LABEL_TIMEOUT_SECONDS': 'invalid'},
                         {**valid, 'CIDERLANE_LABEL_MAX_ATTEMPTS': 'invalid'}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_not_configured$'):
                    get_return_label_provider()
        self.post.assert_not_called()
