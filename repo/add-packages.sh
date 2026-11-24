#!/usr/bin/env bash
# Append package names to repo/packages.txt (used by the "add_packages" input of the
# "Build package repo" workflow; you can also run it by hand: bash repo/add-packages.sh foo,bar).
set -euo pipefail

cd "$(dirname "$0")/.."
file=repo/packages.txt
list="${1:-}"

existing="$(sed -e 's/#.*//' "$file" | tr -s '[:space:],' '\n')"
added=()
IFS=$' \t\n,' read -r -a items <<< "$list"
for name in "${items[@]}"; do
	[ -n "$name" ] || continue
	if ! [[ "$name" =~ ^[a-z0-9][a-z0-9+._-]*$ ]]; then
		echo "ERROR: invalid package name '$name' (lowercase letters, digits, + . _ - only)" >&2
		exit 1
	fi
	if grep -qxF -- "$name" <<< "$existing"; then
		continue
	fi
	existing+=$'\n'"$name"
	added+=("$name")
done

if [ "${#added[@]}" -gt 0 ]; then
	{
		echo
		echo "# added on $(date -u +%Y-%m-%d)"
		printf '%s\n' "${added[@]}"
	} >> "$file"
fi
echo "added: ${added[*]:-(nothing new)}"
