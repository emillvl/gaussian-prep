from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import sympy as sp

from .expressions import ExpressionReader, MathUnsupported, equal, equivalent
from .models import MathModel, Question
from .identity import IdentityDomain


def _acute_trig_equation(equation, domain):
    """sin(A)=cos(B) iff A+B=pi/2 when both angles are explicitly acute."""
    sines, cosines = list(equation.atoms(sp.sin)), list(equation.atoms(sp.cos))
    if len(sines) != 1 or len(cosines) != 1:
        return equation
    sine, cosine = sines[0], cosines[0]
    if sp.simplify(equation - sine + cosine) != 0 and sp.simplify(equation + sine - cosine) != 0:
        return equation
    a, b = sine.args[0], cosine.args[0]
    try:
        if all(domain.is_subset(sp.And(angle > 0, angle < sp.pi / 2).as_set()) is True for angle in (a, b)):
            return a + b - sp.pi / 2
    except (NotImplementedError, ValueError, TypeError):
        pass
    return equation


def _computed_targets(model: MathModel, reader: ExpressionReader):
    if model.mode == "unsupported":
        raise MathUnsupported(model.unsupported_reason or "The question cannot be represented in the supported expression language")
    target = reader.read(model.target)
    equations = [reader.read(e.lhs) - reader.read(e.rhs) for e in model.equations]
    relation_functions = {"gt": sp.Gt, "ge": sp.Ge, "lt": sp.Lt, "le": sp.Le, "ne": sp.Ne, "eq": sp.Eq}
    constraints = [relation_functions[c.operator](reader.read(c.lhs), reader.read(c.rhs)) for c in model.constraints]
    if model.mode == "evaluate":
        if target.free_symbols or model.variables or equations or constraints:
            raise MathUnsupported("Evaluate mode must be a fully specified numeric computation")
        return [target], []
    if model.mode == "identity":
        if equations or model.aggregate != "each":
            raise MathUnsupported("Identity mode compares one expression against the choices")
        return [target], []
    if model.mode != "solve" or not equations or not reader.symbols:
        raise MathUnsupported(model.unsupported_reason or "Missing equations or unsupported problem type")
    variables = list(reader.symbols.values())
    if len(variables) == 1:
        domain = sp.S.Reals
        for constraint in constraints:
            try:
                domain = sp.Intersection(domain, constraint.as_set())
            except (NotImplementedError, ValueError, TypeError):
                pass
        roots = domain
        for equation in equations:
            bounded_equation = _acute_trig_equation(equation, domain)
            roots = sp.Intersection(roots, sp.solveset(bounded_equation, variables[0], domain=domain))
        if isinstance(roots, sp.FiniteSet) or roots == sp.EmptySet:
            candidates = [{variables[0]: root} for root in roots]
        else:
            raise MathUnsupported("Could not establish a complete finite solution set; include the stated interval constraints")
    else:
        roots = sp.nonlinsolve(equations, variables)
        if not isinstance(roots, sp.FiniteSet) and roots != sp.EmptySet:
            raise MathUnsupported("System did not yield a finite solution set")
        candidates = [dict(zip(variables, values, strict=True)) for values in roots]
    targets, solutions = [], []
    for solution in candidates:
        if any(v.free_symbols for v in solution.values()):
            raise MathUnsupported("System is underdetermined")
        if any(v.is_real is False for v in solution.values()):
            continue
        if any(v.is_real is None for v in solution.values()):
            raise MathUnsupported("Could not establish whether a solution is real")
        if any(equal(denominator.subs(solution), sp.S.Zero) for denominator in reader.nonzero):
            continue
        if any(not equal(equation.subs(solution), sp.S.Zero) for equation in equations):
            continue
        checks = [sp.simplify(c.subs(solution)) for c in constraints]
        if any(c is sp.false or c is False for c in checks):
            continue
        if any(c is not sp.true and c is not True for c in checks):
            raise MathUnsupported("A problem constraint could not be resolved")
        value = sp.simplify(target.subs(solution))
        if value.free_symbols or value.is_real is not True or value.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
            raise MathUnsupported("The requested quantity is not a finite real value")
        if not any(equal(value, previous) for previous in targets):
            targets.append(value)
        solutions.append({str(k): str(v) for k, v in solution.items()})
    if model.aggregate == "count":
        return [sp.Integer(len(targets))], solutions
    if not targets:
        raise MathUnsupported("No valid values for the requested quantity")
    reducers = {"sum": lambda t: sum(t), "product": sp.prod, "min": sp.Min, "max": sp.Max}
    if model.aggregate in {"min", "max"}:
        targets = [reducers[model.aggregate](*targets)]
    elif model.aggregate in reducers:
        targets = [reducers[model.aggregate](targets)]
    return targets, solutions


def compute(question: Question, model: MathModel) -> dict:
    evidence = {"status": "unverified", "verification_version": 2, "engine": f"SymPy {sp.__version__}", "interpretation": model.interpretation, "issues": [], "computed_targets": [], "solutions": [], "options": []}
    try:
        if question.format == "FRQ":
            raise MathUnsupported("Written responses require independent subject reasoning and item review")
        identity = IdentityDomain(model) if model.mode == "identity" else None
        reader = identity.reader if identity else ExpressionReader(model.variables)
        targets, solutions = _computed_targets(model, reader)
        if identity:
            identity.include_target()
        evidence.update(computed_targets=[str(t) for t in targets], solutions=solutions)
        if any(t.is_real is False or t.has(sp.zoo, sp.nan, sp.oo, -sp.oo) for t in targets):
            raise MathUnsupported("Result is not a finite real value")
        if question.format == "SPR":
            if model.mode == "identity" or any(t.free_symbols for t in targets):
                raise MathUnsupported("SPR needs numeric answers")
            key = ExpressionReader([]).read(question.answer)
            if not any(equal(key, t) for t in targets):
                evidence["issues"].append("Generated answer does not match any computed valid answer")
        else:
            displayed_labels = sorted(c["label"] if isinstance(c, dict) else c.label for c in question.choices)
            if sorted(o.label for o in model.options) != displayed_labels:
                raise MathUnsupported("Verifier must interpret all displayed choices")
            option_values = {}
            symbols = list(reader.symbols.values())
            for option in model.options:
                option_reader = ExpressionReader(model.variables)
                value = option_reader.read(option.expression) if not identity else None
                if model.mode != "identity" and value.free_symbols:
                    raise MathUnsupported("A numeric-answer choice still has free variables")
                if identity:
                    value, verdict = identity.compare(targets[0], option.expression)
                    if verdict is None:
                        raise MathUnsupported("Could not decide whether a choice is equivalent to the target")
                    is_correct = verdict
                else:
                    is_correct = any(equal(value, t) for t in targets)
                option_values[option.label] = value
                evidence["options"].append({"label": option.label, "value": str(value), "correct": is_correct})
            correct = [o["label"] for o in evidence["options"] if o["correct"]]
            if correct != [question.answer]:
                evidence["issues"].append(f"Expected exactly key {question.answer}; computed correct choices: {correct}")
            pairs = list(option_values.items())
            for index, (label, value) in enumerate(pairs):
                for other_label, other in pairs[index + 1:]:
                    expressions = {option.label: option.expression for option in model.options}
                    same = identity.duplicates(expressions[label], expressions[other_label]) if identity else equal(value, other)
                    if same is None:
                        raise MathUnsupported("Could not establish that the distractors are distinct")
                    if same:
                        evidence["issues"].append(f"Choices {label} and {other_label} are mathematically equivalent")
        evidence["status"] = "fail" if evidence["issues"] else "pass"
    except (MathUnsupported, ValueError, TypeError, NotImplementedError, ZeroDivisionError, RecursionError) as exc:
        evidence["issues"].append(str(exc) or type(exc).__name__)
    return evidence


def verify(question: Question, model: MathModel, timeout: float = 12) -> dict:
    """Run the bounded expression interpreter in a killable child process.

    Uses temporary files instead of pipes: subprocess.run's threaded pipe readers can deadlock on
    Windows when a very short timeout kills the worker mid-import, leaving the reader threads waiting.
    """
    payload = json.dumps({"question": question.model_dump(), "model": model.model_dump()}).encode("utf-8")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(payload)
        stdin.seek(0)
        try:
            process = subprocess.Popen([sys.executable, "-m", "gaussian_prep.math_worker"], stdin=stdin, stdout=stdout, stderr=stderr, **kwargs)
        except OSError:
            return {"status": "unverified", "issues": ["Computation worker did not start"]}
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"status": "unverified", "issues": [f"Computation exceeded {timeout:g} seconds"]}
            if process.returncode != 0:
                return {"status": "unverified", "issues": ["Computation worker failed; see the saved mathematical model"]}
            stdout.seek(0)
            try:
                return json.loads(stdout.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {"status": "unverified", "issues": ["Computation worker did not return valid evidence"]}
        finally:
            # Kill on timeout or interruption (for example Ctrl+C) so no worker is left behind.
            if process.poll() is None:
                process.kill()
                process.wait()
