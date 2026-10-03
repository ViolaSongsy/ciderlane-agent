import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent import ciderlane_agent
from src.agent_tools import start_return_tool
from src.context import ReturnContext, ReturnEvaluation
from src.return_label_provider import ReturnLabelProviderError
from src.storage import connect_database, initialize_database, reserve_return_label_request
from src.tools.order_tools import load_orders
from src.tools.return_tools import check_return_eligibility, start_return
from src.tools.support_tools import request_human_support


class TestStartReturn(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'test.db'
        environment = patch.dict(os.environ, {
            'CIDERLANE_DATABASE_PATH': str(self.db),
            'CIDERLANE_REFERENCE_DATE': '2026-08-31',
        })
        environment.start()
        self.addCleanup(environment.stop)
        initialize_database()
        self.context = ReturnContext(verified_order_id='CL1002')
        provider_patch = patch('src.tools.return_tools.get_return_label_provider')
        self.provider_factory = provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.provider = self.provider_factory.return_value
        self.provider.create_label.return_value = 'https://carrier.example.test/label.pdf'

    def rows(self, table='returns'):
        with connect_database() as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]

    def test_no_prior_check_persists_return_and_label(self):
        def create_label(**request):
            pending = self.rows('return_label_requests')
            self.assertEqual(len(pending), 1)
            self.assertEqual(request, {'order_id': 'CL1002', 'item_id': 'it00004',
                                      'idempotency_key': pending[0]['idempotency_key']})
            self.assertEqual(self.rows(), [])
            # An independent writer can acquire the lock while HTTP is in progress.
            with closing(connect_database()) as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.rollback()
            return 'https://carrier.example.test/label.pdf'
        self.provider.create_label.side_effect = create_label
        self.assertIsNone(self.context.current_evaluation)
        result = start_return('it00004', context=self.context)
        self.assertEqual(result['status'], 'return_created')
        self.assertEqual(result['decision'], 'standard_return')
        self.assertEqual(result['return_id'], 'RET-CL1002-IT00004')
        self.assertEqual(result['label_url'],
                         'https://carrier.example.test/label.pdf')
        self.assertEqual(self.rows(), [{key: result[key] for key in (
            'return_id', 'order_id', 'item_id', 'label_url', 'created_at')}])
        self.assertEqual(self.context.current_evaluation.decision, 'standard_return')
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_provider_failures_leave_only_a_pending_request(self):
        for code in ('label_provider_timeout', 'label_provider_unavailable',
                     'label_provider_invalid_response'):
            with self.subTest(code=code):
                self.provider.create_label.side_effect = ReturnLabelProviderError(code)
                result = start_return('it00004', context=self.context)
                self.assertEqual(result, {'status': 'return_not_created', 'error': code})
                self.assertEqual(self.rows(), [])
                self.assertEqual(len(self.rows('return_label_requests')), 1)

    def test_later_invocation_reuses_pending_key_and_completes(self):
        self.provider.create_label.side_effect = ReturnLabelProviderError('label_provider_timeout')
        failed = start_return('it00004', context=self.context)
        self.assertEqual(failed['status'], 'return_not_created')
        key = self.rows('return_label_requests')[0]['idempotency_key']

        # A new conversation recovers the operation from SQLite, not old context.
        self.provider.create_label.side_effect = None
        result = start_return('it00004', context=ReturnContext(verified_order_id='CL1002'))
        self.assertEqual(result['status'], 'return_created')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows('return_label_requests'), [])
        self.assertEqual(self.provider.create_label.call_count, 2)
        for request in self.provider.create_label.call_args_list:
            self.assertEqual(request.kwargs['idempotency_key'], key)
            self.assertEqual(request.kwargs['order_id'], 'CL1002')
            self.assertEqual(request.kwargs['item_id'], 'it00004')

    def test_provider_success_local_write_failure_then_recovery(self):
        with closing(connect_database()) as connection, connection:
            # Fail after INSERT to prove completion and pending cleanup roll back together.
            connection.execute('CREATE TRIGGER fail_completion BEFORE DELETE ON return_label_requests '
                               "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        failed = start_return('it00004', context=self.context)
        self.assertEqual(failed, {'status': 'return_not_created', 'error': 'return_storage_unavailable'})
        self.assertEqual(self.rows(), [])
        key = self.rows('return_label_requests')[0]['idempotency_key']
        self.assertEqual(self.provider.create_label.call_args.kwargs['idempotency_key'], key)

        with closing(connect_database()) as connection, connection:
            connection.execute('DROP TRIGGER fail_completion')
        recovered = start_return('it00004', context=ReturnContext(verified_order_id='CL1002'))
        self.assertEqual(recovered['status'], 'return_created')
        self.assertEqual(recovered['label_url'], 'https://carrier.example.test/label.pdf')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows('return_label_requests'), [])
        self.assertEqual(self.provider.create_label.call_count, 2)
        self.assertEqual(self.provider.create_label.call_args.kwargs['idempotency_key'], key)

    def test_failed_key_reservation_never_calls_provider(self):
        with closing(connect_database()) as connection, connection:
            connection.execute('CREATE TRIGGER fail_reservation BEFORE INSERT ON return_label_requests '
                               "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        result = start_return('it00004', context=self.context)
        self.assertEqual(result, {'status': 'return_not_created', 'error': 'return_storage_unavailable'})
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_pending_request_does_not_bypass_changed_policy(self):
        self.provider.create_label.side_effect = ReturnLabelProviderError('label_provider_timeout')
        start_return('it00004', context=self.context)
        pending = self.rows('return_label_requests')
        self.assertEqual(len(pending), 1)
        self.provider_factory.reset_mock()
        with patch.dict(os.environ, {'CIDERLANE_REFERENCE_DATE': '2027-01-01'}):
            result = start_return('it00004', context=self.context)
        self.assertEqual(result['status'], 'return_not_created')
        self.assertEqual(result['decision'], 'blocked')
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows('return_label_requests'), pending)

    def test_failed_attempts_clear_stale_evaluation_without_writes(self):
        for order, item, reason, error in (
            (None, 'it00004', None, 'order_not_verified'),
            ('CL1002', 'it00004', 'invented', 'invalid_reason'),
            ('MISSING', 'it00004', None, 'order_or_item_not_found'),
            ('CL1002', 'MISSING', None, 'order_or_item_not_found'),
            ('CL1001', 'it00004', None, 'order_or_item_not_found'),
        ):
            with self.subTest(error=error, order=order, item=item):
                context = ReturnContext(verified_order_id=order,
                    current_evaluation=ReturnEvaluation('CL1002', 'it00002',
                        'exception_flow', ('damaged_item_exception',)))
                result = start_return(item, reason, context=context)
                self.assertEqual(result['error'], error)
                self.assertIsNone(context.current_evaluation)
                self.assertNotIn('label_url', result)
                self.assertEqual(self.rows(), [])
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_fresh_facts_override_old_eligible_result(self):
        for field, value in (('final_sale', True), ('category', 'swimwear'),
                             ('delivered_date', '2026-01-01')):
            with self.subTest(field=field):
                self.assertEqual(check_return_eligibility('it00004',
                    context=self.context)['decision'], 'standard_return')
                orders = load_orders()
                order = next(o for o in orders if o['order_id'] == 'CL1002')
                item = next(i for i in order['items'] if i['item_id'] == 'it00004')
                (order if field == 'delivered_date' else item)[field] = value
                with patch('src.tools.return_tools.load_orders', return_value=orders):
                    result = start_return('it00004', context=self.context)
                self.assertEqual(result['decision'], 'blocked')
                self.assertEqual(self.context.current_evaluation.decision, 'blocked')
                self.assertNotIn('label_url', result)
                self.assertEqual(self.rows(), [])
        self.provider_factory.assert_not_called()

    def test_blocked_and_exception_do_not_write_and_preserve_support_provenance(self):
        for reason, decision, escalation in (
            (None, 'blocked', 'final_sale_dispute'),
            ('damaged', 'exception_flow', 'return_exception'),
        ):
            result = start_return('it00003', reason, context=self.context)
            self.assertEqual(result['decision'], decision)
            self.assertEqual(result['status'], 'return_not_created')
            self.assertNotIn('label_url', result)
            self.assertEqual(self.rows(), [])
            self.assertEqual(len(self.rows('support_cases')), 0 if reason is None else 1)
            support = request_human_support(escalation, 'Review requested.', context=self.context)
            self.assertEqual(support['status'], 'case_created')
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_retries_are_global_and_ignore_changed_policy(self):
        first = start_return('it00004', context=self.context)
        for context in (self.context, ReturnContext(verified_order_id='CL1002')):
            with patch.dict(os.environ, {'CIDERLANE_REFERENCE_DATE': '2027-01-01'}), \
                 patch('src.tools.return_tools.evaluate_return_policy') as evaluate:
                repeated = start_return('it00004', context=context)
            evaluate.assert_not_called()
            self.assertEqual(repeated['status'], 'return_already_exists')
            self.assertEqual(repeated['return_id'], first['return_id'])
            self.assertEqual(repeated['label_url'], first['label_url'])
            self.assertEqual(repeated['created_at'], first['created_at'])
            self.assertIsNone(context.current_evaluation)
        self.assertEqual(len(self.rows()), 1)
        self.provider.create_label.assert_called_once()

    def test_unique_constraint_and_conflict_recovery(self):
        def another_caller_completes(**request):
            with closing(connect_database()) as connection, connection:
                connection.execute('INSERT INTO returns (return_id, order_id, item_id, label_url) '
                                   'VALUES (?, ?, ?, ?)',
                                   ('other-return', 'CL1002', 'it00004', 'https://carrier.example.test/other'))
            return 'https://carrier.example.test/label.pdf'
        self.provider.create_label.side_effect = another_caller_completes
        result = start_return('it00004', context=self.context)
        self.assertEqual(result['status'], 'return_already_exists')
        self.assertEqual(result['return_id'], 'other-return')
        self.assertEqual(result['label_url'], 'https://carrier.example.test/other')
        self.assertIsNone(self.context.current_evaluation)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows('return_label_requests'), [])
        with closing(connect_database()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('INSERT INTO returns (return_id, order_id, item_id, label_url) '
                                   'VALUES (?, ?, ?, ?)', ('different-id', 'CL1002', 'it00004', 'label'))

    def test_completion_during_reservation_returns_existing_without_provider(self):
        def reserve_after_another_caller(order_id, item_id, *, database_path):
            # Complete after the first lookup, before the reservation acquires its lock.
            with closing(connect_database(database_path)) as connection, connection:
                connection.execute('INSERT INTO returns (return_id, order_id, item_id, label_url) '
                                   'VALUES (?, ?, ?, ?)',
                                   ('other-return', order_id, item_id, 'https://carrier.example.test/other'))
            return reserve_return_label_request(order_id, item_id, database_path=database_path)

        with patch('src.tools.return_tools.reserve_return_label_request',
                   side_effect=reserve_after_another_caller):
            result = start_return('it00004', context=self.context)
        self.assertEqual(result, {'status': 'return_already_exists', **self.rows()[0]})
        self.assertIsNone(self.context.current_evaluation)
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows('return_label_requests'), [])

    async def test_schema_and_forged_state_cannot_authorize_write(self):
        self.assertEqual({tool.name for tool in ciderlane_agent.tools}, {
            'verify_order', 'check_return_eligibility', 'start_return', 'request_human_support'})
        schema = start_return_tool.params_json_schema
        self.assertEqual(set(schema['properties']), {'item_id', 'reason'})
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['reason']['anyOf'], [
            {'enum': ['damaged', 'wrong_item', 'other'], 'type': 'string'}, {'type': 'null'}])
        context = ReturnContext()
        raw = json.dumps({'item_id': 'it00004', 'reason': None,
            'order_id': 'CL1002', 'verified_order_id': 'CL1002',
            'eligibility_decision': 'standard_return',
            'context': {'verified_order_id': 'CL1002'}, 'final_sale': False})
        tool_context = ToolContext.from_agent_context(RunContextWrapper(context=context),
            tool_name='start_return', tool_call_id='forged', tool_arguments=raw)
        result = await start_return_tool.on_invoke_tool(tool_context, raw)
        self.assertEqual(result['error'], 'order_not_verified')
        self.assertIsNone(context.verified_order_id)
        self.assertEqual(self.rows(), [])
        self.provider_factory.assert_not_called()

    async def test_sdk_validation_failure_clears_stale_evaluation(self):
        check_return_eligibility('it00003', 'damaged', context=self.context)
        self.assertEqual(self.context.current_evaluation.decision, 'exception_flow')
        raw = json.dumps({'item_id': 'it00004', 'reason': 'invented'})
        tool_context = ToolContext.from_agent_context(
            RunContextWrapper(context=self.context), tool_name='start_return',
            tool_call_id='invalid-reason', tool_arguments=raw)
        result = await start_return_tool.on_invoke_tool(tool_context, raw)
        self.assertIn('Invalid JSON input', result)
        self.assertIsNone(self.context.current_evaluation)
        self.assertEqual(self.rows(), [])
        support = request_human_support('return_exception', 'Review requested.',
                                        context=self.context)
        self.assertEqual(support['error'], 'trusted_evaluation_required')
        self.provider_factory.assert_not_called()
