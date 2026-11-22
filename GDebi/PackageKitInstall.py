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

"""Install a local .deb file through PackageKit.

Instead of re-launching the whole GUI as root via pkexec (which is
broken on Wayland sessions, since root cannot connect to the user
display server), the actual installation is delegated to packagekitd.
polkit authentication is handled by the desktop session agent, the
gdebi process itself stays unprivileged and only displays progress.

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


class PackageKitInstaller(GObject.GObject):
    """Asynchronously install a local deb file via PackageKit.

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

    def __init__(self, filename):
        GObject.GObject.__init__(self)
        self._filename = os.path.abspath(filename)
        self._client = None
        self._cancellable = None
        self._finished_emitted = False
        self._last_status = None
        self._last_pkgid = None

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
        """Start the installation, returns immediately.

        Never raises: every failure ends up as a "finished(False, msg)"
        signal instead.
        """
        try:
            if Pk is None:
                self._emit_finished(
                    False, _("PackageKit support is not installed (%s).")
                    % _PK_IMPORT_ERROR)
                return
            if not os.path.isfile(self._filename):
                self._emit_finished(
                    False, _("File not found: %s") % self._filename)
                return
            self._client = Pk.Client()
            self._cancellable = Gio.Cancellable()
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
            if status is not None:
                status_text = str(status.value_nick).replace("_", " ")
            self.emit("progress", percentage, status_text)
            # only log status changes, not every progress tick
            if status != self._last_status or pkgid != self._last_pkgid:
                self._last_status = status
                self._last_pkgid = pkgid
                line = status_text
                if pkgid:
                    if line:
                        line += ": "
                    line += pkgid
                self.emit("log", line)
        except Exception:
            logging.exception("error while processing PackageKit progress")

    def _ready_cb(self, *args):
        """Gio.AsyncReadyCallback: collect results and emit "finished".

        The async result object is picked from the (possibly version
        dependent) callback arguments by duck-typing.
        """
        try:
            res = None
            for arg in args:
                if hasattr(arg, "get_source_object"):
                    res = arg
                    break
            if res is None:
                self._emit_finished(
                    False, _("Could not collect the PackageKit results."))
                return
            client = self._client
            try:
                results = client.generic_finish(res)
            except GLib.Error as e:
                # polkit denials, daemon errors and the dpkg/apt error
                # details end up in this GError
                msg = e.message or str(e)
                logging.info("PackageKit transaction failed: %s", msg)
                self._emit_finished(False, msg)
                return
            exit_code = results.get_exit_code()
            if exit_code == Pk.ExitEnum.SUCCESS:
                self._emit_finished(True, _("Installation finished"))
                return
            msg = _("PackageKit returned: %s") % str(
                exit_code.value_nick if exit_code is not None else "unknown")
            try:
                error_code = results.get_error_code()
                if error_code is not None and int(error_code) != 0:
                    msg = "%s (%s)" % (
                        msg, str(error_code.value_nick).replace("_", " "))
            except Exception:
                pass
            self._emit_finished(False, msg)
        except Exception as e:
            logging.exception("error while collecting PackageKit results")
            self._emit_finished(False, str(e) or repr(e))

    def _emit_finished(self, success, message):
        if self._finished_emitted:
            return
        self._finished_emitted = True
        self.emit("finished", bool(success), str(message))
