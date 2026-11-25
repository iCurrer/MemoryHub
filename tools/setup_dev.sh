#!/bin/sh
# One-time dev setup for the code repository. Idempotent.
#
# Creates the virtualenv the project needs (pyyaml + mcp), enables the boundary
# hooks, and verifies the vault is where it should be.
# Run from anywhere:  sh tools/setup_dev.sh

set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

# The system interpreter usually lacks pyyaml/mcp, and a bare `python` in the
# MCP client config fails silently ("cannot connect") instead of saying so.
if [ -x .venv/Scripts/python.exe ]; then
  py=".venv/Scripts/python.exe"
elif [ -x .venv/bin/python ]; then
  py=".venv/bin/python"
else
  echo "venv         creating .venv"
  python -m venv .venv
  if [ -x .venv/Scripts/python.exe ]; then py=".venv/Scripts/python.exe"; else py=".venv/bin/python"; fi
  "$py" -m pip install --quiet --disable-pip-version-check "mcp>=2.0,<3" "pyyaml>=6.0" \
    || echo "             install failed -- use a proxy if the index is unreachable:"
  #  echo "             $py -m pip install 'mcp>=2.0,<3' 'pyyaml>=6.0' --proxy http://127.0.0.1:7897"
fi

echo "python       $py"

git config core.hooksPath tools/hooks
chmod +x tools/hooks/pre-commit tools/hooks/commit-msg 2>/dev/null || true

echo "hooks        core.hooksPath = tools/hooks"
echo "             pre-commit -> boundary + PII scan (staged files)"
echo "             commit-msg -> PII scan (commit message)"

if [ -n "${MEMORYHUB_VAULT:-}" ]; then
  echo "vault        MEMORYHUB_VAULT=$MEMORYHUB_VAULT"
else
  echo "vault        (unset; commands read ~/.memoryhub/config.toml)"
fi

PYTHONPATH="$root/src" "$py" - "$root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from memoryhub import config, boundary

vault = config.resolve_vault()
problems = boundary.violations(vault)
print(f"boundary     vault={vault}")
if problems:
    for problem in problems:
        print(f"             FAIL {problem}")
    raise SystemExit(1)
print("             OK (vault is outside this repository)")
PY

echo
echo "verify: PYTHONPATH=src $py tools/lint_staged.py --all"
echo "        PYTHONPATH=src $py -m unittest discover -s tests -q"
