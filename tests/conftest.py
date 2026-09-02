import os
import sys

_TESTS_DIR = os.path.dirname(__file__)
_ROOT_DIR = os.path.dirname(_TESTS_DIR)
_FIXTURES_DIR = os.path.join(_TESTS_DIR, "fixtures")

for p in (_ROOT_DIR, _TESTS_DIR, _FIXTURES_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
