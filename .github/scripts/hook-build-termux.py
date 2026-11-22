with open("build-termux.sh", "r", encoding="utf-8") as f:
    lines = f.readlines()

out = []
hooked_pre = False
hooked_prune = False
hooked_custom = False
for line in lines:
    stripped = line.strip()
    indent = line[:len(line) - len(line.lstrip())]
    if stripped == "build_bootstraps":
        out.append(f"{indent}/tmp/preflight-packages.sh\n")
        hooked_pre = True
    out.append(line)
    if stripped == "move_bootstraps":
        out.append(
            f'{indent}echo "[*] Pruning Docker to free disk space before APK build..."; '
            f'docker rm -f $(docker ps -aq) 2>/dev/null || true; '
            f'docker system prune -af 2>/dev/null || true; df -h .\n'
        )
        hooked_prune = True
        out.append(f'{indent}python3 "$CI_PROJECT_DIR/custom/apply_custom.py"\n')
        hooked_custom = True

assert hooked_pre, "Failed to find build_bootstraps in build-termux.sh"
assert hooked_prune, "Failed to find move_bootstraps in build-termux.sh"
assert hooked_custom, "Failed to hook custom/apply_custom.py into build-termux.sh"
with open("build-termux.sh", "w", encoding="utf-8") as f:
    f.writelines(out)
print("Hooked build-termux.sh successfully:")
