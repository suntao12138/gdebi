# gdebi — PackageKit fork

This is a fork of [gdebi](https://salsa.debian.org/debian/gdebi.git) (0.9.5.x)
that changes **how a local .deb gets installed**.

## Why

Upstream gdebi installs by re-launching its whole GTK GUI as root:

```python
sys.exit(subprocess.call(["/usr/bin/pkexec", "gdebi-gtk", "--non-interactive", deb]))
```

That design breaks on Wayland sessions:

1. root cannot connect to the user's display server — GTK aborts with
   `Can't create a GtkStyleContext without a display connection`;
2. even with the display patched up, VTE (0.84+) refuses to spawn a pty as
   root — `_vte_pty_spawn_sync ... runtime check failed`, and gdebi runs dpkg
   inside that embedded terminal.

Net effect: click *Install Package* → authenticate → the window vanishes and
nothing is installed, with no error shown.

## What this fork does

`GDebi/PackageKitInstall.py` adds a small `PackageKitInstaller` GObject that
runs the install through **PackageKit** (`org.freedesktop.PackageKit`) instead:

* the privileged work happens in `packagekitd` as root;
* polkit authentication is handled by the desktop session agent;
* the gdebi process stays unprivileged the whole time and only displays
  progress (`PkProgress` percentage/status) and the log.

In `GDebiGtk.dpkg_action()` the non-root branch now prefers this path and only
falls back to the old `pkexec` relaunch when PackageKit is unavailable.
The `--remove` (uninstall) path is still on the old code path.

Requirements: `gir1.2-packagekitglib-1.0` + a working `packagekitd`
(the apt backend). No new Python dependencies.

## Smoke test

```bash
python3 -m py_compile GDebi/*.py
python3 tests/pk_smoke_manual.py    # builds a dummy deb, installs it via PackageKitInstaller
```

The smoke test needs polkit authorisation for
`org.freedesktop.packagekit.package-install-untrusted`; unattended runs have to
grant that separately (e.g. a temporary polkit rule).
