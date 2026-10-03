import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests

from evals.mock_label_provider import MockLabelHandler, mock_label_provider
from evals.run_evals import main
from src.context import ReturnContext
from src.return_label_provider import ReturnLabelProvider, ReturnLabelProviderError, get_return_label_provider
from src.storage import connect_database
from src.tools.return_tools import start_return


class TestEvalProviderConfiguration(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(MockLabelHandler, 'log_message'))

    def test_default_eval_entry_uses_mock_http_and_restores_configuration(self):
        original = {'CIDERLANE_LABEL_PROVIDER_URL': 'https://configured.example.test/labels',
                    'CIDERLANE_LABEL_PROVIDER_TOKEN': 'configured-token',
                    'CIDERLANE_LABEL_TIMEOUT_SECONDS': 'invalid',
                    'CIDERLANE_LABEL_MAX_ATTEMPTS': 'invalid'}

        def run_with_mock():
            provider = get_return_label_provider()
            self.assertTrue(provider.url.startswith('http://127.0.0.1:'))
            label = provider.create_label(order_id='CL2007', item_id='it00888',
                                          idempotency_key='eval-key')
            self.assertEqual(label, 'https://mock-labels.example.test/eval-key.pdf')
            return 0

        with patch.dict(os.environ, original), patch('evals.run_evals.load_dotenv'), \
             patch('evals.run_evals.run_suite', side_effect=run_with_mock), patch('builtins.print'):
            self.assertEqual(main([]), 0)
            self.assertEqual({key: os.environ[key] for key in original}, original)

    def test_configured_provider_mode_does_not_start_mock(self):
        configured = {'CIDERLANE_LABEL_PROVIDER_URL': 'https://configured.example.test/labels',
                      'CIDERLANE_LABEL_PROVIDER_TOKEN': 'configured-token'}

        def run_with_configuration():
            self.assertEqual({key: os.environ[key] for key in configured}, configured)
            return 0

        with patch.dict(os.environ, configured), patch('evals.run_evals.load_dotenv'), \
             patch('evals.run_evals.mock_label_provider') as mock_server, \
             patch('evals.run_evals.run_suite', side_effect=run_with_configuration) as suite, \
             patch('builtins.print'):
            self.assertEqual(main(['--use-configured-provider']), 0)
        mock_server.assert_not_called()
        suite.assert_called_once_with()


class TestMockLabelProvider(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(MockLabelHandler, 'log_message'))
        self.url = self.enterContext(mock_label_provider())

    def test_same_key_returns_same_label_and_bad_credentials_are_rejected(self):
        provider = ReturnLabelProvider(self.url, 'mock-token')
        request = {'order_id': 'CL2007', 'item_id': 'it00888', 'idempotency_key': 'saved-key'}
        label = provider.create_label(**request)
        self.assertEqual(provider.create_label(**request), label)
        self.assertNotEqual(provider.create_label(**{**request, 'idempotency_key': 'other-key'}), label)
        with self.assertRaisesRegex(ReturnLabelProviderError, '^label_provider_http_error$'):
            ReturnLabelProvider(self.url, 'wrong-token').create_label(**request)

    def test_start_return_completes_through_http_and_skips_completed_duplicate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'CIDERLANE_LABEL_PROVIDER_URL': self.url,
            'CIDERLANE_LABEL_PROVIDER_TOKEN': 'mock-token',
            'CIDERLANE_REFERENCE_DATE': '2026-08-31',
        }), patch('src.return_label_provider.requests.post', wraps=requests.post) as post:
            db = Path(directory) / 'test.db'
            context = ReturnContext(verified_order_id='CL1002')
            created = start_return('it00004', context=context, database_path=db)
            duplicate = start_return('it00004', context=context, database_path=db)
            self.assertEqual(created['status'], 'return_created')
            self.assertEqual(duplicate['status'], 'return_already_exists')
            self.assertEqual(duplicate['label_url'], created['label_url'])
            post.assert_called_once()
            with closing(connect_database(db)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM returns').fetchone()[0], 1)
                self.assertEqual(connection.execute('SELECT * FROM return_label_requests').fetchall(), [])
