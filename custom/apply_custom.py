#!/usr/bin/env python3
"""Apply custom branding to a generated Termux build.

Runs from build-termux.sh AFTER `move_bootstraps` (so the bootstrap archives
already sit inside termux-apps-main/termux-app/) and BEFORE Gradle compiles
the APK.

Environment (all optional, safe defaults are used when unset):
    PACKAGE_NAME     default "com.rebranded.termux"
    APP_NAME         default "Termux Rebranded"
    STARTUP_MESSAGE  default "Welcome to Termux Rebranded"
    EXTRA_PACKAGES   comma-separated list resolved by the CI job (incl. fzf,tmux); default ""
    ICON_HUE_SHIFT   "true" (default) / "false": per-build hue shift of the icon accent colour

What it does:
    1. App name     -> strings.xml, build.gradle, AndroidManifest.xml, TermuxConstants.java
    2. Startup MOTD -> etc/motd (+ motd-playstore / motd.sh when present) inside every
                       bootstrap archive (.zip as well as the .tar.xz that this
                       generator's f-droid patches actually produce); the same pass
                       injects the comfort & sandbox helpers: etc/profile.d/01-ui-setup.sh,
                       etc/profile.d/02-shell-comfort.sh, bin/sandbox-info, bin/export-out,
                       bin/clean-cache, and appends Package ID + sandbox disk usage lines
                       to etc/motd right below STARTUP_MESSAGE
    3. App icon     -> custom/icon.png -> adaptive + legacy + round launcher icons;
                       with ICON_HUE_SHIFT the hue of the neon-green accent is rotated by a
                       SHA-256(PACKAGE_NAME)-derived angle (the #000000 background stays pure black)

Only the standard library plus Pillow (PIL) is used.
Exit code is non-zero when no strings.xml or no bootstrap archive was patched.
"""

import colorsys
import hashlib
import math
import lzma
import os
import re
import shlex
import stat
import sys
import tarfile
import time
import zipfile
from pathlib import Path

DEFAULT_PACKAGE_NAME = "com.rebranded.termux"
DEFAULT_APP_NAME = "Termux Rebranded"

ROOT = Path(__file__).resolve().parent.parent  # repository root (build-termux.sh lives here)
APPS_DIR = ROOT / "termux-apps-main"
APP_DIR = ROOT / "termux-apps-main" / "termux-app"
RES_DIR = APP_DIR / "app" / "src" / "main" / "res"
ICON_SRC = ROOT / "custom" / "icon.png"

# Same settings as the generator's `XZ_OPT=-e9 tar cJf ...`
XZ_PRESET = 9 | lzma.PRESET_EXTREME

# Pixels at or below this luminance count as the pure-black icon background.
BLACK_THRESHOLD = 8


def log(msg):
    print("[custom] " + msg, flush=True)


def die(msg):
    print("[custom] ERROR: " + msg, flush=True)
    sys.exit(1)


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def read_text(path):
    return Path(path).read_bytes().decode("utf-8")


def write_text(path, text):
    Path(path).write_bytes(text.encode("utf-8"))


# --------------------------------------------------------------------------- #
# Escaping helpers
# --------------------------------------------------------------------------- #

def android_string_escape(text):
    """Escape text for use inside <string>...</string> in Android resources.

    XML-escapes &, <, > and escapes backslash, apostrophe (\\') and double quote
    the way aapt2 expects; a leading @ or ? is escaped so it is not read as a
    resource/theme reference.
    """
    out = text.replace("\\", "\\\\")
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace("'", "\\'").replace('"', '\\"')
    out = out.replace("\r", "").replace("\n", "\\n").replace("\t", "\\t")
    if out.startswith(("@", "?")):
        out = "\\" + out
    return out


def xml_attr_escape(text):
    """Plain XML attribute escaping (used for literal values in AndroidManifest.xml)."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\r", "").replace("\n", " "))


def groovy_string_escape(text):
    """Escape text for a double-quoted Groovy string (build.gradle)."""
    return (text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
            .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))


def java_string_escape(text):
    """Escape text for a Java string literal; non-ASCII becomes \\uXXXX."""
    out = []
    for ch in text:
        cp = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif cp < 0x20 or cp > 0x7E:
            if cp > 0xFFFF:
                cp -= 0x10000
                out.append("\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)))
            else:
                out.append("\\u%04x" % cp)
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- #
# 1. Custom app name
# --------------------------------------------------------------------------- #

STRINGS_APP_NAME_RE = re.compile(
    r"""(<string\b[^>]*?\bname\s*=\s*["']application_name["'][^>]*?>)(.*?)(</string>)""",
    re.DOTALL,
)

_GRADLE_VALUE = r"""(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')"""
GRADLE_ASSIGN_RE = re.compile(r"""(\bTERMUX_APP_NAME\b["']?\s*[=:]\s*)""" + _GRADLE_VALUE)
GRADLE_RESVALUE_RE = re.compile(r"""(["']TERMUX_APP_NAME["']\s*,\s*)""" + _GRADLE_VALUE)

MANIFEST_META_RE = re.compile(r"""(android:name\s*=\s*"TERMUX_APP_NAME"\s+android:value\s*=\s*")[^"]*(")""")
MANIFEST_LABEL_RE = re.compile(r"""(android:label\s*=\s*")([^"]*)(")""")

JAVA_APP_NAME_RE = re.compile(
    r"""(public\s+static\s+final\s+String\s+TERMUX_APP_NAME\s*=\s*)"(?:[^"\\\n]|\\.)*"(\s*;)"""
)


def update_strings_xml(app_name):
    escaped = android_string_escape(app_name)
    files = sorted(RES_DIR.glob("values*/strings.xml"))
    updated = []
    for path in files:
        text = read_text(path)
        new_text, count = STRINGS_APP_NAME_RE.subn(
            lambda m: m.group(1) + escaped + m.group(3), text)
        if count:
            if new_text != text:
                write_text(path, new_text)
            updated.append(path)
            log("app name: %s -> application_name = %r" % (rel(path), app_name))
    log("app name: %d of %d strings.xml files updated" % (len(updated), len(files)))
    return len(updated)


def update_build_gradle(app_name):
    candidates = [APP_DIR / "app" / "build.gradle", APP_DIR / "app" / "build.gradle.kts"]
    value = '"' + groovy_string_escape(app_name) + '"'
    total = 0
    for path in candidates:
        if not path.is_file():
            continue
        text = read_text(path)
        text, n1 = GRADLE_ASSIGN_RE.subn(lambda m: m.group(1) + value, text)
        text, n2 = GRADLE_RESVALUE_RE.subn(lambda m: m.group(1) + value, text)
        if n1 + n2:
            write_text(path, text)
            log("app name: %s TERMUX_APP_NAME updated (%d place(s))" % (rel(path), n1 + n2))
        else:
            log("app name: %s has no hardcoded TERMUX_APP_NAME, left unchanged" % rel(path))
        total += n1 + n2
    return total


def update_manifest(app_name, package_name):
    path = APP_DIR / "app" / "src" / "main" / "AndroidManifest.xml"
    if not path.is_file():
        log("app name: AndroidManifest.xml not found, skipped")
        return 0
    text = read_text(path)
    value = xml_attr_escape(app_name)

    text, n_meta = MANIFEST_META_RE.subn(lambda m: m.group(1) + value + m.group(2), text)

    # Only touch labels that still carry the stock/generated name; leave @string/... references alone.
    stock_labels = {"Termux", package_name, app_name, value}
    count_label = [0]

    def fix_label(m):
        if m.group(2) in stock_labels:
            count_label[0] += 1
            return m.group(1) + value + m.group(3)
        return m.group(0)

    text = MANIFEST_LABEL_RE.sub(fix_label, text)
    total = n_meta + count_label[0]
    if total:
        write_text(path, text)
        log("app name: %s hardcoded name updated (%d place(s))" % (rel(path), total))
    else:
        log("app name: %s has no hardcoded app name, left unchanged" % rel(path))
    return total


def update_termux_constants(app_name):
    java_root = APP_DIR / "termux-shared" / "src" / "main" / "java"
    # The package directory depends on PACKAGE_NAME, so locate the file instead of hardcoding com/termux.
    matches = sorted(java_root.rglob("TermuxConstants.java")) if java_root.is_dir() else []
    if not matches:
        log("WARNING: TermuxConstants.java not found under %s" % rel(java_root))
        return 0
    value = '"' + java_string_escape(app_name) + '"'
    updated = 0
    for path in matches:
        text = read_text(path)
        new_text, n = JAVA_APP_NAME_RE.subn(lambda m: m.group(1) + value + m.group(2), text)
        if n:
            if new_text != text:
                write_text(path, new_text)
            updated += 1
            log("app name: %s TERMUX_APP_NAME updated" % rel(path))
        else:
            log("WARNING: no 'TERMUX_APP_NAME' constant in %s" % rel(path))
    return updated


def apply_app_name(app_name, package_name):
    log("app name: %r" % app_name)
    strings_updated = update_strings_xml(app_name)
    if strings_updated == 0:
        die("no strings.xml with <string name=\"application_name\"> found under %s" % rel(RES_DIR))
    update_build_gradle(app_name)
    update_manifest(app_name, package_name)
    update_termux_constants(app_name)


# --------------------------------------------------------------------------- #
# Add-on app names (Termux:API, Termux:Boot, ...)
# --------------------------------------------------------------------------- #

ADDON_NAMES = ("API", "Boot", "Float", "Styling", "Tasker", "Widget", "GUI", "X11")
ADDON_SKIP_DIRS = {".git", ".gradle", "build", "node_modules"}


def xml_resource_escape(text):
    """android_string_escape, made safe for the double-quoted <!ENTITY ... "..."> literals that the
    add-ons use as well as for plain element text ("--" would end an XML comment)."""
    out = android_string_escape(text).replace('\\"', '\\&#34;').replace("%", "&#37;")
    return out.replace("--", "- -")


def kotlin_string_escape(text):
    return java_string_escape(text).replace("$", "\\$")


def apply_addon_names(app_name, package_name, apps_dir=None):
    """Rename the add-on apps from "<package>:API" to "<app name>:API", and so on.

    The generator's global rename turns every "Termux:" into the package ID, so the add-ons end up
    called e.g. "com.rebranded.termux:API". Only the exact "<package>:<AddonName>" strings are
    rewritten; Maven coordinates such as "<package>:termux-am-library" never match.
    """
    apps_dir = Path(apps_dir) if apps_dir else APPS_DIR
    if app_name == package_name:
        return 0
    pattern = re.compile(re.escape(package_name) + ":(" + "|".join(ADDON_NAMES) + ")(?![A-Za-z0-9_])")
    escapers = {
        ".xml": xml_resource_escape,
        ".gradle": groovy_string_escape,
        ".java": java_string_escape,
        ".kt": kotlin_string_escape,
        ".html": xml_attr_escape,
    }
    # The add-ons also describe the main app ("a plugin app for the Termux app") through their own
    # TERMUX_APP_NAME definitions, which the global rename turned into the bare package ID.
    main_name_defs = {
        ".xml": re.compile(r'(<!ENTITY\s+TERMUX_APP_NAME\s+")' + re.escape(package_name) + '(")'),
        ".gradle": re.compile(r'(manifestPlaceholders\.TERMUX_APP_NAME\s*=\s*")' + re.escape(package_name) + '(")'),
    }
    files = places = 0
    for path in sorted(apps_dir.rglob("*")):
        if path.suffix not in escapers or not path.is_file():
            continue
        if ADDON_SKIP_DIRS.intersection(path.relative_to(apps_dir).parts):
            continue
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            continue
        if package_name + ":" not in text:
            continue
        replacement = escapers[path.suffix](app_name)
        new_text, count = pattern.subn(lambda m: replacement + ":" + m.group(1), text)
        if path.suffix in main_name_defs:
            new_text, extra = main_name_defs[path.suffix].subn(
                lambda m: m.group(1) + replacement + m.group(2), new_text)
            count += extra
        if count:
            write_text(path, new_text)
            files += 1
            places += count
    log("add-on names: %d place(s) in %d file(s) now read '%s:API' etc." % (places, files, app_name))
    return places


# --------------------------------------------------------------------------- #
# 2. Custom startup message (MOTD) inside the bootstrap archives
# --------------------------------------------------------------------------- #

def norm_member(name):
    """'./etc/motd' and 'etc/motd' both -> 'etc/motd'."""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def find_bootstrap_archives():
    found = {}
    zip_patterns = ("bootstrap-*.zip",)
    tar_patterns = ("bootstrap-*.tar.xz",)
    if APP_DIR.is_dir():
        for pat in zip_patterns + tar_patterns:
            for p in APP_DIR.rglob(pat):
                if p.is_file():
                    found[p.resolve()] = p
    # Repository root: also catch renamed copies such as "<package>-f-droid-bootstrap-aarch64.zip".
    for pat in ("*bootstrap-*.zip", "*bootstrap-*.tar.xz"):
        for p in ROOT.glob(pat):
            if p.is_file():
                found[p.resolve()] = p
    return [found[k] for k in sorted(found)]


def _clone_zipinfo(info):
    new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    new.compress_type = info.compress_type
    new.external_attr = info.external_attr
    new.internal_attr = info.internal_attr
    new.create_system = info.create_system
    new.comment = info.comment
    return new


def _zip_level(compress_type):
    # `zip -r9` is what the generator uses; stored/lzma entries ignore the level.
    return 9 if compress_type in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2) else None


def patch_zip(path, motd, motd_sh, extras=None):
    tmp = path.with_name(path.name + ".custom-tmp")
    done = []
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
            zout.comment = zin.comment
            seen_motd = False
            extras = extras or {}      # {member: (bytes, mode)} files to inject / replace
            seen_extra = set()
            dirs_seen = set()
            first = None
            for info in zin.infolist():
                first = first or info
                key = norm_member(info.filename)
                new = _clone_zipinfo(info)
                if info.is_dir():
                    dirs_seen.add(key.rstrip("/"))
                if key == "etc/motd":
                    data, seen_motd = motd, True
                elif key == "etc/motd-playstore":
                    data = motd
                elif key == "etc/motd.sh":
                    data = motd_sh
                    new.external_attr = (stat.S_IFREG | 0o755) << 16
                elif key in extras:
                    data, mode = extras[key]
                    new.external_attr = (stat.S_IFREG | mode) << 16
                    seen_extra.add(key)
                else:
                    zout.writestr(new, zin.read(info), compress_type=info.compress_type,
                                  compresslevel=_zip_level(info.compress_type))
                    continue
                zout.writestr(new, data, compress_type=info.compress_type,
                              compresslevel=_zip_level(info.compress_type))
                done.append(key)
            if not seen_motd:
                new = zipfile.ZipInfo("etc/motd", date_time=time.localtime()[:6])
                new.compress_type = zipfile.ZIP_DEFLATED
                new.create_system = first.create_system if first else 3
                new.external_attr = (stat.S_IFREG | 0o644) << 16
                zout.writestr(new, motd)
                done.append("etc/motd (added)")
            missing = [k for k in extras if k not in seen_extra]
            for d in _missing_dirs(missing, dirs_seen):
                zout.writestr(_new_zip_dir(d, first), b"")
            for k in missing:
                data, mode = extras[k]
                new = zipfile.ZipInfo(k, date_time=time.localtime()[:6])
                new.compress_type = zipfile.ZIP_DEFLATED
                new.create_system = first.create_system if first else 3
                new.external_attr = (stat.S_IFREG | mode) << 16
                zout.writestr(new, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                done.append(k + " (added)")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return done


def _new_tar_member(name, data, mode, ref):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.type = tarfile.REGTYPE
    if ref is not None:
        ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = ref.mtime, ref.uid, ref.gid, ref.uname, ref.gname
    else:
        ti.mtime = int(time.time())
    return ti


def patch_tar_xz(path, motd, motd_sh, extras=None):
    import io
    tmp = path.with_name(path.name + ".custom-tmp")
    done = []
    try:
        with tarfile.open(path, "r|xz") as tin, \
                tarfile.open(tmp, "w:xz", format=tarfile.GNU_FORMAT, preset=XZ_PRESET) as tout:
            seen_motd = False
            extras = extras or {}      # {member: (bytes, mode)} files to inject / replace
            seen_extra = set()
            dirs_seen = set()
            first = None
            for member in tin:
                first = first or member
                key = norm_member(member.name)
                if member.isdir():
                    dirs_seen.add(key.rstrip("/"))
                if key in ("etc/motd", "etc/motd-playstore"):
                    mode = (member.mode & 0o7777) if member.isreg() else 0o644
                    data = motd
                    seen_motd = seen_motd or key == "etc/motd"
                elif key == "etc/motd.sh":
                    mode, data = 0o755, motd_sh
                elif key in extras:
                    data, mode = extras[key]
                    seen_extra.add(key)
                else:
                    if member.isreg():
                        tout.addfile(member, tin.extractfile(member))
                    else:
                        tout.addfile(member)
                    continue
                tout.addfile(_new_tar_member(member.name, data, mode, member), io.BytesIO(data))
                done.append(key)
            if not seen_motd:
                prefix = "./" if first is not None and first.name.startswith("./") else ""
                tout.addfile(_new_tar_member(prefix + "etc/motd", motd, 0o644, first), io.BytesIO(motd))
                done.append("etc/motd (added)")
            prefix = "./" if first is not None and first.name.startswith("./") else ""
            missing = [k for k in extras if k not in seen_extra]
            for d in _missing_dirs(missing, dirs_seen):
                tout.addfile(_new_tar_dir(prefix + d, first))
            for k in missing:
                data, mode = extras[k]
                tout.addfile(_new_tar_member(prefix + k, data, mode, first), io.BytesIO(data))
                done.append(k + " (added)")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return done


# --------------------------------------------------------------------------- #
# 2b. Comfort & sandbox helpers injected into every bootstrap archive
# --------------------------------------------------------------------------- #
#
# The shell sources below are templates with @TOKEN@ placeholders (filled in one
# pass by render_template) because shell code is full of $ and % characters:
#   @PKG@               raw, validated package name (shebangs and paths)
#   @PACKAGE_NAME_Q@    shell-quoted package name
#   @APP_NAME_Q@        shell-quoted app name
#   @EXTRA_PACKAGES_Q@  shell-quoted, comma-separated EXTRA_PACKAGES
#   @MOTD_FALLBACK@     static line shown when no live disk usage is available

PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")
PACKAGE_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
MOTD_USAGE_FALLBACK = "Sandbox disk: run 'sandbox-info' for live usage"
HELPER_BIN_MODE = 0o755      # bin/*
HELPER_PROFILE_MODE = 0o644  # etc/profile.d/*.sh (sourced, never executed)

MOTD_SH = r"""#!/data/data/@PKG@/files/usr/bin/sh
# Startup banner: etc/motd, with its last line (a static fallback) replaced by
# a live, time-capped sandbox disk usage readout.
if [ -r "$PREFIX/etc/motd" ]; then
    sed '$d' "$PREFIX/etc/motd"
fi
used=$(timeout 2 du -sh "/data/data/@PKG@" 2> /dev/null | cut -f1)
if [ -n "$used" ]; then
    echo "Sandbox disk: $used used"
else
    echo "@MOTD_FALLBACK@"
fi
"""

UI_SETUP_SH = r"""#!/data/data/@PKG@/files/usr/bin/sh
# 01-ui-setup.sh - one-time first-boot terminal UI setup.
# Sourced by $PREFIX/etc/profile. Delete $HOME/.termux/.configured to run it again.
_ui_dir="$HOME/.termux"
if [ ! -e "$_ui_dir/.configured" ]; then
    mkdir -p "$_ui_dir"

    if [ ! -e "$_ui_dir/termux.properties" ]; then
        cat > "$_ui_dir/termux.properties" << 'EOF_TERMUX_PROPERTIES'
# Developer extra-keys bar (2 rows), silent bell, pure-black UI.
extra-keys = [['ESC','|','/','HOME','UP','END','PGUP'],['TAB','CTRL','ALT','LEFT','DOWN','RIGHT','PGDN']]
bell-character = ignore
use-black-ui = true
EOF_TERMUX_PROPERTIES
    fi

    if [ ! -e "$_ui_dir/colors.properties" ]; then
        cat > "$_ui_dir/colors.properties" << 'EOF_TERMUX_COLORS'
# High-contrast OLED dark theme: pure #000000 background, vibrant syntax colours.
background=#000000
foreground=#f2f2f2
cursor=#5af78e
color0=#2b2b2b
color1=#ff5c6c
color2=#5af78e
color3=#f4f99d
color4=#57c7ff
color5=#ff6ac1
color6=#9aedfe
color7=#e6e6e6
color8=#686868
color9=#ff8089
color10=#69ff94
color11=#ffffa5
color12=#8fd6ff
color13=#ff92df
color14=#a4ffff
color15=#ffffff
EOF_TERMUX_COLORS
    fi

    touch "$_ui_dir/.configured"

    # Apply right away. 'am' may not be ready on the very first launch, so errors
    # are ignored; the nested subshell keeps the shell from printing a job line.
    if command -v termux-reload-settings > /dev/null 2>&1; then
        ( termux-reload-settings > /dev/null 2>&1 & )
    fi
fi
unset _ui_dir
"""

SHELL_COMFORT_SH = r"""#!/data/data/@PKG@/files/usr/bin/bash
# 02-shell-comfort.sh - universal shell enhancements (interactive shells only).
case $- in
    *i*)
        # fzf key bindings: Ctrl+R fuzzy history, Ctrl+T file search (bash only).
        if [ -n "${BASH_VERSION:-}" ] && command -v fzf > /dev/null 2>&1; then
            eval "$(fzf --bash 2>/dev/null)"
        fi

        alias ll='ls -lah --color=auto'
        alias cls='clear'
        alias storage='termux-setup-storage'
        alias size='du -sh $HOME $PREFIX 2>/dev/null'
        ;;
esac
"""

SANDBOX_INFO_SH = r"""#!/data/data/@PKG@/files/usr/bin/sh
# sandbox-info - show which sandbox app this is.
# PACKAGE_NAME, APP_NAME and EXTRA_PACKAGES are baked in at build time, so the
# package list shows only what was requested, not base system libraries.
PACKAGE_NAME=@PACKAGE_NAME_Q@
APP_NAME=@APP_NAME_Q@
EXTRA_PACKAGES=@EXTRA_PACKAGES_Q@
SANDBOX_DIR="/data/data/$PACKAGE_NAME"

used=$(du -sh "$SANDBOX_DIR" 2> /dev/null | cut -f1)
[ -n "$used" ] || used="unavailable"

if [ -n "$EXTRA_PACKAGES" ]; then
    extras=$(printf '%s' "$EXTRA_PACKAGES" | sed 's/,/, /g')
else
    extras="(none)"
fi

printf '%-16s %s\n' "Package ID:" "$PACKAGE_NAME"
printf '%-16s %s\n' "App name:" "$APP_NAME"
printf '%-16s %s  (%s)\n' "Sandbox disk:" "$used" "$SANDBOX_DIR"
printf '%-16s %s\n' "Extra packages:" "$extras"
"""

EXPORT_OUT_SH = r"""#!/data/data/@PKG@/files/usr/bin/sh
# export-out - copy files/folders out of the sandbox into shared storage, so
# finished APKs, signed keys or logs survive uninstalling this app.
#   usage: export-out <path1> [path2 ...]
PACKAGE_NAME=@PACKAGE_NAME_Q@

if [ "$#" -eq 0 ]; then
    echo "usage: export-out <path1> [path2 ...]" >&2
    exit 1
fi

if [ ! -d "$HOME/storage/shared" ]; then
    echo "Shared storage is not set up yet - requesting access (tap Allow)..."
    termux-setup-storage
    waited=0
    while [ ! -d "$HOME/storage/shared" ] && [ "$waited" -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
    done
fi
if [ ! -d "$HOME/storage/shared" ]; then
    echo "export-out: storage permission was not granted. Run termux-setup-storage, then retry." >&2
    exit 1
fi

dest="/sdcard/SandboxExports/${PACKAGE_NAME}_$(date +%Y%m%d_%H%M)"
if ! mkdir -p "$dest"; then
    echo "export-out: cannot create $dest" >&2
    exit 1
fi

status=0
copied=0
for src in "$@"; do
    if [ ! -e "$src" ]; then
        echo "export-out: not found: $src" >&2
        status=1
        continue
    fi
    if cp -RL "$src" "$dest/"; then
        copied=$((copied + 1))
    else
        echo "export-out: failed to copy: $src" >&2
        status=1
    fi
done

if [ "$copied" -eq 0 ]; then
    rmdir "$dest" 2> /dev/null
    echo "export-out: nothing was exported" >&2
    exit 1
fi
echo "Exported $copied of $# item(s) to: $dest"
exit "$status"
"""

CLEAN_CACHE_SH = r"""#!/data/data/@PKG@/files/usr/bin/sh
# clean-cache - clear heavy build caches inside the sandbox and report how much
# internal storage was reclaimed.
if [ -z "${HOME:-}" ] || [ "$HOME" = "/" ]; then
    echo "clean-cache: HOME is not set" >&2
    exit 1
fi

size_kb() { du -sk "$1" 2> /dev/null | cut -f1; }
human() { numfmt --to=iec --from-unit=1024 "$1" 2> /dev/null || echo "${1}K"; }

reclaimed=0

# pip first: its cache normally lives in ~/.cache/pip, which is wiped below.
if command -v pip > /dev/null 2>&1; then
    pip_dir=$(pip cache dir 2> /dev/null)
    if [ -n "$pip_dir" ] && [ -d "$pip_dir" ]; then
        before=$(size_kb "$pip_dir")
        pip cache purge > /dev/null 2>&1
        after=0
        [ -d "$pip_dir" ] && after=$(size_kb "$pip_dir")
        reclaimed=$((reclaimed + ${before:-0} - ${after:-0}))
        echo "pip cache purged"
    fi
fi

for dir in "$HOME/.gradle/caches" "$HOME/.npm" "$HOME/.cache" "$HOME/.cargo/registry/cache"; do
    [ -d "$dir" ] || continue
    before=$(size_kb "$dir")
    rm -rf "$dir"
    after=0
    [ -d "$dir" ] && after=$(size_kb "$dir")
    reclaimed=$((reclaimed + ${before:-0} - ${after:-0}))
    echo "cleared $dir ($(human "${before:-0}"))"
done

echo "Reclaimed: $(human "$reclaimed") of internal storage"
"""


def parse_package_list(raw):
    """'git, fzf,,git' -> ['git', 'fzf']: comma-separated, trimmed, de-duplicated, order kept."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or item in out:
            continue
        if not PACKAGE_ITEM_RE.match(item):
            die("EXTRA_PACKAGES: invalid package name %r (use comma-separated names)" % item)
        out.append(item)
    return out


def render_template(template, package_name, app_name, extra_packages):
    values = {
        "PKG": package_name,
        "PACKAGE_NAME_Q": shlex.quote(package_name),
        "APP_NAME_Q": shlex.quote(app_name),
        "EXTRA_PACKAGES_Q": shlex.quote(",".join(extra_packages)),
        "MOTD_FALLBACK": MOTD_USAGE_FALLBACK,
    }
    text = re.sub(r"@([A-Z_]+)@", lambda m: values.get(m.group(1), m.group(0)), template)
    return text.encode("utf-8")


def build_helper_files(package_name, app_name, extra_packages):
    """{archive member: (bytes, mode)} for the five files injected into every bootstrap."""
    if not PACKAGE_NAME_RE.match(package_name):
        die("helpers: unsupported characters in PACKAGE_NAME %r" % package_name)

    def render(template):
        return render_template(template, package_name, app_name, extra_packages)

    return {
        "etc/profile.d/01-ui-setup.sh": (render(UI_SETUP_SH), HELPER_PROFILE_MODE),
        "etc/profile.d/02-shell-comfort.sh": (render(SHELL_COMFORT_SH), HELPER_PROFILE_MODE),
        "bin/sandbox-info": (render(SANDBOX_INFO_SH), HELPER_BIN_MODE),
        "bin/export-out": (render(EXPORT_OUT_SH), HELPER_BIN_MODE),
        "bin/clean-cache": (render(CLEAN_CACHE_SH), HELPER_BIN_MODE),
    }


def _missing_dirs(paths, seen_dirs):
    """Parent directories of `paths` that the archive has no entry for, outermost first."""
    missing = []
    for p in paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in seen_dirs and d not in missing:
                missing.append(d)
    return missing


def _new_zip_dir(name, ref):
    new = zipfile.ZipInfo(name + "/", date_time=time.localtime()[:6])
    new.compress_type = zipfile.ZIP_STORED
    new.create_system = ref.create_system if ref is not None else 3
    new.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
    return new


def _new_tar_dir(name, ref):
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    if ref is not None:
        ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = ref.mtime, ref.uid, ref.gid, ref.uname, ref.gname
    else:
        ti.mtime = int(time.time())
    return ti


def apply_startup_message(package_name, startup_message, app_name=DEFAULT_APP_NAME, extra_packages=()):
    log("startup message: %r" % startup_message)
    # STARTUP_MESSAGE first, then a 2-line summary right below it. The second line is a
    # static fallback; the generated etc/motd.sh swaps it for live sandbox disk usage.
    motd = (startup_message + "\n"
            + "Package ID  : %s\n" % package_name
            + MOTD_USAGE_FALLBACK + "\n").encode("utf-8")
    motd_sh = render_template(MOTD_SH, package_name, app_name, extra_packages)
    helpers = build_helper_files(package_name, app_name, extra_packages)
    log("helpers: sandbox-info bakes EXTRA_PACKAGES=%s" % (",".join(extra_packages) or "(none)"))

    archives = find_bootstrap_archives()
    patched = 0
    for path in archives:
        name = path.name
        log("bootstrap: patching %s ..." % rel(path))
        if name.endswith(".zip"):
            done = patch_zip(path, motd, motd_sh, helpers)
        else:
            done = patch_tar_xz(path, motd, motd_sh, helpers)
        patched += 1
        log("bootstrap: patched %s (%s)" % (rel(path), ", ".join(done)))
        log("helpers: injected %d files (%s) + motd summary into %s" % (
            len(helpers), ", ".join(sorted(helpers)), rel(path)))
    log("bootstrap: %d of %d bootstrap archive(s) patched" % (patched, len(archives)))
    if patched == 0:
        die("no bootstrap-*.zip / bootstrap-*.tar.xz found under %s or %s" % (rel(APP_DIR), rel(ROOT)))
    return len(helpers)


# --------------------------------------------------------------------------- #
# 3. Custom adaptive + legacy launcher icons
# --------------------------------------------------------------------------- #

ADAPTIVE_FOREGROUND_SIZES = {  # 108dp canvas
    "mipmap-mdpi": 108,
    "mipmap-hdpi": 162,
    "mipmap-xhdpi": 216,
    "mipmap-xxhdpi": 324,
    "mipmap-xxxhdpi": 432,
}
LEGACY_SIZES = {  # 48dp
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}
SAFE_ZONE = 66.0 / 108.0       # adaptive icon safe zone (diameter / canvas)
SAFE_ZONE_TOLERANCE = 1.02     # squircle may exceed the safe zone by 2% before it is shrunk
LEGACY_PADDING = 0.06          # black margin kept around the squircle, per side
ICON_STEMS = ("ic_launcher", "ic_launcher_round", "ic_launcher_foreground")

ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_custom_bg"/>
    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>
</adaptive-icon>
"""

BG_COLOR_XML = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_custom_bg">#000000</color>
</resources>
"""


def remove_conflicting_icons():
    """Delete icon resources that would clash with (or shadow) the PNGs we write.

    Gradle's mergeResources fails on duplicate resources, and an old
    mipmap-anydpi-*/ic_launcher_foreground.xml would silently win over our PNG.
    """
    removed = []
    for d in sorted(RES_DIR.glob("mipmap*")):
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.stem in ICON_STEMS and f.suffix.lower() != ".png":
                f.unlink()
                removed.append("%s/%s" % (d.name, f.name))
    log("icon: removed %d conflicting file(s)%s" % (len(removed), (": " + ", ".join(removed)) if removed else ""))


def _flatten_on_black(img, image_mod):
    img = img.convert("RGBA")
    base = image_mod.new("RGBA", img.size, (0, 0, 0, 255))
    return image_mod.alpha_composite(base, img).convert("RGB")


def _content_bbox(img):
    mask = img.convert("L").point(lambda v: 255 if v > BLACK_THRESHOLD else 0)
    return mask, mask.getbbox()


def _corner_radius(mask):
    """Farthest distance of icon content from the centre, probed along the four diagonals."""
    w, h = mask.size
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    best = 0.0
    for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        for t in range(int(min(w, h) / 2), -1, -1):
            x, y = int(round(cx + sx * t)), int(round(cy + sy * t))
            if 0 <= x < w and 0 <= y < h and mask.getpixel((x, y)):
                best = max(best, t * math.sqrt(2.0))
                break
    return best


def hue_shift_degrees(package_name):
    """Deterministic 0..359 degree hue rotation derived from SHA-256(PACKAGE_NAME)."""
    digest = hashlib.sha256(package_name.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 360


def shift_hue(img, degrees, image_mod):
    """Rotate the hue of every coloured pixel of an RGB image by `degrees`.

    Saturation and value are kept, so the glow/anti-aliasing of the neon accent survives.
    Pixels with r == g == b (the pure-black #000000 background, greys, white) have no hue
    and are copied through untouched; the count of pure-black pixels is verified.
    """
    img = img.convert("RGB")
    colors = img.getcolors(maxcolors=img.size[0] * img.size[1])
    shift = (degrees % 360) / 360.0
    lut = {}
    black_before = 0
    for count, (r, g, b) in colors:
        if (r, g, b) == (0, 0, 0):
            black_before = count
        if r == g == b:
            lut[bytes((r, g, b))] = bytes((r, g, b))
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        nr, ng, nb = colorsys.hsv_to_rgb((h + shift) % 1.0, s, v)
        lut[bytes((r, g, b))] = bytes((int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))))
    raw = img.tobytes()
    out = image_mod.frombytes("RGB", img.size, b"".join([lut[raw[i:i + 3]] for i in range(0, len(raw), 3)]))
    black_after = 0
    for count, px in out.getcolors(maxcolors=out.size[0] * out.size[1]):
        if px == (0, 0, 0):
            black_after = count
    if black_after != black_before:
        die("icon: hue shift changed the pure-black background (%d -> %d px)" % (black_before, black_after))
    log("icon: hue shift kept %d pure-black (#000000) pixels untouched" % black_after)
    return out


def generate_icons(package_name=DEFAULT_PACKAGE_NAME, hue_shift=False):
    if not ICON_SRC.is_file():
        log("icon: %s not found, icon step skipped" % rel(ICON_SRC))
        return
    if not RES_DIR.is_dir():
        die("icon: resource directory %s does not exist" % rel(RES_DIR))
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        die("icon: Pillow is not installed (apt-get install python3-pil)")

    lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

    src = _flatten_on_black(Image.open(ICON_SRC), Image)
    side = max(src.size)
    square = Image.new("RGB", (side, side), (0, 0, 0))
    square.paste(src, ((side - src.size[0]) // 2, (side - src.size[1]) // 2))

    mask, bbox = _content_bbox(square)
    if bbox is None:
        die("icon: %s is completely black" % rel(ICON_SRC))
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    log("icon: source %dx%d, squircle %dx%d (%.1f%% of canvas)" % (
        src.size[0], src.size[1], bw, bh, 100.0 * max(bw, bh) / side))

    # The squircle geometry above was measured on the original; the hue shift only recolours it.
    shift_deg = None
    if hue_shift:
        shift_deg = hue_shift_degrees(package_name)
        log("icon: hue shift enabled: %s -> +%d deg (SHA-256 of PACKAGE_NAME)" % (package_name, shift_deg))
        square = shift_hue(square, shift_deg, Image)
    else:
        log("icon: hue shift disabled (ICON_HUE_SHIFT=false), original accent colour kept")

    remove_conflicting_icons()

    # ---- Adaptive foreground: whole icon on #000000, kept inside the 66/108 safe zone ----
    frac = max(bw, bh) / float(side)
    foreground = square
    if frac > SAFE_ZONE * SAFE_ZONE_TOLERANCE:
        scale = SAFE_ZONE / frac
        inner = max(1, int(round(side * scale)))
        foreground = Image.new("RGB", (side, side), (0, 0, 0))
        foreground.paste(square.resize((inner, inner), lanczos), ((side - inner) // 2, (side - inner) // 2))
        log("icon: squircle exceeded the safe zone, scaled to %.1f%%" % (scale * 100))

    written = 0
    for density, px in ADAPTIVE_FOREGROUND_SIZES.items():
        out_dir = RES_DIR / density
        out_dir.mkdir(parents=True, exist_ok=True)
        foreground.resize((px, px), lanczos).save(out_dir / "ic_launcher_foreground.png", "PNG", optimize=True)
        written += 1
        log("icon: adaptive foreground %s %dx%d" % (density, px, px))

    # ---- Legacy icons: trim the black margin, keep ~6% padding ----
    crop = square.crop(bbox)
    crop_mask = mask.crop(bbox)
    content = max(crop.size)

    def padded(half_side):
        size = max(1, int(round(half_side * 2)))
        canvas = Image.new("RGB", (size, size), (0, 0, 0))
        canvas.paste(crop, ((size - crop.size[0]) // 2, (size - crop.size[1]) // 2))
        return canvas

    legacy_base = padded(content * (0.5 + LEGACY_PADDING))
    # The circle must not clip the squircle's corners, so the round icon gets whatever
    # extra margin that needs (never less than the regular padding).
    round_half = max(content * (0.5 + LEGACY_PADDING), _corner_radius(crop_mask) * 1.03 + 1)
    round_base = padded(round_half)

    def circle_mask(px):
        ss = 4
        m = Image.new("L", (px * ss, px * ss), 0)
        ImageDraw.Draw(m).ellipse((0, 0, px * ss - 1, px * ss - 1), fill=255)
        return m.resize((px, px), lanczos)

    for density, px in LEGACY_SIZES.items():
        out_dir = RES_DIR / density
        out_dir.mkdir(parents=True, exist_ok=True)
        legacy_base.resize((px, px), lanczos).save(out_dir / "ic_launcher.png", "PNG", optimize=True)
        round_icon = round_base.resize((px, px), lanczos).convert("RGBA")
        round_icon.putalpha(circle_mask(px))
        round_icon.save(out_dir / "ic_launcher_round.png", "PNG", optimize=True)
        written += 2
        log("icon: legacy + round %s %dx%d" % (density, px, px))

    # ---- Adaptive icon XML + background colour ----
    values_dir = RES_DIR / "values"
    values_dir.mkdir(parents=True, exist_ok=True)
    write_text(values_dir / "ic_launcher_custom_bg.xml", BG_COLOR_XML)
    anydpi = RES_DIR / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    write_text(anydpi / "ic_launcher.xml", ADAPTIVE_XML)
    write_text(anydpi / "ic_launcher_round.xml", ADAPTIVE_XML)
    log("icon: wrote %d PNGs (5 densities x foreground/legacy/round), "
        "values/ic_launcher_custom_bg.xml and mipmap-anydpi-v26/ic_launcher{,_round}.xml" % written)
    return shift_deg


# --------------------------------------------------------------------------- #

def env(name, default, allow_empty=False):
    value = os.environ.get(name)
    if value is None or (not allow_empty and value.strip() == ""):
        return default
    return value


def env_flag(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def main():
    package_name = env("PACKAGE_NAME", DEFAULT_PACKAGE_NAME)
    app_name = env("APP_NAME", DEFAULT_APP_NAME)
    startup_message = env("STARTUP_MESSAGE", "", allow_empty=True)
    if not startup_message.strip():
        startup_message = "Welcome to %s" % app_name
    extra_packages = parse_package_list(env("EXTRA_PACKAGES", "", allow_empty=True))
    icon_hue_shift = env_flag("ICON_HUE_SHIFT", True)

    log("applying customizations (package=%s) in %s" % (package_name, rel(APP_DIR)))
    if not APP_DIR.is_dir():
        die("%s does not exist - run this after the app sources were downloaded" % rel(APP_DIR))

    apply_app_name(app_name, package_name)
    apply_addon_names(app_name, package_name)
    helper_count = apply_startup_message(package_name, startup_message, app_name, extra_packages)
    hue = generate_icons(package_name, icon_hue_shift)
    log("summary: package=%s helpers=%d extra_packages=%s icon_hue=%s" % (
        package_name, helper_count, ",".join(extra_packages) or "(none)",
        ("+%d deg" % hue) if hue is not None else "off"))
    log("done")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # make failures obvious in the CI status line
        import traceback
        traceback.print_exc()
        die("unexpected %s: %s" % (type(exc).__name__, exc))
