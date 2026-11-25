# termux-generator (GitHub Actions)

Builds a rebranded Termux app (custom package ID, app name, launcher icon and startup message)
with extra packages pre-installed. Runs entirely on GitHub Actions; nothing to install locally.

## Build an app

1. Open **Actions → Build custom Termux → Run workflow**.
2. Fill in the inputs and start it. A full build takes roughly 1.5 hours.
3. When it finishes, download the artifact `termux-<package_name>-f-droid` from the run page
   (kept for 7 days). It contains the APK(s) and `build.log.gz`.

| Input | Meaning |
|---|---|
| `package_name` | Android package ID, e.g. `com.example.termux`. Letters and digits in dot-separated parts; it must not contain `com.termux`, dashes or underscores. |
| `app_name` | Name shown in the launcher. **Required.** |
| `startup_message` | Text above the first prompt. Empty means `Welcome to <app name>`. |
| `architectures` | Comma-separated: `aarch64`, `arm`, `i686`, `x86_64`. |
| `addons` | `none`, `all`, or a list from `tasker,float,widget,api,boot,styling,gui,x11`. |
| `extra_packages` | Comma-separated Termux packages to bake in, e.g. `git,openssh,openjdk-17,gradle`. `fzf` and `tmux` are always added. |
| `package_repo` | apt repository the app installs packages from. Empty = this repository's `repo` release (see below), `none` = the official Termux repository (only useful for `com.termux`), or an `https://…/` URL. |
| `icon_hue_shift` | Give each package ID its own icon accent colour. |

The run page also shows a summary of the settings that were used.

## What is customised

- `custom/apply_custom.py` sets the app name, generates the launcher icons from `custom/icon.png`,
  and injects the startup message plus helper commands (`sandbox-info`, `export-out`, `clean-cache`)
  into every bootstrap archive.
- `.github/scripts/preflight-packages.sh` fixes upstream package problems at build time: it
  caches and verifies the `less` source tarball, breaks the `pulseaudio`/`libmpg123` dependency
  loop that stops `openjdk-17` from building, pins `gradle` to `openjdk-17` (app builds only),
  points `apt` at the package repository, and retries
  `termux-am`'s Gradle build through a Maven Central mirror (Maven Central rate-limits CI IPs).
- `.github/scripts/run-build.sh` validates the inputs, runs `build-termux.sh` and logs progress.

## Package repository

Baking every package into the APK makes it several gigabytes. Instead, the APK stays small and
installs packages from its own apt repository, which a second workflow builds and hosts as a
GitHub release. (Official Termux packages cannot be used: they are built for the `com.termux`
prefix and break under any other package ID, so everything is compiled here.)

### Build it

1. **Actions → Build package repo → Run workflow.** For a first check, put `jq` into `only`
   (builds just `jq` and its dependencies, a few minutes).
2. For the real thing leave `only` empty. The workflow builds everything in `repo/packages.txt`
   plus all dependencies. One run lasts at most about 5.5 hours, so it publishes what it finished
   and **starts the next run itself** until nothing is left (up to `max_runs`, default 20).
   Light packages go first; each very heavy package (`libllvm` = clang/lldb, `rust`,
   `nodejs-lts`, `openjdk-21`) gets a run of its own.
3. Progress is in each run's summary. Packages are usable as soon as they are published.

Everything lives in the release tagged `repo`: the `.deb` files, the apt index (`Packages`,
`Packages.gz`, `Release`), and bookkeeping (`failed.tsv`, `timings.tsv`). A package that fails
twice is skipped (and everything that depends on it waits); fix the cause and run again with
`retry_failed`. Build logs of every run are attached to it as the `repo-build-logs-run-N`
artifact.

### Use it

Build the app with **Build custom Termux** (`package_repo` empty). The generated app's
`apt` is configured with

```
deb [trusted=yes] https://github.com/<owner>/<repo>/releases/download/repo/ ./
```

so inside the app `pkg update && pkg install clang git` works. The repository must be built for
the same `package_name` as the app (default `com.rebranded.termux`). Until at least one run of
*Build package repo* has finished, the app's `pkg update` finds nothing; use `package_repo: none`
to keep the official repository meanwhile.

### Add more packages

- Append names to `repo/packages.txt` (one per line or comma-separated; a package, a
  sub-package such as `aapt2`, or an alias such as `man` all work) and push, then run
  **Build package repo**; or
- run the workflow with `add_packages` set (for example `ruby,php`): the names are appended to
  `repo/packages.txt` for you and built. Already published packages are not rebuilt.

`rebuild` forces specific packages to be rebuilt.

### Notes and limits

- `repo/termux-packages.rev` pins the termux-packages commit used by **both** workflows, so the
  APK's bootstrap and the repository always agree on library versions. To move to newer upstream
  code, put a new commit hash there; the next run rebuilds every package against it (many hours).
  `TERMUX_PACKAGES_REV=latest` in the environment ignores the pin.
- `repo/weights.txt` holds rough build times per package so a run only starts packages that still
  fit into the time left. Measured times replace the guesses automatically.
- A release holds at most 1000 files; the current list needs about 500. The workflow stops with
  a message before it would hit the limit.
- Packages are installed with `trusted=yes` (the repository is not signed), and the repository is
  public like this GitHub repository.
- Individual packages may fail to build upstream or on a hosted runner. `libllvm` and `rust` are
  the likeliest to exceed the 6 hour job limit; their run summary says so.

## Limits

- GitHub jobs stop after 6 hours. Heavy source builds such as `clang`/LLVM do not fit alongside a
  normal bootstrap build; put them in the package repository instead.
- This repository is public, so anyone signed in to GitHub can download the artifacts of its runs.
