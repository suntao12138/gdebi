#!/usr/bin/python3
"""独立验收：用 PackageKitInstaller 装/卸一个 dummy deb，并核对 dpkg 状态。

  ./tests/pk_smoke_manual.py            安装用例（造 dummy deb 后安装，需要 polkit 授权）
  sudo ./tests/pk_smoke_manual.py --remove
                                        先用 dpkg -i 装好，再用 PackageKit 卸载
  ./tests/pk_smoke_manual.py --remove-user
                                        以普通用户身份用 PackageKit 卸载（包须已装好），
                                        专门走 polkit 授权链路
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gi.repository import GLib
from GDebi.PackageKitInstall import PackageKitInstaller

DEB = "/tmp/pk_smoke_pkg/gdebi-pk-smoke_1.0_all.deb"
BUILD_DIR = "/tmp/pk_smoke_pkg/build"
PKG = "gdebi-pk-smoke"

result = {}
loop = None


def on_progress(inst, pct, status):
    print("  [progress] %5.1f%%  %s" % (pct, status))


def on_log(inst, line):
    print("  [log] %s" % line)


def on_finished(inst, ok, msg):
    result["ok"] = ok
    result["msg"] = msg
    print("  [finished] success=%s message=%s" % (ok, msg))
    loop.quit()


def dpkg_status():
    return subprocess.run(["dpkg-query", "-W", "-f=${Status}", PKG],
                          capture_output=True, text=True).stdout.strip()


def build_dummy_deb():
    """造一个最小的 dummy deb（卸载用例需要它先被装上）。"""
    os.makedirs(os.path.join(BUILD_DIR, "DEBIAN"), exist_ok=True)
    os.makedirs(os.path.join(BUILD_DIR, "usr/share/doc/" + PKG), exist_ok=True)
    with open(os.path.join(BUILD_DIR, "DEBIAN", "control"), "w") as f:
        f.write("Package: %s\n"
                "Version: 1.0\n"
                "Architecture: all\n"
                "Maintainer: gdebi smoke <nobody@example.com>\n"
                "Description: dummy package for the PackageKit smoke test\n"
                " gdebi smoke test payload, safe to remove.\n" % PKG)
    with open(os.path.join(BUILD_DIR, "usr/share/doc/" + PKG, "README"),
              "w") as f:
        f.write("gdebi PackageKit smoke test\n")
    subprocess.run(["dpkg-deb", "-b", BUILD_DIR, DEB], check=True)


def run_installer(**kwargs):
    # "loop" is global: on_finished() quits it
    global loop
    result.clear()
    inst = PackageKitInstaller(DEB, **kwargs)
    inst.connect("progress", on_progress)
    inst.connect("log", on_log)
    inst.connect("finished", on_finished)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(120, lambda: (loop.quit() or False))
    inst.start()
    loop.run()
    return result.get("ok", False)


print("available() =", PackageKitInstaller.available())
if not PackageKitInstaller.available():
    print("RESULT: SKIP (PackageKit 不可用)")
    sys.exit(3)

if "--remove-user" in sys.argv[1:]:
    # 卸载用例（普通用户）：不碰 dpkg -i/-P，只让 PackageKit 去卸，
    # 这样才会真正触发 polkit 授权链路。包必须已经装好。
    if os.geteuid() == 0:
        print("RESULT: SKIP (本模式必须以普通用户运行)")
        sys.exit(3)
    print("dpkg 状态（卸载前）:", dpkg_status() or "(无)")

    ok = run_installer(remove=True, pkgname=PKG)
    after = dpkg_status()
    print("dpkg 状态（卸载后）:", after or "(无)")
    passed = ok and after == ""
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)

if "--remove" in sys.argv[1:]:
    # 卸载用例：dpkg -i 装好（需要 root），再让 PackageKit 卸掉
    build_dummy_deb()
    # 清掉可能残留的旧包，让用例可以重复跑（失败无所谓）
    subprocess.run(["dpkg", "-P", PKG], capture_output=True, text=True)
    subprocess.run(["dpkg", "-i", DEB], check=True)
    print("dpkg 状态（卸载前）:", dpkg_status())

    ok = run_installer(remove=True, pkgname=PKG)
    after = dpkg_status()
    print("dpkg 状态（卸载后）:", after or "(无)")
    # dpkg-query 对已彻底移除的包不输出状态；"deinstall" 之类算失败
    passed = ok and after == ""
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)

# 安装用例：先造 dummy deb（dpkg-deb -b 可覆盖旧内容），再让 PackageKit 装
build_dummy_deb()
ok = run_installer()
installed = dpkg_status()
print("dpkg 状态:", installed or "(无)")
print("RESULT:", "PASS" if (ok and installed == "install ok installed") else "FAIL")
sys.exit(0 if (ok and installed == "install ok installed") else 1)
