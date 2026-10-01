"""Subject settings shared by every role and saved with each run."""
from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class PrepProfile:
    name: str = "SAT Math"
    source_based: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def prep_profile(name: str = "SAT Math") -> PrepProfile:
    name = " ".join(name.split())
    if not name or len(name) > 160:
        raise ValueError("Please name the exam and subject (for example SAT Math or AP Biology).")
    lowered = name.casefold()
    if re.search(r"\b(?:digital\s+)?sat\b", lowered):
        math = bool(re.search(r"\bmath(?:s|ematics)?\b", lowered))
        verbal = bool(re.search(r"\b(?:verbal|reading|writing|english|r&w)\b", lowered))
        if math == verbal:
            raise ValueError("Please explicitly choose SAT Math or SAT Verbal for this dataset.")
        return PrepProfile("SAT Math" if math else "SAT Verbal", verbal)
    # Reading/language/history material retains its source. Unrecognized subjects
    # also default to adaptation instead of inventing source-dependent content.
    language = re.search(r"\b(?:verbal|reading|writing|english|literature|language|history)\b", lowered)
    quantitative = re.search(r"\b(?:math(?:s|ematics)?|algebra|geometry|calculus|statistics|physics|chemistry|biology|science|economics|computer science)\b", lowered)
    return PrepProfile(name, bool(language) or not bool(quantitative))


def saved_profile(state: dict) -> PrepProfile:
    data = state.get("prep_profile")
    return PrepProfile(**data) if data else PrepProfile()
