"""Stable run folders, with a lossless upgrade for older saved runs."""
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class RunFiles:
    root: Path

    @property
    def support(self) -> Path:
        return self.root / "support"

    @property
    def checkpoint(self) -> Path:
        return self.support / "state.json"

    @property
    def diagnostics(self) -> Path:
        return self.support / "diagnostics"

    @property
    def sources(self) -> Path:
        return self.support / "sources"

    @property
    def rendering(self) -> Path:
        return self.support / "rendering"

    @property
    def recovery(self) -> Path:
        return self.support / "recovery"

    def legacy_moves(self) -> list[tuple[Path, Path]]:
        """Move only known app artifacts; never merge or overwrite a destination."""
        moves = []
        if not self.root.is_dir():
            return moves
        for path in self.root.iterdir():
            name = path.name
            target = None
            if name in {"state.json", "test.json"} and path.is_file():
                target = self.support / name
            elif name in {"calls", "items"} and path.is_dir():
                target = self.diagnostics / name
            elif path.is_file() and (name == "dataset-report.json" or re.fullmatch(r"(?:plan-attempt-\d+|audit-\d{8}T\d+)[.]json", name)):
                target = self.diagnostics / name
            elif path.is_file() and re.fullmatch(r"state-before-recovery-\d{8}T\d+[.]json", name):
                target = self.recovery / name
            elif path.is_file() and re.fullmatch(r"(?:questions|answer-key)[.](?:tex|aux|log|out)", name):
                target = self.sources / name
            elif name in {".questions-images", ".answer-key-images"} and path.is_dir():
                target = self.rendering / name[1:]
            if target is not None:
                root = self.root.resolve()
                if not path.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root):
                    raise ValueError("Run support paths must stay inside the saved run")
                if path.is_symlink() or target.is_symlink():
                    raise ValueError("Cannot reorganize linked run artifacts")
                if target.exists():
                    raise ValueError(f"Cannot reorganize {name}: {target} already exists; both copies were preserved")
                moves.append((path, target))
        # Keep the old checkpoint available until other moves have completed.
        return sorted(moves, key=lambda pair: pair[0].name == "state.json")

    def prepare(self, migrate: bool = True) -> None:
        root = self.root.resolve()
        for folder in (self.support, self.diagnostics, self.sources, self.rendering, self.recovery):
            if not folder.resolve().is_relative_to(root):
                raise ValueError("Run support paths must stay inside the saved run")
        moves = self.legacy_moves() if migrate else []  # Validate before changing anything.
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        self.support.mkdir(parents=True, exist_ok=True)
        (self.support / "README.txt").write_text(
            "Gaussian Prep support files\n\n"
            "Keep this folder with the run to resume generation or export again.\n"
            "state.json: saved questions, checks, audit, and progress.\n"
            "diagnostics/: model-call logs, question attempts, and audit history.\n"
            "sources/: editable LaTeX sources and compiler diagnostics.\n"
            "rendering/: images used to build PDF documents.\n"
            "recovery/: original checkpoints preserved before recovery.\n\n"
            "The documents and summary in the parent folder are the finished outputs.\n",
            encoding="utf-8",
        )
