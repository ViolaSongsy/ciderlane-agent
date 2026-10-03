import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import Agent, Model, ModelResponse, Runner, Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from src.agent_tools import start_return_tool, request_human_support_tool
from src.context import ReturnContext
from src.main import approval_description, resolve_approvals
from src.storage import connect_database, initialize_database
from src.tools.order_tools import load_orders


class ScriptedModel(Model):
    def __init__(self, item_id='it00004', reason=None, support=False):
        self.item_id = item_id
        self.reason = reason
        self.support = support
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            output = [ResponseFunctionToolCall(
                arguments=json.dumps({'item_id': self.item_id, 'reason': self.reason}),
                call_id='start-call', name='start_return', type='function_call',
            )]
        elif self.calls == 2 and self.support:
            output = [ResponseFunctionToolCall(
                arguments=json.dumps({'escalation_reason': 'return_exception',
                                      'summary': 'The swimsuit arrived damaged.'}),
                call_id='support-call', name='request_human_support', type='function_call',
            )]
        else:
            output = [ResponseOutputMessage(
                id=f'message-{self.calls}', role='assistant', status='completed',
                type='message', content=[ResponseOutputText(
                    annotations=[], text='Done.', type='output_text')],
            )]
        return ModelResponse(output=output, usage=Usage(), response_id=None)

    async def stream_response(self, *args, **kwargs):
        if False:
            yield


class TestApproval(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'approval.db'
        environment = patch.dict(os.environ, {
            'CIDERLANE_DATABASE_PATH': str(self.path),
            'CIDERLANE_REFERENCE_DATE': '2026-08-31',
        })
        environment.start()
        self.addCleanup(environment.stop)
        initialize_database()
        self.context = ReturnContext(verified_order_id='CL1002')
        provider_patch = patch('src.tools.return_tools.get_return_label_provider')
        self.provider_factory = provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.provider_factory.return_value.create_label.return_value = 'https://carrier.example.test/label.pdf'

    def rows(self, table='returns'):
        with connect_database() as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]

    def begin(self, item_id='it00004', reason=None, support=False):
        model = ScriptedModel(item_id, reason, support)
        agent = Agent(name='Approval test', instructions='Call tools.', model=model,
                      tools=[start_return_tool, request_human_support_tool])
        result = Runner.run_sync(agent, 'Please start my return.', context=self.context)
        return agent, model, result

    def test_start_return_requires_sdk_approval_and_has_no_preapproval_write(self):
        self.assertIs(start_return_tool.needs_approval, True)
        agent, model, result = self.begin()
        self.assertEqual(len(result.interruptions), 1)
        self.assertEqual(result.interruptions[0].tool_name, 'start_return')
        self.assertIsNone(result.final_output)
        self.assertEqual(self.rows(), [])
        self.assertIn('white short dress (item ID it00004)',
                      approval_description(result.interruptions[0], self.context))
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_rejection_does_not_execute_or_write(self):
        agent, model, result = self.begin()
        state = result.to_state()
        state.reject(result.interruptions[0], rejection_message='Customer declined.')
        with patch('src.agent_tools.start_return') as tool:
            resumed = Runner.run_sync(agent, state)
        tool.assert_not_called()
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(self.rows(), [])
        self.provider_factory.assert_not_called()
        self.assertEqual(self.rows('return_label_requests'), [])

    def test_approval_executes_and_persists(self):
        agent, model, result = self.begin()
        self.assertEqual(self.rows(), [])
        state = result.to_state()
        state.approve(result.interruptions[0])
        resumed = Runner.run_sync(agent, state)
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['item_id'], 'it00004')

    def test_approval_does_not_bypass_changed_policy(self):
        agent, model, result = self.begin()
        orders = load_orders()
        order = next(o for o in orders if o['order_id'] == 'CL1002')
        item = next(i for i in order['items'] if i['item_id'] == 'it00004')
        item['final_sale'] = True
        state = result.to_state()
        state.approve(result.interruptions[0])
        with patch('src.tools.return_tools.load_orders', return_value=orders):
            resumed = Runner.run_sync(agent, state)
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.context.current_evaluation.decision, 'blocked')
        self.provider_factory.assert_not_called()

    def test_approved_exception_still_allows_support_escalation(self):
        agent, model, result = self.begin('it00002', 'damaged', support=True)
        state = result.to_state()
        state.approve(result.interruptions[0])
        resumed = Runner.run_sync(agent, state)
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows('support_cases')), 1)
        self.assertEqual(self.rows('support_cases')[0]['eligibility_decision'], 'exception_flow')
        self.provider_factory.assert_not_called()

    def test_cli_prompts_for_concrete_item_and_resumes(self):
        agent, model, result = self.begin()
        with patch('src.main.ciderlane_agent', agent):
            resumed = resolve_approvals(result, self.context, prompt=lambda message: (
                self.assertIn('white short dress (item ID it00004)', message) or 'yes'))
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(len(self.rows()), 1)

    def test_cli_rejection_resumes_without_write(self):
        agent, model, result = self.begin()
        with patch('src.main.ciderlane_agent', agent):
            resumed = resolve_approvals(result, self.context, prompt=lambda _: 'no')
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(self.rows(), [])
        self.provider_factory.assert_not_called()

    def test_unresolved_item_is_rejected_without_prompt_or_write(self):
        for order_id, item_id in (('CL1002', 'missing'), ('CL1001', 'it00004'),
                                  (None, 'it00004')):
            with self.subTest(order_id=order_id, item_id=item_id):
                self.context = ReturnContext(verified_order_id=order_id)
                agent, model, result = self.begin(item_id)
                self.assertIsNone(approval_description(result.interruptions[0], self.context))
                with patch('src.main.ciderlane_agent', agent), \
                     patch('src.agent_tools.start_return') as start:
                    resumed = resolve_approvals(
                        result, self.context,
                        prompt=lambda _: self.fail('Unresolved item was offered for approval'),
                    )
                start.assert_not_called()
                self.assertEqual(resumed.final_output, 'Done.')
                self.assertEqual(self.rows(), [])

    def test_unreadable_order_store_is_rejected_without_prompt(self):
        agent, model, result = self.begin()
        with patch('src.main.ciderlane_agent', agent), \
             patch('src.main.load_orders', side_effect=OSError('unavailable')), \
             patch('src.agent_tools.start_return') as start:
            resumed = resolve_approvals(
                result, self.context,
                prompt=lambda _: self.fail('Unresolved item was offered for approval'),
            )
        start.assert_not_called()
        self.assertEqual(resumed.final_output, 'Done.')
        self.assertEqual(self.rows(), [])
