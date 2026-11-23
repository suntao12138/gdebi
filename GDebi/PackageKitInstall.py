# Copyright (c) 2026 gdebi contributors
#
# This file is part of GDebi
#
# GDebi is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as published
# by the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# GDebi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GDebi; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#

"""Install or remove a package through PackageKit.

Instead of re-launching the whole GUI as root via pkexec (which is
broken on Wayland sessions, since root cannot connect to the user
display server), the actual work is delegated to packagekitd.
polkit authentication is handled by the desktop session agent, the
gdebi process itself stays unprivileged and only displays progress.

While packagekitd waits for the user to answer the polkit prompt the
status is surfaced explicitly and a watchdog cancels the transaction
after AUTH_WAIT_TIMEOUT_SECONDS, so a refused or never-answered prompt
ends as a normal failure instead of a window that hangs forever.

The API used here follows the *local* PackageKitGlib introspection data
(libpackagekit-glib2 >= 1.2): async calls complete via a standard
Gio.AsyncReadyCallback and the results are collected with
Pk.Client.generic_finish().
"""

import logging
import os

import gi
from gi.repository import Gio
from gi.repository import GObject
from gi.repository import GLib

from gettext import gettext as _

Pk = None
_PK_IMPORT_ERROR = None
try:
    gi.require_version("PackageKitGlib", "1.0")
    from gi.repository import PackageKitGlib as Pk
except (ValueError, ImportError) as e:
    # no gir1.2-packagekitglib-1.0 on this system - callers must fall back
    _PK_IMPORT_ERROR = str(e)

# polkit 认证框在 Wayland 会话里可能被人脸/指纹模块卡住，事务会一直停在
# WAITING_FOR_AUTH。超过这个秒数就主动取消，避免 GUI 永久无反馈。
AUTH_WAIT_TIMEOUT_SECONDS = 150


def _waiting_texts():
    """Map the "the daemon is waiting for you" statuses to user text.

    Built at call time, not at import time: gettext is only set up once
    the application started, and the Pk enums only exist when the
    PackageKit bindings could be loaded at all.
    """
    if Pk is None:
        return {}
    return {
        Pk.StatusEnum.WAITING_FOR_AUTH:
            _("Waiting for authorization — please confirm in the system "
              "dialog"),
        Pk.StatusEnum.WAITING_FOR_LOCK:
            _("Waiting for the package database lock"),
    }


def waiting_status_texts():
    """The status strings the "progress" signal uses for the waiting states.

    Callers (the GUI) compare the status_text of a progress signal against
    this tuple to spot the "the daemon is waiting for you" moments.
    """
    return tuple(_waiting_texts().values())


class PackageKitInstaller(GObject.GObject):
    """Asynchronously install or remove a package via PackageKit.

    Signals:
      progress(percentage, status_text)  percentage is 0.0 .. 100.0
      log(line)                          human readable log line
      finished(success, message)         terminal state, emitted exactly once
    """

    __gsignals__ = {
        "progress": (GObject.SignalFlags.RUN_FIRST, None,
                     (GObject.TYPE_DOUBLE, GObject.TYPE_STRING)),
        "log": (GObject.SignalFlags.RUN_FIRST, None,
                (GObject.TYPE_STRING,)),
        "finished": (GObject.SignalFlags.RUN_FIRST, None,
                     (GObject.TYPE_BOOLEAN, GObject.TYPE_STRING)),
    }

    def __init__(self, filename, remove=False, pkgname=None):
        """filename is the local .deb, remove=True removes the package instead.

        pkgname lets a caller that already parsed the deb (the GUI does)
        hand over the package name; when it is None the name is looked up
        from the deb file itself via PackageKit (get_details_local_async).
        Nothing here depends on any GUI state.
        """
        GObject.GObject.__init__(self)
        self._filename = os.path.abspath(filename)
        self._remove = bool(remove)
        self._pkgname = pkgname or None
        self._client = None
        self._cancellable = None
        self._finished_emitted = False
        self._last_status = None
        self._last_pkgid = None
        # 看门狗（等待授权超时）与「等待类状态只记一次日志」
        self._wait_timeout_id = None
        self._wait_logged = set()

    @property
    def is_remove(self):
        """True when this instance removes a package instead of installing."""
        return self._remove

    @staticmethod
    def available():
        """Return True if PackageKit can be used from this process."""
        if Pk is None:
            logging.info("PackageKit bindings unavailable: %s",
                         _PK_IMPORT_ERROR)
            return False
        try:
            # building a proxy for the manager object triggers D-Bus
            # activation, so this fails if packagekitd cannot be reached
            # or started (no system bus, no policy, backend missing)
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.NONE,
                None,
                Pk.DBUS_SERVICE,
                Pk.DBUS_PATH,
                Pk.DBUS_INTERFACE,
                None)
            if proxy is None:
                return False
        except GLib.Error as e:
            logging.info("PackageKit not reachable: %s", e.message)
            return False
        except Exception as e:
            logging.info("PackageKit not reachable: %s", e)
            return False
        return True

    def cancel(self):
        """Best-effort cancellation of a running transaction."""
        if self._cancellable is not None:
            try:
                self._cancellable.cancel()
            except Exception:
                logging.exception("cancelling the PackageKit transaction failed")

    def start(self):
        """Start the transaction, returns immediately.

        Never raises: every failure ends up as a "finished(False, msg)"
        signal instead.
        """
        try:
            if Pk is None:
                self._emit_finished(
                    False, _("PackageKit support is not installed (%s).")
                    % _PK_IMPORT_ERROR)
                return
            # a removal addressed by package name does not need the .deb
            # around any more, everything else does
            if (not self._remove or self._pkgname is None) and \
                    not os.path.isfile(self._filename):
                self._emit_finished(
                    False, _("File not found: %s") % self._filename)
                return
            self._client = Pk.Client()
            self._cancellable = Gio.Cancellable()
            if self._remove:
                self.emit("log", _("Starting removal via PackageKit..."))
                self._start_remove()
                return
            self.emit("log", _("Starting installation via PackageKit..."))
            # signature (verified via introspection on libpackagekit-glib2
            # 1.3.4): install_files_async(transaction_flags, files,
            # cancellable, progress_callback, progress_user_data,
            # callback_ready, user_data). PyGObject expects the two
            # user_data slots as tuples and splats their items into the
            # python callbacks, and the progress callback is invoked as
            # (progress, PkProgressType, *user_data) - so the callbacks
            # below use *args and pick what they need by duck-typing.
            self._client.install_files_async(
                int(Pk.TransactionFlagEnum.NONE),
                [self._filename],
                self._cancellable,
                self._progress_cb, (self,),
                self._ready_cb, (self,))
        except Exception as e:
            logging.exception("could not start the PackageKit transaction")
            self._emit_finished(False, str(e) or repr(e))

    def _start_remove(self):
        """Removal needs a package id, so resolve the name first.

        Two async stages: (1) if no pkgname was handed over, ask
        PackageKit for the details of the local file to learn its name,
        (2) resolve that name against the *installed* packages to get the
        full "name;version;arch;repo" id remove_packages_async() wants.
        """
        if self._pkgname:
            self._resolve_installed(self._pkgname)
            return
        # get_details_local_async(files, cancellable, progress_cb,
        # progress_user_data, callback_ready, user_data) - same shape as
        # install_files_async, verified by introspection.
        self._client.get_details_local_async(
            [self._filename],
            self._cancellable,
            self._progress_cb, (self,),
            self._name_ready_cb, (self,))

    def _name_ready_cb(self, *args):
        """Stage 1 of a removal: read the package name out of the deb."""
        try:
            res = self._find_result(args)
            if res is None:
                self._emit_finished(
                    False, _("Could not collect the PackageKit results."))
                return
            results = self._finish(res)
            if results is None:
                return
            name = None
            for details in results.get_details_array():
                pkgid = details.get_package_id()
                if pkgid:
                    name = str(pkgid).split(";")[0]
                    break
            if not name:
                if not self._fail_if_not_success(results):
                    self._emit_finished(
                        False, _("Could not determine the package name "
                                 "of %s.") % self._filename)
                return
            self._pkgname = name
            self._resolve_installed(name)
        except Exception as e:
            logging.exception("could not read the package name from the deb")
            self._emit_finished(False, str(e) or repr(e))

    def _resolve_installed(self, name):
        """Stage 2 of a removal: name -> installed package id(s)."""
        self.emit("log", _("Looking up the installed package '%s'...") % name)
        # resolve_async(filters, packages, cancellable, progress_cb,
        # progress_user_data, callback_ready, user_data); the INSTALLED
        # filter keeps the daemon from matching candidates in the repos.
        self._client.resolve_async(
            int(Pk.FilterEnum.INSTALLED),
            [name],
            self._cancellable,
            self._progress_cb, (self,),
            self._remove_ready_cb, (self,))

    def _remove_ready_cb(self, *args):
        """Stage 2 of a removal: start remove_packages_async()."""
        try:
            res = self._find_result(args)
            if res is None:
                self._emit_finished(
                    False, _("Could not collect the PackageKit results."))
                return
            results = self._finish(res)
            if results is None:
                return
            package_ids = []
            for package in results.get_package_array():
                try:
                    pkgid = package.get_id()
                except Exception:
                    continue
                if pkgid and str(pkgid) not in package_ids:
                    package_ids.append(str(pkgid))
            if not package_ids:
                if not self._fail_if_not_success(results):
                    self._emit_finished(
                        False, _("Package '%s' is not installed.")
                        % (self._pkgname or self._filename))
                return
            # remove_packages_async(transaction_flags, package_ids,
            # allow_deps, autoremove, cancellable, progress_callback,
            # progress_user_data, callback_ready, user_data) - note the
            # two booleans, which the install call does not have; the
            # dependency check was already done against the apt cache in
            # the GUI, so allow_deps/autoremove stay off here.
            self._client.remove_packages_async(
                int(Pk.TransactionFlagEnum.NONE),
                package_ids,
                False,
                False,
                self._cancellable,
                self._progress_cb, (self,),
                self._ready_cb, (self,))
        except Exception as e:
            logging.exception("could not start the PackageKit removal")
            self._emit_finished(False, str(e) or repr(e))

    # -- internal callbacks ------------------------------------------------

    def _progress_cb(self, *args):
        """PkProgressCallback: relay percentage/status as our signals.

        Depending on the PackageKitGlib version PyGObject hands us either
        (client, progress, type, user_data) or (progress, type, ...),
        so pick the PkProgress object by duck-typing.
        """
        try:
            progress = None
            for arg in args:
                if hasattr(arg, "get_percentage") and \
                        hasattr(arg, "get_status"):
                    progress = arg
                    break
            if progress is None:
                logging.warning("unexpected PackageKit progress args: %r",
                                args)
                return
            percentage = progress.get_percentage()
            if percentage is None or percentage < 0:
                percentage = 0.0
            percentage = float(min(100.0, percentage))
            status = progress.get_status()
            pkgid = progress.get_package_id()
            status_text = ""
            waiting_text = None
            if status is not None:
                waiting_text = self._note_waiting(status)
                status_text = waiting_text or \
                    str(status.value_nick).replace("_", " ")
            self.emit("progress", percentage, status_text)
            # only log status changes, not every progress tick; the
            # "waiting" statuses are logged by _note_waiting() exactly once
            if status != self._last_status or pkgid != self._last_pkgid:
                self._last_status = status
                self._last_pkgid = pkgid
                if waiting_text is None:
                    line = status_text
                    if pkgid:
                        if line:
                            line += ": "
                        line += pkgid
                    self.emit("log", line)
        except Exception:
            logging.exception("error while processing PackageKit progress")

    def _note_waiting(self, status):
        """Handle the "waiting for the user" statuses.

        Returns the human readable text to show for *status*, or None when
        *status* is not one of them (the caller then falls back to the raw
        PackageKit nick).  The hint is logged only once per status so a
        chatty daemon does not flood the log, and a watchdog is armed for
        WAITING_FOR_AUTH so a denied or never-answered polkit prompt ends
        in a failed transaction instead of a frozen window.
        """
        text = _waiting_texts().get(status)
        if text is None:
            return None
        nick = str(status.value_nick)
        if nick not in self._wait_logged:
            self._wait_logged.add(nick)
            self.emit("log", text)
        if status == Pk.StatusEnum.WAITING_FOR_AUTH:
            self._arm_wait_watchdog()
        return text

    def _ready_cb(self, *args):
        """Gio.AsyncReadyCallback: collect results and emit "finished".

        The async result object is picked from the (possibly version
        dependent) callback arguments by duck-typing.
        """
        try:
            res = self._find_result(args)
            if res is None:
                self._emit_finished(
                    False, _("Could not collect the PackageKit results."))
                return
            results = self._finish(res)
            if results is None:
                return
            if results.get_exit_code() == Pk.ExitEnum.SUCCESS:
                self._emit_finished(
                    True, _("Removal finished") if self._remove
                    else _("Installation finished"))
                return
            self._emit_finished(False, self._exit_message(results))
        except Exception as e:
            logging.exception("error while collecting PackageKit results")
            self._emit_finished(False, str(e) or repr(e))

    # -- result / status helpers -------------------------------------------

    @staticmethod
    def _find_result(args):
        """Pick the Gio.AsyncResult out of the callback arguments."""
        for arg in args:
            if hasattr(arg, "get_source_object"):
                return arg
        return None

    def _finish(self, res):
        """generic_finish() wrapper; returns None on a GError.

        polkit denials, daemon errors and the dpkg/apt error details all
        end up in this GError, and they are turned into finished(False,..)
        instead of being raised into the GTK main loop.
        """
        try:
            return self._client.generic_finish(res)
        except GLib.Error as e:
            msg = e.message or str(e)
            logging.info("PackageKit transaction failed: %s", msg)
            self._emit_finished(False, msg)
            return None

    def _exit_message(self, results):
        """Human readable text for a transaction that did not succeed.

        The PackageKit error code is appended whenever there is one; for
        the plain "failed"/"cancelled" exit codes it is what separates
        "you did not authorize this" (not authorized) from "the package
        really did not install" (e.g. dependencies, dpkg error).
        """
        exit_code = results.get_exit_code()
        nick = (str(exit_code.value_nick) if exit_code is not None
                else "unknown")
        error_nick = None
        try:
            error_code = results.get_error_code()
            if error_code is not None and int(error_code) != 0:
                error_nick = str(error_code.value_nick).replace("_", " ")
        except Exception:
            pass
        if error_nick is None:
            return _("PackageKit returned: %s") % nick
        if nick in ("failed", "cancelled"):
            return _("PackageKit transaction %s (%s)") % (nick, error_nick)
        return _("PackageKit returned: %s (%s)") % (nick, error_nick)

    def _fail_if_not_success(self, results):
        """Fail a lookup stage that did not return ExitEnum.SUCCESS.

        Returns True when the caller must stop: finished(False,..) has
        already been emitted in that case.
        """
        if results.get_exit_code() == Pk.ExitEnum.SUCCESS:
            return False
        self._emit_finished(False, self._exit_message(results))
        return True

    # -- authorization watchdog --------------------------------------------

    def _arm_wait_watchdog(self):
        """Start the timeout that ends a stuck polkit prompt."""
        if self._wait_timeout_id is not None or self._finished_emitted:
            return
        self._wait_timeout_id = GLib.timeout_add_seconds(
            AUTH_WAIT_TIMEOUT_SECONDS, self._wait_timeout_cb)

    def _disarm_wait_watchdog(self):
        if self._wait_timeout_id is None:
            return
        try:
            GLib.source_remove(self._wait_timeout_id)
        except Exception:
            logging.exception("removing the PackageKit wait timeout failed")
        self._wait_timeout_id = None

    def _wait_timeout_cb(self):
        """The daemon never got past WAITING_FOR_AUTH - give up."""
        # clear the id first, _emit_finished() would try to remove it again
        self._wait_timeout_id = None
        if self._finished_emitted:
            return False
        self.emit("log", _("Authorization timed out"))
        self.cancel()
        self._emit_finished(
            False, _("Authorization was not granted (timeout)."))
        # returning False removes the source
        return False

    def _emit_finished(self, success, message):
        # the watchdog is killed *before* the signal goes out, so a later
        # timeout can never emit a second finished signal; _finished_emitted
        # covers the other way round (timeout fires while the real result
        # is being collected)
        self._disarm_wait_watchdog()
        if self._finished_emitted:
            return
        self._finished_emitted = True
        self.emit("finished", bool(success), str(message))
