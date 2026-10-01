import json
from pathlib import Path

import pytest
import sympy as sp

from conftest import audit_result, ScriptedProvider, question, mathematical_model
from gaussian_prep.expressions import ExpressionReader, equivalent, _numeric_equivalent
from gaussian_prep.models import Audit, MathModel, Question
from gaussian_prep.notation import normalize_text, prepare_question
from gaussian_prep.pipeline import Pipeline
from gaussian_prep.render import question_html, text_html
from gaussian_prep.verification import compute


CASES = json.loads((Path(__file__).parent / 'fixtures/identity_regressions.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['name'])
def test_real_q26_questions_pass_with_symbolic_evidence(case):
    result = compute(Question.model_validate(case['question']), MathModel.model_validate(case['model']))
    assert result['status'] == 'pass', result
    assert [o['label'] for o in result['options'] if o['correct']] == ['A']


def test_variables_are_not_sampled_only_on_the_diagonal():
    reader = ExpressionReader(['x', 'y'])
    a = reader.read('x*(x*y**2)**(1/6)')
    c = reader.read('x*(x**2*y)**(1/6)')
    assert equivalent(a, c, list(reader.symbols.values())) is False


def test_matching_samples_never_prove_an_identity():
    x = sp.Symbol('x', real=True)
    assert _numeric_equivalent(sp.sin(2*sp.pi*x), sp.S.Zero, [x]) is None


@pytest.mark.parametrize('source,expected', [
    (r'Find \\(\\frac{1}{2}\\).', r'Find \(\frac{1}{2}\).'),
    (r'Find \\(50\\%\\).', r'Find \(50\%\).'),
    ('Find \\('+'\f'+'rac{1}{2}\\).', r'Find \(\frac{1}{2}\).'),
    (r'Find \(x^10 + y^-12\).', r'Find \(x^{10} + y^{-12}\).'),
    (r'Find $x^2$ and $$\frac{1}{2}$$.', r'Find \(x^2\) and \[\frac{1}{2}\].'),
    ('Pay $5 and $10, or $5 + $10.', 'Pay $5 and $10, or $5 + $10.'),
    (r'\(\begin{cases}x&x>0\\y&x<0\end{cases}\)', r'\(\begin{cases}x&x>0\\y&x<0\end{cases}\)'),
])
def test_notation_repairs_are_idempotent_and_preserve_prices(source, expected):
    assert normalize_text(source) == expected
    assert normalize_text(expected) == expected


def test_double_escaping_cannot_survive_the_display_check():
    q = question()
    q.stem = r'Find \\(x\\) in \\(2x+5=17\\).'
    repaired, changes = prepare_question(q)
    assert repaired.answer == q.answer and repaired.choices == q.choices
    assert changes and '\\' not in question_html(repaired, 1)


def test_formatting_audit_never_replaces_a_sound_question(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == 'auditor':
                return audit_result(payload, schema, accepted=False, summary='Escaping concern', issues=[dict(dimension='wording_consistency', repair_kind='formatting', reason='LaTeX has doubled backslashes', slot_ids=['q1'], instruction='Fix escaping; retain all mathematics.')])
            return super().ask(role, payload, schema)
    provider = Provider()
    result = Pipeline(corpus, provider, tmp_path, compute=compute, progress=lambda _:None).run('Generate 1 question')
    assert result['status'] == 'complete'
    assert provider.generated == {'q1':1}
    assert result['audit_resolutions']


def test_audit_replacement_receives_the_original_question(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            if role == 'auditor' and self.generated['q1'] == 1:
                return audit_result(payload, schema, accepted=False, summary='Variety', issues=[dict(dimension='variety',reason='Repeat',slot_ids=['q1'],instruction='Change context')])
            if role == 'generator' and self.generated.get('q1'):
                assert payload['previous_draft']['slot_id'] == 'q1'
            return super().ask(role, payload, schema)
    result = Pipeline(corpus, Provider(), tmp_path, compute=compute, progress=lambda _:None).run('Generate 1 question')
    assert result['status'] == 'complete'


def test_verifier_repairs_its_model_without_rewriting_question(corpus, tmp_path):
    class Provider(ScriptedProvider):
        def ask(self, role, payload, schema):
            result = super().ask(role, payload, schema)
            if role == 'verifier':
                assert 'answer' not in payload['question'] and 'solution' not in payload['question']
                if 'previous_model' not in payload:
                    result.mode = 'evaluate'  # Invalid with variables/equations; repair the model.
            return result
    provider = Provider()
    result = Pipeline(corpus, provider, tmp_path, compute=compute, progress=lambda _:None).run('Generate 1 question')
    assert result['status'] == 'complete'
    assert provider.generated == {'q1':1}
    assert sum(role == 'verifier' for role,_ in provider.calls) == 2


def test_unknown_commands_and_unbalanced_math_still_require_repair():
    for text in (r'Find \(\unknown{2}\).',r'Find \(\frac{1}{2\).'):
        with pytest.raises(ValueError):
            text_html(text)


def identity_model(target, choices, constraints=()):
    return MathModel(mode='identity', interpretation='Domain regression', variables=['x'], equations=[], constraints=list(constraints), target=target, aggregate='each', options=[{'label':l,'expression':v} for l,v in zip('ABCD',choices)], unsupported_reason='')


def test_identity_respects_the_negative_domain():
    m = identity_model('sqrt(x**2)', ['-x','x','2*x','x**2'], [{'lhs':'x','rhs':'0','operator':'lt'}])
    assert compute(question(),m)['status'] == 'pass'


def test_cancelled_denominator_does_not_hide_an_extra_domain_hole():
    m = identity_model('x+1', ['x+1','(x**2-1)/(x-1)','x-1','x+2'])
    result = compute(question(),m)
    assert result['status'] == 'pass', result
    assert result['options'][1]['correct'] is False


def test_empty_identity_domain_is_not_approved():
    m = identity_model('x', ['x','x+1','x+2','x+3'], [{'lhs':'x','rhs':'0','operator':'gt'},{'lhs':'x','rhs':'0','operator':'lt'}])
    assert compute(question(),m)['status'] == 'unverified'


def test_subscripts_are_not_silently_erased():
    with pytest.raises(ValueError):
        ExpressionReader(['x']).read('x_{1}')


def test_periodic_solution_count_includes_the_complete_bounded_set():
    m = MathModel(mode='solve',interpretation='Count roots in the closed interval',variables=['x'],equations=[{'lhs':'sin(x)','rhs':'0'}],constraints=[{'lhs':'x','rhs':'0','operator':'ge'},{'lhs':'x','rhs':'2*pi','operator':'le'}],target='x',aggregate='count',options=[],unsupported_reason='')
    q = question(format='SPR'); q.answer='3'
    assert compute(q,m)['status'] == 'pass'
    m.constraints = []
    assert compute(q,m)['status'] == 'unverified'


def stalled_checkpoint(corpus, run_dir):
    state = Pipeline(corpus, ScriptedProvider(), run_dir, compute=compute, progress=lambda _:None).run('Generate 1 question')
    issue = dict(dimension='wording_consistency',reason='Double-escaped LaTeX delimiters',slot_ids=['q1'],instruction='Fix escaping and keep content.')
    state.pop('audit_work', None)
    state['items']['q1'].pop('formatting_check', None)
    state['status'] = 'needs_attention'
    state['audit'] = dict(accepted=False,summary='Formatting only',issues=[issue])
    state['pending_audit_feedback'] = {'q1':[issue]}
    state['slot_failures'] = {'q1':'Replacement failed'}
    state['error'] = 'Replacement failed'
    state['items']['q1']['question']['stem'] = r'If \\(2x + 5 = 19\\), what is the value of \\(x\\)?'
    state['items']['q1']['verification'].pop('verification_version',None)
    path = run_dir/'state.json'
    path.write_text(json.dumps(state),encoding='utf-8')
    return state, path.read_text(encoding='utf-8')


def test_offline_recovery_preserves_original_and_is_idempotent(corpus,tmp_path):
    from gaussian_prep.recovery import recover_saved_run
    before, original = stalled_checkpoint(corpus,tmp_path)
    result = recover_saved_run(corpus,tmp_path,progress=lambda _:None,compute=compute)
    assert result['status'] == 'complete'
    assert not result['pending_audit_feedback'] and not result['slot_failures']
    assert 'error' not in result
    assert result['items']['q1']['question']['answer'] == before['items']['q1']['question']['answer']
    backups = list(tmp_path.glob('state-before-recovery-*.json'))
    assert len(backups)==1 and backups[0].read_text(encoding='utf-8')==original
    def no_recompute(*args):
        raise AssertionError('Already rechecked')
    assert recover_saved_run(corpus,tmp_path,progress=lambda _:None,compute=no_recompute)==result
    assert len(list(tmp_path.glob('state-before-recovery-*.json')))==1


def test_recovery_never_approves_wrong_answers(corpus,tmp_path):
    from gaussian_prep.recovery import recover_saved_run
    state,_ = stalled_checkpoint(corpus,tmp_path)
    state['items']['q1']['question']['answer']='B'
    (tmp_path/'state.json').write_text(json.dumps(state),encoding='utf-8')
    result = recover_saved_run(corpus,tmp_path,progress=lambda _:None,compute=compute)
    assert result['status']=='needs_attention'
    assert result['items']['q1']['verification']['status']=='fail'
    assert 'q1' in result['pending_audit_feedback']


def test_semantic_audit_claim_is_not_dismissed_as_formatting(corpus,tmp_path):
    from gaussian_prep.pipeline import resolve_formatting_audit
    state=Pipeline(corpus,ScriptedProvider(),tmp_path,compute=compute,progress=lambda _:None).run('Generate 1 question')
    audit=Audit(accepted=False,summary='Content',issues=[dict(dimension='wording_consistency',repair_kind='formatting',reason='The unit should be inches rather than centimeters',slot_ids=['q1'],instruction='Correct units')])
    effective,resolved=resolve_formatting_audit(audit,state['items'])
    assert not effective.accepted and not resolved


def test_explicit_content_finding_is_not_dismissed_for_mentioning_escaping(corpus, tmp_path):
    from gaussian_prep.pipeline import resolve_formatting_audit
    state = Pipeline(corpus, ScriptedProvider(), tmp_path, compute=compute, progress=lambda _: None).run('Generate 1 question')
    audit = Audit(accepted=False, summary='Wrong unit', issues=[dict(
        dimension='wording_consistency', repair_kind='content', slot_ids=['q1'],
        reason='The double-escaped unit also names centimeters where the data require inches.',
        instruction='Correct the unit and its meaning.')])
    effective, resolved = resolve_formatting_audit(audit, state['items'])
    assert not effective.accepted
    assert not resolved


def test_resume_recovers_before_asking_for_credentials(corpus,tmp_path,monkeypatch):
    from gaussian_prep import cli
    stalled_checkpoint(corpus,tmp_path)
    def no_credentials(*args,**kwargs):
        raise AssertionError('Local recovery must not ask for an API key')
    monkeypatch.setattr(cli,'resolve_credentials',no_credentials)
    assert cli.main(['resume',str(tmp_path),'--format','none'])==0


def test_truncated_provider_result_gets_a_bounded_retry(tmp_path,monkeypatch):
    import httpx
    from gaussian_prep.provider import ModelProvider
    from gaussian_prep.models import Plan
    plan=ScriptedProvider().ask('planner',{},Plan)
    replies=iter([
        {'choices':[{'finish_reason':'length','message':{'content':'{'}}]},
        {'choices':[{'finish_reason':'stop','message':{'content':'```json\n'+plan.model_dump_json()+'\n```'}}]},
    ])
    monkeypatch.setattr(httpx,'post',lambda *a,**k:httpx.Response(200,json=next(replies)))
    provider=ModelProvider('test-model','test-key',tmp_path)
    assert provider.ask('planner',{},Plan)==plan
    assert provider.calls==2


def test_malformed_provider_envelopes_fail_cleanly(tmp_path,monkeypatch):
    import httpx
    from gaussian_prep.provider import ModelProvider,ProviderError
    from gaussian_prep.models import Plan
    monkeypatch.setattr(httpx,'post',lambda *a,**k:httpx.Response(200,json={'choices':['invalid']}))
    provider=ModelProvider('test-model','test-key',tmp_path)
    with pytest.raises(ProviderError,match='request failed'):
        provider.ask('planner',{},Plan)
    assert provider.calls==3


@pytest.mark.parametrize('fault', ['formatting', 'computation'])
def test_revalidation_preserves_pending_content_feedback(corpus,tmp_path,fault):
    from gaussian_prep.recovery import recover_saved_run
    state,_ = stalled_checkpoint(corpus,tmp_path)
    issue = dict(dimension='variety', reason='Repeated context', slot_ids=['q1'], instruction='Use a different context')
    state['pending_audit_feedback'] = {'q1':[issue]}
    if fault == 'formatting':
        state['items']['q1']['question']['stem'] = r'Find \(\frac{1}{2\).'
    else:
        state['items']['q1']['question']['answer'] = 'B'
    (tmp_path/'state.json').write_text(json.dumps(state),encoding='utf-8')
    result = recover_saved_run(corpus,tmp_path,progress=lambda _:None,compute=compute)
    assert issue in result['pending_audit_feedback']['q1']
    assert len(result['pending_audit_feedback']['q1']) == 2


def test_resume_repairs_saved_review_mismatch_instead_of_looping(corpus,tmp_path):
    state = Pipeline(corpus,ScriptedProvider(),tmp_path,compute=compute,progress=lambda _:None).run('Generate 1 question')
    state['items']['q1']['review']['estimated_difficulty'] = 'Hard'
    (tmp_path/'state.json').write_text(json.dumps(state),encoding='utf-8')
    provider = ScriptedProvider()
    result = Pipeline(corpus,provider,tmp_path,compute=compute,progress=lambda _:None).run(state['request'],resume=True)
    assert result['status'] == 'complete'
    assert provider.generated == {'q1':1}
    assert result['items']['q1']['review']['estimated_difficulty'] == 'Easy'
