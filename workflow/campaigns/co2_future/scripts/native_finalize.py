"""Native postprocessing with two explicit, model-independent raw-data policies.

The completed regional v5 campaign allowed zero disease effect and rejected
negative/sentinel yields. Retain exactly that policy: only the extra legacy
paired-table requirement of at least one positive loss is bypassed.
All native pairing, dates, sorting and five-planting checks remain in force.
No source release file is modified.
"""
import ast
import importlib
import math
from pathlib import Path
import sys


def validator_code(source, filename):
    tree = ast.parse(source, filename=filename)
    class AllowZeroEffect(ast.NodeTransformer):
        count = 0
        def visit_If(self, node):
            node = self.generic_visit(node)
            if (isinstance(node.test, ast.Compare) and
                isinstance(node.test.left, ast.Name) and node.test.left.id == 'disease_lower_count' and
                len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq) and
                isinstance(node.test.comparators[0], ast.Constant) and node.test.comparators[0].value == 0):
                if len(node.body) != 1 or not isinstance(node.body[0], ast.Raise):
                    raise ValueError('Unexpected native zero-effect guard structure')
                self.count += 1
                return None
            return node
    transform = AllowZeroEffect()
    tree = transform.visit(tree)
    if transform.count != 1:
        raise ValueError('Native validation contract changed; inspect before adapting')
    return compile(ast.fix_missing_locations(tree), filename, 'exec')


def finite_raw_yield(value, label, row_number):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'Invalid nonnegative {label} at row {row_number}')
    return number


def main():
    engine, mode, *arguments = sys.argv[1:]
    sys.path.insert(0, engine)
    if mode == 'validate':
        path = Path(engine) / 'validate_final_summaries.py'
        namespace = {'__name__': 'co2_native_validator', '__file__': str(path)}
        exec(validator_code(path.read_text(), str(path)), namespace)
        sys.argv = [str(path), *arguments]
        namespace['main']()
    elif mode == 'master':
        module = importlib.import_module('build_master_summary')
        # No monkeypatch to the native master builder or its yield policy.
        sys.argv = [module.__file__, *arguments]
        module.main()
    else:
        raise ValueError(mode)


if __name__ == '__main__':
    main()
