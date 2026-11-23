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
| `icon_hue_shift` | Give each package ID its own icon accent colour. |

The run page also shows a summary of the settings that were used.

## What is customised

- `custom/apply_custom.py` sets the app name, generates the launcher icons from `custom/icon.png`,
  and injects the startup message plus helper commands (`sandbox-info`, `export-out`, `clean-cache`)
  into every bootstrap archive.
- `.github/scripts/preflight-packages.sh` fixes upstream package problems at build time: it
  caches and verifies the `less` source tarball, breaks the `pulseaudio`/`libmpg123` dependency
  loop that stops `openjdk-17` from building, pins `gradle` to `openjdk-17`, and retries
  `termux-am`'s Gradle build through a Maven Central mirror (Maven Central rate-limits CI IPs).
- `.github/scripts/run-build.sh` validates the inputs, runs `build-termux.sh` and logs progress.

## Limits

- GitHub jobs stop after 6 hours. Heavy source builds such as `clang`/LLVM are unlikely to fit
  alongside a normal bootstrap build; build them in a separate run.
- This repository is public, so anyone signed in to GitHub can download the artifacts of its runs.
