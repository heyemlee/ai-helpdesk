"""Selected Case targets are query context, never returned business facts."""
import copy

import pytest
from test_case_followups import Tools, dependencies

from app.agents.turn_prep import _case_data_query, prepare_turn
from app.core.business_facts import render_draft
from app.core.lookup_scope import assess_lookup


@pytest.mark.parametrize('reference', ['TARGET-41', 'TARGET-82'])
async def test_selected_case_binding_reaches_the_actual_prefetch_without_using_another_case(reference):
    tools = Tools({'queried_database': False, 'execution_status': 'not_applicable', 'has_risk': False})
    message = '切回第一个工单；只汇报它对应记录的状态。'
    snapshot = {'case_id': 'selected-case', 'order_refs': [reference], 'issue_type': 'order',
                'messages': [{'role': 'user', 'content': '暂缺编号'}, {'role': 'user', 'content': message}],
                'facts': {}, 'status': 'in_progress'}
    turn = await prepare_turn(message, user_id='employee', conv_id='shared-conversation', request_id='request',
                              deps=dependencies(tools), case_context=snapshot)
    query = next(p['query'] for name,p in tools.calls if name == 'order_data_query')
    assert '当前工单选择由上游服务处理' in query
    assert '本次选定工单关联的业务查询目标为：["' + reference + '"]' in query
    assert '不能用目标常量补造' in query and query.endswith(turn.request.message)
    assert '另一个工单' not in query
    assert not turn.request.grounding_sources
    assert not turn.evidence['business_data'].get('evidence')
    assert '未找到记录。' not in render_draft([],turn.evidence['business_data'],lookup_goal=turn.request.lookup_goal)


def test_binding_keeps_the_complete_user_request_and_does_not_invent_unselected_targets():
    message = '查 TARGET-B 的状态，并转人工审批。'
    query = _case_data_query(message,case_refs=['TARGET-A'])
    assert query.endswith(message) and '转人工审批' in query
    assert 'TARGET-A' in query and 'TARGET-B' in query
    assert '上游服务处理' not in _case_data_query(message)


def test_public_model_tool_query_limit_is_unchanged():
    from pydantic import ValidationError

    from app.tools.turn_tools import OrderDataQueryArgs
    with pytest.raises(ValidationError):
        OrderDataQueryArgs(query='x'*301)


def test_healthy_absence_prose_leaves_execution_details_in_the_unchanged_structured_trace():
    from test_lookup_scope import goal, trace
    g = goal()
    business = trace(g)
    original = copy.deepcopy(business)
    body = render_draft([],business,lookup_goal=g)
    assert '查询成功，未找到记录。' in body
    for detail in ['erp_db.orders','order_no =',business['evidence_payload']['steps'][0]['as_of']]:
        assert detail not in body
    assert business == original
    checked = assess_lookup(business,g)
    assert checked['covers_goal'] and checked['receipt']['steps'][0]['final_sql']
    assert checked['scope']['predicates'] == [('order_no','=','CONTROL-A')]
