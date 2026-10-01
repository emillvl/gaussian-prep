import httpx
import pytest
from pydantic import ValidationError

from gaussian_prep.models import Plan
from gaussian_prep.pipeline import Pipeline, PipelineError
from gaussian_prep.provider import ModelProvider


def conflict_response():
    return dict(title="Request conflict", interpretation="The requested formats conflict.", assumptions=[],
                requirements=[], conflicts=["A verbal explanation cannot be an SPR numeric answer."],
                total_questions=0, module_count=0, minutes_per_module=[], slots=[])


def test_conflicting_request_stops_after_one_planner_response(corpus, tmp_path, monkeypatch):
    import json
    requests = []

    def respond(*args, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(conflict_response())}}]})

    monkeypatch.setattr(httpx, "post", respond)
    provider = ModelProvider("test-model", "test-key", tmp_path / "calls")
    with pytest.raises(PipelineError, match="Request needs clarification"):
        Pipeline(corpus, provider, tmp_path / "run", progress=lambda _: None).run("Generate 1 question requiring a verbal explanation in SPR format")
    assert len(requests) == 1


def test_empty_plan_without_conflicts_is_not_valid():
    data = conflict_response()
    data["conflicts"] = []
    with pytest.raises(ValidationError):
        Plan.model_validate(data)
