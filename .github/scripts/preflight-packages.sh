#!/usr/bin/env bash
set -e
PKG_DIR="$(find "${CI_PROJECT_DIR:-$PWD}" -maxdepth 1 -type d -name 'termux-packages*' | head -n 1)"
if [ -z "$PKG_DIR" ] || [ ! -d "$PKG_DIR" ]; then
  echo "ERROR: Could not find termux-packages directory in ${CI_PROJECT_DIR:-$PWD}"
  exit 1
fi
cd "$PKG_DIR"
mkdir -p .ci-cache
[ -f .ci-cache/.preflight_done ] && exit 0

echo "[*] Pre-flight: patching mirrors and verifying 'less' tarball in $PKG_DIR..."
python3 - << 'PYEOF'
import re, subprocess, os

# 1. Pre-fetch and SHA256-verify 'less' tarball from GNU mirrors and rewrite packages/less/build.sh
less_path = "packages/less/build.sh"
with open(less_path, "r", encoding="utf-8") as f:
    less_build = f.read()

ver_m = re.search(r'(?m)^TERMUX_PKG_VERSION=["\']?([^"\'\s#]+)', less_build)
sha_m = re.search(r'(?m)^TERMUX_PKG_SHA256=["\']?([0-9a-fA-F]{64})', less_build)
assert ver_m and sha_m, "Could not parse less version/SHA256 from packages/less/build.sh"

ver, sha = ver_m.group(1), sha_m.group(1).lower()
fname = f"less-{ver}.tar.gz"
dest = os.path.join(".ci-cache", fname)
ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
candidates = [
    f"https://mirrors.edge.kernel.org/gnu/less/{fname}",
    f"https://gnu.mirror.constant.com/less/{fname}",
    f"https://ftp.gnu.org/gnu/less/{fname}",
    f"https://ftpmirror.gnu.org/gnu/less/{fname}",
    f"https://ftp.osuosl.org/pub/gnu/less/{fname}",
    f"https://www.greenwoodsoftware.com/less/{fname}",
    f"https://sources.archlinux.org/other/packages/less/{fname}",
    f"https://deb.debian.org/debian/pool/main/l/less/less_{ver}.orig.tar.gz",
]

working_url = None
for url in candidates:
    tmp = dest + ".tmp"
    res = subprocess.run(
        ["curl", "-fsSL", "--retry", "2", "--connect-timeout", "20", "-A", ua, "-o", tmp, url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if res.returncode == 0 and os.path.exists(tmp):
        got = subprocess.check_output(["sha256sum", tmp], text=True).split()[0].lower()
        if got == sha:
            os.replace(tmp, dest)
            working_url = url
            print(f"[*] Pre-flight: cached {fname} from {url} (SHA256 OK)", flush=True)
            break
        os.remove(tmp)

if not working_url:
    raise SystemExit(f"ERROR: Could not download SHA256-verified {fname} from any mirror")

# Directly point packages/less/build.sh to the verified working mirror URL
less_patched = re.sub(
    r'(?m)^TERMUX_PKG_SRCURL=.*$',
    f'TERMUX_PKG_SRCURL="{working_url}"',
    less_build,
    count=1,
)
with open(less_path, "w", encoding="utf-8") as f:
    f.write(less_patched)

# 2. Wrap scripts/build/termux_download.sh to use .ci-cache + mirror/UA fallback for other packages
dl_path = "scripts/build/termux_download.sh"
if os.path.exists(dl_path):
    with open(dl_path, "r", encoding="utf-8") as f:
        text = f.read()
    patched, count = re.subn(
        r'(?m)^(\s*function\s+)?termux_download\s*\(\)',
        '_orig_termux_download()',
        text,
        count=1,
    )
    if count == 1:
        wrapper = r'''
termux_download() {
    local URL="$1"
    local DESTINATION="$2"
    local CHECKSUM="$3"
    local FNAME
    FNAME="$(basename "$DESTINATION")"
    local CACHED="${TERMUX_SCRIPTDIR:-/home/builder/termux-packages}/.ci-cache/$FNAME"
    if [ ! -f "$DESTINATION" ] && [ -f "$CACHED" ]; then
        mkdir -p "$(dirname "$DESTINATION")"
        cp -f "$CACHED" "$DESTINATION"
    fi
    if [ ! -f "$DESTINATION" ]; then
        local UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        local MIRRORS=()
        case "$URL" in
            https://www.greenwoodsoftware.com/less/*|http://www.greenwoodsoftware.com/less/*)
                MIRRORS+=(
                    "https://mirrors.edge.kernel.org/gnu/less/$FNAME"
                    "https://gnu.mirror.constant.com/less/$FNAME"
                    "https://ftp.gnu.org/gnu/less/$FNAME"
                    "https://ftpmirror.gnu.org/gnu/less/$FNAME"
                    "$URL"
                )
                ;;
            https://ftp.gnu.org/gnu/*|https://ftpmirror.gnu.org/gnu/*)
                MIRRORS+=(
                    "https://mirrors.edge.kernel.org/gnu/${URL#*/gnu/}"
                    "$URL"
                )
                ;;
        esac
        local M TMPF
        for M in "${MIRRORS[@]}"; do
            TMPF="${DESTINATION}.tmp.$$"
            mkdir -p "$(dirname "$DESTINATION")"
            if curl -fsSL --retry 3 --connect-timeout 20 -A "$UA" -o "$TMPF" "$M"; then
                if [ "$CHECKSUM" = "SKIP_CHECKSUM" ] || [ -z "$CHECKSUM" ] || [ "$(sha256sum "$TMPF" | awk '{print $1}')" = "$CHECKSUM" ]; then
                    mv -f "$TMPF" "$DESTINATION"
                    break
                fi
            fi
            rm -f "$TMPF"
        done
    fi
    _orig_termux_download "$@"
}
'''
        with open(dl_path, "w", encoding="utf-8") as f:
            f.write(patched + "\n" + wrapper)
        print("[*] Pre-flight: wrapped termux_download() successfully", flush=True)
PYEOF
mkdir -p .ci-cache
if [ -f "${CI_PROJECT_DIR:-$PWD}/.github/scripts/maven-mirror.init.gradle" ]; then
  cp -f "${CI_PROJECT_DIR:-$PWD}/.github/scripts/maven-mirror.init.gradle" .ci-cache/maven-mirror.init.gradle
fi
echo "[*] Pre-flight: breaking upstream dependency cycles for openjdk-17/gradle..."
python3 - << 'PYEOF2'
import re, os

def sub_file(path, pattern, repl, label):
    if not os.path.exists(path):
        print(f"[!] {label}: {path} not found (skipped)", flush=True)
        return
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n == 0:
        print(f"[=] {label}: nothing to change (already fixed upstream?)", flush=True)
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"[+] {label}: patched {path}", flush=True)

# pulseaudio -> libsndfile -> libmp3lame -> libmpg123 -> pulseaudio is a dependency loop.
# buildorder.py silently breaks it in the wrong place, so pulseaudio gets built before
# libsndfile and fails. mpg123 only needs pulse for its optional audio-output backend.
sub_file("packages/libmpg123/build.sh",
         r'^TERMUX_PKG_BUILD_DEPENDS="[^"]*pulseaudio[^"]*"$',
         'TERMUX_PKG_BUILD_DEPENDS=""', "libmpg123 build deps")
sub_file("packages/libmpg123/mpg123.subpackage.sh",
         r'^TERMUX_SUBPKG_DEPENDS="[^"]*pulseaudio[^"]*"$',
         'TERMUX_SUBPKG_DEPENDS=""', "mpg123 subpackage deps")

# gradle lists "openjdk-21 | openjdk-25 | openjdk-17" and the build order takes the first
# alternative, which would compile a second (huge) JDK. Pin it to the JDK we build anyway.
if os.environ.get("REPO_BUILD") == "1":
    print("[=] gradle depends: package repository build, keeping the upstream JDK choice", flush=True)
else:
    sub_file("packages/gradle/build.sh",
             r'^TERMUX_PKG_DEPENDS="openjdk-21 \| openjdk-25 \| openjdk-17"$',
             'TERMUX_PKG_DEPENDS="openjdk-17"', "gradle depends")
# termux-am is built with Gradle inside the container. Maven Central answers shared CI IPs with
# HTTP 429, so use Google's mirror (attempts 1 and 3), plain Maven Central (attempt 2), and retry.
_GRADLE_CALL = r'^\t\$TERMUX_PKG_TMPDIR/gradle/gradle-\$_GRADLE_VERSION/bin/gradle \\\n\t\t:app:assembleRelease$'
_GRADLE_NEW = """\tlocal _gradle_ok=false _attempt
\tfor _attempt in 1 2 3; do
\t\tlocal _init=()
\t\tif [ "$_attempt" != 2 ] && [ -f "${TERMUX_SCRIPTDIR:-/home/builder/termux-packages}/.ci-cache/maven-mirror.init.gradle" ]; then
\t\t\t_init=(--init-script "${TERMUX_SCRIPTDIR:-/home/builder/termux-packages}/.ci-cache/maven-mirror.init.gradle")
\t\tfi
\t\tif $TERMUX_PKG_TMPDIR/gradle/gradle-$_GRADLE_VERSION/bin/gradle "${_init[@]}" :app:assembleRelease; then
\t\t\t_gradle_ok=true
\t\t\tbreak
\t\tfi
\t\techo "termux-am: gradle attempt $_attempt failed, retrying in 45s..."
\t\tsleep 45
\tdone
\t$_gradle_ok"""
sub_file("packages/termux-am/build.sh", _GRADLE_CALL, lambda m: _GRADLE_NEW, "termux-am gradle mirror+retry")
# Point apt at our own package repository. The official Termux packages are built for the
# com.termux prefix and cannot work under another package ID, so the official repo is removed.
repo_url = os.environ.get("REPO_URL", "").strip()
if repo_url and repo_url != "none":
    if not re.fullmatch(r"https://[A-Za-z0-9._~/%-]+/", repo_url):
        raise SystemExit(f"ERROR: invalid REPO_URL '{repo_url}' (https URL ending with a slash)")
    apt_path = "packages/apt/build.sh"
    apt_old = (
        '\t\techo "# The main termux repository, with cloudflare cache"\n'
        '\t\techo "deb https://packages-cf.termux.dev/apt/termux-main/ stable main"\n'
        '\t\techo "# The main termux repository, without cloudflare cache"\n'
        '\t\techo "# deb https://packages.termux.dev/apt/termux-main/ stable main"'
    )
    apt_new = (
        '\t\techo "# Custom package repository (GitHub release built by the package-repo workflow)"\n'
        f'\t\techo "deb [trusted=yes] {repo_url} ./"'
    )
    with open(apt_path, "r", encoding="utf-8") as f:
        apt_text = f.read()
    if apt_old not in apt_text:
        raise SystemExit("ERROR: sources.list lines in packages/apt/build.sh changed upstream; update the pre-flight patch")
    with open(apt_path, "w", encoding="utf-8") as f:
        f.write(apt_text.replace(apt_old, apt_new))
    print(f"[+] apt sources.list: using {repo_url}", flush=True)
else:
    print("[=] apt sources.list: keeping the official Termux repository", flush=True)
PYEOF2
touch .ci-cache/.preflight_done
