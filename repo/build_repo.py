#!/usr/bin/env python3
"""Build and publish the custom Termux apt repository (used by build-repo.yml).

Subcommands
  plan  Resolve repo/packages.txt against the pinned termux-packages checkout and print the
        build plan. Needs no Docker and no GitHub access.
  run   Build whatever is missing inside the termux package-builder container, publish the
        .deb files and the apt index to a GitHub release, and tell the workflow whether
        another run is needed (the full list takes several 5.5 h runs).

State lives in the release tagged $RELEASE_TAG ("repo"):
  *.deb                 every published package file
  Packages(.gz)/Release the flat apt index (each stanza carries X-Build-Dir / X-Termux-Rev)
  failed.tsv            build dirs that failed: <dir> <attempts> <reason>
  timings.tsv           measured build minutes per dir, used to schedule later runs
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_DIR = ROOT / "repo"
TP = Path(os.environ.get("TP_DIR") or (ROOT / "termux-packages-main"))
WORK = Path(os.environ.get("WORK_DIR") or (ROOT / "repo-work"))
LOGS = WORK / "logs"
UPLOAD = WORK / "upload"
ARCH = "aarch64"
TOP_DIRS = ("packages", "root-packages", "x11-packages")
MAX_ATTEMPTS = 2          # a build dir that failed this often is skipped until retry_failed
DEFAULT_WEIGHT = 8        # minutes, for dirs without a weight or a measured time
ASSET_LIMIT = 960         # GitHub allows 1000 assets per release
MIN_START_S = float(os.environ.get("MIN_START_MINUTES", "30")) * 60   # no new build this close to the hard limit
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")

TAG = os.environ.get("RELEASE_TAG", "repo")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")
PACKAGE_NAME = os.environ.get("PACKAGE_NAME", "")
ASSET_BASE_URL = os.environ.get("ASSET_BASE_URL") or f"https://github.com/{GH_REPO}/releases/download/{TAG}"


# --------------------------------------------------------------------------- helpers
def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def die(msg: str) -> None:
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def retry(fn, what: str, tries: int = 4, delay: int = 15):
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - any failure is worth a retry here
            if attempt == tries:
                raise
            warn(f"{what} failed ({exc}); retrying in {delay * attempt}s")
            time.sleep(delay * attempt)


def parse_names(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        for tok in re.split(r"[\s,]+", line.split("#", 1)[0]):
            if tok and tok not in names:
                names.append(tok)
    return names


def short(rel_dir: str) -> str:
    return rel_dir.split("/", 1)[1]


def sanitize(filename: str) -> str:
    """GitHub rewrites unusual characters in asset names; use only safe ones ourselves."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def termux_rev() -> str:
    out = subprocess.run(["git", "-C", str(TP), "rev-parse", "HEAD"], capture_output=True, text=True)
    if out.returncode:
        die(f"{TP} is not a git checkout; run repo/prepare-packages.sh first")
    return out.stdout.strip()


# --------------------------------------------------------------------------- plan
def index_names() -> tuple[dict, dict, dict]:
    """name -> build dir for packages, sub-packages and TERMUX_PKG_PROVIDES aliases."""
    direct: dict[str, str] = {}
    sub: dict[str, str] = {}
    provides: dict[str, str] = {}
    for top in TOP_DIRS:
        base = TP / top
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            build_sh = d / "build.sh"
            if not build_sh.is_file():
                continue
            rel = f"{top}/{d.name}"
            direct.setdefault(d.name, rel)
            for sp in d.glob("*.subpackage.sh"):
                sub.setdefault(sp.name[: -len(".subpackage.sh")], rel)
            for line in build_sh.read_text(errors="replace").splitlines():
                if line.startswith("TERMUX_PKG_PROVIDES="):
                    value = re.sub(r"\(.*?\)", "", line.split("=", 1)[1])
                    for alias in re.findall(r"[\w+.-]+", value):
                        provides.setdefault(alias, rel)
    return direct, sub, provides


def dir_closure(rel: str) -> list[str]:
    """Build dirs `rel` needs (dependencies first, excluding itself), via upstream buildorder.py."""
    env = {**os.environ, "TERMUX_ARCH": ARCH}
    out = subprocess.run(
        [sys.executable, "scripts/buildorder.py", rel, *TOP_DIRS],
        cwd=TP, capture_output=True, text=True, env=env,
    )
    if out.returncode:
        die(f"buildorder.py failed for {rel}: {out.stderr.strip() or out.stdout.strip()}")
    deps: list[str] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] != rel and parts[-1] not in deps:
            deps.append(parts[-1])
    return deps


class Plan:
    def __init__(self, names: list[str]):
        self.names = names
        self.direct, self.sub, self.provides = index_names()
        self.name_to_dir = {**self.provides, **self.sub, **self.direct}
        self.unknown: list[str] = []
        self.aliases: dict[str, str] = {}
        self.wanted: list[str] = []
        for name in names:
            rel = None
            if NAME_RE.match(name):
                rel = self.direct.get(name) or self.sub.get(name) or self.provides.get(name)
            if not rel:
                self.unknown.append(name)
                continue
            if short(rel) != name:
                self.aliases[name] = rel
            if rel not in self.wanted:
                self.wanted.append(rel)

        workers = os.cpu_count() or 4
        lists: dict[str, list[str]] = {}
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for rel, deps in zip(self.wanted, pool.map(dir_closure, self.wanted)):
                lists[rel] = deps
            todo = sorted({d for deps in lists.values() for d in deps} - set(lists))
            for rel, deps in zip(todo, pool.map(dir_closure, todo)):
                lists[rel] = deps
        self.closure = lists
        self.closure_set = {rel: set(deps) for rel, deps in lists.items()}
        # Dependency-respecting order: each wanted dir's closure, then the dir itself.
        self.order: list[str] = []
        seen: set[str] = set()
        for rel in self.wanted:
            for d in [*self.closure[rel], rel]:
                if d not in seen:
                    seen.add(d)
                    self.order.append(d)

    def deb_estimate(self) -> int:
        return sum(1 + len(list((TP / rel).glob("*.subpackage.sh"))) for rel in self.order)


def load_weights() -> dict[str, float]:
    weights: dict[str, float] = {}
    path = REPO_DIR / "weights.txt"
    if path.is_file():
        for line in path.read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) == 2:
                try:
                    weights[parts[0]] = float(parts[1])
                except ValueError:
                    warn(f"weights.txt: bad line '{line}'")
    return weights


def wanted_names() -> list[str]:
    only = os.environ.get("ONLY", "").strip()
    if only:
        return parse_names(only)
    names = parse_names((REPO_DIR / "packages.txt").read_text())
    for extra in parse_names(os.environ.get("ADD_PACKAGES", "")):
        if extra not in names:
            names.append(extra)
    return names


def cmd_plan(_args) -> int:
    plan = Plan(wanted_names())
    weights = load_weights()
    log(f"wanted names: {len(plan.names)}  -> build dirs: {len(plan.wanted)}")
    for name, rel in plan.aliases.items():
        log(f"  alias: {name} -> {rel}")
    if plan.unknown:
        warn("unknown package names: " + ", ".join(plan.unknown))
    log(f"build dirs including dependencies: {len(plan.order)}")
    log(f"estimated .deb files: {plan.deb_estimate()} (release limit {ASSET_LIMIT})")
    est = sum(weights.get(short(d), DEFAULT_WEIGHT) for d in plan.order)
    log(f"estimated build time: {est / 60:.1f} h in total")
    heavy = sorted(plan.order, key=lambda d: -weights.get(short(d), DEFAULT_WEIGHT))[:8]
    log("heaviest: " + ", ".join(f"{short(d)}={weights.get(short(d), DEFAULT_WEIGHT):.0f}m" for d in heavy))
    return 0


# --------------------------------------------------------------------------- apt index
class Stanza:
    def __init__(self, text: str):
        self.text = text.strip("\n")
        self.fields: dict[str, str] = {}
        for line in self.text.split("\n"):
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            self.fields[key] = value.strip()

    @property
    def package(self) -> str:
        return self.fields.get("Package", "")

    @property
    def version(self) -> str:
        return self.fields.get("Version", "")

    @property
    def filename(self) -> str:
        return self.fields.get("Filename", "")


def make_stanza(deb: Path, build_dir: str, rev: str) -> Stanza:
    ctrl = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True, check=True).stdout.strip("\n")
    own = r"(Filename|Size|MD5sum|SHA1|SHA256|X-Build-Dir|X-Termux-Rev|X-Termux-App):"
    kept = [ln for ln in ctrl.split("\n") if not re.match(own, ln)]
    kept += [
        f"Filename: {deb.name}",
        f"Size: {deb.stat().st_size}",
        f"MD5sum: {file_hash(deb, 'md5')}",
        f"SHA1: {file_hash(deb, 'sha1')}",
        f"SHA256: {file_hash(deb, 'sha256')}",
        f"X-Build-Dir: {build_dir}",
        f"X-Termux-Rev: {rev}",
        f"X-Termux-App: {PACKAGE_NAME}",
    ]
    return Stanza("\n".join(kept))


class Index:
    def __init__(self) -> None:
        self.by_dir: dict[str, list[Stanza]] = {}

    @staticmethod
    def parse(text: str) -> "Index":
        idx = Index()
        for block in re.split(r"\n\s*\n", text.strip("\n")) if text.strip() else []:
            stanza = Stanza(block)
            build_dir = stanza.fields.get("X-Build-Dir")
            if build_dir and stanza.filename:
                idx.by_dir.setdefault(build_dir, []).append(stanza)
        return idx

    def built_dirs(self, rev: str) -> set[str]:
        return {d for d, ss in self.by_dir.items() if ss and all(s.fields.get("X-Termux-Rev") == rev for s in ss)}

    def replace(self, build_dir: str, stanzas: list[Stanza]) -> set[str]:
        """Swap in new stanzas for a build dir; returns the old file names that are now unused."""
        old = {s.filename for s in self.by_dir.get(build_dir, [])}
        self.by_dir[build_dir] = stanzas
        return old - {s.filename for s in stanzas}

    def drop(self, build_dir: str) -> None:
        self.by_dir.pop(build_dir, None)

    def filenames(self) -> set[str]:
        return {s.filename for ss in self.by_dir.values() for s in ss}

    def count(self) -> int:
        return sum(len(ss) for ss in self.by_dir.values())

    def text(self) -> str:
        every = sorted((s for ss in self.by_dir.values() for s in ss), key=lambda s: (s.package, s.version))
        return "".join(s.text + "\n\n" for s in every)


def release_text(packages: bytes, packages_gz: bytes) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    lines = [
        f"Origin: {GH_REPO or PACKAGE_NAME}",
        f"Label: {PACKAGE_NAME} packages",
        "Suite: stable",
        "Codename: stable",
        f"Date: {now}",
        f"Architectures: {ARCH} all",
        "Components: main",
        f"Description: Termux packages built for {PACKAGE_NAME}",
    ]
    for header, algo in (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256")):
        lines.append(f"{header}:")
        for name, blob in (("Packages", packages), ("Packages.gz", packages_gz)):
            lines.append(f" {hashlib.new(algo, blob).hexdigest()} {len(blob):>10} {name}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- GitHub release
def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["gh", *args], text=True, capture_output=True)
    if check and res.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(res.stderr or res.stdout).strip()}")
    return res


def ensure_release() -> None:
    if gh("release", "view", TAG, "--repo", GH_REPO, check=False).returncode == 0:
        return
    url = f"https://github.com/{GH_REPO}/releases/download/{TAG}/"
    notes = (
        f"Package repository for `{PACKAGE_NAME}`, maintained by the *Build package repo* workflow.\n\n"
        f"Add it to apt with:\n\n```\ndeb [trusted=yes] {url} ./\n```\n\n"
        "Do not upload or delete assets by hand; the workflow keeps `Packages`, `Packages.gz` and "
        "`Release` in sync with the `.deb` files."
    )
    gh("release", "create", TAG, "--repo", GH_REPO, "--title", f"Package repository ({ARCH})",
       "--notes", notes, "--latest=false")


def list_assets() -> set[str]:
    rid = gh("api", f"repos/{GH_REPO}/releases/tags/{TAG}", "--jq", ".id").stdout.strip()
    out = gh("api", "--paginate", f"repos/{GH_REPO}/releases/{rid}/assets?per_page=100", "--jq", ".[].name").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def read_asset(name: str, assets: set[str]) -> str:
    if name not in assets:
        return ""
    return retry(lambda: gh("release", "download", TAG, "--repo", GH_REPO, "--pattern", name,
                            "--output", "-").stdout, f"download {name}")


def upload_assets(paths: list[Path]) -> None:
    for p in paths:
        if p.stat().st_size == 0:  # GitHub answers "HTTP 400: Bad Content-Length" for empty files
            p.write_text("\n")
    for i in range(0, len(paths), 20):
        chunk = [str(p) for p in paths[i:i + 20]]
        retry(lambda c=chunk: gh("release", "upload", TAG, *c, "--repo", GH_REPO, "--clobber"),
              "upload", tries=5, delay=20)


def delete_asset(name: str) -> None:
    gh("release", "delete-asset", TAG, name, "--repo", GH_REPO, "--yes", check=False)


def fetch_debs(names: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    def one(name: str) -> None:
        target = dest / name
        if target.exists():
            return
        part = str(target) + ".part"
        retry(lambda: subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20", "-o", part,
             f"{ASSET_BASE_URL}/{name}"], check=True), f"download {name}")
        os.replace(part, target)

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, names))


# --------------------------------------------------------------------------- state files
def parse_tsv(text: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if parts and parts[0]:
            rows[parts[0]] = parts[1:]
    return rows


def dump_tsv(rows: dict[str, list[str]]) -> str:
    # GitHub rejects empty release assets, so these files always carry a header line.
    return "# managed by build_repo.py\n" + "".join(
        "\t".join([k, *v]) + "\n" for k, v in sorted(rows.items()))


# --------------------------------------------------------------------------- container
def docker_exec(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["scripts/run-docker.sh", *cmd], cwd=TP, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and res.returncode:
        die(f"container command failed ({res.returncode}): {' '.join(cmd)}\n{(res.stdout or '')[-2000:]}")
    return res


def container_ready() -> None:
    # Same step the generator runs before building (pypy and similar need /system).
    docker_exec("sudo", "ln", "-sf", f"/data/data/{PACKAGE_NAME}/aosp", "/system")


def restore_packages(idx: Index, dirs: list[str]) -> int:
    stage = TP / ".repo-restore"
    shutil.rmtree(stage, ignore_errors=True)
    stanzas = [s for d in dirs for s in idx.by_dir.get(d, [])]
    if not stanzas:
        return 0
    fetch_debs([s.filename for s in stanzas], stage)
    with open(stage / "manifest.tsv", "w") as fh:
        for s in stanzas:
            fh.write(f"{s.filename}\t{s.package}\t{s.version}\n")
    shutil.copy(REPO_DIR / "restore-debs.sh", stage / "restore-debs.sh")
    inside = "/home/builder/termux-packages/.repo-restore"
    docker_exec("bash", f"{inside}/restore-debs.sh", inside)
    shutil.rmtree(stage, ignore_errors=True)
    return len(stanzas)


def clean_build_dirs() -> None:
    """Drop source/build trees of finished packages so the runner does not run out of disk.

The shared directories must stay: _cache (toolchain, host python) and the python crossenv
prefix, whose own build/ directory is the build-side virtualenv that holds build-pip."""
    docker_exec(
        "bash", "-c",
        'for d in /home/builder/.termux-build/*/; do '
        'case "$(basename "$d")" in _cache|*-crossenv-prefix-*) continue;; esac; '
        'rm -rf "$d/src" "$d/build" "$d/massage"; done; true',
        check=False,
    )


def disk_report() -> str:
    out = subprocess.run(["df", "-h", "--output=avail,pcent", str(ROOT)], capture_output=True, text=True).stdout
    return " ".join(out.split()[2:4]) if out else "?"


# --------------------------------------------------------------------------- build loop
class Runner:
    def __init__(self, plan: Plan):
        self.plan = plan
        self.rev = termux_rev()
        self.weights = load_weights()
        self.assets: set[str] = set()
        self.idx = Index()
        self.failed: dict[str, list[str]] = {}
        self.timings: dict[str, list[str]] = {}
        self.job_start = float(os.environ.get("JOB_START") or time.time())
        self.soft = float(os.environ.get("DEADLINE_SOFT") or (self.job_start + 325 * 60))
        self.hard = float(os.environ.get("DEADLINE_HARD") or (self.job_start + 340 * 60))
        self.pending_deletes: set[str] = set()
        self.last_index_publish = 0.0
        self.index_dirty = False
        self.built_now: list[str] = []
        self.failed_now: list[str] = []
        self.deferred: list[str] = []
        self.stop_reason = ""
        self.attempted_any = False

    # ---- state ----
    def load_state(self) -> None:
        ensure_release()
        self.assets = list_assets()
        self.idx = Index.parse(read_asset("Packages", self.assets))
        self.failed = parse_tsv(read_asset("failed.tsv", self.assets))
        self.timings = parse_tsv(read_asset("timings.tsv", self.assets))
        # Debs hold the app's data path (/data/data/<package>/...), so debs built for another
        # package name (or before stanzas carried X-Termux-App) must never be reused or served.
        stale = [d for d, ss in self.idx.by_dir.items()
                 if any(st.fields.get("X-Termux-App") != PACKAGE_NAME for st in ss)]
        for d in stale:
            self.pending_deletes |= {st.filename for st in self.idx.by_dir.pop(d)}
        if stale:
            warn(f"dropped {len(stale)} build dirs published for a different package name")
            self.index_dirty = True

    def weight(self, rel: str) -> float:
        measured = self.timings.get(rel)
        if measured:
            try:
                return float(measured[0])
            except ValueError:
                pass
        return self.weights.get(short(rel), DEFAULT_WEIGHT)

    def attempts(self, rel: str) -> int:
        try:
            return int(self.failed.get(rel, ["0"])[0])
        except ValueError:
            return 0

    def publish_index(self, force: bool = False) -> None:
        if not self.index_dirty and not force:
            return
        if not force and time.time() - self.last_index_publish < 600:
            return
        UPLOAD.mkdir(parents=True, exist_ok=True)
        text = self.idx.text().encode()
        gz = gzip.compress(text, mtime=0)
        (UPLOAD / "Packages").write_bytes(text)
        (UPLOAD / "Packages.gz").write_bytes(gz)
        (UPLOAD / "Release").write_text(release_text(text, gz))
        (UPLOAD / "failed.tsv").write_text(dump_tsv(self.failed))
        (UPLOAD / "timings.tsv").write_text(dump_tsv(self.timings))
        names = ["Packages", "Packages.gz", "Release", "failed.tsv", "timings.tsv"]
        upload_assets([UPLOAD / n for n in names])
        self.assets.update(names)
        keep = self.idx.filenames()
        for name in sorted(self.pending_deletes):
            if name in self.assets and name not in keep:
                delete_asset(name)
                self.assets.discard(name)
        self.pending_deletes.clear()
        self.last_index_publish = time.time()
        self.index_dirty = False
        log(f"[index] published {self.idx.count()} package files, {len(self.assets)} release assets")

    def publish_debs(self, owner_dir: str, debs: list[Path]) -> bool:
        """Upload debs that belong to one build dir and record them in the index."""
        UPLOAD.mkdir(parents=True, exist_ok=True)
        staged: list[Path] = []
        for deb in debs:
            target = UPLOAD / sanitize(deb.name)
            shutil.copy(deb, target)
            staged.append(target)
        new_files = [p for p in staged if p.name not in self.assets]
        if len(self.assets) + len(new_files) > ASSET_LIMIT:
            self.stop_reason = (f"the release would exceed {ASSET_LIMIT} assets (GitHub's limit is 1000); "
                                "the repository needs to be split over a second release")
            return False
        upload_assets(staged)
        self.assets.update(p.name for p in staged)
        stanzas = [make_stanza(p, owner_dir, self.rev) for p in staged]
        self.pending_deletes |= self.idx.replace(owner_dir, stanzas)
        self.index_dirty = True
        return True

    # ---- building ----
    def run_build(self, rel: str, timeout_s: float) -> tuple[int, bool]:
        LOGS.mkdir(parents=True, exist_ok=True)
        log_path = LOGS / f"{short(rel)}.log"
        cmd = ["scripts/run-docker.sh", "./build-package.sh", "-a", ARCH, short(rel)]
        start = time.time()
        next_beat = start + 600
        with open(log_path, "wb") as fh:
            proc = subprocess.Popen(cmd, cwd=TP, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
            while True:
                try:
                    return proc.wait(timeout=20), False
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if now - start > timeout_s:
                    for sig, grace in ((signal.SIGTERM, 60), (signal.SIGKILL, 10)):
                        try:
                            os.killpg(proc.pid, sig)
                            proc.wait(timeout=grace)
                            break
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            continue
                    return -1, True
                if now >= next_beat:
                    log(f"    ... {short(rel)} still building after {(now - start) / 60:.0f} min, disk free {disk_report()}")
                    next_beat += 600

    def new_debs(self, before: set[str]) -> list[Path]:
        out = TP / "output"
        if not out.is_dir():
            return []
        return [p for p in sorted(out.glob("*.deb")) if p.name not in before]

    def owner_of(self, deb: Path, fallback: str) -> str:
        res = subprocess.run(["dpkg-deb", "-f", str(deb), "Package"], capture_output=True, text=True)
        return self.plan.name_to_dir.get(res.stdout.strip(), fallback)

    def build_one(self, rel: str) -> set[str]:
        """Build one dir; returns every build dir that ended up published (empty on failure)."""
        start = time.time()
        remaining = self.hard - start
        full_budget = self.hard - self.job_start
        counts_on_timeout = remaining >= 0.85 * full_budget
        out_dir = TP / "output"
        before = {p.name for p in out_dir.glob("*.deb")} if out_dir.is_dir() else set()
        number = len(self.built_now) + len(self.failed_now) + 1
        log(f"::group::[{number}] {rel} (est. {self.weight(rel):.0f} min, disk free {disk_report()})")
        self.attempted_any = True
        rc, timed_out = self.run_build(rel, remaining)
        minutes = (time.time() - start) / 60
        debs = self.new_debs(before) if rc == 0 else []
        groups: dict[str, list[Path]] = {}
        for deb in debs:
            groups.setdefault(self.owner_of(deb, rel), []).append(deb)
        reason = ""
        if rc == 0 and rel not in groups:
            rc, reason = 1, "build finished but produced no .deb for this package"
        elif timed_out:
            reason = f"timed out after {minutes:.0f} min"
        elif rc != 0:
            reason = f"build-package.sh exited with {rc}"

        published: set[str] = set()
        if rc == 0:
            for owner, files in groups.items():
                if not self.publish_debs(owner, files):
                    break
                published.add(owner)
            if rel in published:
                self.timings[rel] = [f"{minutes:.1f}"]
                self.failed.pop(rel, None)
                self.built_now.extend(sorted(published))
                names = ", ".join(sorted(p.name for p in debs))
                log(f"built {rel} in {minutes:.1f} min -> {names}")
            else:
                log(f"not publishing {rel}: {self.stop_reason}")
        else:
            log_path = LOGS / f"{short(rel)}.log"
            tail = log_path.read_text(errors="replace").splitlines()[-60:] if log_path.is_file() else []
            log("\n".join(tail))
            if timed_out and not counts_on_timeout:
                self.deferred.append(rel)
                log(f"{rel}: {reason}; not counted as a failure because the run was already part-way through")
            else:
                attempts = self.attempts(rel) + 1
                self.failed[rel] = [str(attempts), reason]
                self.failed_now.append(rel)
                warn(f"{rel} failed (attempt {attempts}/{MAX_ATTEMPTS}): {reason}")
            self.index_dirty = True
        log("::endgroup::")
        clean_build_dirs()
        self.publish_index()
        return published

    def pick_next(self, perm_failed: set[str], blocked: set[str], tried: set[str], built: set[str]) -> str | None:
        plan = self.plan
        now = time.time()
        best: tuple[float, int, str] | None = None
        for pos, rel in enumerate(plan.order):
            if rel in built or rel in perm_failed or rel in blocked or rel in tried:
                continue
            ready = True
            for dep in plan.closure[rel]:
                if dep in built or rel in plan.closure_set.get(dep, ()):   # built, or a dependency cycle
                    continue
                ready = False
                break
            if not ready:
                continue
            weight = self.weight(rel)
            fits = now + weight * 60 <= self.soft
            # The first package of a run may use the whole run, however heavy it is.
            if not fits and (self.attempted_any or self.hard - now < MIN_START_S):
                continue
            key = (weight, pos, rel)
            if best is None or key < best:
                best = key
        return best[2] if best else None

    def compute_blocked(self, perm_failed: set[str], built: set[str]) -> set[str]:
        blocked: set[str] = set()
        for rel in self.plan.order:
            if rel in built or rel in perm_failed:
                continue
            if any(dep in perm_failed or dep in blocked for dep in self.plan.closure[rel]):
                blocked.add(rel)
        return blocked

    def run(self) -> dict:
        plan = self.plan
        built = self.idx.built_dirs(self.rev) & set(plan.order)
        stale = (set(self.idx.by_dir) & set(plan.order)) - built
        if stale:
            log(f"{len(stale)} build dirs were published from another termux-packages revision and will be rebuilt")
        perm_failed = {d for d in plan.order if self.attempts(d) >= MAX_ATTEMPTS and d not in built}
        blocked = self.compute_blocked(perm_failed, built)
        todo = [d for d in plan.order if d not in built and d not in perm_failed and d not in blocked]
        log(f"plan: {len(plan.order)} build dirs, {len(built)} published, {len(perm_failed)} given up, "
            f"{len(blocked)} blocked, {len(todo)} to do")
        result = {"more": False, "built": built, "remaining": [], "blocked": sorted(blocked),
                  "perm_failed": sorted(perm_failed)}
        if not todo:
            return result

        # Restore only what the remaining packages depend on.
        needed = sorted({dep for d in todo for dep in plan.closure[d]} & built)
        log(f"restoring {len(needed)} published build dirs into the container")
        container_ready()
        log(f"restored {restore_packages(self.idx, needed)} package files")

        tried: set[str] = set()
        while not self.stop_reason:
            rel = self.pick_next(perm_failed, blocked, tried, built)
            if rel is None:
                break
            tried.add(rel)
            built |= self.build_one(rel)
            if rel not in built and self.attempts(rel) >= MAX_ATTEMPTS:
                perm_failed.add(rel)
                blocked = self.compute_blocked(perm_failed, built)

        blocked = self.compute_blocked(perm_failed, built)
        remaining = [d for d in plan.order if d not in built and d not in perm_failed and d not in blocked]
        made_progress = bool(self.built_now or self.failed_now)
        result.update(built=built, remaining=remaining, blocked=sorted(blocked), perm_failed=sorted(perm_failed),
                      more=bool(remaining) and made_progress and not self.stop_reason)
        return result

    def finalize(self) -> None:
        keep = self.idx.filenames()
        debs = [a for a in self.assets if a.endswith(".deb")]
        orphans = sorted(a for a in debs if a not in keep)
        # A damaged or empty index must never make us delete the whole repository.
        if orphans and len(orphans) > max(50, len(debs) // 4):
            warn(f"{len(orphans)} of {len(debs)} .deb assets look unreferenced; not deleting anything")
        else:
            for name in orphans:
                delete_asset(name)
                self.assets.discard(name)
            if orphans:
                log(f"removed {len(orphans)} orphaned .deb assets")
        self.publish_index(force=True)


def write_summary(runner: Runner, result: dict, unknown: list[str]) -> None:
    plan = runner.plan
    built = result.get("built", set())
    lines = ["### Package repository", ""]
    lines.append(f"- Built in this run: **{len(runner.built_now)}**")
    lines.append(f"- Published in total: **{len(built)} / {len(plan.order)}** build dirs "
                 f"({runner.idx.count()} package files, {len(runner.assets)} release assets)")
    if result.get("remaining"):
        lines.append(f"- Still to build: {len(result['remaining'])}")
    if runner.failed_now:
        lines.append("- Failed in this run: " + ", ".join(f"`{short(d)}`" for d in runner.failed_now))
    if runner.deferred:
        lines.append("- Interrupted by the time limit, will be retried: " + ", ".join(f"`{short(d)}`" for d in runner.deferred))
    if result.get("perm_failed"):
        lines.append("- Given up after repeated failures (re-run with *retry_failed* after a fix): "
                     + ", ".join(f"`{short(d)}`" for d in result["perm_failed"]))
    if result.get("blocked"):
        lines.append("- Waiting on failed dependencies: " + ", ".join(f"`{short(d)}`" for d in result["blocked"][:40]))
    if unknown:
        lines.append("- Unknown package names (ignored): " + ", ".join(f"`{n}`" for n in unknown))
    if runner.stop_reason:
        lines.append(f"- **Stopped:** {runner.stop_reason}")
    lines.append(f"- Another run needed: **{'yes' if result.get('more') else 'no'}**")
    text = "\n".join(lines) + "\n"
    log(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text)


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{key}={value}\n")


def cmd_run(_args) -> int:
    if not GH_REPO or not PACKAGE_NAME:
        die("GITHUB_REPOSITORY and PACKAGE_NAME must be set")
    if not TP.is_dir():
        die(f"{TP} does not exist; run repo/prepare-packages.sh first")
    plan = Plan(wanted_names())
    if plan.unknown:
        warn("unknown package names (ignored): " + ", ".join(plan.unknown))
    if not plan.order:
        die("nothing to build: no known package names")
    estimate = plan.deb_estimate()
    log(f"{len(plan.wanted)} wanted build dirs, {len(plan.order)} including dependencies, about {estimate} package files")
    if estimate > ASSET_LIMIT:
        warn(f"estimated {estimate} package files; a release holds at most 1000 assets")

    runner = Runner(plan)
    runner.load_state()

    # Requests that must survive a chained restart are applied to the published state first.
    if os.environ.get("RETRY_FAILED") == "true" and runner.failed:
        log(f"retry_failed: clearing {len(runner.failed)} failure records")
        runner.failed.clear()
        runner.index_dirty = True
    for name in parse_names(os.environ.get("REBUILD", "")):
        rel = plan.name_to_dir.get(name)
        if rel is None:
            warn(f"rebuild: unknown package '{name}'")
        elif rel in runner.idx.by_dir:
            log(f"rebuild: {rel} will be rebuilt")
            runner.idx.drop(rel)
            runner.index_dirty = True
    runner.publish_index(force=runner.index_dirty)

    result: dict = {"more": False}
    try:
        result = runner.run()
    finally:
        try:
            runner.finalize()
        except Exception as exc:  # noqa: BLE001 - never hide the original error
            warn(f"finalizing the index failed: {exc}")
    write_summary(runner, result, plan.unknown)
    if LOGS.is_dir():   # compress logs for the artifact upload
        for p in LOGS.glob("*.log"):
            with open(p, "rb") as src, gzip.open(str(p) + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
    set_output("more", "true" if result.get("more") else "false")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").set_defaults(fn=cmd_plan)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
