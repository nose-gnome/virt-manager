# -*- coding: utf-8 -*-
#
# Copyright (C) 2006-2008 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
# Copyright (C) 2010 Marc-André Lureau <marcandre.lureau@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import gtk
import gobject

import libvirt

import gtkvnc

try:
    import SpiceClientGtk as spice
except Exception, e:
    spice = None

import os
import sys
import signal
import socket
import logging
import traceback

from virtManager import util
from virtManager.baseclass import vmmGObjectUI
from virtManager.error import vmmErrorDialog

# Console pages
PAGE_UNAVAILABLE = 0
PAGE_AUTHENTICATE = 1
PAGE_VIEWER = 2

def has_property(obj, setting):
    try:
        obj.get_property(setting)
    except TypeError:
        return False
    return True


class Tunnel(object):
    def __init__(self):
        self.outfd = None
        self.errfd = None
        self.pid = None

    def open(self, server, addr, port, username, sshport):
        if self.outfd is not None:
            return -1

        # Build SSH cmd
        argv = ["ssh", "ssh"]
        if sshport:
            argv += ["-p", str(sshport)]

        if username:
            argv += ['-l', username]

        argv += [server]

        # Build 'nc' command run on the remote host
        #
        # This ugly thing is a shell script to detect availability of
        # the -q option for 'nc': debian and suse based distros need this
        # flag to ensure the remote nc will exit on EOF, so it will go away
        # when we close the VNC tunnel. If it doesn't go away, subsequent
        # VNC connection attempts will hang.
        #
        # Fedora's 'nc' doesn't have this option, and apparently defaults
        # to the desired behavior.
        #
        nc_params = "%s %s" % (addr, str(port))
        nc_cmd = (
            """nc -q 2>&1 | grep -q "requires an argument";"""
            """if [ $? -eq 0 ] ; then"""
            """   CMD="nc -q 0 %(nc_params)s";"""
            """else"""
            """   CMD="nc %(nc_params)s";"""
            """fi;"""
            """eval "$CMD";""" %
            {'nc_params': nc_params})

        argv.append("sh -c")
        argv.append("'%s'" % nc_cmd)

        argv_str = reduce(lambda x, y: x + " " + y, argv[1:])
        logging.debug("Creating SSH tunnel: %s" % argv_str)

        fds      = socket.socketpair()
        errorfds = socket.socketpair()

        pid = os.fork()
        if pid == 0:
            fds[0].close()
            errorfds[0].close()

            os.close(0)
            os.close(1)
            os.close(2)
            os.dup(fds[1].fileno())
            os.dup(fds[1].fileno())
            os.dup(errorfds[1].fileno())
            os.execlp(*argv)
            os._exit(1)
        else:
            fds[1].close()
            errorfds[1].close()

        logging.debug("Tunnel PID=%d OUTFD=%d ERRFD=%d" %
                      (pid, fds[0].fileno(), errorfds[0].fileno()))
        errorfds[0].setblocking(0)

        self.outfd = fds[0]
        self.errfd = errorfds[0]
        self.pid = pid

        fd = fds[0].fileno()
        if fd < 0:
            raise SystemError("can't open a new tunnel: fd=%d" % fd)
        return fd

    def close(self):
        if self.outfd is None:
            return

        logging.debug("Shutting down tunnel PID=%d OUTFD=%d ERRFD=%d" %
                      (self.pid, self.outfd.fileno(),
                       self.errfd.fileno()))
        self.outfd.close()
        self.outfd = None
        self.errfd.close()
        self.errfd = None

        os.kill(self.pid, signal.SIGKILL)
        self.pid = None

    def get_err_output(self):
        errout = ""
        while True:
            try:
                new = self.errfd.recv(1024)
            except:
                break

            if not new:
                break

            errout += new

        return errout


class Tunnels(object):
    def __init__(self, server, addr, port, username, sshport):
        self.server = server
        self.addr = addr
        self.port = port
        self.username = username
        self.sshport = sshport
        self._tunnels = []

    def open_new(self):
        t = Tunnel()
        fd = t.open(self.server, self.addr, self.port,
                    self.username, self.sshport)
        self._tunnels.append(t)
        return fd

    def close_all(self):
        for l in self._tunnels:
            l.close()

    def get_err_output(self):
        errout = ""
        for l in self._tunnels:
            errout += l.get_err_output()
        return errout


class Viewer(object):
    def __init__(self, console, config):
        self.console = console
        self.config = config
        self.display = None

    def get_pixbuf(self):
        return self.display.get_pixbuf()

    def get_grab_keys_from_config(self):
        keys = []
        grab_keys = self.config.get_keys_combination(True)
        if grab_keys is not None:
            # If somebody edited this in GConf it would fail so
            # we encapsulate this into try/except block
            try:
                keys = map(int, grab_keys.split(','))
            except:
                logging.debug("Error in grab_keys configuration in GConf")

        return keys

    def get_grab_keys(self):
        keystr = None
        try:
            keys = self.display.get_grab_keys()
            for k in keys:
                if keystr is None:
                    keystr = gtk.gdk.keyval_name(k)
                else:
                    keystr = keystr + "+" + gtk.gdk.keyval_name(k)
        except:
            pass

        return keystr

    def send_keys(self, keys):
        return self.display.send_keys(keys)

    def set_grab_keys(self):
        try:
            keys = self.get_grab_keys_from_config()
            if keys:
                self.display.set_grab_keys(keys)
        except Exception, e:
            logging.debug("Error when getting the grab keys combination: %s" %
                          str(e))

class VNCViewer(Viewer):
    def __init__(self, console, config):
        Viewer.__init__(self, console, config)
        self.display = gtkvnc.Display()

    def get_widget(self):
        return self.display

    def init_widget(self):
        # Set default grab key combination if found and supported
        if self.config.vnc_grab_keys_supported():
            self.set_grab_keys()

        self.display.realize()

        # Make sure viewer doesn't force resize itself
        self.display.set_force_size(False)

        self.console.refresh_scaling()

        self.display.set_keyboard_grab(False)
        self.display.set_pointer_grab(True)

        self.display.connect("vnc-pointer-grab", self.console.pointer_grabbed)
        self.display.connect("vnc-pointer-ungrab", self.console.pointer_ungrabbed)
        self.display.connect("vnc-auth-credential", self._auth_credential)
        self.display.connect("vnc-initialized",
                             lambda src: self.console.connected())
        self.display.connect("vnc-disconnected",
                             lambda src: self.console.disconnected())
        self.display.connect("vnc-desktop-resize", self.console.desktop_resize)
        self.display.connect("focus-in-event", self.console.viewer_focus_changed)
        self.display.connect("focus-out-event", self.console.viewer_focus_changed)

        self.display.show()

    def _auth_credential(self, src_ignore, credList):
        for i in range(len(credList)):
            if credList[i] not in [gtkvnc.CREDENTIAL_PASSWORD,
                                   gtkvnc.CREDENTIAL_USERNAME,
                                   gtkvnc.CREDENTIAL_CLIENTNAME]:
                self.console.err.show_err(summary=_("Unable to provide requested credentials to the VNC server"),
                                          details=_("The credential type %s is not supported") % (str(credList[i])),
                                          title=_("Unable to authenticate"),
                                          async=True)
                self.console.viewerRetriesScheduled = 10 # schedule_retry will error out
                self.close()
                self.console.activate_unavailable_page(_("Unsupported console authentication type"))
                return

        withUsername = False
        withPassword = False
        for i in range(len(credList)):
            logging.debug("Got credential request %s", str(credList[i]))
            if credList[i] == gtkvnc.CREDENTIAL_PASSWORD:
                withPassword = True
            elif credList[i] == gtkvnc.CREDENTIAL_USERNAME:
                withUsername = True
            elif credList[i] == gtkvnc.CREDENTIAL_CLIENTNAME:
                self.display.set_credential(credList[i], "libvirt-vnc")

        if withUsername or withPassword:
            self.console.activate_auth_page(withPassword, withUsername)

    def get_scaling(self):
        return self.display.get_scaling()

    def set_scaling(self, scaling):
        return self.display.set_scaling(scaling)

    def close(self):
        self.display.close()

    def is_open(self):
        return self.display.is_open()

    def open_host(self, uri_ignore, connhost, port):
        self.display.open_host(connhost, port)

    def open_fd(self, fd):
        self.display.open_fd(fd)

    def get_grab_keys(self):
        keystr = None
        if self.config.vnc_grab_keys_supported():
            keystr = super(VNCViewer, self).get_grab_keys()

        # If grab keys are set to None then preserve old behaviour since
        # the GTK-VNC - we're using older version of GTK-VNC
        if keystr is None:
            keystr = "Control_L+Alt_L"
        return keystr

    def set_credential_username(self, cred):
        self.display.set_credential(gtkvnc.CREDENTIAL_USERNAME, cred)

    def set_credential_password(self, cred):
        self.display.set_credential(gtkvnc.CREDENTIAL_PASSWORD, cred)


class SpiceViewer(Viewer):
    def __init__(self, console, config):
        Viewer.__init__(self, console, config)
        self.spice_session = None
        self.display = None
        self.audio = None

    def get_widget(self):
        return self.display

    def _init_widget(self):
        self.set_grab_keys()
        self.console.refresh_scaling()

        self.display.realize()
        self.display.connect("mouse-grab", lambda src, g: g and self.console.pointer_grabbed(src))
        self.display.connect("mouse-grab", lambda src, g: g or self.console.pointer_ungrabbed(src))
        self.display.show()

    def close(self):
        if self.spice_session is not None:
            self.spice_session.disconnect()
            self.spice_session = None
            self.audio = None
            self.display = None

    def is_open(self):
        return self.spice_session != None

    def _main_channel_event_cb(self, channel, event):
        if event == spice.CHANNEL_CLOSED:
            self.console.disconnected()

    def _channel_open_fd_request(self, channel, tls_ignore):
        if not self.console.tunnels:
            raise SystemError("Got fd request with no configured tunnel!")

        fd = self.console.tunnels.open_new()
        channel.open_fd(fd)

    def _channel_new_cb(self, session, channel):
        gobject.GObject.connect(channel, "open-fd",
                                self._channel_open_fd_request)

        if type(channel) == spice.MainChannel:
            channel.connect_after("channel-event", self._main_channel_event_cb)
            return

        if type(channel) == spice.DisplayChannel:
            channel_id = channel.get_property("channel-id")
            self.display = spice.Display(self.spice_session, channel_id)
            self.console.window.get_widget("console-vnc-viewport").add(self.display)
            self._init_widget()
            self.console.connected()
            return

        if (type(channel) in [spice.PlaybackChannel, spice.RecordChannel] and
            not self.audio):
            self.audio = spice.Audio(self.spice_session)
            return

    def open_host(self, uri, connhost_ignore, port_ignore, password=None):
        self.spice_session = spice.Session()
        self.spice_session.set_property("uri", uri)
        if password:
            self.spice_session.set_property("password", password)
        gobject.GObject.connect(self.spice_session, "channel-new",
                                self._channel_new_cb)
        self.spice_session.connect()

    def open_fd(self, fd, password=None):
        self.spice_session = spice.Session()
        if password:
            self.spice_session.set_property("password", password)
        gobject.GObject.connect(self.spice_session, "channel-new",
                                self._channel_new_cb)
        self.spice_session.open_fd(fd)

    def set_credential_password(self, cred):
        self.spice_session.set_property("password", cred)

    def get_scaling(self):
        return self.display.get_property("resize-guest")

    def set_scaling(self, scaling):
        self.display.set_property("resize-guest", scaling)


class vmmConsolePages(vmmGObjectUI):
    def __init__(self, vm, window):
        vmmGObjectUI.__init__(self, None, None)

        self.vm = vm

        self.windowname = "vmm-details"
        self.window = window
        self.topwin = self.window.get_widget(self.windowname)
        self.err = vmmErrorDialog(self.topwin)

        self.title = vm.get_name() + " " + self.topwin.get_title()
        self.topwin.set_title(self.title)

        # State for disabling modifiers when keyboard is grabbed
        self.accel_groups = gtk.accel_groups_from_object(self.topwin)
        self.gtk_settings_accel = None
        self.gtk_settings_mnemonic = None

        # Last noticed desktop resolution
        self.desktop_resolution = None

        # Initialize display widget
        self.scale_type = self.vm.get_console_scaling()
        self.tunnels = None
        self.viewerRetriesScheduled = 0
        self.viewerRetryDelay = 125
        self.viewer = None
        self.viewer_connected = False

        finish_img = gtk.image_new_from_stock(gtk.STOCK_YES,
                                              gtk.ICON_SIZE_BUTTON)
        self.window.get_widget("console-auth-login").set_image(finish_img)

        # Make viewer widget background always be black
        black = gtk.gdk.Color(0, 0, 0)
        self.window.get_widget("console-vnc-viewport").modify_bg(
                                                        gtk.STATE_NORMAL,
                                                        black)

        # Signals are added by vmmDetails. Don't use signal_autoconnect here
        # or it changes will be overwritten
        # Set console scaling
        self.vm.on_console_scaling_changed(self.refresh_scaling)

        scroll = self.window.get_widget("console-vnc-scroll")
        scroll.connect("size-allocate", self.scroll_size_allocate)
        self.config.on_console_accels_changed(self.set_enable_accel)

    def is_visible(self):
        if self.topwin.flags() & gtk.VISIBLE:
            return 1
        return 0

    ##########################
    # Initialization helpers #
    ##########################

    def viewer_focus_changed(self, ignore1=None, ignore2=None):
        has_focus = self.viewer and self.viewer.get_widget() and \
            self.viewer.get_widget().get_property("has-focus")
        force_accel = self.config.get_console_accels()

        if force_accel:
            self._enable_modifiers()
        elif has_focus and self.viewer_connected:
            self._disable_modifiers()
        else:
            self._enable_modifiers()

    def pointer_grabbed(self, src_ignore):
        keystr = self.viewer.get_grab_keys()
        self.topwin.set_title(_("Press %s to release pointer.") % keystr +
                              " " + self.title)

    def pointer_ungrabbed(self, src_ignore):
        self.topwin.set_title(self.title)

    def _disable_modifiers(self):
        if self.gtk_settings_accel is not None:
            return

        for g in self.accel_groups:
            self.topwin.remove_accel_group(g)

        settings = gtk.settings_get_default()
        self.gtk_settings_accel = settings.get_property('gtk-menu-bar-accel')
        settings.set_property('gtk-menu-bar-accel', None)

        if has_property(settings, "gtk-enable-mnemonics"):
            self.gtk_settings_mnemonic = settings.get_property(
                                                        "gtk-enable-mnemonics")
            settings.set_property("gtk-enable-mnemonics", False)

    def _enable_modifiers(self):
        if self.gtk_settings_accel is None:
            return

        settings = gtk.settings_get_default()
        settings.set_property('gtk-menu-bar-accel', self.gtk_settings_accel)
        self.gtk_settings_accel = None

        if self.gtk_settings_mnemonic is not None:
            settings.set_property("gtk-enable-mnemonics",
                                  self.gtk_settings_mnemonic)

        for g in self.accel_groups:
            self.topwin.add_accel_group(g)

    def set_enable_accel(self, ignore=None, ignore1=None,
                         ignore2=None, ignore3=None):
        # Make sure modifiers are up to date
        self.viewer_focus_changed()

    def refresh_scaling(self, ignore1=None, ignore2=None, ignore3=None,
                        ignore4=None):
        self.scale_type = self.vm.get_console_scaling()
        self.window.get_widget("details-menu-view-scale-always").set_active(
            self.scale_type == self.config.CONSOLE_SCALE_ALWAYS)
        self.window.get_widget("details-menu-view-scale-never").set_active(
            self.scale_type == self.config.CONSOLE_SCALE_NEVER)
        self.window.get_widget("details-menu-view-scale-fullscreen").set_active(
            self.scale_type == self.config.CONSOLE_SCALE_FULLSCREEN)

        self.update_scaling()

    def set_scale_type(self, src):
        if not src.get_active():
            return

        if src == self.window.get_widget("details-menu-view-scale-always"):
            self.scale_type = self.config.CONSOLE_SCALE_ALWAYS
        elif src == self.window.get_widget("details-menu-view-scale-fullscreen"):
            self.scale_type = self.config.CONSOLE_SCALE_FULLSCREEN
        elif src == self.window.get_widget("details-menu-view-scale-never"):
            self.scale_type = self.config.CONSOLE_SCALE_NEVER

        self.vm.set_console_scaling(self.scale_type)
        self.update_scaling()

    def update_scaling(self):
        if not self.viewer:
            return

        curscale = self.viewer.get_scaling()
        fs = self.window.get_widget("control-fullscreen").get_active()
        vnc_scroll = self.window.get_widget("console-vnc-scroll")

        if (self.scale_type == self.config.CONSOLE_SCALE_NEVER
            and curscale == True):
            self.viewer.set_scaling(False)
        elif (self.scale_type == self.config.CONSOLE_SCALE_ALWAYS
              and curscale == False):
            self.viewer.set_scaling(True)
        elif (self.scale_type == self.config.CONSOLE_SCALE_FULLSCREEN
              and curscale != fs):
            self.viewer.set_scaling(fs)

        # Refresh viewer size
        vnc_scroll.queue_resize()

    def auth_login(self, ignore):
        self.set_credentials()
        self.activate_viewer_page()

    def toggle_fullscreen(self, src):
        do_fullscreen = src.get_active()

        self.window.get_widget("control-fullscreen").set_active(do_fullscreen)

        if do_fullscreen:
            self.topwin.fullscreen()
            self.window.get_widget("toolbar-box").hide()
        else:
            self.topwin.unfullscreen()

            if self.window.get_widget("details-menu-view-toolbar").get_active():
                self.window.get_widget("toolbar-box").show()

        self.update_scaling()

    def size_to_vm(self, src_ignore):
        # Resize the console to best fit the VM resolution
        if not self.desktop_resolution:
            return

        w, h = self.desktop_resolution
        self.topwin.unmaximize()
        self.topwin.resize(1, 1)
        self.queue_scroll_resize_helper(w, h)

    def send_key(self, src):
        keys = None
        if src.get_name() == "details-menu-send-cad":
            keys = ["Control_L", "Alt_L", "Delete"]
        elif src.get_name() == "details-menu-send-cab":
            keys = ["Control_L", "Alt_L", "BackSpace"]
        elif src.get_name() == "details-menu-send-caf1":
            keys = ["Control_L", "Alt_L", "F1"]
        elif src.get_name() == "details-menu-send-caf2":
            keys = ["Control_L", "Alt_L", "F2"]
        elif src.get_name() == "details-menu-send-caf3":
            keys = ["Control_L", "Alt_L", "F3"]
        elif src.get_name() == "details-menu-send-caf4":
            keys = ["Control_L", "Alt_L", "F4"]
        elif src.get_name() == "details-menu-send-caf5":
            keys = ["Control_L", "Alt_L", "F5"]
        elif src.get_name() == "details-menu-send-caf6":
            keys = ["Control_L", "Alt_L", "F6"]
        elif src.get_name() == "details-menu-send-caf7":
            keys = ["Control_L", "Alt_L", "F7"]
        elif src.get_name() == "details-menu-send-caf8":
            keys = ["Control_L", "Alt_L", "F8"]
        elif src.get_name() == "details-menu-send-caf9":
            keys = ["Control_L", "Alt_L", "F9"]
        elif src.get_name() == "details-menu-send-caf10":
            keys = ["Control_L", "Alt_L", "F10"]
        elif src.get_name() == "details-menu-send-caf11":
            keys = ["Control_L", "Alt_L", "F11"]
        elif src.get_name() == "details-menu-send-caf12":
            keys = ["Control_L", "Alt_L", "F12"]
        elif src.get_name() == "details-menu-send-printscreen":
            keys = ["Print"]

        if keys != None:
            self.viewer.send_keys(keys)


    ##########################
    # State tracking methods #
    ##########################

    def view_vm_status(self):
        status = self.vm.status()
        if status == libvirt.VIR_DOMAIN_SHUTOFF:
            self.activate_unavailable_page(_("Guest not running"))
        else:
            if status == libvirt.VIR_DOMAIN_CRASHED:
                self.activate_unavailable_page(_("Guest has crashed"))

    def close_viewer(self):
        if self.viewer is not None:
            v = self.viewer # close_viewer() can be reentered
            self.viewer = None
            if v.get_widget():
                self.window.get_widget("console-vnc-viewport").remove(v.get_widget())
            v.close()
            self.viewer_connected = False

    def update_widget_states(self, vm, status_ignore):
        runable = vm.is_runable()
        pages   = self.window.get_widget("console-pages")
        page    = pages.get_current_page()

        if runable:
            if page != PAGE_UNAVAILABLE:
                pages.set_current_page(PAGE_UNAVAILABLE)

            self.view_vm_status()
            return

        elif page in [PAGE_UNAVAILABLE, PAGE_VIEWER]:
            if self.viewer and self.viewer.is_open():
                self.activate_viewer_page()
            else:
                self.viewerRetriesScheduled = 0
                self.viewerRetryDelay = 125
                self.try_login()

        return

    ###################
    # Page Navigation #
    ###################

    def activate_unavailable_page(self, msg):
        self.window.get_widget("console-pages").set_current_page(PAGE_UNAVAILABLE)
        self.window.get_widget("details-menu-vm-screenshot").set_sensitive(False)
        self.window.get_widget("console-unavailable").set_label("<b>" + msg + "</b>")

    def activate_auth_page(self, withPassword=True, withUsername=False):
        (pw, username) = self.config.get_console_password(self.vm)
        self.window.get_widget("details-menu-vm-screenshot").set_sensitive(False)

        if withPassword:
            self.window.get_widget("console-auth-password").show()
            self.window.get_widget("label-auth-password").show()
        else:
            self.window.get_widget("console-auth-password").hide()
            self.window.get_widget("label-auth-password").hide()

        if withUsername:
            self.window.get_widget("console-auth-username").show()
            self.window.get_widget("label-auth-username").show()
        else:
            self.window.get_widget("console-auth-username").hide()
            self.window.get_widget("label-auth-username").hide()

        self.window.get_widget("console-auth-username").set_text(username)
        self.window.get_widget("console-auth-password").set_text(pw)

        if self.config.has_keyring():
            self.window.get_widget("console-auth-remember").set_sensitive(True)
            if pw != "" or username != "":
                self.window.get_widget("console-auth-remember").set_active(True)
            else:
                self.window.get_widget("console-auth-remember").set_active(False)
        else:
            self.window.get_widget("console-auth-remember").set_sensitive(False)
        self.window.get_widget("console-pages").set_current_page(PAGE_AUTHENTICATE)
        if withUsername:
            self.window.get_widget("console-auth-username").grab_focus()
        else:
            self.window.get_widget("console-auth-password").grab_focus()


    def activate_viewer_page(self):
        self.window.get_widget("console-pages").set_current_page(PAGE_VIEWER)
        self.window.get_widget("details-menu-vm-screenshot").set_sensitive(True)
        if self.viewer and self.viewer.get_widget():
            self.viewer.get_widget().grab_focus()

    def disconnected(self):
        errout = ""
        if self.tunnels is not None:
            errout = self.tunnels.get_err_output()
            self.tunnels.close_all()
            self.tunnels = None

        self.close_viewer()
        logging.debug("Viewer disconnected")

        # Make sure modifiers are set correctly
        self.viewer_focus_changed()

        if (self.skip_connect_attempt() or
            self.guest_not_avail()):
            # Exit was probably for legitimate reasons
            self.view_vm_status()
            return

        error = _("Error: viewer connection to hypervisor host got refused "
                  "or disconnected!")
        if errout:
            logging.debug("Error output from closed console: %s" % errout)
            error += "\n\nError: %s" % errout

        self.activate_unavailable_page(error)

    def connected(self):
        self.viewer_connected = True
        logging.debug("Viewer connected")
        self.activate_viewer_page()

        # Had a succesfull connect, so reset counters now
        self.viewerRetriesScheduled = 0
        self.viewerRetryDelay = 125

        # Make sure modifiers are set correctly
        self.viewer_focus_changed()

    def schedule_retry(self):
        if self.viewerRetriesScheduled >= 10:
            logging.error("Too many connection failures, not retrying again")
            return

        util.safe_timeout_add(self.viewerRetryDelay, self.try_login)

        if self.viewerRetryDelay < 2000:
            self.viewerRetryDelay = self.viewerRetryDelay * 2

    def skip_connect_attempt(self):
        return (self.viewer_connected or
                not self.is_visible())

    def guest_not_avail(self):
        return (not self.vm.get_handle() or
                self.vm.status() in [libvirt.VIR_DOMAIN_SHUTOFF,
                                     libvirt.VIR_DOMAIN_CRASHED] or
                self.vm.get_id() < 0)

    def try_login(self, src_ignore=None):
        if self.skip_connect_attempt():
            # Don't try and login for these cases
            return

        if self.guest_not_avail():
            # Guest isn't running, schedule another try
            self.activate_unavailable_page(_("Guest not running"))
            self.schedule_retry()
            return

        try:
            (protocol, connhost,
             gport, trans, username,
             connport, guri) = self.vm.get_graphics_console()
        except Exception, e:
            # We can fail here if VM is destroyed: xen is a bit racy
            # and can't handle domain lookups that soon after
            logging.exception("Getting graphics console failed: %s" % str(e))
            return

        if protocol is None:
            logging.debug("No graphics configured for guest")
            self.activate_unavailable_page(
                            _("Graphical console not configured for guest"))
            return

        if protocol not in self.config.embeddable_graphics():
            logging.debug("Don't know how to show graphics type '%s'"
                          "disabling console page" % protocol)

            msg = (_("Cannot display graphical console type '%s'")
                     % protocol)
            if protocol == "spice":
                msg += ":\n %s" % self.config.get_spice_error()

            self.activate_unavailable_page(msg)
            return

        if gport == -1:
            self.activate_unavailable_page(
                            _("Graphical console is not yet active for guest"))
            self.schedule_retry()
            return

        if protocol == "vnc":
            self.viewer = VNCViewer(self, self.config)
            self.window.get_widget("console-vnc-viewport").add(self.viewer.get_widget())
            self.viewer.init_widget()
        elif protocol == "spice":
            self.viewer = SpiceViewer(self, self.config)

        self.set_enable_accel()

        self.activate_unavailable_page(
                _("Connecting to graphical console for guest"))
        logging.debug("Starting connect process for %s: %s %s" %
                      (guri, connhost, str(gport)))

        try:
            if trans in ("ssh", "ext"):
                if self.tunnels:
                    # Tunnel already open, no need to continue
                    return

                self.tunnels = Tunnels(connhost, "127.0.0.1", gport,
                                       username, connport)
                fd = self.tunnels.open_new()
                if fd >= 0:
                    self.viewer.open_fd(fd)

            else:
                self.viewer.open_host(guri, connhost, str(gport))

        except:
            (typ, value, stacktrace) = sys.exc_info()
            details = \
                    "Unable to start virtual machine '%s'" % \
                    (str(typ) + " " + str(value) + "\n" + \
                     traceback.format_exc(stacktrace))
            logging.error(details)

    def set_credentials(self, src_ignore=None):
        passwd = self.window.get_widget("console-auth-password")
        if passwd.flags() & gtk.VISIBLE:
            self.viewer.set_credential_password(passwd.get_text())
        username = self.window.get_widget("console-auth-username")
        if username.flags() & gtk.VISIBLE:
            self.viewer.set_credential_username(username.get_text())

        if self.window.get_widget("console-auth-remember").get_active():
            self.config.set_console_password(self.vm, passwd.get_text(),
                                             username.get_text())

    def desktop_resize(self, src_ignore, w, h):
        self.desktop_resolution = (w, h)
        self.window.get_widget("console-vnc-scroll").queue_resize()

    def queue_scroll_resize_helper(self, w, h):
        """
        Resize the VNC container widget to the requested size. The new size
        isn't a hard requirment so the user can still shrink the window
        again, as opposed to set_size_request
        """
        widget = self.window.get_widget("console-vnc-scroll")
        signal_holder = []

        def restore_scroll(src):
            is_scale = self.viewer.get_scaling()

            if is_scale:
                w_policy = gtk.POLICY_NEVER
                h_policy = gtk.POLICY_NEVER
            else:
                w_policy = gtk.POLICY_AUTOMATIC
                h_policy = gtk.POLICY_AUTOMATIC

            src.set_policy(w_policy, h_policy)
            return False

        def unset_cb(src):
            src.queue_resize_no_redraw()
            util.safe_idle_add(restore_scroll, src)
            return False

        def request_cb(src, req):
            signal_id = signal_holder[0]
            req.width = w
            req.height = h

            src.disconnect(signal_id)

            util.safe_idle_add(unset_cb, widget)
            return False

        # Disable scroll bars while we resize, since resizing to the VM's
        # dimensions can erroneously show scroll bars when they aren't needed
        widget.set_policy(gtk.POLICY_NEVER, gtk.POLICY_NEVER)

        signal_id = widget.connect("size-request", request_cb)
        signal_holder.append(signal_id)

        widget.queue_resize()

    def scroll_size_allocate(self, src_ignore, req):
        if not self.viewer or not self.desktop_resolution:
            return

        scroll = self.window.get_widget("console-vnc-scroll")
        is_scale = self.viewer.get_scaling()

        dx = 0
        dy = 0
        align_ratio = float(req.width) / float(req.height)

        desktop_w, desktop_h = self.desktop_resolution
        desktop_ratio = float(desktop_w) / float(desktop_h)

        if not is_scale:
            # Scaling disabled is easy, just force the VNC widget size. Since
            # we are inside a scrollwindow, it shouldn't cause issues.
            scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            self.viewer.get_widget().set_size_request(desktop_w, desktop_h)
            return

        # Make sure we never show scrollbars when scaling
        scroll.set_policy(gtk.POLICY_NEVER, gtk.POLICY_NEVER)

        # Make sure there is no hard size requirement so we can scale down
        self.viewer.get_widget().set_size_request(-1, -1)

        # Make sure desktop aspect ratio is maintained
        if align_ratio > desktop_ratio:
            desktop_w = int(req.height * desktop_ratio)
            desktop_h = req.height
            dx = (req.width - desktop_w) / 2

        else:
            desktop_w = req.width
            desktop_h = int(req.width / desktop_ratio)
            dy = (req.height - desktop_h) / 2

        viewer_alloc = gtk.gdk.Rectangle(x=dx,
                                         y=dy,
                                         width=desktop_w,
                                         height=desktop_h)

        self.viewer.get_widget().size_allocate(viewer_alloc)

vmmGObjectUI.type_register(vmmConsolePages)
