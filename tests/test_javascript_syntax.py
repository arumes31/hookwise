"""Every first-party script must at least parse.

A syntax error in one script silently disables every feature it wires up; the
Python test suite never loads most of these files.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_JS = Path(__file__).resolve().parent.parent / "static" / "js"
SCRIPTS = sorted(path for path in STATIC_JS.glob("*.js") if not path.name.endswith(".min.js"))


def test_first_party_scripts_are_found():
    assert SCRIPTS, f"no first-party scripts found in {STATIC_JS}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_script_parses(script: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
