#!/usr/bin/python3
"""独立验收：用 PackageKitInstaller 装一个 dummy deb，并核对 dpkg 状态。"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib
from GDebi.PackageKitInstall import PackageKitInstaller

DEB = "/tmp/pk_smoke_pkg/gdebi-pk-smoke_1.0_all.deb"
PKG = "gdebi-pk-smoke"

result = {}


def on_progress(inst, pct, status):
    print("  [progress] %5.1f%%  %s" % (pct, status))


def on_log(inst, line):
    print("  [log] %s" % line)


def on_finished(inst, ok, msg):
    result["ok"] = ok
    result["msg"] = msg
    print("  [finished] success=%s message=%s" % (ok, msg))
    loop.quit()


print("available() =", PackageKitInstaller.available())
if not PackageKitInstaller.available():
    print("RESULT: SKIP (PackageKit 不可用)")
    sys.exit(3)

inst = PackageKitInstaller(DEB)
inst.connect("progress", on_progress)
inst.connect("log", on_log)
inst.connect("finished", on_finished)

loop = GLib.MainLoop()
GLib.timeout_add_seconds(120, lambda: (loop.quit() or False))
inst.start()
loop.run()

installed = subprocess.run(["dpkg-query", "-W", "-f=${Status}", PKG],
                           capture_output=True, text=True).stdout.strip()
print("dpkg 状态:", installed or "(无)")
ok = result.get("ok", False)
print("RESULT:", "PASS" if (ok and installed == "install ok installed") else "FAIL")
sys.exit(0 if (ok and installed == "install ok installed") else 1)
