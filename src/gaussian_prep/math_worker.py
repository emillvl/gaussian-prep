import json
import sys

from .models import MathModel, Question
from .verification import compute


def main():
    data = json.load(sys.stdin)
    result = compute(Question.model_validate(data["question"]), MathModel.model_validate(data["model"]))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
