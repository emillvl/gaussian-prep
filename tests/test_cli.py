import pytest

from gaussian_prep import cli
from gaussian_prep.cli import choose_format, choose_request, dataset_path, example_requests
from conftest import ScriptedProvider


def test_example_prompts_are_shown(capsys, monkeypatch):
    monkeypatch.setattr(cli, "example_requests", lambda: [("Mock test", "A mock [TOPIC] request.")])
    monkeypatch.setattr("builtins.input", lambda _: "Generate 2 questions")
    assert choose_request() == "Generate 2 questions"
    output = capsys.readouterr().out
    assert "A mock [TOPIC] request." in output


def test_example_requests_parses_marked_lines(tmp_path):
    path = tmp_path / "PROMPTS.md"
    path.write_text("# guide\nEXAMPLE MOCK: One.\nEXAMPLE PRACTICE: Two.\nnot a marker\n", encoding="utf-8")
    assert example_requests(path) == [("Mock test", "One."), ("Practice test", "Two.")]


def test_blank_request_is_rejected(monkeypatch):
    monkeypatch.setattr(cli, "example_requests", lambda: [])
    monkeypatch.setattr("builtins.input", lambda _: "   ")
    with pytest.raises(ValueError, match="enter a request"):
        choose_request()


@pytest.mark.parametrize("reply,expected", [("", "pdf"), ("1", "pdf"), ("2", "docx"), ("3", "both"), ("4", "none")])
def test_format_choice(reply, expected, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: reply)
    assert choose_format(True) == expected


def test_non_interactive_format_skips_export():
    assert choose_format(False) == "none"


def test_dataset_path_rejects_missing(tmp_path):
    with pytest.raises(ValueError, match="Dataset not found"):
        dataset_path(tmp_path / "missing.json")


def test_generate_flow_asks_for_key_and_exports(corpus, tmp_path, monkeypatch, capsys):
    for name in ("OPENCODE_MODEL", "OPENCODE_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    class Provider(ScriptedProvider):
        name = "opencode"

    def factory(*args, **kwargs):
        return Provider()

    replies = iter(["SAT Math", "Generate 1 question", "1", "test-model", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(cli, "getpass", lambda _: "hidden-key")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "ModelProvider", factory)

    run_dir = tmp_path / "run"
    code = cli.main(["generate", "--dataset", str(corpus.path), "--out", str(run_dir)])
    assert code == 0
    assert (run_dir / "questions.docx").is_file()
    assert (run_dir / "answer-key.docx").is_file()
    output = capsys.readouterr().out
    assert "Gaussian Prep" in output
    assert "Sheet:" in output
