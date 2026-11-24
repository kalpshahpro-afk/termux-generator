#!/bin/bash
# Runs INSIDE the termux package-builder container (not on the runner).
#
# Re-installs previously built packages into the container's $TERMUX_PREFIX and records them
# as "already built", so build-package.sh does not rebuild them from source.
#
#   restore-debs.sh <dir>     <dir> holds the .deb files and a manifest.tsv with one
#                             "<file>\t<package>\t<version>" line per deb.
#
# RESTORE_ROOT and MARKER_DIR can be overridden for tests.
set -euo pipefail

dir="${1:?usage: restore-debs.sh <dir>}"
root="${RESTORE_ROOT:-/}"
marker_dir="${MARKER_DIR:-/data/data/.built-packages}"

mkdir -p "$marker_dir"
count=0
while IFS=$'\t' read -r file package version; do
	[ -n "$file" ] || continue
	deb="$dir/$file"
	if [ ! -f "$deb" ]; then
		echo "restore: missing $deb" >&2
		exit 1
	fi
	# Same extraction upstream uses for downloaded dependencies (see
	# scripts/build/termux_step_get_dependencies.sh).
	ar p "$deb" data.tar.xz | tar xJ --no-overwrite-dir --transform='s#^.$#data#' -C "$root"
	# termux_step_finish_build writes "<full version>" to a file named after every package
	# and sub-package; build-package.sh trusts these files.
	printf '%s\n' "$version" > "$marker_dir/$package"
	count=$((count + 1))
done < "$dir/manifest.tsv"
echo "restore: installed $count packages from previous runs"
