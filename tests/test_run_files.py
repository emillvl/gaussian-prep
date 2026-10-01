import json
import pytest

from conftest import ScriptedProvider
from gaussian_prep import cli
from gaussian_prep.pipeline import Pipeline
from gaussian_prep.provider import ModelProvider, ProviderError
from gaussian_prep.run_files import RunFiles
from gaussian_prep.run_summary import write_summary
from gaussian_prep.verification import compute
from gaussian_prep import export as exporters
from test_export import state as export_state


def test_legacy_organization_preserves_every_artifact_and_is_repeatable(tmp_path):
    originals = {
        "state.json": b'{"status":"complete"}',
        "questions.pdf": b"%PDF keep exactly",
        "questions.tex": b"original LaTeX",
        "calls/one.json": b"call history",
        "items/m1-q1.json": b"draft",
        "audit-20260919T164032093028.json": b"audit",
        "plan-attempt-1.json": b"plan",
        "dataset-report.json": b"dataset",
        "state-before-recovery-20260919T164032093028.json": b"backup",
        ".questions-images/math.png": b"image",
        "personal-notes.txt": b"untouched",
    }
    for name, data in originals.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    files = RunFiles(tmp_path)
    moves = files.legacy_moves()
    files.prepare()
    files.prepare()
    for source, target in moves:
        if source.suffix:
            assert target.read_bytes() == originals[source.relative_to(tmp_path).as_posix()]
        else:
            assert target.is_dir()
        assert not source.exists()
    assert (files.diagnostics / "calls/one.json").read_bytes() == originals["calls/one.json"]
    assert (files.diagnostics / "items/m1-q1.json").read_bytes() == originals["items/m1-q1.json"]
    assert (files.rendering / "questions-images/math.png").read_bytes() == b"image"
    assert (tmp_path / "questions.pdf").read_bytes() == originals["questions.pdf"]
    assert (tmp_path / "personal-notes.txt").read_bytes() == b"untouched"
    assert not files.legacy_moves()


def test_collision_preserves_both_checkpoints_and_does_not_partially_move(tmp_path):
    (tmp_path / "state.json").write_text("old")
    (tmp_path / "questions.tex").write_text("source")
    files = RunFiles(tmp_path)
    files.support.mkdir()
    files.checkpoint.write_text("new")
    with pytest.raises(ValueError, match="both copies were preserved"):
        files.prepare()
    assert (tmp_path / "state.json").read_text() == "old"
    assert files.checkpoint.read_text() == "new"
    assert (tmp_path / "questions.tex").read_text() == "source"
    assert not files.sources.exists()


def test_log_folder_change_preserves_provider_session(tmp_path):
    original = ModelProvider("model", "test-key", tmp_path / "calls")
    moved = ModelProvider("model", "test-key", tmp_path / "support/diagnostics/calls", session_path=tmp_path / "calls")
    assert moved.session_id == original.session_id


def test_fallback_exports_keep_support_files_out_of_document_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(exporters, "find_latex", lambda: None)
    run = tmp_path / "documents"
    written = exporters.export(export_state(), run, "both")
    assert len(written) == 4 and all(path.is_file() for path in written)
    assert {path.name for path in run.iterdir()} == {"questions.pdf", "answer-key.pdf", "questions.docx", "answer-key.docx", "support"}
    assert (run / "support/sources/questions.tex").is_file()
    assert list((run / "support/rendering/questions-images").glob("*.png"))


def test_native_export_uses_support_workspace_then_delivers_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(exporters, "find_latex", lambda: "pdflatex")
    commands = []
    def compile_document(command, **kwargs):
        from types import SimpleNamespace
        from pathlib import Path
        commands.append(command)
        tex = Path(command[-1])
        tex.with_suffix(".pdf").write_bytes(b"%PDF compiler output")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(exporters.subprocess, "run", compile_document)
    written = exporters.export(export_state(), tmp_path, "pdf")
    assert all(path.parent == tmp_path and path.read_bytes().startswith(b"%PDF") for path in written)
    assert len(commands) == 2
    assert all("support" in command[-1] and "-no-shell-escape" in command for command in commands)
    assert not list((tmp_path / "support/sources").glob("*.pdf"))


def test_export_does_not_relocate_a_library_callers_checkpoint(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_bytes(b"checkpoint")
    exporters.export(export_state(), tmp_path, "docx")
    assert (tmp_path / "state.json").read_bytes() == b"checkpoint"


def test_cli_organizes_legacy_run_and_reexports_without_model_calls(corpus, tmp_path, monkeypatch):
    run = tmp_path / "old-run"
    original = Pipeline(corpus, ScriptedProvider(), run, compute=compute, progress=lambda _: None).run("Generate 1 question")
    def no_credentials(*args, **kwargs):
        raise AssertionError("Completed run must export offline")
    monkeypatch.setattr(cli, "resolve_credentials", no_credentials)
    assert cli.main(["resume", str(run), "--format", "docx"]) == 0
    assert not (run / "state.json").exists()
    saved = json.loads(RunFiles(run).checkpoint.read_text(encoding="utf-8"))
    assert saved["items"] == original["items"]
    assert (run / "questions.docx").is_file()
    assert (run / "summary.html").is_file()
    assert not list(run.glob("*.json"))


def test_interrupted_generation_resumes_from_organized_checkpoint(corpus, tmp_path, monkeypatch):
    class Provider(ScriptedProvider):
        name = "opencode"
        def ask(self, role, payload, schema):
            if role == "generator" and payload["slot"]["id"] == "q2":
                raise ProviderError("Simulated interruption")
            return super().ask(role, payload, schema)
    first = Provider(count=2)
    captured = []
    def factory(model, key, log_dir, **kwargs):
        captured.append(log_dir)
        return first
    monkeypatch.setattr(cli, "ModelProvider", factory)
    monkeypatch.setattr(cli, "resolve_credentials", lambda *a: ("opencode", first.model, "test-key"))
    run = tmp_path / "new-run"
    assert cli.main(["generate", "Generate 2 questions", "--prep-material", "SAT Math", "--dataset", str(corpus.path), "--out", str(run), "--format", "none"]) == 1
    files = RunFiles(run)
    assert captured == [files.diagnostics / "calls"]
    before = json.loads(files.checkpoint.read_text(encoding="utf-8"))
    assert before["status"] == "needs_attention" and list(before["items"]) == ["q1"]
    assert "Simulated interruption" in (run / "summary.html").read_text(encoding="utf-8")
    assert list((files.diagnostics / "items").glob("*.json"))
    assert not list(run.glob("*.json"))
    following = ScriptedProvider(count=2)
    following.name = "opencode"
    monkeypatch.setattr(cli, "ModelProvider", lambda *a, **k: following)
    assert cli.main(["resume", str(run), "--format", "none"]) == 0
    after = json.loads(files.checkpoint.read_text(encoding="utf-8"))
    assert after["status"] == "complete"
    assert following.generated == {"q2": 1}
    assert after["items"]["q1"] == before["items"]["q1"]
    assert "Whole-test audit · Passed" in (run / "summary.html").read_text(encoding="utf-8")


def test_new_generate_cannot_overwrite_a_saved_run(corpus, tmp_path, monkeypatch):
    files = RunFiles(tmp_path / "run")
    files.prepare()
    files.checkpoint.write_bytes(b"keep this checkpoint")
    monkeypatch.setattr(cli, "resolve_credentials", lambda *a: ("opencode", "model", "test-key"))
    assert cli.main(["generate", "Generate 1 question", "--prep-material", "SAT Math", "--dataset", str(corpus.path), "--out", str(files.root)]) == 1
    assert files.checkpoint.read_bytes() == b"keep this checkpoint"


@pytest.mark.parametrize("phase,status,expected", [("done", "complete", "Passed"), ("review", "needs_attention", "Follow-up pending")])
def test_summary_uses_saved_audit_and_escapes_content(corpus, tmp_path, phase, status, expected):
    state = Pipeline(corpus, ScriptedProvider(), tmp_path / "work", compute=compute, progress=lambda _: None).run("Generate 1 question")
    state["status"] = status
    state["audit_work"]["phase"] = phase
    state["plan"]["title"] = '<script>alert("title")</script>'
    state["request"] = '<img src="external" onerror="anything">'
    state["audit"]["summary"] = "Checked <all> questions."
    (tmp_path / "questions.pdf").write_bytes(b"%PDF")
    html = write_summary(state, tmp_path).read_text(encoding="utf-8")
    assert f"Whole-test audit · {expected}" in html
    assert "Checked &lt;all&gt; questions." in html
    assert "<script>" not in html and '<img src="external"' not in html
    assert ('href="questions.pdf"' in html) == (status == "complete")
    assert "Question-level checks" in html


def test_summary_keeps_unresolved_audit_notes_visible(corpus, tmp_path):
    state = Pipeline(corpus, ScriptedProvider(), tmp_path / "work", compute=compute, progress=lambda _: None).run("Generate 1 question")
    state["audit_warnings"] = {"issues": [{"slot_ids": ["q1"], "reason": "More variety would help."}]}
    html = write_summary(state, tmp_path).read_text(encoding="utf-8")
    assert "Completed with notes" in html
    assert "Questions 1" in html and "More variety would help." in html
