"""AST syntax scan — source files only (excludes venv*, legacy, __pycache__, .git, tests/test_v*)"""
import ast, os, sys
from pathlib import Path

ROOT = Path(r"c:\Users\gligo\My Projects\OMNI Agent — Production AI Agent System")
SKIP_DIRS = {'.venv', '.venv-1', '.venv-2', 'legacy', '__pycache__', '.git', 'node_modules'}

errors = []
clean = 0
files_scanned = []

for dirpath, dirnames, filenames in os.walk(ROOT):
    # prune skipped dirs in-place
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.venv')]
    rel = Path(dirpath).relative_to(ROOT)
    for fn in filenames:
        if not fn.endswith('.py'):
            continue
        # skip generated test files test_v*.py
        if fn.startswith('test_v') and fn[6:].split('.')[0].isdigit():
            continue
        fpath = Path(dirpath) / fn
        try:
            src = fpath.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(src, filename=str(fpath))
            clean += 1
            files_scanned.append(str(fpath.relative_to(ROOT)))
        except SyntaxError as e:
            errors.append({'file': str(fpath.relative_to(ROOT)), 'line': e.lineno, 'col': e.offset, 'msg': e.msg, 'text': e.text})
        except Exception as e:
            errors.append({'file': str(fpath.relative_to(ROOT)), 'line': None, 'col': None, 'msg': str(e), 'text': None})

print(f"=== AST SCAN RESULTS ===")
print(f"Files scanned (clean): {clean}")
print(f"Files with syntax errors: {len(errors)}")
print()
if errors:
    print("--- SYNTAX ERRORS ---")
    for e in errors:
        loc = f":{e['line']}:{e['col']}" if e['line'] else ""
        print(f"  ERROR {e['file']}{loc}")
        print(f"         {e['msg']}")
        if e['text']:
            print(f"         >> {e['text'].strip()}")
else:
    print("  ✓ No syntax errors detected in source code.")
