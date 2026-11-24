#!/usr/bin/env bash
set -e
export PATH="$JAVA_HOME/bin:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$PATH"
# The runner image ships its own Android SDK/NDK variables; use only the SDK installed by this workflow.
unset ANDROID_NDK_HOME ANDROID_NDK_ROOT ANDROID_NDK ANDROID_NDK_LATEST_HOME
export GRADLE_OPTS="-Dorg.gradle.daemon=false -Xmx3g"
git config --global user.name "Builder"
git config --global user.email "builder@localhost"
git config --global --add safe.directory "*"

# Always-on quality-of-life packages: the user's extra_packages first, then fzf and
# tmux. Items are trimmed, validated and de-duplicated; empty input is fine. The final
# comma-separated list is exported so custom/apply_custom.py can read EXTRA_PACKAGES.
ALWAYS_PACKAGES="fzf,tmux"
RESOLVED_PACKAGES=""
declare -A SEEN_PKG=()
IFS=',' read -r -a PKG_ITEMS <<< "${EXTRA_PACKAGES:-},${ALWAYS_PACKAGES}"
for pkg in "${PKG_ITEMS[@]}"; do
  pkg="${pkg#"${pkg%%[![:space:]]*}"}"
  pkg="${pkg%"${pkg##*[![:space:]]}"}"
  if [ -z "$pkg" ]; then continue; fi
  if ! [[ "$pkg" =~ ^[A-Za-z0-9][A-Za-z0-9+._-]*$ ]]; then
    echo "ERROR: invalid package name '$pkg' in extra_packages (use comma-separated names)"
    exit 1
  fi
  if [ -n "${SEEN_PKG[$pkg]:-}" ]; then continue; fi
  SEEN_PKG[$pkg]=1
  RESOLVED_PACKAGES="${RESOLVED_PACKAGES:+${RESOLVED_PACKAGES},}${pkg}"
done
export EXTRA_PACKAGES="$RESOLVED_PACKAGES"
echo "Resolved EXTRA_PACKAGES: $EXTRA_PACKAGES"

# Validate the free-text inputs up front; they end up on a command line.
ARCHITECTURES="${ARCHITECTURES//[[:space:]]/}"
if [ -z "${APP_NAME//[[:space:]]/}" ]; then
  echo "ERROR: app_name is empty - enter the app name you want (for example 'My Termux')"
  exit 1
fi
if ! [[ "${PACKAGE_NAME:-}" =~ ^[A-Za-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)+$ ]]; then
  echo "ERROR: invalid package_name '${PACKAGE_NAME:-}' (dot-separated parts made of letters and digits, e.g. com.example.termux)"
  exit 1
fi
if ! [[ "${ARCHITECTURES:-}" =~ ^(aarch64|arm|i686|x86_64)(,(aarch64|arm|i686|x86_64))*$ ]]; then
  echo "ERROR: invalid architectures '${ARCHITECTURES:-}' (comma-separated list of aarch64, arm, i686, x86_64)"
  exit 1
fi

# apt repository the app is configured with (consumed by the pre-flight patch of packages/apt).
# Empty PACKAGE_REPO = the "repo" release of this GitHub repository.
case "${PACKAGE_REPO:-}" in
  "") REPO_URL="${GITHUB_REPOSITORY:+https://github.com/${GITHUB_REPOSITORY}/releases/download/repo/}" ;;
  none) REPO_URL="" ;;
  *) REPO_URL="$PACKAGE_REPO" ;;
esac
if [ -n "$REPO_URL" ] && ! [[ "$REPO_URL" =~ ^https://[A-Za-z0-9._~/%-]+/$ ]]; then
  echo "ERROR: invalid package_repo '$REPO_URL' (use an https URL ending with /, 'none', or leave it empty)"
  exit 1
fi
export REPO_URL
echo "apt repository: ${REPO_URL:-official Termux repository}"

CMD=(./build-termux.sh --name "$PACKAGE_NAME" --type f-droid --architectures "$ARCHITECTURES")

# Addon selection: ADDONS is "none" (default), "all", or a comma-separated subset of
# the eight addon apps. Every addon that is not selected gets its --disable-* flag.
ALL_ADDONS="tasker float widget api boot styling gui x11"
declare -A WANT_ADDON=()
IFS=',' read -r -a ADDON_ITEMS <<< "${ADDONS:-none}"
for item in "${ADDON_ITEMS[@]}"; do
  item="${item#"${item%%[![:space:]]*}"}"
  item="${item%"${item##*[![:space:]]}"}"
  item="${item,,}"
  item="${item#termux-}"
  case "$item" in
    "" | none) ;;
    all) for a in $ALL_ADDONS; do WANT_ADDON[$a]=1; done ;;
    tasker | float | widget | api | boot | styling | gui | x11) WANT_ADDON[$item]=1 ;;
    *)
      echo "ERROR: unknown addon '$item' in addons (use none, all, or any of: ${ALL_ADDONS// /,})"
      exit 1
      ;;
  esac
done
SELECTED_ADDONS=""
for a in $ALL_ADDONS; do
  if [ -n "${WANT_ADDON[$a]:-}" ]; then
    SELECTED_ADDONS="${SELECTED_ADDONS:+${SELECTED_ADDONS},}${a}"
  else
    CMD+=("--disable-${a}")
  fi
done
echo "Selected addons: ${SELECTED_ADDONS:-none}"
if [ -n "$EXTRA_PACKAGES" ]; then
  CMD+=(--add "$EXTRA_PACKAGES")
fi

LOG_FILE="$CI_PROJECT_DIR/build.log"
echo "Running: ${CMD[*]}"
echo "Full output -> $LOG_FILE (keeping the Actions log small)."

"${CMD[@]}" > "$LOG_FILE" 2>&1 &
BUILD_PID=$!

while kill -0 "$BUILD_PID" 2>/dev/null; do
  sleep 30
  FREE=$(df -h "$CI_PROJECT_DIR" | awk 'NR==2 {print $4 " free (" $5 " used)"}')
  LAST=$(grep -E '(^\[\*\] (Building|Pre-flight:|Pruning Docker)|^termux - build of|^Migrating folder:|^\[custom\]|^BUILD |^> Task :|^ERROR:|^FAILED)' "$LOG_FILE" 2>/dev/null | tail -n 1 || true)
  CUSTOM=$(grep -E '^\[custom\] summary:' "$LOG_FILE" 2>/dev/null | tail -n 1 || true)
  echo "[$(date -u +%H:%M:%S)] Disk: $FREE | Latest: ${LAST:-working...}${CUSTOM:+ | ${CUSTOM}}"

  CID=$(docker ps --format '{{.Names}}' 2>/dev/null | grep 'package-builder' | head -n 1 || true)
  if [ -n "$CID" ]; then
    docker exec "$CID" bash -c 'find /data/data/.built-packages -maxdepth 1 -type f -mmin +2 -printf "%f\n" 2>/dev/null | while read -r p; do [ -n "$p" ] && rm -rf "/home/builder/.termux-build/$p/src" "/home/builder/.termux-build/$p/build" "/home/builder/.termux-build/$p/massage"; done' 2>/dev/null || true
  fi
done

set +e
wait "$BUILD_PID"
EXIT_CODE=$?
set -e

echo "=== Build exited with code $EXIT_CODE ==="
df -h "$CI_PROJECT_DIR"
if [ "$EXIT_CODE" -ne 0 ]; then
  echo "=== Last 120 lines of build.log ==="
  tail -n 120 "$LOG_FILE" || true
fi
gzip -c "$LOG_FILE" > "$CI_PROJECT_DIR/build.log.gz" || true
rm -f "$LOG_FILE" || true
exit "$EXIT_CODE"
