#!/usr/bin/env sh
# harness — one-line local install.
#
#   curl -fsSL <raw url of this file> | sh
#   # or, from a checkout:
#   sh scripts/install.sh
#
# What it does, and nothing else:
#   - requires `uv` (installs it via the official installer only if missing
#     and HARNESS_INSTALL_UV=1)
#   - clones (or reuses) the source into ~/.harness/src
#   - installs the `harness` command with `uv tool install`
#
# Everything lives under ~/.harness plus uv's tool directory. Uninstall:
#   uv tool uninstall meta-harness-backend && rm -rf ~/.harness
#
# Candidate evaluation additionally needs Docker and the sandbox image:
#   docker build -t meta-harness-sandbox -f ~/.harness/src/infra/sandbox.Dockerfile ~/.harness/src/infra
set -eu

HARNESS_HOME="${HARNESS_HOME:-$HOME/.harness}"
REPO_URL="${HARNESS_REPO_URL:-https://github.com/ManagementMO/Meta-Harness.git}"
REF="${HARNESS_REF:-main}"

if ! command -v uv >/dev/null 2>&1; then
  if [ "${HARNESS_INSTALL_UV:-0}" = "1" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
  else
    echo "harness needs uv (https://docs.astral.sh/uv/). Re-run with HARNESS_INSTALL_UV=1 to install it." >&2
    exit 1
  fi
fi

mkdir -p "$HARNESS_HOME"
SRC="$HARNESS_HOME/src"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../backend/app/harness_cli.py" ]; then
  SRC="$(cd "$SCRIPT_DIR/.." && pwd)"        # installing from a checkout
elif [ -d "$SRC/.git" ]; then
  git -C "$SRC" fetch --quiet origin "$REF" && git -C "$SRC" checkout --quiet "$REF"
else
  git clone --quiet --branch "$REF" "$REPO_URL" "$SRC"
fi

uv tool install --force --with "$SRC/sdk" "$SRC/backend"

echo
echo "installed: $(uv tool dir --bin)/harness"
echo "next:"
echo "  cd ~/my-repo && harness init && harness wrap claude"
if ! docker image inspect meta-harness-sandbox >/dev/null 2>&1; then
  echo "  (for candidate evaluation) docker build -t meta-harness-sandbox -f $SRC/infra/sandbox.Dockerfile $SRC/infra"
fi
