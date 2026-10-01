from __future__ import annotations

import argparse
from datetime import datetime
from getpass import getpass
import json
import os
from pathlib import Path
import sys

from .corpus import Corpus, read_records
from .pipeline import Pipeline, PipelineError, RequestConflict, save_json
from .prep import prep_profile, saved_profile
from .provider import ModelProvider, PROVIDERS, ProviderError
from .run_files import RunFiles
from .run_summary import write_summary

GUIDE_FILE = Path("PROMPTS.md")


def show_banner() -> None:
    banner = Path(__file__).with_name("banner.txt").read_text(encoding="ascii")
    print("\n" + banner.rstrip("\n") + "\n")


def guide_text(path: Path = GUIDE_FILE) -> str:
    """The editable request guide; conventions live in the file, not in this code."""
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def example_requests(path: Path = GUIDE_FILE) -> list[tuple[str, str]]:
    """Universal mock and practice prompts shown after the banner; editable in the guide."""
    markers = (("EXAMPLE MOCK:", "Mock test"), ("EXAMPLE PRACTICE:", "Practice test"))
    examples = []
    for line in guide_text(path).splitlines():
        stripped = line.strip()
        for prefix, label in markers:
            if stripped.startswith(prefix):
                examples.append((label, stripped[len(prefix):].strip()))
    return examples


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="gaussian-prep", description="Build exam practice from your dataset with five collaborating roles.")
    commands = cli.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="Check the dataset without making model calls")
    inspect.add_argument("dataset", type=Path, nargs="?")
    inspect.add_argument("--report", type=Path)
    inspect.add_argument("--prep-material", help="Exam and subject, for example AP Biology")
    generate = commands.add_parser("generate", help="Generate from a free-form request, or enter it interactively")
    generate.add_argument("request", nargs="?", help="Your request in quotes; omit to type it at a prompt")
    generate.add_argument("--dataset", type=Path)
    generate.add_argument("--prep-material", help="Exam and subject; specify SAT Math or SAT Verbal for SAT material")
    generate.add_argument("--out", type=Path)
    generate.add_argument("--provider", choices=list(PROVIDERS), help="Provider for all roles; asked at the prompt when omitted")
    generate.add_argument("--model", help="One shared model for all roles; asked at the prompt when omitted")
    generate.add_argument("--format", choices=["pdf", "docx", "both", "none"], help="Export format; asked at the prompt when omitted")
    generate.add_argument("--max-calls", type=int, default=400)
    generate.add_argument("--revisions", type=int, default=2)
    generate.add_argument("--audit-revisions", type=int, default=1)
    resume = commands.add_parser("resume", help="Continue a saved run using its original dataset and model")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--format", choices=["pdf", "docx", "both", "none"], help="Export format; asked at the prompt when omitted")
    resume.add_argument("--max-calls", type=int, default=400)
    resume.add_argument("--revisions", type=int, default=2)
    resume.add_argument("--audit-revisions", type=int, default=1)
    return cli


def dataset_path(value) -> Path:
    value = value or os.getenv("GAUSSIAN_DATASET") or os.getenv("SATPREP_DATASET")
    if value:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Dataset not found: {path}")
        return path
    candidates = []
    for path in sorted(Path.cwd().iterdir()):
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".csv"}:
            try:
                rows = read_records(path)
            except (ValueError, OSError, UnicodeError):
                continue
            if rows and all(isinstance(row, dict) for row in rows) and any(any(key in row for key in ("question", "stem", "prompt")) for row in rows):
                candidates.append(path.resolve())
    if len(candidates) == 1:
        print(f"Dataset found: {candidates[0].name}")
        return candidates[0]
    print("Please provide us with a dataset." if not candidates else "Several datasets were found. Please choose the one to use.")
    return pick_dataset()


def pick_dataset() -> Path:
    """A native file chooser, opened by the running app rather than a path prompt."""
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(parent=root, title="Please provide us with a dataset.", initialdir=str(Path.cwd()), filetypes=[("Question datasets", "*.json *.jsonl *.csv"), ("All files", "*.*")])
    except (ImportError, RuntimeError) as exc:
        raise ValueError("The dataset chooser could not open. Install Python with Tcl/Tk support, or use --dataset for unattended runs.") from exc
    except Exception as exc:
        raise ValueError(f"The dataset chooser could not open: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()
    if not selected:
        raise ValueError("Dataset selection cancelled; no run was started.")
    path = Path(selected).resolve()
    if not path.is_file():
        raise ValueError("The selected dataset is no longer available.")
    return path


def choose_prep_material():
    print("Can you please let us know what prep material is this?")
    print("Name the exam and subject, e.g. AP Biology, A-Levels Mathematics, ACT English, GCSE Physics, or CSCA Mathematics.")
    print("For SAT, explicitly enter SAT Math or SAT Verbal.")
    while True:
        try:
            return prep_profile(input("Prep material: "))
        except ValueError as exc:
            print(exc)


def print_report(report):
    print(f"Dataset: {report['total_records']:,} records")
    print(f"Loaded: {report['records_loaded']:,} / {report['total_records']:,}; discarded: {report['discarded_records']}")
    print(f"Advisory quality notes: {report['records_with_notes']:,} records (all retained)")
    for domain, count in report["by_domain"].items():
        print(f"  {domain}: {count:,}")


def choose_request() -> str:
    print("Gaussian Prep — describe the practice you want.")
    examples = example_requests()
    if examples:
        print(f"Example requests (replace the [BRACKETS]; full guide in {GUIDE_FILE}):")
        for label, example in examples:
            print(f'  {label}: "{example}"')
    else:
        print(f"See {GUIDE_FILE} for request shapes and examples.")
    request = input("> ").strip()
    if not request:
        raise ValueError("Please enter a request")
    return request


def choose_provider() -> str:
    slugs = list(PROVIDERS)
    print("Provider for every role:")
    for index, slug in enumerate(slugs, start=1):
        suffix = " (default)" if slug == "opencode" else ""
        print(f"  {index:>2}. {PROVIDERS[slug]['label']}{suffix}")
    selection = input("Provider [1]: ").strip().lower()
    if not selection:
        return "opencode"
    if selection in PROVIDERS:
        return selection
    if selection.isdigit() and 1 <= int(selection) <= len(slugs):
        return slugs[int(selection) - 1]
    raise ProviderError(f"Enter a number from 1 to {len(slugs)}, or type a provider name")


def choose_model(provider: str) -> str:
    print(f"Enter a model ID available in your {PROVIDERS[provider]['label']} account. It serves every role.")
    model = input("Model ID: ").strip()
    if provider == "opencode":
        model = model.removeprefix("opencode-go/")
    if not model:
        raise ProviderError("A model ID is required")
    return model


def choose_key(provider: str) -> str:
    key = getpass(f"{PROVIDERS[provider]['label']} API key (hidden, kept only for this run): ").strip()
    if not key:
        raise ProviderError("An API key is required")
    return key


def resolve_credentials(provider: str | None = None, model: str | None = None) -> tuple[str, str, str]:
    provider = provider or choose_provider()
    spec = PROVIDERS[provider]
    prefix = spec["prefix"]
    model = model or os.getenv(f"{prefix}_MODEL", "") or choose_model(provider)
    key = os.getenv(f"{prefix}_API_KEY", "")
    if not key and not spec.get("keyless"):
        key = choose_key(provider)
    return provider, model, key


def choose_format(interactive: bool = True) -> str:
    if not interactive:
        return "none"
    print("Export the finished sheet:")
    print("  1. PDF")
    print("  2. DOCX")
    print("  3. Both")
    print("  4. Skip")
    while True:
        selection = input("Format [1]: ").strip().lower()
        if selection in {"", "1", "pdf"}:
            return "pdf"
        if selection in {"2", "docx"}:
            return "docx"
        if selection in {"3", "both"}:
            return "both"
        if selection in {"4", "none", "skip"}:
            return "none"
        print("Enter 1, 2, 3, or 4.")


def export_run(state: dict, run_dir: Path, fmt: str) -> int:
    from .export import export

    try:
        written = export(state, run_dir, fmt)
    finally:
        save_run_summary(state, run_dir)
    if written:
        for path in written:
            print(f"Sheet: {path}")
    else:
        print("Export skipped.")
    return 0


def save_run_summary(state: dict, run_dir: Path) -> None:
    try:
        print(f"Test summary: {write_summary(state, run_dir)}")
    except OSError as exc:
        # A summary failure must not hide the original generation/export error.
        print(f"Could not write the test summary: {exc}", file=sys.stderr)


def generate_run(corpus: Corpus, request: str, provider_name: str, model: str, key: str, run_dir: Path, args, resume: bool = False) -> int:
    if not corpus.references:
        raise ValueError("The dataset is empty")
    print_report(corpus.report())
    print(f"Prep material: {corpus.profile.name}; {'adapt existing source questions' if corpus.profile.source_based else 'create new questions'}.")
    files = RunFiles(run_dir)
    if not resume and (files.checkpoint.exists() or (run_dir / "state.json").exists()):
        raise ValueError("Run already exists. Use resume or choose a new output directory")
    files.prepare()
    provider = ModelProvider(model, key, files.diagnostics / "calls", max_calls=args.max_calls, provider=provider_name, session_path=run_dir / "calls")
    save_json(files.diagnostics / "dataset-report.json", corpus.report())
    print(f"Provider: {PROVIDERS[provider_name]['label']}; shared model: {provider.model}")
    print(f"Saved run: {run_dir}")
    pipeline = Pipeline(corpus, provider, files.support, revisions=args.revisions, audit_revisions=args.audit_revisions, request_guide=guide_text(), diagnostics_dir=files.diagnostics)
    try:
        for attempt in range(3):
            try:
                state = pipeline.run(request, resume=resume or attempt > 0)
                break
            except RequestConflict:
                raise
            except (PipelineError, ProviderError) as exc:
                if pipeline.state.get("status") != "needs_attention":
                    raise
                if attempt == 2:
                    print("Automatic retries exhausted. Accepted questions and pending audit scope are saved.")
                    print(f'Continue when ready: .\\run.ps1 resume "{run_dir}"')
                    raise
                pipeline.state.setdefault("automatic_retries", []).append({"attempt": attempt + 1, "reason": str(exc)})
                pipeline.checkpoint()
                print(f"Automatic retry {attempt + 1}/2: continuing unfinished questions or the pending audit.")
    finally:
        if pipeline.state:
            save_run_summary(pipeline.state, run_dir)
    if state.get("audit_warnings"):
        print("Audit notes kept as warnings (the test still passed per-item checks):")
        for issue in state["audit_warnings"].get("issues", []):
            slots = ", ".join(issue.get("slot_ids", [])) or "whole test"
            print(f"  - {issue.get('dimension', 'audit')}: {issue.get('reason', '')} [{slots}]")
    fmt = args.format or choose_format(sys.stdin.isatty())
    return export_run(state, run_dir, fmt)


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command is None:
        args = cli.parse_args(["generate"])
    try:
        if args.command == "inspect":
            corpus = Corpus(dataset_path(args.dataset), prep_profile(args.prep_material) if args.prep_material else None)
            report = corpus.report()
            print_report(report)
            if args.report:
                save_json(args.report, report)
                print(f"Report saved: {args.report.resolve()}")
            return 0
        if args.command == "resume":
            run_dir = args.run_dir.resolve()
            files = RunFiles(run_dir)
            if not files.checkpoint.is_file() and not (run_dir / "state.json").is_file():
                raise ValueError(f"No saved run at {run_dir}")
            files.prepare()
            saved = json.loads(files.checkpoint.read_text(encoding="utf-8"))
            source_path = Path(saved["dataset"]).expanduser()
            if not source_path.is_file():
                print("Please provide us with a dataset.")
                print("Choose the original dataset to continue this saved run.")
                source_path = pick_dataset()
            corpus = Corpus(source_path, saved_profile(saved))
            from .recovery import recover_saved_run
            saved = recover_saved_run(corpus, files.support, backup_dir=files.recovery)
            provider_name = saved.get("provider", "opencode")
            if saved.get("status") == "complete":
                fmt = args.format or choose_format(sys.stdin.isatty())
                return export_run(saved, run_dir, fmt)
            _, _, key = resolve_credentials(provider_name, saved["model"])
            return generate_run(corpus, saved["request"], provider_name, saved["model"], key, run_dir, args, resume=True)
        path = dataset_path(args.dataset)
        profile = prep_profile(args.prep_material) if args.prep_material else choose_prep_material()
        corpus = Corpus(path, profile)
        if sys.stdin.isatty():
            show_banner()
        request = args.request or choose_request()
        if not request.strip():
            raise ValueError("Please enter a request")
        provider_name, model, key = resolve_credentials(args.provider, args.model)
        run_dir = (args.out or Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")).resolve()
        return generate_run(corpus, request, provider_name, model, key, run_dir, args)
    except KeyboardInterrupt:
        print("\nStopped. Accepted questions are saved; use resume to continue.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, KeyError, EOFError) as exc:
        print(f"Gaussian Prep: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
