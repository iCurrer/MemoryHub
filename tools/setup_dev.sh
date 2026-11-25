#!/bin/sh
# One-time dev setup for the code repository. Idempotent.
#
# Enables the boundary hooks and verifies the vault is where it should be.
# Run from anywhere:  sh tools/setup_dev.sh

set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

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

python - "$root" <<'PY'
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
echo "verify: python tools/lint_staged.py --all"
