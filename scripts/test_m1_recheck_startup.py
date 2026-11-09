"""Repeatable m1-03 validator; uses temporary databases and fixture providers only."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
# The invoking interpreter must have the standalone package installed.

if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([
        str(ROOT/'tests/test_service.py'),
        str(ROOT/'tests/test_recheck.py'), '-q',
    ]))
