#!/usr/bin/env bash
# Record the clean-container quickstart: uvx-install turnstile from a git tag in
# a fresh container with no ambient state, then render an asciinema GIF. Proves
# a brand-new user's one-command install path works end to end.
#
# Post-flip (produces the shippable asset from the public tag):
#   scripts/record_quickstart.sh
#     -> docs/assets/quickstart.cast + docs/assets/quickstart.gif
#
# Dry run before the repo is public (records against the local checkout; the GIF
# shows a file:// URL, so it is a rehearsal, not the shippable asset):
#   scripts/record_quickstart.sh "file://$PWD" v0.1.0 /tmp/quickstart-out
#
# Requires: docker, asciinema, agg.
set -euo pipefail

REPO_URL="${1:-https://github.com/PaperArmada/turnstile.git}"
REF="${2:-v0.1.0}"
OUT_DIR="${3:-docs/assets}"
IMG="ghcr.io/astral-sh/uv:python3.12-bookworm-slim"

mkdir -p "$OUT_DIR"
CAST="$OUT_DIR/quickstart.cast"
GIF="$OUT_DIR/quickstart.gif"
FROM="turnstile-cli @ git+${REPO_URL}@${REF}#subdirectory=packages/turnstile-cli"

# Inner script executed inside the clean container.
INNER="set -e
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git >/dev/null 2>&1"

# A file:// source is a bind-mounted host repo; add the mount and a git
# safe.directory exception (needed only because the mount is owned by the host
# UID while the container runs as root — irrelevant for the public https URL).
DOCKER_MOUNT=""
case "$REPO_URL" in
  file://*)
    SRC="${REPO_URL#file://}"
    DOCKER_MOUNT="-v ${SRC}:${SRC}:ro"
    INNER="${INNER}
git config --global --add safe.directory '${SRC}'
git config --global --add safe.directory '${SRC}/.git'"
    ;;
esac

INNER="${INNER}
mkdir -p /work && cd /work
printf '\$ uvx --from \"turnstile-cli @ git+%s@%s#subdirectory=packages/turnstile-cli\" turnstile init\n' '${REPO_URL}' '${REF}'
uvx --python 3.12 --from '${FROM}' turnstile init
printf '\n\$ turnstile list\n'
uvx --python 3.12 --from '${FROM}' turnstile list"

# Write the inner script to a temp file and mount it (avoids nested quoting).
TMP_INNER="$(mktemp)"
trap 'rm -f "$TMP_INNER"' EXIT
printf '%s\n' "$INNER" >"$TMP_INNER"

echo ">>> Recording clean-container quickstart (${REPO_URL} @ ${REF}) ..."
asciinema rec --overwrite --cols 100 --rows 34 \
  --command "docker run --rm ${DOCKER_MOUNT} -v ${TMP_INNER}:/rec/inner.sh:ro --entrypoint bash ${IMG} /rec/inner.sh" \
  "$CAST"

echo ">>> Rendering GIF ..."
agg --font-size 16 "$CAST" "$GIF"
echo ">>> Wrote ${CAST}"
echo ">>> Wrote ${GIF}"
