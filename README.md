# gdebi

**English** | [简体中文](README.zh-CN.md)

A simple tool to install and remove local `.deb` packages, letting apt resolve
dependencies — plus a GTK front end that does the privileged work through
**PackageKit** instead of re-launching itself as root.

Upstream: <https://salsa.debian.org/debian/gdebi.git> · This fork:
<https://github.com/suntao12138/gdebi>

```
gdebi-gtk foo.deb      # GUI: inspect, then install/remove with one click
gdebi foo.deb          # CLI: resolve deps and install through apt
gdebi --apt-line foo.deb   # print the equivalent apt-get install line, change nothing
```

---

## 1. Why this fork

Upstream gdebi installs a package by **re-executing its own GUI as root**:

```python
sys.exit(subprocess.call(["/usr/bin/pkexec", "gdebi-gtk", "--non-interactive", deb]))
```

That model is dead on current desktops:

| Failure | What you see |
|---|---|
| The relaunched GUI cannot reach the user's display server (Wayland) | `Can't create a GtkStyleContext without a display connection` |
| VTE ≥ 0.84 refuses to spawn a process as root | `_vte_pty_spawn_sync … (spawn_flags & forbidden_spawn_flags()) == 0` |
| `pkexec` dies before doing anything (Wayland / polkit session) | `pkexec[..]: g_variant_new_string(): requires valid UTF-8`, and the parent process waits in `do_wait` **forever** — the window is dead, not slow |

So both directions of the operation were broken: install could not start, and
remove hung permanently.

## 2. What this fork changes

| Area | Upstream | This fork |
|---|---|---|
| Install | `pkexec` + root GUI + VTE | `PackageKitGlib.Client.install_files_async()` — `packagekitd` runs dpkg as root, polkit asks the user, the GUI only displays progress |
| Remove | `pkexec` + root GUI (`--remove`) | `PackageKitGlib.Client.remove_packages_async()` after resolving the installed package id |
| Waiting for authorization | nothing shown → looks like a crash | `WAITING_FOR_AUTH` / `WAITING_FOR_LOCK` are surfaced as status text plus a log line ("please confirm the system dialog") |
| Denied / unanswered prompt | silent hang | a 150 s watchdog cancels the transaction and reports `Authorization was not granted (timeout).` |
| Fallback | — | if PackageKit cannot be reached, the old `pkexec` path is still used |
| CLI (`gdebi`, `gdebi-core`) | apt + dpkg as root | **unchanged** |

The GTK process never gains privileges: no root, no X11 requirements, no VTE
root spawn. Nothing about dependency resolution changed — it is still
`python-apt`, so the dependency list you see is the one apt would act on.

## 3. How it works

```
gdebi-gtk (unprivileged, your session)
   │  click Install / Remove
   ▼
GDebi/PackageKitInstall.py  ── PackageKitInstaller (GObject, async)
   │  progress / log / finished signals  ─────────────► GUI progress bar + log
   ▼
org.freedesktop.PackageKit  (system D-Bus)
   │
   ▼
packagekitd (root)  ── polkit authorization ──► system auth dialog
   │                     (password, or face/fingerprint if your PAM stack
   │                      provides it — that part is not gdebi's business)
   ▼
dpkg/apt backend  ── installs or removes the package
```

The transaction ends with exactly one `finished(success, message)` signal. All
errors — polkit denial, dpkg failure, missing file, unavailable daemon — are
converted into that signal instead of being raised into the GTK main loop.

## 4. Requirements

- Python 3 with `python3-gi`, `python3-apt`, `python3-debian`
- GTK 3 (`gir1.2-gtk-3.0`), VTE (`gir1.2-vte-2.91`) for the GUI
- PackageKit + GObject bindings (`packagekit`, `gir1.2-packagekitglib-1.0`) —
  pulled in automatically by the `gdebi` package; without them the fork falls
  back to the old `pkexec` path
- A polkit agent in your desktop session (GNOME Shell, KDE, …) to answer the
  authorization dialog

## 5. Install

From the release page (recommended — the `.deb`s are built from the tag):

```bash
sudo apt install ./gdebi-core_*.deb ./gdebi_*.deb
```

If the distribution package is installed, hand over cleanly first:

```bash
sudo apt purge gdebi gdebi-core
sudo apt install ./gdebi-core_*.deb ./gdebi_*.deb
```

The version string is `0.9.5.8+pkgkitN`, i.e. **higher** than the distribution's
`0.9.5.8`, so `apt upgrade` will not silently replace it with the original.

Uninstall:

```bash
sudo apt purge gdebi gdebi-core
```

## 6. Usage

### GUI

```bash
gdebi-gtk /path/to/package.deb          # inspect, then Install
gdebi-gtk /path/to/package.deb --remove # open the removal dialog
gdebi-gtk package.deb -n                # non-interactive (no questions)
gdebi-gtk package.deb --auto-close      # close the window after success
```

Double-clicking a `.deb` in your file manager uses the same program
(`gdebi.desktop`).

What you will see during an install/remove:

1. the package details and the dependency list from apt;
2. a system authorization dialog (from your polkit agent) — this is where the
   privileged action is approved;
3. a progress bar, then `Installation finished` / `Removal finished`, or a red
   failure line with the reason.

### CLI

`gdebi` (from `gdebi-core`) is the classic command line front end and has
nothing to do with PackageKit — it runs apt/dpkg directly, so it needs root:

```bash
gdebi --version
sudo gdebi package.deb          # resolve + install, show progress
sudo gdebi -n package.deb       # non-interactive (dangerous: no prompts)
gdebi --apt-line package.deb    # print the apt-get line, change nothing
gdebi --root=/some/root package.deb
gdebi -o Debug::pkgProblemResolver=1 package.deb
```

## 7. Authorization: what to expect

The authorization dialog is your desktop's **polkit** dialog; gdebi only asks
PackageKit for the transaction, so whatever your PAM stack does for the
`polkit-1` service is what you get — password, face recognition (howdy),
fingerprint, hardware token, …

Two practical notes:

- **One privileged dialog at a time.** If you open several gdebi windows and
  start them all, each prompt starts its own PAM conversation; modules such as
  face recognition contend for the camera and the losing conversations get
  cancelled (`pam_unix(polkit-1:auth): conversation failed`), which looks like
  "the dialog accepts nothing". Close the extra windows and retry.
- A local `.deb` is an **untrusted** origin, so PackageKit requires the
  `org.freedesktop.packagekit.package-install-untrusted` action, whose default
  policy is "authenticate as admin" — one authorization per install. That is by
  design; it is not something gdebi can skip.

## 8. Build from source

```bash
sudo apt install debhelper dh-sequence-python3 pyflakes3 python3-all \
                 python3-setuptools intltool xvfb xauth lintian \
                 gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-packagekitglib-1.0
dpkg-checkbuilddeps          # must stay quiet
dpkg-buildpackage -us -uc -b
```

If you build inside a shell where a virtualenv or a bundled interpreter comes
first in `PATH`, pybuild will pick that interpreter and fail on
`ModuleNotFoundError: No module named 'setuptools'`. Build with a clean
environment:

```bash
env -i HOME="$HOME" PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LANG=C.UTF-8 dpkg-buildpackage -us -uc -b
```

Artifacts land one directory above the source tree
(`gdebi_<version>_all.deb`, `gdebi-core_<version>_all.deb`). The build rewrites
`po/*.po` (line references), so `git checkout -- po/` afterwards if you care
about a clean tree.

## 9. Tests

`tests/pk_smoke_manual.py` drives the real PackageKit transaction against a
dummy package it builds itself:

```bash
./tests/pk_smoke_manual.py          # install (needs an authorization dialog)
sudo ./tests/pk_smoke_manual.py --remove       # install with dpkg, remove via PackageKit
./tests/pk_smoke_manual.py --remove-user       # same, as an unprivileged user:
                                               # exercises the real polkit path
```

`--remove-user` is the interesting one: running the removal as root makes
polkit approve immediately, so only the unprivileged run covers the code path
that used to hang.

Expected output ends with `RESULT: PASS` and the matching `dpkg` state
(`install ok installed` / `(none)`).

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Window stuck at `Installing…` with no progress | waiting for the polkit dialog, or the dialog is behind other windows | look for the auth dialog; gdebi now logs `Waiting for authorization — please confirm in the system dialog` and fails with a message after 150 s |
| `Authorization was not granted (timeout).` | dialog was cancelled / never answered | retry and approve the dialog |
| `PackageKit transaction failed (not authorized)` | polkit denied the action | retry as an admin user, or adjust your polkit policy |
| The auth dialog accepts neither face nor password | several privileged dialogs opened at once; PAM conversations collided and were cancelled | close the extra windows, retry with one dialog |
| `pkexec[..]: g_variant_new_string(): requires valid UTF-8` | you are running an old build that still uses the pkexec path for install/remove | install a `+pkgkitN` build |
| `Can't create a GtkStyleContext without a display connection` | same: upstream pkexec relaunch under Wayland | install a `+pkgkitN` build |
| Install silently does nothing, no dialog | no polkit agent in the session, or PackageKit unreachable | check `systemctl status packagekit`; without PackageKit gdebi falls back to pkexec |

Logs are useful for diagnosis:

```bash
journalctl -b --since '-10 min' | grep -iE 'packagekit|polkit|gdebi|pam_'
```

## 11. Versioning and releases

- Package version: `0.9.5.8+pkgkitN` (upstream version + fork suffix), so it
  sorts above the distribution package.
- Git tag: `v0.9.5.8-pkgkitN` (the `+` is replaced by `-` to keep the tag name
  simple), pointing at the commit the release was built from.
- Each release ships both `.deb` files as assets. `debian/changelog` and the
  tag are the authoritative history of what changed.

## 12. Compatibility

| Platform | Status |
|---|---|
| Ubuntu 24.04 (noble), 26.04 (resolute) | built and tested |
| Debian trixie / testing (libpackagekit-glib2 ≥ 1.2) | expected to work; API used is the introspection-documented one |
| GNOME Shell + Wayland | tested (the whole point of the fork) |
| KDE / X11 sessions | expected to work — gdebi no longer needs a display as root, and polkit agents are desktop-agnostic |

## 13. License

GPL-2+ (see `COPYING` and `debian/copyright`). This fork keeps the upstream
licence and the upstream authors' copyright; the PackageKit integration lives in
`GDebi/PackageKitInstall.py` and the `install_via_packagekit()` /
`remove_via_packagekit()` methods of `GDebi/GDebiGtk.py`.
