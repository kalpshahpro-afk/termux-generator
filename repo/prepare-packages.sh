#!/usr/bin/env bash
# shellcheck disable=SC2034  # the variables below are read by the sourced generator functions
# Clone the pinned termux-packages and rewrite it for the custom package ID exactly like
# build-termux.sh does for the app, so packages built here match the lean bootstrap in the APK.
#
# Needs: PACKAGE_NAME. Optional: REPO_URL (written into apt's sources.list by the pre-flight).
# Result: ./termux-packages-main (patched), with the pre-flight fixes applied.
set -euo pipefail

cd "$(dirname "$0")/.."
: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
if ! [[ "$PACKAGE_NAME" =~ ^[A-Za-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)+$ ]]; then
	echo "ERROR: invalid package name '$PACKAGE_NAME'" >&2
	exit 1
fi

# Variables the generator functions expect (see the top of build-termux.sh).
TERMUX_GENERATOR_HOME="$(pwd)"
TERMUX_APP__PACKAGE_NAME="$PACKAGE_NAME"
TERMUX_APP__USER_PATH="/data/data"
TERMUX_APP_TYPE="f-droid"
ENABLE_SSH_SERVER=""
TERMUX_GENERATOR_CONTAINER_NAME="$TERMUX_APP__PACKAGE_NAME-$TERMUX_APP_TYPE-package-builder"

# shellcheck source=../scripts/termux_generator_utils.sh
source "$TERMUX_GENERATOR_HOME/scripts/termux_generator_utils.sh"
# shellcheck source=../scripts/termux_generator_steps.sh
source "$TERMUX_GENERATOR_HOME/scripts/termux_generator_steps.sh"

rm -rf termux-packages-main
clone_termux_packages https://github.com/termux/termux-packages.git termux-packages-main
patch_bootstraps

export CI_PROJECT_DIR="$TERMUX_GENERATOR_HOME"
export REPO_BUILD=1
bash .github/scripts/preflight-packages.sh
echo "[*] termux-packages-main is ready ($(git -C termux-packages-main rev-parse --short HEAD))"
