"""Domain-aware symbolic identities, with counterexamples rather than sampled proofs."""
from __future__ import annotations

import sympy as sp

from .expressions import ExpressionReader, MathUnsupported, equivalent
from .models import MathModel

RELATIONS = {"gt": sp.Gt, "ge": sp.Ge, "lt": sp.Lt, "le": sp.Le, "ne": sp.Ne, "eq": sp.Eq}


def _sets(symbols, conditions):
    domains = {symbol: sp.S.Reals for symbol in symbols}
    for condition in conditions:
        if condition == sp.false:
            raise MathUnsupported("The stated identity domain is empty")
        if len(condition.free_symbols) != 1:
            continue
        symbol = next(iter(condition.free_symbols))
        try:
            domains[symbol] = domains[symbol].intersect(condition.as_set())
        except (NotImplementedError, ValueError, TypeError):
            continue
        if domains[symbol] == sp.EmptySet:
            raise MathUnsupported("The stated identity domain is empty")
    return domains


class IdentityDomain:
    def __init__(self, model: MathModel):
        original = ExpressionReader(model.variables)
        conditions = [RELATIONS[c.operator](original.read(c.lhs), original.read(c.rhs)) for c in model.constraints]
        self.explicit_domains = _sets(original.symbols.values(), conditions)
        self.reader = ExpressionReader(model.variables)
        for name, symbol in original.symbols.items():
            domain = self.explicit_domains[symbol]
            assumptions = {"real": True}
            if domain.is_subset(sp.Interval.open(0, sp.oo)) is True:
                assumptions["positive"] = True
            elif domain.is_subset(sp.Interval(0, sp.oo)) is True:
                assumptions["nonnegative"] = True
            elif domain.is_subset(sp.Interval.open(-sp.oo, 0)) is True:
                assumptions["negative"] = True
            self.reader.symbols[name] = sp.Symbol(name, **assumptions)
        substitution = {old: self.reader.symbols[name] for name, old in original.symbols.items()}
        # Keep explicit constraints as well as assumptions for counterexample filtering.
        self.conditions = [c.xreplace(substitution) for c in conditions]
        self.domains = {self.reader.symbols[s.name]: domain for s, domain in self.explicit_domains.items()}
        self.conditions.extend(sp.Contains(symbol, domain, evaluate=False) for symbol, domain in self.domains.items())

    def include_target(self):
        self.conditions.extend(self.reader.conditions)
        inferred = _sets(self.reader.symbols.values(), self.conditions)
        for symbol, domain in inferred.items():
            self.domains[symbol] = self.domains[symbol].intersect(domain)
            if self.domains[symbol] == sp.EmptySet:
                raise MathUnsupported("The target is undefined throughout the stated domain")

    def compare(self, target, expression: str):
        option = ExpressionReader(list(self.reader.symbols))
        option.symbols = dict(self.reader.symbols)
        value = option.read(expression)
        defined = True
        for condition in option.conditions:
            if condition == sp.true or condition in self.conditions:
                continue
            if condition == sp.false:
                return value, False
            if len(condition.free_symbols) == 1:
                symbol = next(iter(condition.free_symbols))
                try:
                    missing = self.domains[symbol] - condition.as_set()
                    subset = missing.is_empty
                except (NotImplementedError, ValueError, TypeError):
                    subset = None
                if subset is False:
                    return value, False
                if subset is True:
                    continue
            defined = False
        verdict = equivalent(target, value, list(option.symbols.values()), self.conditions)
        if verdict is True and not defined:
            verdict = None
        return value, verdict

    def duplicates(self, first: str, second: str):
        readers = [ExpressionReader(list(self.reader.symbols)) for _ in range(2)]
        for reader in readers:
            reader.symbols = dict(self.reader.symbols)
        values = [reader.read(expression) for reader, expression in zip(readers, (first, second))]
        conditions = self.conditions + readers[0].conditions + readers[1].conditions
        verdict = equivalent(*values, list(self.reader.symbols.values()), conditions)
        if verdict is not True:
            return verdict
        domains = []
        for reader in readers:
            own = _sets(reader.symbols.values(), reader.conditions)
            domains.append({s: d.intersect(self.domains[s]) for s, d in own.items()})
        if domains[0] != domains[1]:
            return False
        multi = [{c for c in reader.conditions if len(c.free_symbols) > 1} for reader in readers]
        return True if multi[0] == multi[1] else None
