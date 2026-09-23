import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from src.agent import ciderlane_agent
from src.agent_tools import start_return_tool
from src.context import ReturnContext, ReturnEvaluation
from src.storage import connect_database, initialize_database
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

    def rows(self, table='returns'):
        with connect_database() as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]

    def test_no_prior_check_persists_return_and_label(self):
        self.assertIsNone(self.context.current_evaluation)
        result = start_return('it00004', context=self.context)
        self.assertEqual(result['status'], 'return_created')
        self.assertEqual(result['decision'], 'standard_return')
        self.assertEqual(result['return_id'], 'RET-CL1002-IT00004')
        self.assertEqual(result['label_url'],
                         'https://mock.ciderlane.com/returns/RET-CL1002-IT00004/label')
        self.assertEqual(self.rows(), [{key: result[key] for key in (
            'return_id', 'order_id', 'item_id', 'label_url', 'created_at')}])
        self.assertEqual(self.context.current_evaluation.decision, 'standard_return')

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

    def test_unique_constraint_and_conflict_recovery(self):
        first = start_return('it00004', context=self.context)
        # Hide the initial SELECT to simulate another writer winning the race.
        # The INSERT still reaches SQLite and hits its real UNIQUE constraint.
        with connect_database() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('INSERT INTO returns (return_id, order_id, item_id, label_url) '
                                   'VALUES (?, ?, ?, ?)', ('different-id', 'CL1002', 'it00004', 'label'))
            connection.rollback()
            proxy = MagicMock(wraps=connection)
            proxy.__enter__.return_value = proxy
            proxy.__exit__.side_effect = connection.__exit__
            calls = 0
            def execute(sql, parameters=()):
                nonlocal calls
                calls += 1
                if calls == 1:
                    cursor = MagicMock()
                    cursor.fetchone.return_value = None
                    return cursor
                return connection.execute(sql, parameters)
            proxy.execute.side_effect = execute
            with patch('src.tools.return_tools.connect_database', return_value=proxy):
                result = start_return('it00004', context=self.context)
        self.assertEqual(result['status'], 'return_already_exists')
        self.assertEqual(result['return_id'], first['return_id'])
        self.assertEqual(len(self.rows()), 1)

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
