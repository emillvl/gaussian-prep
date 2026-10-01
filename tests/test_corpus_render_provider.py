import json

import httpx
import pytest

from gaussian_prep.corpus import Corpus
from gaussian_prep.models import Figure, Plan
from gaussian_prep.provider import DeepSeekProvider, ProviderError
from gaussian_prep.render import figure_svg, text_html
from conftest import slot


def test_quality_notes_preserve_every_record_and_source(corpus, tmp_path):
    original = json.loads(corpus.path.read_text())
    rows = [dict(original[0], question_id="ok")]
    rows += [dict(original[0], question_id="calculus", question="Evaluate the integral of this expression over the given interval.")]
    rows += [dict(original[0], question_id="flagged", needs_review=True)]
    rows += [dict(original[0], question_id="image", question="In the figure shown, what is the length of the segment?")]
    rows += [dict(original[0], question_id="missing", answer=None)]
    path = tmp_path / "sample.json"
    content = json.dumps(rows)
    path.write_text(content)
    checked = Corpus(path)
    assert {r.question_id for r in checked.references} == {"ok", "calculus", "flagged", "image", "missing"}
    assert len(checked.flags) == 4
    assert checked.records == rows
    assert checked.report()["records_loaded"] == len(rows)
    assert checked.report()["discarded_records"] == 0
    assert next(r for r in checked.references if r.question_id == "missing").answer is None
    assert path.read_text() == content
    assert checked.report()["quality_note_counts"]["calculus_outside_scope"] == 1


def test_flagged_records_are_still_retrievable(corpus, tmp_path):
    rows = json.loads(corpus.path.read_text())
    rows[0].update(needs_review=True, classification_confidence=0.1, answer=None)
    path = tmp_path / "flagged.json"
    path.write_text(json.dumps(rows))
    checked = Corpus(path)
    reference = checked.retrieve(slot())[0]
    assert reference.question_id == "ref1"
    assert reference.quality_notes
    assert reference.source_metadata["classification_confidence"] == 0.1


def test_retrieval_is_small_and_keeps_ids(corpus):
    selected = corpus.retrieve(slot())
    assert len(selected) <= 3
    assert selected[0].question_id == "ref1"


def test_render_math_and_escape_untrusted_text():
    rendered = text_html(r"Find \(x^2 + \frac{1}{2}\). <script>alert(1)</script>")
    assert "<math" in rendered and "<msup>" in rendered and "<mfrac>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_currency_does_not_become_math():
    assert text_html("The prices are $700 and $200.") == "The prices are $700 and $200."


@pytest.mark.parametrize("text", [
    r"Find \(x^2 now.", r"Find x\).", r"Find \(x\].", r"Find \(\).",
    r"Find \(\frac{1}{2\).", r"Find \(x}\).", r"Find \(\unknown{2}\).",
    r"Find x^2.", r"Find \frac{1}{2}.", r"Find \(x + \(y\)\).",
])
def test_bad_notation_is_rejected_before_item_acceptance(text):
    with pytest.raises(ValueError):
        text_html(text)


def test_math_allows_literal_braces_and_grouped_powers():
    assert "<math" in text_html(r"Choose from \(\{x^{10}, x^{-1}\}\).")


def test_svg_escapes_labels():
    figure = Figure(description="Triangle", show_axes=False, points=[{"label": "<script>", "x": 0, "y": 0}, {"label": "B", "x": 2, "y": 0}], segments=[{"start": "<script>", "end": "B"}], circles=[])
    svg = figure_svg(figure)
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_one_model_and_no_key_in_logs(tmp_path, monkeypatch):
    plan = Plan(title="Test", interpretation="Test", assumptions=[], requirements=[], total_questions=1, module_count=1, minutes_per_module=[2], slots=[slot()])
    requests = []
    def post(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": plan.model_dump_json()}}]})
    monkeypatch.setattr(httpx, "post", post)
    provider = DeepSeekProvider("chosen-model", "test-secret-key", tmp_path)
    assert provider.ask("planner", {"request": "one question"}, Plan).total_questions == 1
    assert requests[0][0] == "https://api.deepseek.com/chat/completions"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer test-secret-key"
    assert requests[0][1]["json"]["model"] == "chosen-model"
    assert requests[0][1]["json"]["response_format"] == {"type": "json_object"}
    assert "JSON schema" in requests[0][1]["json"]["messages"][0]["content"]
    assert "test-secret-key" not in next(tmp_path.glob("*.json")).read_text()


def test_truncated_response_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}))
    provider = DeepSeekProvider("chosen-model", "test-key", tmp_path)
    with pytest.raises(ProviderError, match="finish normally"):
        provider.ask("planner", {}, Plan)


def test_quota_retries_are_bounded(tmp_path, monkeypatch):
    requests = []
    def post(*args, **kwargs):
        requests.append(1)
        return httpx.Response(429)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("gaussian_prep.provider.time.sleep", lambda _: None)
    provider = DeepSeekProvider("chosen-model", "test-key", tmp_path)
    with pytest.raises(ProviderError, match="429"):
        provider.ask("planner", {}, Plan)
    assert len(requests) == 3


def test_missing_configuration_is_actionable(tmp_path):
    with pytest.raises(ProviderError, match="DEEPSEEK_MODEL"):
        DeepSeekProvider("", "", tmp_path)
    with pytest.raises(ProviderError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider("selected-model", "", tmp_path)
