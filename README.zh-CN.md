# gdebi

[English](README.md) | **简体中文**

一个用来安装/卸载本地 `.deb` 包的小工具，依赖交给 apt 解析；本仓库的 GTK 前端把特权动作
交给 **PackageKit** 执行，不再以 root 身份重开自己。

上游：<https://salsa.debian.org/debian/gdebi.git> · 本 fork：
<https://github.com/suntao12138/gdebi>

```
gdebi-gtk foo.deb           # 图形界面：先看信息，再一键安装/卸载
gdebi foo.deb               # 命令行：解析依赖并用 apt 安装
gdebi --apt-line foo.deb    # 只打印等价的 apt-get install 命令，什么都不做
```

---

## 1. 为什么要改

上游 gdebi 安装包的方式是**用自己的 GUI 以 root 身份重开一遍**：

```python
sys.exit(subprocess.call(["/usr/bin/pkexec", "gdebi-gtk", "--non-interactive", deb]))
```

这套模型在现在的桌面上已经不可用：

| 故障 | 现象 |
|---|---|
| 重开的 GUI 连不上用户的显示服务（Wayland） | `Can't create a GtkStyleContext without a display connection` |
| VTE ≥ 0.84 拒绝以 root 启动子进程 | `_vte_pty_spawn_sync … (spawn_flags & forbidden_spawn_flags()) == 0` |
| `pkexec` 在 Wayland/polkit 会话里直接死掉 | `pkexec[..]: g_variant_new_string(): requires valid UTF-8`，随后父进程在 `do_wait` 里**永远等下去** —— 窗口是真死，不是慢 |

结果就是两个方向都坏：安装起不来、卸载卡死。

## 2. 本 fork 改了什么

| 方面 | 上游 | 本 fork |
|---|---|---|
| 安装 | `pkexec` + root GUI + VTE | `PackageKitGlib.Client.install_files_async()`：`packagekitd` 以 root 跑 dpkg，polkit 负责征询用户，GUI 只显示进度 |
| 卸载 | `pkexec` + root GUI（`--remove`） | 先解析出「已安装」的 package id，再 `remove_packages_async()` |
| 等待授权 | 什么都不显示 → 看起来像崩溃 | `WAITING_FOR_AUTH` / `WAITING_FOR_LOCK` 变成可读状态 + 一行日志（“请到系统弹窗确认”） |
| 授权被拒/无人应答 | 静默挂死 | 150 秒看门狗主动取消事务，报 `Authorization was not granted (timeout).` |
| 兜底 | — | PackageKit 不可达时仍走旧的 `pkexec` 路径 |
| 命令行（`gdebi`、`gdebi-core`） | apt + dpkg 直接以 root 执行 | **未改动** |

GTK 进程全程不提权：不要 root、不要求 X11、不用 VTE 拉 root 子进程。依赖解析逻辑一点没动
—— 仍是 `python-apt`，所以界面上看到的依赖清单就是 apt 会动手的那份。

## 3. 工作原理

```
gdebi-gtk（普通用户，你的会话里）
   │  点“安装 / 卸载”
   ▼
GDebi/PackageKitInstall.py  ── PackageKitInstaller（GObject，异步）
   │  progress / log / finished 信号 ──────────────► 界面进度条 + 日志区
   ▼
org.freedesktop.PackageKit（系统 D-Bus）
   │
   ▼
packagekitd（root）── polkit 授权 ──► 系统认证弹窗
   │                    （密码，或者你的 PAM 栈里配的人脸/指纹
   │                      —— 那不是 gdebi 的事）
   ▼
dpkg/apt 后端 ── 真正安装或卸载
```

事务结束时**只会发一次** `finished(success, message)`。polkit 拒绝、dpkg 失败、文件不存在、
守护进程不可达等所有错误，都被转成这个信号，而不会抛到 GTK 主循环外。

## 4. 依赖

- Python 3 + `python3-gi`、`python3-apt`、`python3-debian`
- 图形界面需要 GTK 3（`gir1.2-gtk-3.0`）与 VTE（`gir1.2-vte-2.91`）
- PackageKit 及其 GObject 绑定（`packagekit`、`gir1.2-packagekitglib-1.0`）——
  `gdebi` 包会自动带上；缺了就会回落到旧的 `pkexec` 路径
- 桌面会话里要有 polkit agent（GNOME Shell、KDE 等都自带）来应答授权框

## 5. 安装

推荐直接用 Release 里的 deb（就是对应 tag 构建出来的那两个）：

```bash
sudo apt install ./gdebi-core_*.deb ./gdebi_*.deb
```

如果系统里已经装着发行版包，先干净地交接：

```bash
sudo apt purge gdebi gdebi-core
sudo apt install ./gdebi-core_*.deb ./gdebi_*.deb
```

版本号是 `0.9.5.8+pkgkitN`，**高于**发行版的 `0.9.5.8`，所以 `apt upgrade` 不会把它偷偷换回原版。

卸载：

```bash
sudo apt purge gdebi gdebi-core
```

## 6. 用法

### 图形界面

```bash
gdebi-gtk /path/to/package.deb          # 查看信息，然后点安装
gdebi-gtk /path/to/package.deb --remove # 打开卸载对话框
gdebi-gtk package.deb -n                # 非交互（不再询问）
gdebi-gtk package.deb --auto-close      # 成功后自动关窗口
```

在文件管理器里双击 `.deb` 走的是同一个程序（`gdebi.desktop`）。

安装/卸载过程中你会看到：

1. 包信息与 apt 给出的依赖清单；
2. 一个系统授权框（由你的 polkit agent 弹出）—— 特权动作在这里被批准；
3. 进度条，然后 `Installation finished` / `Removal finished`，失败则是红色错误行 + 原因。

### 命令行

`gdebi`（来自 `gdebi-core`）是经典命令行前端，跟 PackageKit 无关 —— 它直接调 apt/dpkg，
所以需要 root：

```bash
gdebi --version
sudo gdebi package.deb          # 解析依赖并安装，显示进度
sudo gdebi -n package.deb       # 非交互（危险：不询问）
gdebi --apt-line package.deb    # 只打印 apt-get 命令行，不做任何修改
gdebi --root=/some/root package.deb
gdebi -o Debug::pkgProblemResolver=1 package.deb
```

## 7. 授权：该期待什么

弹出来的就是桌面的 **polkit** 认证框。gdebi 只是向 PackageKit 请求事务，所以你在 `polkit-1`
这个 PAM 服务上配了什么，就会得到什么 —— 密码、人脸（howdy）、指纹、硬件令牌……

两件实务提醒：

- **一次只开一个提权弹框。** 如果同时开好几个 gdebi 窗口一起点，每个弹框都会各自启动一个
  PAM 会话；人脸识别这类模块会争抢摄像头，抢不到的那个会话被取消
  （`pam_unix(polkit-1:auth): conversation failed`），表现就是“这个框既不能刷脸也不收密码”。
  关掉多余窗口重试即可。
- 本地 `.deb` 属于**未签名来源**，PackageKit 需要
  `org.freedesktop.packagekit.package-install-untrusted` 这个 action，其默认策略是“管理员认证”
  —— 也就是每次安装要授权一次。这是设计如此，gdebi 无法跳过。

## 8. 从源码构建

```bash
sudo apt install debhelper dh-sequence-python3 pyflakes3 python3-all \
                 python3-setuptools intltool xvfb xauth lintian \
                 gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-packagekitglib-1.0
dpkg-checkbuilddeps          # 应该没有任何输出
dpkg-buildpackage -us -uc -b
```

如果构建时 shell 的 `PATH` 里排在最前面的是某个 venv 或自带解释器，pybuild 会挑到它，
然后报 `ModuleNotFoundError: No module named 'setuptools'`。用干净环境构建：

```bash
env -i HOME="$HOME" PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LANG=C.UTF-8 dpkg-buildpackage -us -uc -b
```

产物落在源码目录的上一层（`gdebi_<版本>_all.deb`、`gdebi-core_<版本>_all.deb`）。
构建过程会改写 `po/*.po` 的行号引用，介意工作区干净的话收尾跑一次 `git checkout -- po/`。

## 9. 测试

`tests/pk_smoke_manual.py` 会自建一个 dummy 包，跑真实的 PackageKit 事务：

```bash
./tests/pk_smoke_manual.py          # 安装（会弹一次授权框）
sudo ./tests/pk_smoke_manual.py --remove       # 先用 dpkg 装上，再用 PackageKit 卸载
./tests/pk_smoke_manual.py --remove-user       # 同上，但以普通用户身份运行：
                                              # 这条才覆盖真实 polkit 授权链路
```

`--remove-user` 是最关键的一条：以 root 跑卸载时 polkit 直接放行，只有非 root 跑才会经过
当初卡死的那段代码。

期望输出以 `RESULT: PASS` 结束，并且 `dpkg` 状态对得上（`install ok installed` / `(无)`）。

## 10. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 停在 `Installing…`、进度 0% | 在等 polkit 弹框，或弹框被别的窗口挡住 | 找一下认证框；gdebi 现在会打 `Waiting for authorization — please confirm in the system dialog`，150 秒后给出失败原因 |
| `Authorization was not granted (timeout).` | 弹框被取消 / 一直没人点 | 重试并在弹框里确认 |
| `PackageKit transaction failed (not authorized)` | polkit 拒绝了这个 action | 用管理员用户重试，或调整你的 polkit 策略 |
| 认证框既刷不了脸也不收密码 | 同时开了多个提权弹框，PAM 会话互相踩踏被取消 | 关掉多余窗口，只留一个弹框重试 |
| `pkexec[..]: g_variant_new_string(): requires valid UTF-8` | 还在用旧的、安装/卸载走 pkexec 的版本 | 装 `+pkgkitN` 版本 |
| `Can't create a GtkStyleContext without a display connection` | 同上：上游 pkexec 重开在 Wayland 下必死 | 装 `+pkgkitN` 版本 |
| 点了安装毫无反应、也没有弹框 | 会话里没有 polkit agent，或 PackageKit 不可达 | `systemctl status packagekit`；没有 PackageKit 时会回落 pkexec |

排查日志：

```bash
journalctl -b --since '-10 min' | grep -iE 'packagekit|polkit|gdebi|pam_'
```

## 11. 版本与发布

- 包版本：`0.9.5.8+pkgkitN`（上游版本 + fork 后缀），排序高于发行版包。
- Git tag：`v0.9.5.8-pkgkitN`（tag 名里把 `+` 换成 `-`），指向该次发布的构建提交。
- 每个 Release 都附带两个 `.deb`。变更历史以 `debian/changelog` 和 tag 为准。

## 12. 兼容性

| 平台 | 状态 |
|---|---|
| Ubuntu 24.04 (noble)、26.04 (resolute) | 已构建并实测 |
| Debian trixie / testing（libpackagekit-glib2 ≥ 1.2） | 预期可用；用到的都是 introspection 有据可查的 API |
| GNOME Shell + Wayland | 已实测（本 fork 的核心场景） |
| KDE / X11 会话 | 预期可用 —— gdebi 不再需要 root 去连显示，polkit agent 与桌面无关 |

## 13. 许可证

GPL-2+（见 `COPYING` 与 `debian/copyright`）。本 fork 沿用上游许可证与上游作者的版权；
PackageKit 集成代码位于 `GDebi/PackageKitInstall.py` 与 `GDebi/GDebiGtk.py` 的
`install_via_packagekit()` / `remove_via_packagekit()` 方法。
