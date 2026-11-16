#!/usr/bin/env bash
# graphify openai-oauth provider patch — installer (self-use)
# Usage: ./install.sh <graphify-source-dir>
#   <graphify-source-dir> = a graphify git checkout, or the site-packages
#   directory containing graphify/ (find it with:
#   python3 -c "import graphify,os;print(os.path.dirname(os.path.dirname(graphify.__file__)))")
set -euo pipefail

VERSION="0.1.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$SCRIPT_DIR/0001-feat-add-openai-oauth-backend.patch"

echo "graphify openai-oauth patch v$VERSION"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <graphify-source-dir>" >&2
    echo "Hint: python3 -c \"import graphify,os;print(os.path.dirname(os.path.dirname(graphify.__file__)))\"" >&2
    exit 1
fi
TARGET="$(cd "$1" && pwd)"
[[ -f "$PATCH" ]] || { echo "error: patch file not found next to install.sh" >&2; exit 1; }
[[ -d "$TARGET/graphify" ]] || { echo "error: $TARGET does not contain a graphify/ package" >&2; exit 1; }

if [[ -f "$TARGET/graphify/openai_oauth.py" ]]; then
    echo "Already applied: $TARGET/graphify/openai_oauth.py exists. Nothing to do."
    exit 0
fi

cd "$TARGET"
echo "Target: $TARGET"

if git apply --check "$PATCH" 2>/dev/null; then
    git apply "$PATCH"
    echo "Applied cleanly."
elif git rev-parse --git-dir >/dev/null 2>&1 && git apply --3way "$PATCH"; then
    echo "Applied via 3-way merge."
else
    echo "Clean apply failed; applying with --reject (conflicts left as .rej files)." >&2
    git apply --reject "$PATCH" || true
    echo >&2
    echo "Manual merge needed. Conflicts are confined to small hooks in" >&2
    echo "graphify/llm.py and graphify/cli.py — see README.md 'patch 失败怎么办'." >&2
    echo "New files (openai_oauth.py / tests / docs) never conflict." >&2
    exit 2
fi

echo
echo "Done. Next steps:"
echo "  1. Log in once:  python -m graphify.openai_oauth"
echo "  2. Use it:       graphify extract <dir> --backend openai-oauth"
echo "  Details: docs/openai-oauth.md inside the patched tree, or README.md here."