#!/usr/bin/env python3
#
# When
#
# Copyright (c) 2015 Francesco Garosi
# Released under the BSD License (see LICENSE.md)
#
# Small startup applet that runs tasks when particular conditions are met.
#
#############################################################################
# SOME NOTES ABOUT THE CODE
# The code tries to follow the usual guidelines for Python 3.x, and takes
# inspiration from other Gnome applets that sit in the indicator tray.
# I tried to reduce the comments to the very least, and let the code speak
# for itself. Some of the conventions here are the following:
#
# * system wide constants are spelled in all uppercase (as usual in C/C++)
# * variables tend to be all lowercase, both globals and locals
# * class names start with an uppercase letter and are in camelcase
# * global instances of classes are lowercase
# * private members start, as usual, with an underscore
# * function names are all lowercase with underscores
# * transitional (or debug) functions start with underscores
# * the core classes implement their own loggers, borrowing from global
# * user interaction strings (as log messages) use double quotes
# * program internal strings use single quotes
# * log messages mostly sport a prefix to determine what part generated them
# * log messages containing the NTBS strings are *never to be seen*

import os
import sys
import time
import signal
import subprocess
import threading
import configparser
import pickle
import json
import logging
import logging.config
import logging.handlers
import argparse
import shutil
import re

from gi.repository import GLib, Gio
from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import AppIndicator3 as AppIndicator
from gi.repository import Notify
from gi.repository import Pango

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

from concurrent.futures import ThreadPoolExecutor

from collections import OrderedDict, deque, namedtuple


#############################################################################
# constants

# base constants
APPLET_NAME = 'when-command'
APPLET_FULLNAME = "When Gnome Scheduler"
APPLET_SHORTNAME = "When"
APPLET_COPYRIGHT = "(c) 2015 Francesco Garosi"
APPLET_URL = "http://almostearthling.github.io/when-command/"
APPLET_VERSION = "0.6.2-beta.6"
APPLET_ID = "it.jks.WhenCommand"
APPLET_BUS_NAME = '%s.BusService' % APPLET_ID
APPLET_BUS_PATH = '/' + APPLET_BUS_NAME.replace('.', '/')

# logging constants
LOG_FORMAT = '%(asctime)s %(levelname)s: %(message)s'
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_MAX_BACKUPS = 4

# action constants
ACTION_OK = 0
ACTION_CANCEL = -1
ACTION_DELETE = 9

# validation constants
VALIDATE_TASK_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')
VALIDATE_CONDITION_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')

# user interface constants
UI_INTERVALS_MINUTES = [1, 2, 3, 5, 15, 30, 60]
UI_INTERVALS_HOURS = [1, 2, 3, 4, 6, 8, 12, 24]

# folders
USER_FOLDER = os.path.expanduser('~')
USER_DATA_FOLDER = os.path.join(USER_FOLDER, '.local', 'share', APPLET_NAME)
USER_LAUNCHER_FOLDER = os.path.join(USER_FOLDER, '.local', 'share', 'applications')
USER_AUTOSTART_FOLDER = os.path.join(USER_FOLDER, '.config', 'autostart')
USER_CONFIG_FOLDER = os.path.join(USER_FOLDER, '.config', APPLET_NAME)
USER_LOG_FOLDER = os.path.join(USER_DATA_FOLDER, 'log')
USER_LOG_FILE = os.path.join(USER_LOG_FOLDER, "%s.log" % APPLET_NAME)
USER_CONFIG_FILE = os.path.join(USER_CONFIG_FOLDER, "%s.conf" % APPLET_NAME)
USER_PAUSE_FILE = os.path.join(USER_CONFIG_FOLDER, "%s.pause" % APPLET_NAME)

GRAPHIC_ENVIRONMENT = 'DISPLAY' in os.environ.keys()

# event strings (applet, system, session)
EVENT_APPLET_STARTUP = 'startup'
EVENT_APPLET_SHUTDOWN = 'shutdown'
EVENT_SYSTEM_SUSPEND = 'system_suspend'
EVENT_SYSTEM_RESUME = 'system_resume'
EVENT_SYSTEM_DEVICE_ATTACH = 'device_attach'
EVENT_SYSTEM_DEVICE_DETACH = 'device_detach'
EVENT_SYSTEM_NETWORK_JOIN = 'network_join'
EVENT_SYSTEM_NETWORK_LEAVE = 'network_leave'
EVENT_SESSION_SCREENSAVER = 'screensaver'
EVENT_SESSION_SCREENSAVER_EXIT = 'screensaver_exit'
EVENT_SESSION_LOCK = 'session_lock'
EVENT_SESSION_UNLOCK = 'session_unlock'

# network manager constants
NM_STATE_UNKNOWN = 0
NM_STATE_ASLEEP = 10
NM_STATE_DISCONNECTED = 20
NM_STATE_DISCONNECTING = 30
NM_STATE_CONNECTING = 40
NM_STATE_CONNECTED_LOCAL = 50
NM_STATE_CONNECTED_SITE = 60
NM_STATE_CONNECTED_GLOBAL = 70

#############################################################################
# global variables referenced through the code (this should be redundant)
applet = None
applet_lock = threading.Lock()
applet_enabled_events = None
main_dbus_loop = None
current_system_event = None
current_system_event_param = None
current_deferred_events = None

applet_log_handler = logging.NullHandler()
applet_log_formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
applet_log_handler.setFormatter(applet_log_formatter)
applet_log = logging.getLogger(APPLET_NAME)
applet_log.addHandler(applet_log_handler)

config = None
periodic = None
tasks = None
conditions = None
history = None
deferred_events = None


# verify that the user folders are present, otherwise create them
def verify_user_folders():
    if not os.path.exists(USER_DATA_FOLDER):
        os.makedirs(USER_DATA_FOLDER, exist_ok=True)
    if not os.path.exists(USER_LOG_FOLDER):
        os.makedirs(USER_LOG_FOLDER, exist_ok=True)
    if not os.path.exists(USER_CONFIG_FOLDER):
        os.makedirs(USER_CONFIG_FOLDER, exist_ok=True)
    if not os.path.exists(USER_LAUNCHER_FOLDER):
        os.makedirs(USER_LAUNCHER_FOLDER, exist_ok=True)
    if not os.path.exists(USER_AUTOSTART_FOLDER):
        os.makedirs(USER_AUTOSTART_FOLDER, exist_ok=True)


#############################################################################
# Support these installation schemes:
#
# * default: LSB standard (/usr/bin/when-command, /usr/share/when-command/*)
# * /opt based (/opt/when-command/when-command, /opt/when-command/share/*)
# * LSB local (/usr/local/bin/when-command, /usr/local/share/when-command/*)
# * $HOME generic (~/.local/bin/when-command, ~/.local/when-command/share/*)
# * own folder ($FOLDER/when-command, $FOLDER/share/*)
#
# where the first element is the invoked command, the second one is the
# prefix of the applet data folder (where dialogs, icons and other resources
# have to be installed); icons have their own subtree in the data folder
APP_BASE_FOLDER = '/usr'
APP_BIN_FOLDER = os.path.join(APP_BASE_FOLDER, 'bin')
APP_DATA_FOLDER = os.path.join(APP_BASE_FOLDER, 'share', APPLET_NAME)
APP_ICON_FOLDER = os.path.join(APP_DATA_FOLDER, 'icons')

INVOKED_CMD = sys.argv[0]
INVOKED_DIR = os.path.dirname(INVOKED_CMD)
if INVOKED_DIR == os.path.join('/opt', APPLET_NAME):
    APP_BIN_FOLDER = INVOKED_DIR
    APP_BASE_FOLDER = INVOKED_DIR
    APP_DATA_FOLDER = os.path.join(APP_BASE_FOLDER, 'share')
    APP_ICON_FOLDER = os.path.join(APP_DATA_FOLDER, 'icons')
elif INVOKED_DIR == '/usr/local/bin':
    APP_BASE_FOLDER = '/usr/local'
    APP_BIN_FOLDER = INVOKED_DIR
    APP_DATA_FOLDER = os.path.join(APP_BASE_FOLDER, 'share', APPLET_NAME)
    APP_ICON_FOLDER = os.path.join(APP_DATA_FOLDER, 'icons')
elif INVOKED_DIR == os.path.join(USER_FOLDER, '.local', 'bin'):
    APP_BIN_FOLDER = INVOKED_DIR
    APP_BASE_FOLDER = USER_DATA_FOLDER
    APP_DATA_FOLDER = os.path.join(USER_DATA_FOLDER)
    APP_ICON_FOLDER = os.path.join(USER_DATA_FOLDER, 'icons')
elif not INVOKED_DIR.startswith('/usr'):
    APP_BIN_FOLDER = INVOKED_DIR
    APP_BASE_FOLDER = INVOKED_DIR
    APP_DATA_FOLDER = os.path.join(APP_BASE_FOLDER, 'share')
    APP_ICON_FOLDER = os.path.join(APP_DATA_FOLDER, 'icons')

# update Python path
sys.path.insert(0, APP_DATA_FOLDER)


# dialog boxes
def load_applet_dialog(name):
    base = os.path.dirname(sys.argv[0])
    with open(os.path.join(APP_DATA_FOLDER, '%s.glade' % name)) as f:
        dialog_xml = f.read()
    return dialog_xml


# actual dialog box definitions (could be packed in .py files)
DIALOG_ADD_TASK = load_applet_dialog('when-command-edit-task')
DIALOG_ADD_CONDITION = load_applet_dialog('when-command-edit-condition')
DIALOG_SETTINGS = load_applet_dialog('when-command-settings')
DIALOG_TASK_HISTORY = load_applet_dialog('when-command-task-history')
DIALOG_ABOUT = load_applet_dialog('when-command-about')


# resource strings (consider making a module out of them)
class Resources(object):
    pass

resources = Resources()
resources.DLG_CONFIRM_DELETE_TASK = "Are you sure you want to delete task %s?"
resources.DLG_CONFIRM_DELETE_CONDITION = "Are you sure you want to delete condition %s?"
resources.DLG_CANNOT_DELETE_TASK = "Task %s could not be deleted."
resources.DLG_CANNOT_DELETE_CONDITION = "Condition %s could not be deleted."
resources.DLG_CANNOT_FIND_TASK = "Task %s could not be found."
resources.DLG_CANNOT_FIND_CONDITION = "Condition %s could not be found."
resources.DLG_WRONG_EXIT_STATUS = "Wrong value for exit status specified.\nPlease consider reviewing it."
resources.DLG_ABOUT_VERSION_STRING = "Version: %s"
resources.DLG_ITEM_DISABLED = "[disabled]"

resources.NOTIFY_TASK_FAILED = "Task failed: %s"

resources.MENU_EDIT_TASKS = "Edit Tasks..."
resources.MENU_EDIT_CONDITIONS = "Edit Conditions..."
resources.MENU_SETTINGS = "Settings..."
resources.MENU_TASK_HISTORY = "Task History..."
resources.MENU_PAUSE = "Pause"
resources.MENU_ABOUT = "About..."
resources.MENU_QUIT = "Quit"

resources.LISTCOL_ENVVARS_NAME = "Variable"
resources.LISTCOL_ENVVARS_VALUE = "Value"
resources.LISTCOL_TASKS_NAME = "Task Name"
resources.LISTCOL_HISTORY_TIMESTAMP = "Time / Duration"
resources.LISTCOL_HISTORY_TASK = "Task Name"
resources.LISTCOL_HISTORY_TRIGGER = "Trigger"
resources.LISTCOL_HISTORY_EXITCODE = "Exit Code"
resources.LISTCOL_HISTORY_SUCCESS = "Result"
resources.LISTCOL_HISTORY_REASON = "Reason"
resources.LISTCOL_HISTORY_ROWID = "Row ID"

resources.COMMAND_LINE_HELP_VERSION = "show applet version"
resources.COMMAND_LINE_HELP_SHOW_SETTINGS = "show settings dialog box for the running instance [R]"
resources.COMMAND_LINE_HELP_SHOW_HISTORY = "show history dialog box for the running instance [R]"
resources.COMMAND_LINE_HELP_SHOW_TASKS = "show tasks dialog box for the running instance [R]"
resources.COMMAND_LINE_HELP_SHOW_CONDITIONS = "show conditions box for the running instance [R]"
resources.COMMAND_LINE_HELP_RESET_CONFIG = "reset general configuration to default [S]"
resources.COMMAND_LINE_HELP_SHOW_ICON = "show applet icon [N]"
resources.COMMAND_LINE_HELP_CLEAR = "clear all tasks and conditions [S]"
resources.COMMAND_LINE_HELP_INSTALL = "install application icons and autostart [S]"
resources.COMMAND_LINE_HELP_QUERY = "query for a running instance"
resources.COMMAND_LINE_HELP_SHUTDOWN = "run shutdown tasks and close an existing istance [R]"
resources.COMMAND_LINE_HELP_KILL = "kill an existing istance [R]"
resources.COMMAND_LINE_HELP_EXPORT = "save tasks and conditions to a portable format"
resources.COMMAND_LINE_HELP_IMPORT = "import tasks and conditions from saved file [S]"
resources.COMMAND_LINE_HELP_VERBOSE = "show verbose output for some options"
resources.COMMAND_LINE_PREAMBLE = """\
%s: %s - %s /
When is a configurable user task scheduler for Ubuntu.
The command line interface can be used to interact with running instances of
When or to perform maintenance tasks. Use the --verbose option to read output
from the command, as most operations will show no output by default.
""" % (APPLET_NAME, APPLET_FULLNAME, APPLET_COPYRIGHT)
resources.COMMAND_LINE_EPILOG = """\
Note: options marked with [R] require an instance running in the background,
with [S] require that no instance is running and with [N] have only effect
after restart. Go to %s for more information.
""" % APPLET_URL

# constants for desktop entry and autostart entry
APP_ENTRY_DESKTOP = """\
#!/usr/bin/env xdg-open
[Desktop Entry]
Version={applet_version}
Name={applet_shortname}
Comment={applet_fullname}
Icon={icon_path}/alarmclock-128.png
Terminal=false
Type=Application
Categories=Utility;Application;System;
Exec=/usr/bin/env python3 {applet_exec}
"""

APP_ENTRY_AUTOSTART = """\
[Desktop Entry]
Name={applet_shortname}
GenericName={applet_fullname}
Comment={applet_fullname}
Icon={icon_path}/alarmclock-128.png
Terminal=false
Type=Application
Categories=Utility;Application;System;
Exec=/usr/bin/env python3 {applet_exec}
StartupNotify=false
X-GNOME-Autostart-enabled={autostart_enable}
"""


# utility to create a desktop file
def create_desktop_file(overwrite=False):
    filename = "%s.desktop" % APPLET_NAME
    pathname = os.path.join(USER_DATA_FOLDER, filename)
    if not os.path.exists(pathname) or overwrite:
        applet_log.info("MAIN: creating desktop entries")
        content = APP_ENTRY_DESKTOP.format(
            applet_version=APPLET_VERSION,
            applet_shortname=APPLET_SHORTNAME,
            applet_fullname=APPLET_FULLNAME,
            icon_path=APP_ICON_FOLDER,
            applet_exec=sys.argv[0],
        )
        with open(pathname, 'w') as f:
            f.write(content)
        os.chmod(pathname, 0o755)
        if os.path.isdir(USER_LAUNCHER_FOLDER):
            try:
                os.symlink(pathname, os.path.join(USER_LAUNCHER_FOLDER, filename))
            except Error:
                applet_log.error("MAIN: could not create the launcher")


# utility to create the autostart file
def create_autostart_file(overwrite=True):
    filename = "%s-startup.desktop" % APPLET_NAME
    pathname = os.path.join(USER_DATA_FOLDER, filename)
    enable = 'true' if config.get('General', 'autostart') else 'false'
    if not os.path.exists(pathname) or overwrite:
        applet_log.info("MAIN: creating autostart entries")
        content = APP_ENTRY_AUTOSTART.format(
            applet_version=APPLET_VERSION,
            applet_shortname=APPLET_SHORTNAME,
            applet_fullname=APPLET_FULLNAME,
            icon_path=APP_ICON_FOLDER,
            autostart_enable=enable,
            applet_exec=sys.argv[0],
        )
        with open(pathname, 'w') as f:
            f.write(content)
        os.chmod(pathname, 0o755)
        linkname = os.path.join(USER_AUTOSTART_FOLDER, filename)
        if os.path.isdir(USER_AUTOSTART_FOLDER) and not os.path.exists(linkname):
            try:
                os.symlink(pathname, linkname)
            except Error:
                applet_log.error("MAIN: could not create the autostart launcher")


# manage pause file
def create_pause_file():
    if not os.path.exists(USER_PAUSE_FILE):
        applet_log.info("MAIN: creating pause file")
        with open(USER_PAUSE_FILE, 'w') as f:
            f.write('paused')
        return True
    else:
        return False


def check_pause_file():
    applet_log.info("MAIN: checking if pause file exists")
    if os.path.exists(USER_PAUSE_FILE):
        applet_log.debug("MAIN: pause file exists")
        return True
    else:
        applet_log.debug("MAIN: pause file does not exist")
        return False


def unlink_pause_file():
    if os.path.exists(USER_PAUSE_FILE):
        applet_log.info("MAIN: removing pause file")
        os.unlink(USER_PAUSE_FILE)
        return True
    else:
        return False


def config_loglevel(level=None):
    if level is None:
        try:
            level = config.get('General', 'log level').lower()
        except Error:
            level = logging.WARNING
    if type(level) == str:
        try:
            level = {
                'debug': logging.DEBUG,
                'info': logging.INFO,
                'warning': logging.WARNING,
                'error': logging.ERROR,
                'critical': logging.CRITICAL,
            }[level.lower()]
        except KeyError as e:
            level = logging.DEBUG
    if type(level) == int:
        applet_log.setLevel(level)
    else:
        applet_log.setLevel(logging.WARNING)


def config_loghandler(max_size=None, max_backups=None):
    global applet_log_handler
    if max_size is None:
        max_size = config.get('History', 'log size')
    if max_backups is None:
        max_backups = config.get('History', 'log backups')
    handler = logging.handlers.RotatingFileHandler(USER_LOG_FILE, 'a', max_size, max_backups)
    handler.setFormatter(applet_log_formatter)
    applet_log.removeHandler(applet_log_handler)
    applet_log_handler = handler
    applet_log.addHandler(applet_log_handler)


#############################################################################
# the DBus service is needed for the running applet to receive commands
class AppletDBusService(dbus.service.Object):
    def __init__(self):
        bus_name = dbus.service.BusName(APPLET_BUS_NAME, bus=dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, APPLET_BUS_PATH)

    @dbus.service.method(APPLET_BUS_NAME)
    def kill_instance(self):
        self.remove_from_connection()
        Gtk.main_quit()

    @dbus.service.method(APPLET_BUS_NAME)
    def quit_instance(self):
        self.remove_from_connection()
        applet.quit(None)

    @dbus.service.method(APPLET_BUS_NAME)
    def show_dialog(self, dlgname):
        if dlgname == 'settings':
            applet.dlgsettings(None)
        elif dlgname == 'about':
            applet.dlgabout(None)
        elif dlgname == 'task':
            applet.dlgtask(None)
        elif dlgname == 'condition':
            applet.dlgcondition(None)
        elif dlgname == 'history':
            applet.dlghistory(None)

    @dbus.service.method(APPLET_BUS_NAME)
    def show_icon(self, show=True):
        applet.hide_icon(not show)


#############################################################################
# Core classes

# Main configuration handler
class Config(object):
    _config_file = None
    _config_parser = None

    def __init__(self, config_file=USER_CONFIG_FILE):
        def bool_spec(v):
            if type(v) == str:
                v = v.lower()
                if v in ('true', 'yes', 'on', '1'):
                    return True
                elif v in ('false', 'no', 'off', '0'):
                    return False
                else:
                    return None
            else:
                return bool(v)
        self._config_file = config_file
        self._config_parser = configparser.ConfigParser()
        self._default()
        if os.path.exists(self._config_file):
            self.load()
        else:
            self.save()
        self._types = {
            'Scheduler': {
                'tick seconds': int,
                'skip seconds': int,
                'preserve pause': bool_spec,
            },
            'General': {
                'show icon': bool_spec,
                'autostart': bool_spec,
                'notifications': bool_spec,
                'log level': str,
                'icon theme': str,
            },
            'Concurrency': {
                'max threads': int,
            },
            'History': {
                'max items': int,
                'log size': int,
                'log backups': int,
            },
        }

    def _default(self):
        self._config_parser.read_string("""
            [Scheduler]
            tick seconds = 15
            skip seconds = 60
            preserve pause = true

            [General]
            show icon = true
            autostart = false
            notifications = true
            icon theme = guess
            log level = warning

            [Concurrency]
            max threads = 5

            [History]
            max items = 100
            log size = 1048576
            log backups = 4
            """)

    def get(self, section, entry):
        type_spec = self._types[section][entry]
        return type_spec(self._config_parser.get(section, entry))

    def set(self, section, entry, value):
        typespec = self._types[section][entry]
        try:
            v = str(typespec(value))
            if type(v) == bool:
                v = v.lower()
            self._config_parser.set(section, entry, v)
        except (TypeError, configparser.Error) as e:
            applet_log.warning("CONFIG: cannot set option %s:%s = %s [%s]" % (section, entry, value, e))

    def load(self):
        try:
            self._config_parser.read(self._config_file)
        except configparser.Error as e:
            applet_log.warning("CONFIG: malformed configuration file, using default [%s]" % e)
            self._default()
            self.save()

    def reset(self):
        self._default()
        self.save()

    def save(self):
        try:
            with open(self._config_file, mode='w') as f:
                self._config_parser.write(f)
        except IOError as e:
            applet_log.error("CONFIG: cannot write file %s [%s]" % (_config_file, e))


# scheduler logic, see http://stackoverflow.com/a/18906292 for details
class Periodic(object):
    def __init__(self, interval, function, *args, **kwargs):
        self.stopped = True
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self._logger = logging.getLogger(APPLET_NAME)
        self._lock = threading.Lock()
        self._timer = None
        self._logger.info("SCHED: initialized")
        if kwargs.pop('autostart', True):
            self.start()

    def _run(self):
        self.start(from_run=True)
        self._logger.info("SCHED: periodic run")
        self.function(*self.args, **self.kwargs)

    def start(self, from_run=False):
        self._lock.acquire()
        if from_run or self.stopped:
            self.stopped = False
            self._timer = threading.Timer(self.interval, self._run)
            self._timer.start()
        self._lock.release()
        if not from_run:
            self._logger.info("SCHED: started")

    def stop(self):
        self._lock.acquire()
        self.stopped = True
        if self._timer is not None:
            self._timer.cancel()
        self._lock.release()
        self._logger.info("SCHED: stopped")

    def restart(self, new_interval=None):
        if new_interval:
            self._logger.info("SCHED: changing interval to %s" % new_interval)
            self.interval = new_interval
        self.stop()
        self.start()


# module level functions to handle condition checks and thus task execution
def periodic_condition_check():
    global current_deferred_events
    applet_lock.acquire()
    current_deferred_events = deferred_events.items(clear=True)
    applet_lock.release()
    try:
        with ThreadPoolExecutor(config.get('Concurrency', 'max threads')) as e:
            e.map(lambda c: c.tick(), conditions)
    finally:
        applet_lock.acquire()
        current_deferred_events = None
        applet_lock.release()


# check among the system event conditions setting the global system event
def sysevent_condition_check(event, param=None):
    global current_system_event
    applet_lock.acquire()
    current_system_event = event
    applet_lock.release()
    try:
        if periodic.stopped:
            applet_log.info("SYSEVENT: check skipped due to scheduler pause")
            return
        conds = filter(lambda c: type(c) == EventBasedCondition, conditions)
        with ThreadPoolExecutor(config.get('Concurrency', 'max threads')) as e:
            e.map(lambda c: c.tick(), conds)
    finally:
        applet_lock.acquire()
        current_system_event = None
        applet_lock.release()


# application-wide collections
class Tasks(object):
    _list = []
    _last_id = 0
    _lock = threading.Lock()

    def __iter__(self):
        for task in self._list:
            yield task

    names = property(lambda self: [t.task_name for t in self._list])

    def get_id(self):
        self._lock.acquire()
        self._last_id += 1
        retval = self._last_id
        self._lock.release()
        return retval

    def save(self):
        l = []
        for t in self._list:
            t.dump()
            l.append(t.task_name)
        file_name = os.path.join(USER_CONFIG_FOLDER, 'task.list')
        with open(file_name, 'wb') as f:
            pickle.dump(l, f)

    def load(self):
        self._list = []
        self._last_id = 0
        file_name = os.path.join(USER_CONFIG_FOLDER, 'task.list')
        with open(file_name, 'rb') as f:
            l = pickle.load(f)
        for x in l:
            t = Task.restore(x)
            self.add(t)

    def add(self, task):
        if task.task_id == 0:
            raise ValueError("task must be initialized or loaded")
        if task.task_id <= self._last_id or task.task_name in [t.task_name for t in self._list]:
            applet_log.info("GLOBAL: adding task %s" % task.task_name)
            self._lock.acquire()
            l = list(filter(lambda x: x.task_name != task.task_name, self._list))
            l.append(task)
            self._list = l
            self._lock.release()
        else:
            self._lock.acquire()
            self._list.append(task)
            self._lock.release()

    def remove(self, task_name=None, task_id=None):
        task = None
        if task_id:
            task = next((t for t in self._list if t.task_id == task_id), None)
        elif task_name:
            task = next((t for t in self._list if t.task_name == task_name), None)
        if task:
            removable = True
            for c in conditions:
                if task.task_name in c.task_names:
                    removable = False
                    break
            if removable:
                self._lock.acquire()
                self._list.remove(task)
                task.unlink_file()
                self._lock.release()
                return True
            else:
                return False
        else:
            return False

    def get(self, task_id=None, task_name=None):
        if task_id:
            try:
                return list(filter(lambda x: task_id == x.task_id, self._list))[0]
            except IndexError as e:
                return None
        elif task_name:
            try:
                return list(filter(lambda x: task_name == x.task_name, self._list))[0]
            except IndexError as e:
                return None


class Conditions(object):
    _list = []
    _last_id = 0
    _lock = threading.Lock()

    def __iter__(self):
        for condition in self._list:
            yield condition

    names = property(lambda self: [t.cond_name for t in self._list])

    def get_id(self):
        self._lock.acquire()
        self._last_id += 1
        retval = self._last_id
        self._lock.release()
        return retval

    def save(self):
        l = []
        for c in self._list:
            c.dump()
            l.append((c.cond_name, c.__class__.__name__))
        file_name = os.path.join(USER_CONFIG_FOLDER, 'condition.list')
        with open(file_name, 'wb') as f:
            pickle.dump(l, f)

    def load(self):
        self._list = []
        self._last_id = 0
        file_name = os.path.join(USER_CONFIG_FOLDER, 'condition.list')
        with open(file_name, 'rb') as f:
            l = pickle.load(f)
        for x, xc in l:
            c = Condition.restore(x)
            c.reset()
            self.add(c)

    def add(self, cond):
        if cond.cond_id == 0:
            raise ValueError("condition must be initialized or loaded")
        if cond.cond_id <= self._last_id or cond.cond_name in [t.cond_name for t in self._list]:
            applet_log.info("GLOBAL: adding condition %s" % cond.cond_name)
            self._lock.acquire()
            l = list(filter(lambda x: x.cond_name != cond.cond_name, self._list))
            l.append(cond)
            self._list = l
            self._lock.release()
        else:
            self._lock.acquire()
            self._list.append(cond)
            self._lock.release()

    def remove(self, cond_name=None, cond_id=None):
        cond = None
        if cond_id:
            cond = next((c for c in self._list if c.cond_id == cond_id), None)
        elif cond_name:
            cond = next((c for c in self._list if c.cond_name == cond_name), None)
        if cond:
            self._lock.acquire()
            self._list.remove(cond)
            cond.unlink_file()
            self._lock.release()
            return True
        else:
            return False

    def get(self, cond_id=None, cond_name=None):
        if cond_id:
            try:
                return list(filter(lambda x: cond_id == x.cond_id, self._list))[0]
            except IndexError as e:
                return None
        elif cond_name:
            try:
                return list(filter(lambda x: cond_name == x.cond_name, self._list))[0]
            except IndexError as e:
                return None


# data for the history queue (which is actually a resizable bounded deque)
historyitem = namedtuple('historyitem', ['item_id', 'startup_time', 'run_time',
                                         'task_name', 'trigger_cond', 'success',
                                         'exit_code', 'stdout', 'stderr',
                                         'failure_reason'])


class HistoryQueue(object):

    def __init__(self, max_items=None):
        if max_items is None:
            max_items = config.get('History', 'max items')
        self._lock = threading.Lock()
        self._queue = deque(maxlen=max_items)
        self._iter_id = self._item_id()

    def _item_id(self):
        # an ID that is unique just within the queue: a reusable int is enough
        # FIXME: find a way to avoid conflicts when the queue is shrunk
        next_id = 0
        while True:
            yield next_id
            next_id = (next_id + 1) % (self._queue.maxlen * 2)

    def append(self, name, startup_time, success, trigger, status=None, stdout=None, stderr=None, reason=None):
        t = time.time()
        run_time = t - startup_time
        item = historyitem(next(self._iter_id), startup_time, run_time, name,
                           trigger, success, status, stdout, stderr, reason)
        self._lock.acquire()
        self._queue.append(item)
        self._lock.release()

    def resize(self, max_items=None):
        if max_items is None:
            max_items = config.get('History', 'max items')
        self._lock.acquire()
        self._queue = deque(self._queue, max_items)
        self._iter_id = self._item_id()
        self._lock.release()

    def clear(self):
        self._lock.acquire()
        self._queue.clear()
        self._lock.release()

    def item_by_id(self, item_id):
        l = list([item for item in self._queue if item.item_id == item_id])
        if len(l) == 1:
            return l[0]
        else:
            return None

    def items(self, max_items=None):
        self._lock.acquire()
        if not max_items:
            rv = deque(self._queue)
        else:
            rv = deque(self._queue, max_items)
        self._lock.release()
        return list(rv)


# list of events that are enqueued by external agents: it is implemented
# as a set; this means that a deferred event only will appear once, no
# matter how many times it is pushed to the list
class DeferredEvents(object):

    def __init__(self):
        self._lock = threading.Lock()
        self._events = set()

    def append(self, event):
        self._lock.acquire()
        self._events.add(event)
        self._lock.release()

    def clear(self):
        self._lock.acquire()
        self._events.clear()
        self._lock.release()

    def items(self, clear=False):
        self._lock.acquire()
        rv = self._events.copy()
        if clear:
            self._events.clear()
        self._lock.release()
        return rv


#############################################################################
# The main Task class: this represents a certain task registered in the
# application. It contains all information about the task to execute, as well
# as its environment and postconditions
class Task(object):

    _logger = logging.getLogger(APPLET_NAME)

    # make task level logging easier
    def _debug(self, msg):
        self._logger.debug("TASK: %s [%s]: %s" % (self.task_name, self.task_id, msg))

    def _info(self, msg):
        self._logger.info("TASK: %s [%s]: %s" % (self.task_name, self.task_id, msg))

    def _warning(self, msg):
        self._logger.warning("TASK: %s [%s]: %s" % (self.task_name, self.task_id, msg))

    def _error(self, msg):
        self._logger.error("TASK: %s [%s]: %s" % (self.task_name, self.task_id, msg))

    def _critical(self, msg):
        self._logger.critical("TASK: %s [%s]: %s" % (self.task_name, self.task_id, msg))

    # the real McCoy
    def __init__(self, name=None, command=None, startup_dir=None):
        self.task_id = 0
        self.task_name = name
        self.environment_vars = {}
        self.include_env = True
        self.success_stdout = None
        self.success_stderr = None
        self.success_status = None
        self.failure_stdout = None
        self.failure_stderr = None
        self.failure_status = None
        self.match_exact = False
        self.match_regexp = False
        self.case_sensitive = False
        self.command = ""
        self.startup_dir = None
        self.running = False
        self._process_stdout = ""
        self._process_stderr = ""
        self._process_status = 0
        self._process_failed = False
        if name is None:
            self._info("empty task created")
        else:
            self.command = command
            self.startup_dir = startup_dir
            self.task_id = tasks.get_id()
            self._info("task created")

    def set_env(self, var, value):
        self.environment_vars[var] = value

    def clear_env(self, var):
        self.set_env(var, None)

    def set_check(self, **kwargs):
        for k in kwargs.keys():
            if k == 'success_stdout':
                self.success_stdout = str(kwargs[k])
            elif k == 'success_stderr':
                self.success_stderr = str(kwargs[k])
            elif k == 'failure_stdout':
                self.failure_stdout = str(kwargs[k])
            elif k == 'failure_stderr':
                self.failure_stderr = str(kwargs[k])
            elif k == 'success_status':
                self.success_status = int(kwargs[k])
            elif k == 'failure_status':
                self.failure_status = int(kwargs[k])
            else:
                self._error("internal error")
                raise TypeError("keyword argument '%s' not supported" % k)

    def renew_id(self):
        self.task_id = tasks.get_id()

    def dump(self):
        if self.task_name is None:
            raise RuntimeError("task not initialized")
        file_name = os.path.join(USER_CONFIG_FOLDER, "%s.task" % self.task_name)
        with open(file_name, 'wb') as f:
            pickle.dump(self, f)

    def unlink_file(self):
        file_name = os.path.join(USER_CONFIG_FOLDER, "%s.task" % self.task_name)
        if os.path.exists(file_name):
            os.unlink(file_name)

    @staticmethod
    def restore(name):
        file_name = os.path.join(USER_CONFIG_FOLDER, "%s.task" % name)
        with open(file_name, 'rb') as f:
            o = pickle.load(f)
            o.renew_id()
            return o

    # run a task in a subprocess possibly specifying trigger condition name
    def run(self, trigger_name=None):
        if self.running:
            raise RuntimeError("overlapping tasks")
        applet_lock.acquire()
        self.running = True
        applet_lock.release()
        self._process_failed = False
        if self.task_name is None:
            self._process_failed = True
            raise RuntimeError("task not initialized")
        if trigger_name:
            self._info("[trigger: %s] running command: %s" % (trigger_name, self.command))
        else:
            self._info("[trigger: unknown] running command: %s" % self.command)
        if self.environment_vars:
            if self.include_env:
                env = os.environ
                for k in self.environment_vars:
                    env[k] = self.environment_vars[k]
            else:
                env = self.environment_vars
        else:
            env = None
        startup_time = time.time()
        self._process_stdout = None
        self._process_stderr = None
        self._process_status = None
        try:
            failure_reason = None
            startup_dir = self.startup_dir if self.startup_dir else '.'
            self._debug("spawning subprocess: %s" % self.command)
            with subprocess.Popen(self.command,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  shell=True,
                                  cwd=startup_dir,
                                  env=env) as proc:
                stdout, stderr = proc.communicate()
                self._process_stdout = stdout.decode().strip()
                self._process_stderr = stderr.decode().strip()
                self._process_status = proc.returncode
                if self.success_status is not None:
                    if self._process_status != self.success_status:
                        self._warning("task failed (process status=%s)" % self._process_status)
                        self._process_failed = True
                        failure_reason = 'status'
                elif self.failure_status is not None:
                    if self._process_status == self.failure_status:
                        self._warning("task failed (process status=%s)" % self._process_status)
                        self._process_failed = True
                        failure_reason = 'status'
                elif self.failure_stdout is not None:
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        if self.match_exact:
                            if re.match(self.failure_stdout, self._process_stdout, flags=0) is not None:
                                self._warning("task failed (stdout regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stdout'
                        else:
                            if re.search(self.failure_stdout, self._process_stdout, flags=0) is not None:
                                self._warning("task failed (stdout regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stdout'
                    else:
                        if self.case_sensitive:
                            if self.match_exact:
                                if self._process_stdout == self.failure_stdout:
                                    self._warning("task failed (stdout check)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                            else:
                                if self.failure_stdout in self._process_stdout:
                                    self._warning("task failed (stdout check)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                        else:
                            if self.match_exact:
                                if self._process_stdout.upper() == self.failure_stdout.upper():
                                    self._warning("task failed (stdout check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                            else:
                                if self.failure_stdout.upper() in self._process_stdout.upper():
                                    self._warning("task failed (stdout check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                elif self.failure_stderr is not None:
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        if self.match_exact:
                            if re.match(self.failure_stderr, self._process_stderr, flags=0) is not None:
                                self._warning("task failed (stderr regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stderr'
                        else:
                            if re.search(self.failure_stderr, self._process_stderr, flags=0) is not None:
                                self._warning("task failed (stderr regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stderr'
                    else:
                        if self.case_sensitive:
                            if self.match_exact:
                                if self._process_stderr == self.failure_stderr:
                                    self._warning("task failed (stderr check)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                            else:
                                if self.failure_stderr in self._process_stderr:
                                    self._warning("task failed (stderr check)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                        else:
                            if self.match_exact:
                                if self._process_stderr.upper() == self.failure_stderr.upper():
                                    self._warning("task failed (stderr check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                            else:
                                if self.failure_stderr.upper() in self._process_stderr.upper():
                                    self._warning("task failed (stderr check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                elif self.success_stdout is not None:
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        if self.match_exact:
                            if re.match(self.success_stdout, self._process_stdout, flags=0) is None:
                                self._warning("task failed (stdout regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stdout'
                        else:
                            if re.search(self.success_stdout, self._process_stdout, flags=0) is None:
                                self._warning("task failed (stdout regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stdout'
                    else:
                        if self.case_sensitive:
                            if self.match_exact:
                                if self._process_stdout != self.success_stdout:
                                    self._warning("task failed (stdout check)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                            else:
                                if self.success_stdout not in self._process_stdout:
                                    self._warning("task failed (stdout check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                        else:
                            if self.match_exact:
                                if self._process_stdout.upper() != self.success_stdout.upper():
                                    self._warning("task failed (stdout check)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                            else:
                                if not self.success_stdout.upper() in self._process_stdout.upper():
                                    self._warning("task failed (stdout check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stdout'
                elif self.success_stderr is not None:
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        if self.match_exact:
                            if re.match(self.success_stderr, self._process_stderr, flags=0) is None:
                                self._warning("task failed (stderr regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stderr'
                        else:
                            if re.search(self.success_stderr, self._process_stderr, flags=0) is None:
                                self._warning("task failed (stderr regexp%s check)" % " nocase" if flags else "")
                                self._process_failed = True
                                failure_reason = 'stderr'
                    else:
                        if self.case_sensitive:
                            if self.match_exact:
                                if self._process_stderr != self.success_stderr:
                                    self._warning("task failed (stderr check)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                            else:
                                if self.success_stderr not in self._process_stderr:
                                    self._warning("task failed (stderr check)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                        else:
                            if self.match_exact:
                                if self._process_stderr.upper() != self.success_stderr.upper():
                                    self._warning("task failed (stderr check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
                            else:
                                if self.success_stderr.upper() not in self._process_stderr.upper():
                                    self._warning("task failed (stderr check, case insensitive)")
                                    self._process_failed = True
                                    failure_reason = 'stderr'
        except RuntimeError as e:
            self._warning("task failed, runtime error: %s" % e)
            failure_reason = 'overlap'
        except OSError as e:
            self._warning("task failed, system error: %s" % e)
            self._process_failed = True
            failure_reason = 'system'
        except subprocess.CalledProcessError as e:
            self._warning("task failed, called process error")
            self._process_failed = True
            failure_reason = 'process'
        except subprocess.TimeoutExpired as e:
            self._warning("task failed, timeout expired")
            self._process_failed = True
            failure_reason = 'timeout'
        except Exception as e:
            self._error("task failed (unexpected error)")
            self._process_failed = True
            failure_reason = 'generic'
        finally:
            success = self.running and not self._process_failed
            history.append(self.task_name, startup_time, success, trigger_name,
                           self._process_status, self._process_stdout,
                           self._process_stderr, failure_reason)
            if self._process_failed:
                applet.set_attention(True)
                applet.notify(resources.NOTIFY_TASK_FAILED % self.task_name)
            else:
                self._info("task successfully finished")
            if self.running:
                applet_lock.acquire()
                self.running = False
                applet_lock.release()
            to_report = bool(
                self.failure_status is not None or
                self.failure_stderr is not None or
                self.failure_stdout is not None or
                self.success_status is not None or
                self.success_stderr is not None or
                self.success_stdout is not None
            )
            if to_report:
                return success
            else:
                return None


# these functions convert a Task instance to a dictionary and back
def Task_to_dict(t):
    applet_log.info("MAIN: trying to dump task %s" % t.task_name)
    d = {}
    d['type'] = 'task'
    d['task_id'] = t.task_id
    d['task_name'] = t.task_name
    d['environment_vars'] = t.environment_vars
    d['include_env'] = t.include_env
    d['success_stdout'] = t.success_stdout
    d['success_stderr'] = t.success_stderr
    d['success_status'] = t.success_status
    d['failure_stdout'] = t.failure_stdout
    d['failure_stderr'] = t.failure_stderr
    d['failure_status'] = t.failure_status
    d['match_exact'] = t.match_exact
    d['case_sensitive'] = t.case_sensitive
    d['command'] = t.command
    d['startup_dir'] = t.startup_dir
    d['match_regexp'] = t.match_regexp
    applet_log.debug("MAIN: task %s dumped" % t.task_name)
    return d


def dict_to_Task(d):
    if d['type'] != 'task':
        raise ValueError("incorrect dictionary type")
    t = Task()
    applet_log.debug("MAIN: trying to restore task %s" % t.task_name)
    t.task_id = d['task_id']
    t.task_name = d['task_name']
    t.environment_vars = d['environment_vars']
    t.include_env = d['include_env']
    t.success_stdout = d['success_stdout']
    t.success_stderr = d['success_stderr']
    t.success_status = d['success_status']
    t.failure_stdout = d['failure_stdout']
    t.failure_stderr = d['failure_stderr']
    t.failure_status = d['failure_status']
    t.match_exact = d['match_exact']
    t.case_sensitive = d['case_sensitive']
    t.command = d['command']
    t.startup_dir = d['startup_dir']
    # TODO: if there are more parameters, use d.get('key', default_val)
    t.match_regexp = d.get('match_regexp', False)
    applet_log.info("MAIN: task %s restored" % t.task_name)
    return t


#############################################################################
# The main condition class, with test verification functions: conditions are
# scanned at each clock tick in parallel, and if a condition finds that the
# time to actually test itself it performs the test and possibly runs all the
# associated tasks. The condition logic is provided by the (abstract) base
# class, and all the particular condition types must be derived from it
class Condition(object):

    def _check_condition(self):
        self._critical("NTBS: checking base condition")
        return False

    def _start_timer(self):
        self.startup_time = time.time()
        self.last_tested = self.startup_time

    _logger = logging.getLogger(APPLET_NAME)

    # make condition level logging easier
    def _debug(self, msg):
        self._logger.debug("COND: %s [%s]: %s" % (self.cond_name, self.cond_id, msg))

    def _info(self, msg):
        self._logger.info("COND: %s [%s]: %s" % (self.cond_name, self.cond_id, msg))

    def _warning(self, msg):
        self._logger.warning("COND: %s [%s]: %s" % (self.cond_name, self.cond_id, msg))

    def _error(self, msg):
        self._logger.error("COND: %s [%s]: %s" % (self.cond_name, self.cond_id, msg))

    def _critical(self, msg):
        self._logger.critical("COND: %s [%s]: %s" % (self.cond_name, self.cond_id, msg))

    def __init__(self, name=None, repeat=True, exec_sequence=True):
        self.cond_id = None
        self.cond_name = name
        self.last_tested = None
        self.last_succeeded = None
        self.skip_seconds = config.get('Scheduler', 'skip seconds')
        self.startup_time = None
        self.task_names = []
        self.repeat = True
        self.exec_sequence = True
        self.break_failure = False
        self.break_success = False
        self.suspended = False
        self._has_succeeded = False
        if self.__class__.__name__ == 'Condition':
            raise NotImplementedError("abstract class")
        if name is None:
            self._info("empty condition created")
        else:
            self.repeat = repeat
            self.exec_sequence = exec_sequence
            self.cond_id = conditions.get_id()
            self._info("condition created")
            self._start_timer()

    def add_task(self, task_name):
        l = list(filter(lambda x: x != task_name, self.task_names))
        self.task_names = l
        self.task_names.append(task_name)

    def delete_task(self, task_name):
        l = list(filter(lambda x: x != task_name, self.task_names))
        self.task_names = l

    def renew_id(self):
        self.cond_id = conditions.get_id()

    def dump(self):
        if self.cond_name is None:
            raise RuntimeError("condition not initialized")
        file_name = os.path.join(USER_CONFIG_FOLDER, '%s.cond' % self.cond_name)
        with open(file_name, 'wb') as f:
            pickle.dump(self, f)

    def unlink_file(self):
        file_name = os.path.join(USER_CONFIG_FOLDER, "%s.cond" % self.cond_name)
        if os.path.exists(file_name):
            os.unlink(file_name)

    @staticmethod
    def restore(name):
        file_name = os.path.join(USER_CONFIG_FOLDER, '%s.cond' % name)
        with open(file_name, 'rb') as f:
            o = pickle.load(f)
            o.renew_id()
            return o

    def reset(self):
        self.last_tested = None
        self.last_succeeded = None
        self._has_succeeded = False

    def suspend(self):
        self.suspended = True

    def resume(self):
        self.suspended = False

    def test(self):
        if self.cond_name is None:
            raise RuntimeError("condition not initialized")
        if self.suspended:
            self._info("check skipped (suspended condition)")
            return False
        elif not self.repeat and self._has_succeeded:
            self._info("check skipped (non-repeating condition)")
            return False
        else:
            self._debug("check or skip condition test")
            t = time.time()
            if not self.last_tested:
                self.last_tested = t
            delta_t = round(t - self.last_tested, 3)
            self._debug("verifying check time (%s >= %s)" % (delta_t, self.skip_seconds))
            if delta_t >= self.skip_seconds:
                self.last_tested = t
                if self._check_condition():
                    self.last_succeeded = t
                    self._has_succeeded = True
                    self._info("check successful")
                    return True
                else:
                    self._info("check failed")
            else:
                self._info("check skipped")
        return False

    def verified(self):
        if self.last_succeeded:
            rv = bool(self.last_tested == self.last_succeeded)
        else:
            rv = False
        self.last_succeeded = None
        return rv

    def tick(self):
        self.test()
        if self.verified():
            if self.exec_sequence:
                for task_name in self.task_names:
                    task = tasks.get(task_name=task_name)
                    if task:
                        self._info("sequential run of task %s" % task_name)
                        outcome = task.run(self.cond_name)
                        if outcome is not None:
                            if outcome and self.break_success:
                                self._info("breaking on task %s success" % task_name)
                                break
                            elif not outcome and self.break_failure:
                                self._info("breaking on task %s failure" % task_name)
                                break
                    else:
                        self._warning("task not found: %s" % task_name)
            else:
                localtasks = []
                foundnames = []
                for task_name in self.task_names:
                    task = tasks.get(task_name=task_name)
                    if task:
                        localtasks.append(task)
                        foundnames.append(task_name)
                    else:
                        self._warning("task not found: %s" % task_name)
                self._info("parallel run of tasks %s" % ", ".join(foundnames))
                with ThreadPoolExecutor(config.get('Concurrency', 'max threads')) as e:
                    e.map(lambda task: task.run(self.cond_name), localtasks)


# convert a condition to a dictionary and back
def Condition_to_dict(c):
    d = {}
    d['type'] = 'condition'
    d['subtype'] = None
    d['cond_id'] = c.cond_id
    d['cond_name'] = c.cond_name
    d['task_names'] = c.task_names
    d['repeat'] = c.repeat
    d['exec_sequence'] = c.exec_sequence
    d['suspended'] = c.suspended
    d['break_failure'] = c.break_failure
    d['break_success'] = c.break_success
    applet_log.debug("MAIN: condition %s dumped" % c.cond_name)
    return d


def dict_to_Condition(d, c=None):
    if d['type'] != 'condition':
        raise ValueError("incorrect dictionary type")
    # this will raise an error
    if c is None:
        applet_log.critical("MAIN: NTBS: attempt to restore base Condition")
        c = Condition()
    applet_log.debug("MAIN: trying to restore condition %s" % c.cond_name)
    c.cond_id = d['cond_id']
    c.cond_name = d['cond_name']
    c.task_names = d['task_names']
    c.repeat = d['repeat']
    c.exec_sequence = d['exec_sequence']
    c.suspended = d['suspended']
    # TODO: if there are more parameters, use d.get('key', default_val)
    c.break_failure = d.get('break_failure', False)
    c.break_success = d.get('break_success', False)
    return c


class IntervalBasedCondition(Condition):

    def _check_condition(self):
        self._debug("checking interval based condition")
        if self.interval:
            t = time.time()
            if self.interval <= round(t - self.checked):
                self.checked = t
                return True
        return False

    def __init__(self, name, interval, repeat=True, exec_sequence=True):
        self.interval = interval
        self.checked = time.time()
        Condition.__init__(self, name, repeat, exec_sequence)
        self.skip_seconds = 0


def IntervalBasedCondition_to_dict(c):
    applet_log.info("MAIN: dump interval based condition %s" % c.cond_name)
    d = Condition_to_dict(c)
    d['subtype'] = 'IntervalBasedCondition'
    d['interval'] = c.interval
    return d


def dict_to_IntervalBasedCondition(d):
    if d['type'] != 'condition' or d['subtype'] != 'IntervalBasedCondition':
        raise ValueError("incorrect dictionary type")
    name = d['cond_name']
    interval = d['interval']
    # TODO: if there are more parameters, use d.get('key', default_val)
    c = IntervalBasedCondition(name, interval)
    c = dict_to_Condition(d, c)
    applet_log.info("MAIN: restored interval based condition %s" % c.cond_name)
    return c


class TimeBasedCondition(Condition):

    def _check_condition(self):
        self._debug("checking time based condition")
        cur_time = time.time()
        now = time.localtime(cur_time)
        test_tuple = (
            self.year or now[0],
            self.month or now[1],
            self.day or now[2],
            self.hour or now[3],
            self.minute or now[4],
            0,
            now[6],
            self.weekday or now[6],
            now[8],
        )
        test_time = time.mktime(test_tuple)
        span = max(self.tick_seconds, self.skip_seconds)
        self._debug("checking %.3f - %.3f (=%.3f) in [0-%s]" % (cur_time, test_time, cur_time - test_time, span))
        if 0 < cur_time - test_time <= span:
            return True
        else:
            return False

    def __init__(self, name, timedict, repeat=True, exec_sequence=True):
        if not timedict:
            raise ValueError("time specification must be given")
        self.year = None if 'year' not in timedict.keys() else timedict['year']
        self.month = None if 'month' not in timedict.keys() else timedict['month']
        self.day = None if 'day' not in timedict.keys() else timedict['day']
        self.hour = None if 'hour' not in timedict.keys() else timedict['hour']
        self.minute = None if 'minute' not in timedict.keys() else timedict['minute']
        self.weekday = None if 'weekday' not in timedict.keys() else timedict['weekday']
        self.tick_seconds = config.get('Scheduler', 'tick seconds')
        Condition.__init__(self, name, repeat, exec_sequence)
        self.skip_seconds = 0


def TimeBasedCondition_to_dict(c):
    applet_log.info("MAIN: dump time based condition %s" % c.cond_name)
    d = Condition_to_dict(c)
    d['subtype'] = 'TimeBasedCondition'
    d['year'] = c.year
    d['month'] = c.month
    d['day'] = c.day
    d['hour'] = c.hour
    d['minute'] = c.minute
    d['weekday'] = c.weekday
    return d


def dict_to_TimeBasedCondition(d):
    if d['type'] != 'condition' or d['subtype'] != 'TimeBasedCondition':
        raise ValueError("incorrect dictionary type")
    name = d['cond_name']
    # we can use d for timedict because the needed keys (intentionally) match
    # TODO: if there are more parameters, use d.get('key', default_val)
    c = TimeBasedCondition(name, d)
    c = dict_to_Condition(d, c)
    applet_log.info("MAIN: restored time based condition %s" % c.cond_name)
    return c


class CommandBasedCondition(Condition):

    def _check_condition(self):
        self._debug("checking command based condition")
        try:
            self._debug("spawning test subprocess: %s" % self.command)
            with subprocess.Popen(self.command,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  shell=True) as proc:
                stdout, stderr = proc.communicate()
                if self.expected_status is not None:
                    self._info("checking condition command exit status")
                    self._debug("test: %s == %s" % (proc.returncode, self.expected_status))
                    if int(proc.returncode) == self.expected_status:
                        return True
                elif self.expected_stdout:
                    self._info("checking condition command output")
                    expected = self.expected_stdout
                    returned = stdout.decode().strip()
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        self._debug("test: '%s' ~ '%s'%s" % (returned, expected, ' (I)' if flags else ''))
                        try:
                            if self.match_exact:
                                if re.match(expected, returned, flags) is not None:
                                    return True
                            else:
                                if re.search(expected, returned, flags) is not None:
                                    return True
                        except:
                            self._warning("invalid regular expression (skipped)")
                    else:
                        if not self.case_sensitive:
                            expected = expected.upper()
                            returned = returned.upper()
                        if self.match_exact:
                            self._debug("test: '%s' == '%s'" % (expected, returned))
                            if expected == returned:
                                return True
                        else:
                            self._debug("test: '%s' in '%s'" % (expected, returned))
                            if expected in returned:
                                return True
                elif self.expected_stderr:
                    self._info("checking condition command error output")
                    expected = self.expected_stderr
                    returned = stderr.decode().strip()
                    if self.match_regexp:
                        if self.case_sensitive:
                            flags = 0
                        else:
                            flags = re.IGNORECASE
                        self._debug("test: '%s' ~ '%s'%s" % (returned, expected, ' (I)' if flags else ''))
                        try:
                            if self.match_exact:
                                if re.match(expected, returned, flags) is not None:
                                    return True
                            else:
                                if re.search(expected, returned, flags) is not None:
                                    return True
                        except:
                            self._warning("invalid regular expression (skipped)")
                    else:
                        if not self.case_sensitive:
                            expected = expected.upper()
                            returned = returned.upper()
                        if self.match_exact:
                            self._debug("test: '%s' == '%s'" % (expected, returned))
                            if expected == returned:
                                return True
                        else:
                            self._debug("test: '%s' in '%s'" % (expected, returned))
                            if expected in returned:
                                return True
                self._debug("test failed")
                return False
        except OSError as e:
            self._warning("condition failed, system error: %s" % e)
            return False
        except subprocess.CalledProcessError as e:
            self._warning("condition failed, called process error")
            return False
        except subprocess.TimeoutExpired as e:
            self._warning("condition failed, timeout expired")
            return False
        except Exception as e:
            self._error("condition failed (unexpected error: %s)" % e)
            return False

    def __init__(self, name, command, status=None, stdout=None, stderr=None, repeat=True, exec_sequence=True):
        self.match_exact = False
        self.match_regexp = False
        self.case_sensitive = False
        check_count = 0
        if status is not None:
            check_count += 1
        if stdout is not None:
            check_count += 1
        if stderr is not None:
            check_count += 1
        if check_count != 1:
            raise ValueError("only one of status, stdout, stderr should be set")
        self.command = command
        self.expected_status = status
        self.expected_stdout = stdout
        self.expected_stderr = stderr
        self.last_tick = time.time()
        Condition.__init__(self, name, repeat, exec_sequence)


def CommandBasedCondition_to_dict(c):
    applet_log.info("MAIN: dump command based condition %s" % c.cond_name)
    d = Condition_to_dict(c)
    d['subtype'] = 'CommandBasedCondition'
    d['match_exact'] = c.match_exact
    d['match_regexp'] = c.match_regexp
    d['case_sensitive'] = c.case_sensitive
    d['command'] = c.command
    d['expected_status'] = c.expected_status
    d['expected_stdout'] = c.expected_stdout
    d['expected_stderr'] = c.expected_stderr
    return d


def dict_to_CommandBasedCondition(d):
    if d['type'] != 'condition' or d['subtype'] != 'CommandBasedCondition':
        raise ValueError("incorrect dictionary type")
    name = d['cond_name']
    command = d['command']
    status = d['expected_status']
    stdout = d['expected_stdout']
    stderr = d['expected_stderr']
    match_exact = d['match_exact']
    case_sensitive = d['case_sensitive']
    # TODO: if there are more parameters, use d.get('key', default_val)
    match_regexp = d.get('match_regexp', False)
    c = CommandBasedCondition(name, command, status, stdout, stderr)
    c.match_exact = match_exact
    c.match_regexp = match_regexp
    c.case_sensitive = case_sensitive
    c = dict_to_Condition(d, c)
    applet_log.info("MAIN: restored command based condition %s" % c.cond_name)
    return c


# this version should use DBus and the screensaver manager to determine
# idle time, but it segfaults when the condition is verified
# class IdleTimeBasedCondition(Condition):
class IdleTimeBasedCondition(CommandBasedCondition):

    def _check_condition(self):
        if self.idle_reset:
            # if get_applet_idle_seconds() >= idle_secs:
            if CommandBasedCondition._check_condition(self):
                self.idle_reset = False
                return True
            else:
                self.idle_reset = True
                return False
        else:
            # if get_applet_idle_seconds() < idle_secs:
            if not CommandBasedCondition._check_condition(self):
                self.idle_reset = True
            return False

    def __init__(self, name, idle_secs, repeat=True, exec_sequence=True):
        self.idle_secs = idle_secs
        self.idle_reset = True
        command = """test $(xprintidle) -gt %s""" % (idle_secs * 1000)
        # Condition.__init__(self, name, repeat, exec_sequence)
        CommandBasedCondition.__init__(self, name, command, status=0, repeat=repeat, exec_sequence=exec_sequence)


def IdleTimeBasedCondition_to_dict(c):
    applet_log.info("MAIN: dump idle time based condition %s" % c.cond_name)
    d = Condition_to_dict(c)
    d['subtype'] = 'IdleTimeBasedCondition'
    d['idle_secs'] = c.idle_secs
    return d


def dict_to_IdleTimeBasedCondition(d):
    if d['type'] != 'condition' or d['subtype'] != 'IdleTimeBasedCondition':
        raise ValueError("incorrect dictionary type")
    name = d['cond_name']
    idle_secs = d['idle_secs']
    # TODO: if there are more parameters, use d.get('key', default_val)
    c = IdleTimeBasedCondition(name, idle_secs)
    c = dict_to_Condition(d, c)
    applet_log.info("MAIN: restored idle time based condition %s" % c.cond_name)
    return c


# current_system_event is for events that are not queued and are directly
# triggered by something external (eg. startup, shutdown, other signals),
# while current_deferred_events is a copy of the events that have been
# more gently enqueued by other triggers (eg. via DBus or services)
class EventBasedCondition(Condition):

    def _check_condition(self):
        self._debug("checking event based condition: %s" % self.event)
        if current_system_event and self.event == current_system_event:
            return True
        elif current_deferred_events and self.event in current_deferred_events:
            return True
        else:
            return False

    def __init__(self, name, event, no_skip=True, repeat=False, exec_sequence=True):
        self.event = event
        Condition.__init__(self, name, repeat, exec_sequence)
        if no_skip:
            self.skip_seconds = 0


def EventBasedCondition_to_dict(c):
    applet_log.info("MAIN: dump event based condition %s" % c.cond_name)
    d = Condition_to_dict(c)
    d['subtype'] = 'EventBasedCondition'
    d['event'] = c.event
    d['no_skip'] = bool(c.skip_seconds == 0)
    return d


def dict_to_EventBasedCondition(d):
    if d['type'] != 'condition' or d['subtype'] != 'EventBasedCondition':
        raise ValueError("incorrect dictionary type")
    name = d['cond_name']
    event = d['event']
    no_skip = d['no_skip']
    # TODO: if there are more parameters, use d.get('key', default_val)
    c = EventBasedCondition(name, event, no_skip)
    c = dict_to_Condition(d, c)
    applet_log.info("MAIN: restored event based condition %s" % c.cond_name)
    return c


#############################################################################
# dialog boxes and windows

# This dialog box is populated by loading a dummy task from a file, when the
# task is edited it goes directly to the loaded file (if existing or a new one
# otherwise) and is listed or replaced in the current task list.
class TaskDialog(object):

    def __init__(self):
        self.builder = Gtk.Builder().new_from_string(DIALOG_ADD_TASK, -1)
        self.builder.connect_signals(self)
        o = self.builder.get_object
        self.dialog = o('dlgAddTask')
        self.stored_tasks = tasks.names
        self.stored_tasks.sort()
        cb_tasks = o('cbTaskName')
        for x in self.stored_tasks:
            cb_tasks.append_text(x)
        l = o('listEnvVars')
        c = Gtk.TreeViewColumn(resources.LISTCOL_ENVVARS_NAME, Gtk.CellRendererText(), text=0)
        l.append_column(c)
        c = Gtk.TreeViewColumn(resources.LISTCOL_ENVVARS_VALUE, Gtk.CellRendererText(), text=1)
        l.append_column(c)

    def validate_int(self, s, min_value=None, max_value=None):
        try:
            n = int(s)
            if min_value is not None and n < min_value:
                return None
            elif max_value is not None and n > max_value:
                return None
            else:
                return n
        except ValueError as e:
            return None

    def click_btnVarAdd(self, _):
        o = self.builder.get_object
        name = o('txtVarName').get_text()
        value = o('txtVarValue').get_text()
        m = o('store_listEnvVars')
        li = []
        updated = False
        for row in m:
            if name == row[0]:
                updated = True
                li.append([name, value])
            else:
                li.append([row[0], row[1]])
        if not updated:
            li.append([name, value])
        li.sort()
        m.clear()
        for i in li:
            m.append(i)

    def click_listEnvVars(self, selected):
        o = self.builder.get_object
        m, i = selected.get_selected()
        if i is not None:
            o('txtVarName').set_text(m[i][0])
            o('txtVarValue').set_text(m[i][1])

    def click_btnVarRemove(self, _):
        o = self.builder.get_object
        name = o('txtVarName').get_text()
        m = o('store_listEnvVars')
        li = []
        for row in m:
            if name != row[0]:
                li.append([row[0], row[1]])
        li.sort()
        m.clear()
        for i in li:
            m.append(i)

    def click_btnChooseDir(self, _):
        o = self.builder.get_object
        curpath = o('txtFolder').get_text()
        dirdlg = Gtk.FileChooserDialog(title="Choose Directory",
                                       action=Gtk.FileChooserAction.SELECT_FOLDER,
                                       buttons=(Gtk.STOCK_CANCEL,
                                                Gtk.ResponseType.CANCEL,
                                                Gtk.STOCK_OK,
                                                Gtk.ResponseType.OK))
        if os.path.exists(curpath) and os.path.isdir(curpath):
            dirdlg.set_filename(curpath)
        ret = dirdlg.run()
        dirdlg.hide()
        if ret == Gtk.ResponseType.OK:
            o('txtFolder').set_text(dirdlg.get_filename())

    def choose_task(self, box):
        o = self.builder.get_object
        name = box.get_active_text()
        self.default_box()
        if name in self.stored_tasks:
            task = tasks.get(task_name=name)
            o('txtVarName').set_text('')
            o('txtVarValue').set_text('')
            o('txtCommand').set_text(task.command)
            o('txtFolder').set_text(task.startup_dir)
            if task.success_status is not None:
                o('rdSuccess').set_active(True)
                o('cbCheckWhat').set_active(0)
                o('txtCheckValue').set_text(str(task.success_status))
            elif task.success_stdout is not None:
                o('rdSuccess').set_active(True)
                o('cbCheckWhat').set_active(1)
                o('txtCheckValue').set_text(task.success_stdout)
            elif task.success_stderr is not None:
                o('rdSuccess').set_active(True)
                o('cbCheckWhat').set_active(2)
                o('txtCheckValue').set_text(task.success_stderr)
            elif task.failure_status is not None:
                o('rdFailure').set_active(True)
                o('cbCheckWhat').set_active(0)
                o('txtCheckValue').set_text(str(task.failure_status))
            elif task.failure_stdout is not None:
                o('rdFailure').set_active(True)
                o('cbCheckWhat').set_active(1)
                o('txtCheckValue').set_text(task.failure_stdout)
            elif task.failure_stderr is not None:
                o('rdFailure').set_active(True)
                o('cbCheckWhat').set_active(2)
                o('txtCheckValue').set_text(task.failure_stderr)
            else:
                o('rdNoCheck').set_active(True)
                o('txtCheckValue').set_text("0")
            o('chkExactMatch').set_active(task.match_exact)
            o('chkRegExp').set_active(task.match_regexp)
            o('chkCaseSensitive').set_active(task.case_sensitive)
            o('chkImportEnvironment').set_active(task.include_env)
            m = o('store_listEnvVars')
            li = []
            e = task.environment_vars
            for k in e.keys():
                li.append([k, e[k]])
            li.sort()
            m.clear()
            for i in li:
                m.append(i)

    def change_txtName(self, _):
        o = self.builder.get_object
        name = o('txtName').get_text()
        if VALIDATE_TASK_RE.match(name):
            o('buttonOK').set_sensitive(True)
            if name in self.stored_tasks:
                o('btnDelete').set_sensitive(True)
            else:
                o('btnDelete').set_sensitive(False)
        else:
            o('buttonOK').set_sensitive(False)
            o('btnDelete').set_sensitive(False)

    def change_cbCheckWhat(self, _):
        o = self.builder.get_object
        if o('cbCheckWhat').get_active() == 0:
            o('chkExactMatch').set_sensitive(False)
            o('chkCaseSensitive').set_sensitive(False)
            o('chkRegExp').set_sensitive(False)
        else:
            o('chkExactMatch').set_sensitive(True)
            o('chkCaseSensitive').set_sensitive(True)
            o('chkRegExp').set_sensitive(True)

    def default_box(self, include_name=False):
        o = self.builder.get_object
        if include_name:
            o('txtName').set_text('')
        o('txtVarName').set_text('')
        o('txtVarValue').set_text('')
        o('txtCommand').set_text('')
        o('txtFolder').set_text('')
        o('chkExactMatch').set_active(False)
        o('chkCaseSensitive').set_active(False)
        o('chkRegExp').set_active(False)
        o('chkImportEnvironment').set_active(True)
        o('rdSuccess').set_active(True)
        o('cbCheckWhat').set_active(0)
        o('txtCheckValue').set_text("0")
        o('store_listEnvVars').clear()

    def run(self):
        o = self.builder.get_object
        self.stored_tasks = tasks.names
        self.stored_tasks.sort()
        cb_tasks = o('cbTaskName')
        cb_tasks.get_model().clear()
        for x in self.stored_tasks:
            cb_tasks.append_text(x)
        self.dialog.set_keep_above(True)
        self.dialog.present()
        self.change_txtName(None)
        self.change_cbCheckWhat(None)
        ret = self.dialog.run()
        self.dialog.hide()
        self.dialog.set_keep_above(False)
        if ret == ACTION_OK:
            name = o('txtName').get_text()
            command = o('txtCommand').get_text()
            startup_dir = o('txtFolder').get_text()
            task = Task(name, command, startup_dir)
            idx = o('cbCheckWhat').get_active()
            if o('rdSuccess').get_active():
                if idx == 0:
                    s = self.validate_int(o('txtCheckValue').get_text(), 0)
                    if s is None:
                        msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                                   buttons=Gtk.ButtonsType.OK)
                        msgbox.set_markup(resources.DLG_WRONG_EXIT_STATUS)
                        msgbox.run()
                        msgbox.hide()
                        applet_log.warning("DLGTASK: wrong exit code specification")
                        s = 0
                    task.set_check(success_status=s)
                elif idx == 1:
                    s = str(o('txtCheckValue').get_text())
                    task.set_check(success_stdout=s)
                elif idx == 2:
                    s = str(o('txtCheckValue').get_text())
                    task.set_check(success_stderr=s)
            elif o('rdFailure').get_active():
                if idx == 0:
                    s = self.validate_int(o('txtCheckValue').get_text(), 0)
                    if s is None:
                        msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                                   buttons=Gtk.ButtonsType.OK)
                        msgbox.set_markup(resources.DLG_WRONG_EXIT_STATUS)
                        msgbox.run()
                        msgbox.hide()
                        s = 0
                    task.set_check(failure_status=s)
                elif idx == 1:
                    s = str(o('txtCheckValue').get_text())
                    task.set_check(failure_stdout=s)
                elif idx == 2:
                    s = str(o('txtCheckValue').get_text())
                    task.set_check(failure_stderr=s)
            m = o('store_listEnvVars')
            for row in m:
                task.set_env(row[0], row[1])
            task.match_exact = o('chkExactMatch').get_active()
            task.match_regexp = o('chkRegExp').get_active()
            task.case_sensitive = o('chkCaseSensitive').get_active()
            task.include_env = o('chkImportEnvironment').get_active()
            task.dump()
            tasks.add(task)
            tasks.save()
            self.stored_tasks = tasks.names
            self.stored_tasks.sort()
            cb_tasks = o('cbTaskName')
            cb_tasks.get_model().clear()
            for x in self.stored_tasks:
                cb_tasks.append_text(x)
            return task
        elif ret == ACTION_DELETE:
            name = o('txtName').get_text()
            task = tasks.get(task_name=name)
            if task:
                msgbox = Gtk.MessageDialog(type=Gtk.MessageType.QUESTION,
                                           buttons=Gtk.ButtonsType.YES_NO)
                msgbox.set_markup(resources.DLG_CONFIRM_DELETE_TASK % name)
                ret = msgbox.run()
                msgbox.hide()
                if ret == Gtk.ResponseType.YES:
                    self.default_box(True)
                    if tasks.remove(task_name=name):
                        tasks.save()
                        cb_tasks.get_model().clear()
                        for x in self.stored_tasks:
                            cb_tasks.append_text(x)
                    else:
                        applet_log.error("DLGTASK: task %s could not be deleted" % name)
                        msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                                   buttons=Gtk.ButtonsType.OK)
                        msgbox.set_markup(resources.DLG_CANNOT_DELETE_TASK % name)
                        msgbox.run()
                        msgbox.hide()
                else:
                    applet_log.info("DLGTASK: removal of task %s canceled" % name)
            else:
                applet_log.error("DLGTASK: task %s not found" % name)
                msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.OK)
                msgbox.set_markup(resources.DLG_CANNOT_FIND_TASK % name)
                msgbox.run()
                msgbox.hide()
            return None
        else:
            return None


# This dialog box is populated by loading a dummy condition from a file, with
# a mechanism similar to the above one. The main difference is that this box
# should contain a dynamic part, since there are different condition types.
class ConditionDialog(object):

    def __init__(self):
        self.builder = Gtk.Builder().new_from_string(DIALOG_ADD_CONDITION, -1)
        self.builder.connect_signals(self)
        o = self.builder.get_object
        self.dialog = o('dlgAddCondition')
        self.stored_conditions = conditions.names
        self.stored_conditions.sort()
        cb_conds = o('cbConditionName')
        cb_conds.get_model().clear()
        for x in self.stored_conditions:
            cb_conds.append_text(x)
        self.stored_tasks = tasks.names
        self.stored_tasks.sort()
        cb_tasks = o('cbAddTask')
        cb_tasks.get_model().clear()
        for x in self.stored_tasks:
            cb_tasks.append_text(x)
        self.update_box_type(None)
        l = o('listTasks')
        c = Gtk.TreeViewColumn(resources.LISTCOL_TASKS_NAME, Gtk.CellRendererText(), text=0)
        l.append_column(c)
        self.all_events = [
            EVENT_APPLET_STARTUP,
            EVENT_APPLET_SHUTDOWN,
            EVENT_SYSTEM_SUSPEND,
            EVENT_SYSTEM_RESUME,
            EVENT_SYSTEM_DEVICE_ATTACH,
            EVENT_SYSTEM_DEVICE_DETACH,
            EVENT_SYSTEM_NETWORK_JOIN,
            EVENT_SYSTEM_NETWORK_LEAVE,
            EVENT_SESSION_SCREENSAVER,
            EVENT_SESSION_SCREENSAVER_EXIT,
            EVENT_SESSION_LOCK,
            EVENT_SESSION_UNLOCK,
        ]
        if applet_enabled_events:
            enabled_events = applet_enabled_events
        else:
            enabled_events = []
        i = 0
        li = []
        model = o('cbSysEvent').get_model()
        for row in model:
            r, v = row[0], row[1]
            if self.all_events[i] not in enabled_events:
                r = "%s %s" % (r, resources.DLG_ITEM_DISABLED)
            li.append([r, v])
            i += 1
        model.clear()
        for x in li:
            model.append(x)
        o('cbSysEvent').set_model(model)

    def validate_int(self, s, min_value=None, max_value=None):
        try:
            n = int(s)
            if min_value is not None and n < min_value:
                return None
            elif max_value is not None and n > max_value:
                return None
            else:
                return n
        except ValueError as e:
            return None

    def change_txtName(self, _):
        o = self.builder.get_object
        name = o('txtName').get_text()
        if VALIDATE_CONDITION_RE.match(name):
            o('buttonOK').set_sensitive(True)
            if name in self.stored_conditions:
                o('btnDelete').set_sensitive(True)
            else:
                o('btnDelete').set_sensitive(False)
        else:
            o('buttonOK').set_sensitive(False)
            o('btnDelete').set_sensitive(False)

    def change_cbCheckWhat(self, _):
        o = self.builder.get_object
        if o('cbCheckWhat').get_active() == 0:
            o('chkExactMatch').set_sensitive(False)
            o('chkCaseSensitive').set_sensitive(False)
            o('chkRegExp').set_sensitive(False)
        else:
            o('chkExactMatch').set_sensitive(True)
            o('chkCaseSensitive').set_sensitive(True)
            o('chkRegExp').set_sensitive(True)

    def change_cbTimeUnit(self, _):
        o = self.builder.get_object
        if o('cbTimeUnit').get_active() == 0:
            l = UI_INTERVALS_HOURS
        else:
            l = UI_INTERVALS_MINUTES
        cb = o('cbInterval')
        cb.get_model().clear()
        for x in l:
            cb.append_text(str(x))

    def default_box(self, include_name=False):
        o = self.builder.get_object
        if include_name:
            o('txtName').set_text('')
        o('txtInterval').set_text('')
        o('txtYear').set_text('')
        o('cbMonth').set_active(-1)
        o('txtDay').set_text('')
        o('cbWeekday').set_active(-1)
        o('txtHour').set_text('')
        o('txtMinute').set_text('')
        o('txtCommand').set_text('')
        o('txtCheckValue').set_text('0')
        o('txtIdleMins').set_text('')
        o('cbType').set_active(2)
        o('cbTimeUnit').set_active(1)
        o('cbSysEvent').set_active(1)
        o('cbAddTask').set_active(-1)
        o('cbCheckWhat').set_active(0)
        o('chkRepeat').set_active(True)
        o('chkSequence').set_active(True)
        o('cbBreakSequence').set_active(0)
        o('chkExactMatch').set_active(False)
        o('chkCaseSensitive').set_active(False)
        o('chkRegExp').set_active(False)
        o('chkSuspend').set_active(False)
        o('store_listTasks').clear()
        cb = o('cbInterval')
        cb.get_model().clear()
        for x in UI_INTERVALS_MINUTES:
            cb.append_text(str(x))

    def choose_condition(self, box):
        o = self.builder.get_object
        name = box.get_active_text()
        self.default_box()
        if name in self.stored_conditions:
            cond = conditions.get(cond_name=name)
            self.update_box_type(None)
            if type(cond) == IntervalBasedCondition:
                intv = cond.interval / 60
                if intv % 60 == 0:
                    o('cbTimeUnit').set_active(0)
                    intv /= 60
                o('txtInterval').set_text(str(int(intv)))
                o('cbType').set_active(0)
            elif type(cond) == TimeBasedCondition:
                if cond.year:
                    o('txtYear').set_text(str(cond.year))
                if cond.month:
                    o('cbMonth').set_active_item(cond.month - 1)
                if cond.day:
                    o('txtDay').set_text(str(cond.day))
                if cond.weekday:
                    o('cbWeekday').set_active_item(cond.weekday)
                if cond.hour:
                    o('txtHour').set_text(str(cond.hour))
                if cond.minute:
                    o('txtMinute').set_text(str(cond.minute))
                o('cbType').set_active(1)
            elif type(cond) == CommandBasedCondition:
                o('txtCommand').set_text(cond.command)
                o('chkExactMatch').set_active(cond.match_exact)
                o('chkCaseSensitive').set_active(cond.case_sensitive)
                o('chkRegExp').set_active(cond.match_regexp)
                if cond.expected_status is not None:
                    o('cbCheckWhat').set_active(0)
                    o('txtCheckValue').set_text(str(cond.expected_status))
                elif cond.expected_stdout is not None:
                    o('cbCheckWhat').set_active(1)
                    o('txtCheckValue').set_text(cond.expected_stdout)
                elif cond.expected_stderr is not None:
                    o('cbCheckWhat').set_active(2)
                    o('txtCheckValue').set_text(cond.expected_stderr)
                o('cbType').set_active(2)
            elif type(cond) == IdleTimeBasedCondition:
                o('txtIdleMins').set_text(str(int(cond.idle_secs / 60)))
                o('cbType').set_active(3)
            elif type(cond) == EventBasedCondition:
                evt = cond.event
                if evt in self.all_events:
                    o('cbSysEvent').set_active(self.all_events.index(evt))
                else:
                    o('cbSysEvent').set_active(-1)
                o('cbType').set_active(4)
            else:
                o('cbType').set_active(-1)
                pass
            o('chkRepeat').set_active(cond.repeat)
            o('chkSequence').set_active(cond.exec_sequence)
            o('chkSuspend').set_active(cond.suspended)
            if cond.break_failure:
                o('cbBreakSequence').set_active(1)
            elif cond.break_success:
                o('cbBreakSequence').set_active(2)
            else:
                o('cbBreakSequence').set_active(0)
            o('cbBreakSequence').set_sensitive(cond.exec_sequence)
            m = o('store_listTasks')
            for x in cond.task_names:
                m.append([x])

    def click_btnRemoveTask(self, _):
        o = self.builder.get_object
        task_name = o('cbAddTask').get_active_text()
        if task_name:
            li = []
            m = o('store_listTasks')
            for row in m:
                r = row[0]
                if r != task_name:
                    li.append(r)
            m.clear()
            for x in li:
                m.append([x])

    def click_btnAddTask(self, _):
        o = self.builder.get_object
        task_name = o('cbAddTask').get_active_text()
        if task_name:
            li = []
            m = o('store_listTasks')
            for row in m:
                r = row[0]
                if r != task_name:
                    li.append(r)
            li.append(task_name)
            m.clear()
            for x in li:
                m.append([x])

    def click_listTasks(self, selected):
        o = self.builder.get_object
        m = o('cbAddTask').get_model()
        m1, i = selected.get_selected()
        if i is not None:
            idx = 0
            for row in m:
                if row[0] == m1[i][0]:
                    o('cbAddTask').set_active(idx)
                    break
                else:
                    idx += 1

    def click_chkSequence(self, _):
        o = self.builder.get_object
        o('cbBreakSequence').set_sensitive(o('chkSequence').get_active())

    def update_box_type(self, _):
        o = self.builder.get_object
        idx = o('cbType').get_active()
        widgets = [
            'canvasOptions_Interval',
            'canvasOptions_Time',
            'canvasOptions_Command',
            'canvasOptions_IdleTime',
            'canvasOptions_SysEvent',
            'canvasOptions_Empty',
        ]
        can_disable = [
            'chkRepeat',
            'chkSequence',
            'cbBreakSequence',
            'chkSuspend',
        ]
        to_disable = []
        if idx == 0:
            current_widget = 'canvasOptions_Interval'
        elif idx == 1:
            current_widget = 'canvasOptions_Time'
        elif idx == 2:
            current_widget = 'canvasOptions_Command'
        elif idx == 3:
            current_widget = 'canvasOptions_IdleTime'
        elif idx == 4:
            current_widget = 'canvasOptions_SysEvent'
            to_disable = ['chkRepeat']
        else:
            current_widget = 'canvasOptions_Empty'
        for w in widgets:
            if w == current_widget:
                o(w).show()
            else:
                o(w).hide()
        for w in can_disable:
            if w in to_disable:
                o(w).set_sensitive(False)
            else:
                o(w).set_sensitive(True)

    def run(self):
        self.default_box()
        o = self.builder.get_object
        self.stored_conditions = conditions.names
        self.stored_conditions.sort()
        cb_conds = o('cbConditionName')
        cb_conds.get_model().clear()
        for x in self.stored_conditions:
            cb_conds.append_text(x)
        self.stored_tasks = tasks.names
        self.stored_tasks.sort()
        cb_tasks = o('cbAddTask')
        cb_tasks.get_model().clear()
        for x in self.stored_tasks:
            cb_tasks.append_text(x)
        self.dialog.set_keep_above(True)
        self.dialog.present()
        self.change_txtName(None)
        self.change_cbCheckWhat(None)
        ret = self.dialog.run()
        self.dialog.hide()
        self.dialog.set_keep_above(False)
        if ret == ACTION_OK:
            name = o('txtName').get_text()
            repeat = o('chkRepeat').get_active()
            sequence = o('chkSequence').get_active()
            suspend = o('chkSuspend').get_active()
            break_sequence = o('cbBreakSequence').get_active()
            break_success = False
            break_failure = False
            if break_sequence == 1:
                break_failure = True
            elif break_sequence == 2:
                break_success = True
            task_names = []
            m = o('store_listTasks')
            for row in m:
                task_names.append(row[0])
            idx = o('cbType').get_active()
            if idx == 0:
                v = int(o('txtInterval').get_text())
                if o('cbTimeUnit').get_active == 0:
                    interval = v * 60 * 60
                else:
                    interval = v * 60
                c = IntervalBasedCondition(name, interval, repeat, sequence)
                c.break_failure = break_failure
                c.break_success = break_success
            elif idx == 1:
                now = time.localtime()
                current_year = now.tm_year
                timedict = {
                    'year': self.validate_int(o('txtYear').get_text(), min_value=current_year),
                    'month': self.validate_int(o('cbMonth').get_active() + 1, min_value=1, max_value=12),
                    'day': self.validate_int(o('txtDay').get_text(), min_value=1, max_value=31),
                    'weekday': self.validate_int(o('cbWeekday').get_active(), min_value=0, max_value=6),
                    'hour': self.validate_int(o('txtHour').get_text(), min_value=0, max_value=24),
                    'minute': self.validate_int(o('txtMinute').get_text(), min_value=0, max_value=59),
                }
                c = TimeBasedCondition(name, timedict, repeat, sequence)
                c.break_failure = break_failure
                c.break_success = break_success
            elif idx == 2:
                command = o('txtCommand').get_text()
                status, stdout, stderr = None, None, None
                chk = o('cbCheckWhat').get_active()
                if chk == 0:
                    status = self.validate_int(o('txtCheckValue').get_text(), 0)
                    if status is None:
                        msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                                   buttons=Gtk.ButtonsType.OK)
                        msgbox.set_markup(resources.DLG_WRONG_EXIT_STATUS)
                        msgbox.run()
                        msgbox.hide()
                        applet_log.warning("DLGCOND: wrong exit code specification")
                        status = 0
                elif chk == 1:
                    stdout = str(o('txtCheckValue').get_text())
                elif chk == 2:
                    stderr = str(o('txtCheckValue').get_text())
                c = CommandBasedCondition(name, command, status, stdout, stderr, repeat, sequence)
                c.break_failure = break_failure
                c.break_success = break_success
                c.match_exact = o('chkExactMatch').get_active()
                c.case_sensitive = o('chkCaseSensitive').get_active()
                c.match_regexp = o('chkRegExp').get_active()
            elif idx == 3:
                idle_secs = int(o('txtIdleMins').get_text()) * 60
                c = IdleTimeBasedCondition(name, idle_secs, repeat, sequence)
                c.break_failure = break_failure
                c.break_success = break_success
            elif idx == 4:
                event_type = self.all_events[o('cbSysEvent').get_active()]
                c = EventBasedCondition(name, event_type, True, repeat, sequence)
                c.break_failure = break_failure
                c.break_success = break_success
            for x in task_names:
                if tasks.get(task_name=x):
                    c.add_task(x)
            if suspend:
                c.suspend()
            else:
                c.resume()
            c.dump()
            conditions.add(c)
            conditions.save()
            self.stored_conditions = conditions.names
            self.stored_conditions.sort()
            cb_conds = o('cbConditionName')
            cb_conds.get_model().clear()
            for x in self.stored_conditions:
                cb_conds.append_text(x)
            return c
        elif ret == ACTION_DELETE:
            name = o('txtName').get_text()
            cond = conditions.get(cond_name=name)
            if cond:
                msgbox = Gtk.MessageDialog(type=Gtk.MessageType.QUESTION,
                                           buttons=Gtk.ButtonsType.YES_NO)
                msgbox.set_markup(resources.DLG_CONFIRM_DELETE_CONDITION % name)
                ret = msgbox.run()
                msgbox.hide()
                if ret == Gtk.ResponseType.YES:
                    self.default_box(True)
                    if conditions.remove(cond_name=name):
                        conditions.save()
                        cb_conds.get_model().clear()
                        for x in self.stored_conditions:
                            cb_conds.append_text(x)
                    else:
                        applet_log.error("DLGCOND: condition %s could not be deleted" % name)
                        msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                                   buttons=Gtk.ButtonsType.OK)
                        msgbox.set_markup(resources.DLG_CANNOT_DELETE_CONDITION % name)
                        msgbox.run()
                        msgbox.hide()
                else:
                    applet_log.debug("DLGCOND: removal of condition %s canceled")
            else:
                applet_log.error("DLGCOND: condition %s not found" % name)
                msgbox = Gtk.MessageDialog(type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.OK)
                msgbox.set_markup(resources.DLG_CANNOT_FIND_CONDITION % name)
                msgbox.run()
                msgbox.hide()
            return None
        else:
            return None


# settings dialog
class SettingsDialog(object):

    def __init__(self):
        self.builder = Gtk.Builder().new_from_string(DIALOG_SETTINGS, -1)
        self.builder.connect_signals(self)
        o = self.builder.get_object
        self.dialog = o('dlgSettings')
        self.log_levels = [
            'critical',
            'error',
            'warning',
            'info',
            'debug',
        ]
        self.icon_themes = [
            'light',
            'dark',
            'color',
            'guess',
        ]

    def default_box(self):
        o = self.builder.get_object
        log_level_idx = self.log_levels.index(config.get('General', 'log level'))
        icon_theme_idx = self.icon_themes.index(config.get('General', 'icon theme'))
        o('chkShowIcon').set_active(config.get('General', 'show icon'))
        o('chkAutostart').set_active(config.get('General', 'autostart'))
        o('chkNotifications').set_active(config.get('General', 'notifications'))
        o('chkPreservePause').set_active(config.get('Scheduler', 'preserve pause'))
        o('cbLogLevel').set_active(log_level_idx)
        o('cbIconTheme').set_active(icon_theme_idx)
        o('txtTickSeconds').set_text(str(config.get('Scheduler', 'tick seconds')))
        o('txtSkipSeconds').set_text(str(config.get('Scheduler', 'skip seconds')))
        o('txtMaxThreads').set_text(str(config.get('Concurrency', 'max threads')))
        o('txtMaxLogSize').set_text(str(config.get('History', 'log size')))
        o('txtMaxLogBackups').set_text(str(config.get('History', 'log backups')))
        o('txtMaxHistoryItems').set_text(str(config.get('History', 'max items')))

    def run(self):
        self.default_box()
        self.dialog.set_keep_above(True)
        self.dialog.present()
        ret = self.dialog.run()
        self.dialog.hide()
        self.dialog.set_keep_above(False)
        o = self.builder.get_object
        if ret == ACTION_OK:
            config_skip = []
            v = o('cbLogLevel').get_active()
            if v >= 0:
                config.set('General', 'log level', self.log_levels[v])
            else:
                config_skip.append('log level')
            config.set('General', 'icon theme', self.icon_themes[o('cbIconTheme').get_active()])
            config.set('General', 'show icon', o('chkShowIcon').get_active())
            config.set('General', 'autostart', o('chkAutostart').get_active())
            config.set('General', 'notifications', o('chkNotifications').get_active())
            preserve_pause = o('chkPreservePause').get_active()
            config.set('Scheduler', 'preserve pause', preserve_pause)
            if not preserve_pause:
                unlink_pause_file()
            try:
                t = o('txtTickSeconds').get_text()
                v = int(t)
                if v > 0:
                    config.set('Scheduler', 'tick seconds', v)
                else:
                    config_skip.append('tick seconds')
            except ValueError:
                config_skip.append('tick seconds')
            try:
                t = o('txtSkipSeconds').get_text()
                v = int(t)
                if v >= config.get('Scheduler', 'tick seconds'):
                    config.set('Scheduler', 'skip seconds', v)
                else:
                    config_skip.append('skip seconds')
            except ValueError:
                config_skip.append('skip seconds')
            try:
                t = o('txtMaxThreads').get_text()
                v = int(t)
                if v > 1:
                    config.set('Concurrency', 'max threads', v)
                else:
                    config_skip.append('max threads')
            except ValueError:
                config_skip.append('max threads')
            try:
                t = o('txtMaxLogSize').get_text()
                v = int(t)
                if v > 0:
                    config.set('History', 'log size', v)
                else:
                    config_skip.append('log size')
            except ValueError:
                config_skip.append('log size')
            try:
                t = o('txtMaxLogBackups').get_text()
                v = int(t)
                if v > 0:
                    config.set('History', 'log backups', v)
                else:
                    config_skip.append('log backups')
            except ValueError:
                config_skip.append('log backups')
            try:
                t = o('txtMaxHistoryItems').get_text()
                v = int(t)
                if v > 0:
                    config.set('History', 'max items', v)
                else:
                    config_skip.append('max items')
            except ValueError:
                config_skip.append('max items')
            if config_skip:
                applet_log.info("DLGCONF: values for %s invalid: skipped" % ", ".join(config_skip))
            applet_log.info("DLGCONF: saving user configuration")
            config.save()
            # reconfigure running applet as much as possible
            applet_log.info("DLGCONF: reconfiguring application")
            config_loghandler()
            config_loglevel()
            periodic.restart(new_interval=config.get('Scheduler', 'tick seconds'))
            history.resize()
            create_autostart_file()
            # FIXME: find applet even when called by a second instance
            if applet:
                applet.hide_icon(not config.get('General', 'show icon'))


class HistoryDialog(object):

    def __init__(self):
        self.builder = Gtk.Builder().new_from_string(DIALOG_TASK_HISTORY, -1)
        self.builder.connect_signals(self)
        o = self.builder.get_object
        self.dialog = o('dlgHistory')
        self.image_success = Gtk.Image.new_from_file(os.path.join(APP_ICON_FOLDER, 'emblems', 'success.png'))
        self.image_failure = Gtk.Image.new_from_file(os.path.join(APP_ICON_FOLDER, 'emblems', 'failure.png'))
        l = o('listHistory')
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_TIMESTAMP, renderer, text=0)
        l.append_column(c)
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_TASK, renderer, text=1)
        c.props.expand = True
        l.append_column(c)
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_TRIGGER, renderer, text=2)
        c.props.expand = True
        l.append_column(c)
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_EXITCODE, renderer, text=3)
        l.append_column(c)
        renderer = Gtk.CellRendererPixbuf()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_SUCCESS, renderer, pixbuf=4)
        l.append_column(c)
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_REASON, renderer, text=5)
        l.append_column(c)
        renderer = Gtk.CellRendererText()
        c = Gtk.TreeViewColumn(resources.LISTCOL_HISTORY_ROWID, renderer, text=6)
        c.set_visible(False)
        l.append_column(c)
        o('txtStdOut').modify_font(Pango.FontDescription("Monospace"))
        o('txtStdErr').modify_font(Pango.FontDescription("Monospace"))

    def choose_item(self, selected):
        o = self.builder.get_object
        m, i = selected.get_selected()
        if i is not None:
            item_id = m[i][6]
            item = history.item_by_id(item_id)
            if item:
                if item.stdout:
                    o('txtStdOut').get_buffer().set_text(item.stdout)
                else:
                    o('txtStdOut').get_buffer().set_text("")
                if item.stderr:
                    o('txtStdErr').get_buffer().set_text(item.stderr)
                else:
                    o('txtStdErr').get_buffer().set_text("")
            else:
                o('txtStdOut').get_buffer().set_text("")
                o('txtStdErr').get_buffer().set_text("")

    def update_list(self):
        o = self.builder.get_object
        rows = []
        for i in history.items():
            s_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(i.startup_time))
            emblem = self.image_success if i.success else self.image_failure
            row = [
                '%s / %.2f' % (s_time, i.run_time),
                i.task_name,
                i.trigger_cond if i.trigger_cond is not None else '',
                str(i.exit_code) if i.exit_code is not None else '',
                emblem.get_pixbuf(),
                i.failure_reason if i.failure_reason is not None else '',
                i.item_id,
            ]
            rows.insert(0, row)
        m = o('store_listHistory')
        m.clear()
        for row in rows:
            m.append(row)

    def click_btnReload(self, _):
        self.update_list()

    def run(self):
        self.update_list()
        self.dialog.set_keep_above(True)
        self.dialog.present()
        ret = self.dialog.run()
        self.dialog.hide()
        self.dialog.set_keep_above(False)


class AboutDialog(object):

    def __init__(self):
        self.image_logo = Gtk.Image.new_from_file(os.path.join(APP_ICON_FOLDER, 'alarmclock-128.png'))
        self.builder = Gtk.Builder().new_from_string(DIALOG_ABOUT, -1)
        self.builder.connect_signals(self)
        o = self.builder.get_object
        self.dialog = o('dlgAbout')
        self.dialog.set_logo(self.image_logo.get_pixbuf())
        self.dialog.set_version(resources.DLG_ABOUT_VERSION_STRING % APPLET_VERSION)
        self.dialog.set_icon_from_file(os.path.join(APP_ICON_FOLDER, 'alarmclock.png'))

    def run(self):
        self.dialog.set_keep_above(True)
        self.dialog.present()
        ret = self.dialog.run()
        self.dialog.hide()
        self.dialog.set_keep_above(False)


#############################################################################
# Applet

# program startup
class AppletIndicator(Gtk.Application):

    def __init__(self):
        Gtk.Application.__init__(self,
                                 application_id=APPLET_ID,
                                 flags=Gio.ApplicationFlags.FLAGS_NONE)
        try:
            self.register(None)
        except Exception as e:
            applet_log.critical("MAIN: exception %s registering application" % e)
            sys.exit(2)

        self.connect("activate", self.applet_activate)
        self.leaving = False

        # The system and session bus signals give many possibilities for events
        self.system_bus = dbus.SystemBus()
        self.session_bus = dbus.SessionBus()
        self.applet_bus = AppletDBusService()

        enabled_events = [EVENT_APPLET_STARTUP]
        try:
            self.login_mgr = dbus.Interface(
                self.system_bus.get_object('org.freedesktop.login1', '/org/freedesktop/login1'),
                'org.freedesktop.login1.Manager')
            self.login_mgr.connect_to_signal('PrepareForShutdown', self.before_shutdown)
            self.login_mgr.connect_to_signal('PrepareForSleep', self.system_sleep_manager)
            enabled_events.append(EVENT_APPLET_SHUTDOWN)
            enabled_events.append(EVENT_SYSTEM_SUSPEND)
            enabled_events.append(EVENT_SYSTEM_RESUME)
        except dbus.exceptions.DBusException:
            applet_log.error("MAIN: error registering logout manager")
            self.login_mgr = None

        # DBus events
        try:
            self.screensaver_mgr = dbus.Interface(
                self.session_bus.get_object('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver'),
                'org.gnome.ScreenSaver')
            self.screensaver_mgr.connect_to_signal('ActiveChanged', self.screensaver_manager)
            enabled_events.append(EVENT_SESSION_SCREENSAVER)
            enabled_events.append(EVENT_SESSION_SCREENSAVER_EXIT)
        except dbus.exceptions.DBusException:
            applet_log.error("MAIN: error registering screensaver manager")
            self.screensaver_mgr = None

        try:
            # This feature should possibly be deactivated, unless the applet
            # really has to be supported on Ubuntu only
            #
            # raise dbus.exceptions.DBusException
            # self.lock_mgr = dbus.Interface(
            #     self.system_bus.get_object('org.freedesktop.login1', '/org/freedesktop/login1'),
            #     'org.freedesktop.login1.Session')
            # self.lock_mgr.connect_to_signal('Lock', self.session_login_lock)
            # self.lock_mgr.connect_to_signal('Unlock', self.session_login_unlock)
            # enabled_events.append(EVENT_SESSION_LOCK)
            # enabled_events.append(EVENT_SESSION_UNLOCK)
            self.lock_mgr = dbus.Interface(
                self.session_bus.get_object('com.ubuntu.Upstart', '/com/ubuntu/Upstart'),
                'com.ubuntu.Upstart0_6')
            self.lock_mgr.connect_to_signal('EventEmitted', self.upstart_lock_manager)
            enabled_events.append(EVENT_SESSION_LOCK)
            enabled_events.append(EVENT_SESSION_UNLOCK)
        except dbus.exceptions.DBusException:
            applet_log.error("MAIN: error registering session lock manager")
            self.lock_mgr = None

        try:
            self.storage_mgr = dbus.Interface(
                self.system_bus.get_object('org.freedesktop.UDisks2', '/org/freedesktop/UDisks2'),
                'org.freedesktop.DBus.ObjectManager')
            devices = self.storage_mgr.GetManagedObjects().keys()
            drives = filter(lambda x: x.startswith('/org/freedesktop/UDisks2/block_devices/'), devices)
            self.storage_mgr_num_devices = len(list(drives))
            self.storage_mgr.connect_to_signal('InterfacesAdded', self.storage_device_manager)
            self.storage_mgr.connect_to_signal('InterfacesRemoved', self.storage_device_manager)
            enabled_events.append(EVENT_SYSTEM_DEVICE_ATTACH)
            enabled_events.append(EVENT_SYSTEM_DEVICE_DETACH)
        except dbus.exceptions.DBusException:
            applet_log.error("MAIN: error registering storage device manager")
            self.storage_mgr = None

        try:
            self.network_mgr = dbus.Interface(
                self.system_bus.get_object('org.freedesktop.NetworkManager', '/org/freedesktop/NetworkManager'),
                'org.freedesktop.NetworkManager')
            self.network_mgr.connect_to_signal('StateChanged', self.network_manager)
            enabled_events.append(EVENT_SYSTEM_NETWORK_JOIN)
            enabled_events.append(EVENT_SYSTEM_NETWORK_LEAVE)
        except dbus.exceptions.DBusException:
            applet_log.error("MAIN: error registering network connection manager")
            self.network_mgr = None

        set_applet_enabled_events(enabled_events)

        # now we can build dialog boxes since all necessary data is present
        self.dialog_add_task = TaskDialog()
        self.dialog_add_condition = ConditionDialog()
        self.dialog_about = AboutDialog()
        self.dialog_settings = SettingsDialog()
        self.dialog_history = HistoryDialog()

    def applet_activate(self, applet_instance):
        self.main()

    def main(self):
        # GUI management
        settings = Gtk.Settings.get_default()
        icon_suffix = config.get('General', 'icon theme').lower()
        if icon_suffix not in ('dark', 'light', 'color'):
            theme_name = settings.get_property('gtk-icon-theme-name').lower()
            if 'dark' in theme_name:
                icon_suffix = 'dark'
            elif 'light' in theme_name:
                icon_suffix = 'light'
            else:
                icon_suffix = 'color'
        self.indicator = AppIndicator.Indicator.new(APPLET_NAME, 'alarm', AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
        self.indicator.set_icon_theme_path(os.path.join(APP_ICON_FOLDER, icon_suffix))
        if config.get('General', 'show icon'):
            self.indicator.set_icon('alarm')
            self.indicator.set_attention_icon('warning')
            if config.get('General', 'show icon'):
                self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self.build_menu())
        if config.get('General', 'notifications'):
            Notify.init(APPLET_NAME)
            self._notify = Notify.Notification()

        # load tasks and conditions if any, otherwise save empty lists
        try:
            tasks.load()
        except FileNotFoundError:
            tasks.save()
        try:
            conditions.load()
        except FileNotFoundError:
            conditions.save()

        applet_log.info("MAIN: trying to run startup tasks")
        sysevent_condition_check(EVENT_APPLET_STARTUP)
        Gtk.main()

    # system and session DBus event managers
    def screensaver_manager(self, *args):
        if self.screensaver_mgr.GetActive():
            applet_log.debug("MAIN: screensaver is active")
            deferred_events.append(EVENT_SESSION_SCREENSAVER)
        else:
            applet_log.debug("MAIN: screensaver deactivated")
            deferred_events.append(EVENT_SESSION_SCREENSAVER_EXIT)

    def session_login_lock(self, *args):
        applet_log.debug("MAIN: session lock")
        deferred_events.append(EVENT_SESSION_LOCK)

    def session_login_unlock(self, *args):
        applet_log.debug("MAIN: session unlock")
        deferred_events.append(EVENT_SESSION_UNLOCK)

    # this is apparently valid for ubuntu 14.04 and above
    # see: http://unix.stackexchange.com/a/211863/125979
    def upstart_lock_manager(self, *args):
        message = args[0]
        if message == 'desktop-lock':
            applet_log.debug("MAIN: session lock")
            deferred_events.append(EVENT_SESSION_LOCK)
        elif message == 'desktop-unlock':
            applet_log.debug("MAIN: session unlock")
            deferred_events.append(EVENT_SESSION_UNLOCK)

    def system_sleep_manager(self, *args):
        entering_sleep = args[0]
        if entering_sleep:
            applet_log.debug("MAIN: about to enter sleep state")
            sysevent_condition_check(EVENT_SYSTEM_SUSPEND)
        else:
            applet_log.debug("MAIN: woke up from sleep state")
            deferred_events.append(EVENT_SYSTEM_RESUME)

    def storage_device_manager(self, *args):
        applet_log.debug("MAIN: storage change detected")
        devices = self.storage_mgr.GetManagedObjects().keys()
        drives = filter(lambda x: x.startswith('/org/freedesktop/UDisks2/block_devices/'), devices)
        num_devices = len(list(drives))
        if self.storage_mgr_num_devices > num_devices:
            applet_log.debug("MAIN: device detached")
            deferred_events.append(EVENT_SYSTEM_DEVICE_DETACH)
        elif self.storage_mgr_num_devices < num_devices:
            applet_log.debug("MAIN: new device attached")
            deferred_events.append(EVENT_SYSTEM_DEVICE_ATTACH)
        self.storage_mgr_num_devices = num_devices

    def network_manager(self, *args):
        applet_log.debug("MAIN: network state changed")
        state = self.network_mgr.state()
        if state in [NM_STATE_CONNECTED_LOCAL, NM_STATE_CONNECTED_SITE, NM_STATE_CONNECTED_GLOBAL]:
            applet_log.debug("MAIN: joined network")
            deferred_events.append(EVENT_SYSTEM_NETWORK_JOIN)
        elif state in [NM_STATE_DISCONNECTED]:
            applet_log.debug("MAIN: left network")
            deferred_events.append(EVENT_SYSTEM_NETWORK_LEAVE)

    def before_shutdown(self, *args):
        if not self.leaving:
            applet_log.info("MAIN: trying to run shutdown tasks")
            sysevent_condition_check(EVENT_APPLET_SHUTDOWN)
            self.leaving = True

    def quit(self, _):
        self.before_shutdown()
        Notify.uninit()
        Gtk.main_quit()

    def pause(self, o):
        if o.get_active():
            applet_log.info("MAIN: pausing the scheduler")
            periodic.stop()
            if config.get('Scheduler', 'preserve pause'):
                create_pause_file()
            self.indicator.set_icon('alarm-off')
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        else:
            self.indicator.set_icon('alarm')
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            if config.get('Scheduler', 'preserve pause'):
                unlink_pause_file()
            periodic.start()
            applet_log.info("MAIN: scheduler resumed operation")

    def icon_change(self, name='alarm'):
        self.indicator.set_icon(name)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def icon_dialog(self, active=True):
        name = 'alarm-add' if active else 'alarm'
        self.indicator.set_icon(name)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def dlgtask(self, _):
        self.icon_dialog()
        applet_log.debug("MAIN: opening task dialog")
        self.dialog_add_task.run()
        self.icon_dialog(False)

    def dlgcondition(self, _):
        self.icon_dialog()
        applet_log.debug("MAIN: opening condition dialog")
        self.dialog_add_condition.run()
        self.icon_dialog(False)

    def dlgabout(self, _):
        applet_log.debug("MAIN: opening about dialog")
        self.dialog_about.run()

    def dlgsettings(self, _):
        self.icon_dialog()
        applet_log.debug("MAIN: opening settings dialog")
        self.dialog_settings.run()
        self.icon_dialog(False)

    def dlghistory(self, _):
        applet_log.debug("MAIN: opening task history dialog")
        self.set_attention(False)
        self.dialog_history.run()

    def set_attention(self, warn=True):
        if warn and config.get('General', 'notifications'):
            self.indicator.set_status(AppIndicator.IndicatorStatus.ATTENTION)
        else:
            if periodic.stopped:
                self.indicator.set_icon('alarm-off')
                self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            else:
                self.indicator.set_icon('alarm')
                self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def hide_icon(self, hide=True):
        if hide:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        else:
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

    def notify(self, message, icon='dialog-warning'):
        if config.get('General', 'notifications'):
            self._notify.update(APPLET_SHORTNAME, message, icon)
            self._notify.show()

    # TODO: use this instead of 'xprintidle' to save system resources
    def session_idle_seconds(self):
        return self.screensaver_mgr.getSessionIdleTime()

    def build_menu(self):
        menu = Gtk.Menu()
        item_newtask = Gtk.MenuItem(label=resources.MENU_EDIT_TASKS)
        item_newtask.connect('activate', self.dlgtask)
        item_newtask.show()
        menu.append(item_newtask)

        item_newcond = Gtk.MenuItem(label=resources.MENU_EDIT_CONDITIONS)
        item_newcond.connect('activate', self.dlgcondition)
        item_newcond.show()
        menu.append(item_newcond)

        item_settings = Gtk.MenuItem(label=resources.MENU_SETTINGS)
        item_settings.connect('activate', self.dlgsettings)
        item_settings.show()
        menu.append(item_settings)

        separator = Gtk.SeparatorMenuItem()
        separator.show()
        menu.append(separator)

        item_history = Gtk.MenuItem(label=resources.MENU_TASK_HISTORY)
        item_history.connect('activate', self.dlghistory)
        item_history.show()
        menu.append(item_history)

        separator = Gtk.SeparatorMenuItem()
        separator.show()
        menu.append(separator)

        item_pause = Gtk.CheckMenuItem(label=resources.MENU_PAUSE)
        if config.get('Scheduler', 'preserve pause') and check_pause_file():
            item_pause.set_active(True)
            self.indicator.set_icon('alarm-off')
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        item_pause.connect('activate', self.pause)
        item_pause.show()
        menu.append(item_pause)

        separator = Gtk.SeparatorMenuItem()
        separator.show()
        menu.append(separator)

        item_about = Gtk.MenuItem(label=resources.MENU_ABOUT)
        item_about.connect('activate', self.dlgabout)
        item_about.show()
        menu.append(item_about)

        item_quit = Gtk.MenuItem(label=resources.MENU_QUIT)
        item_quit.connect('activate', self.quit)
        item_quit.show()
        menu.append(item_quit)
        menu.show_all()
        return menu


# signal handler (see http://stackoverflow.com/questions/26388088): this is
# needed in order to handle the logout event (until it is managed by dbus)
def init_signal_handler(applet_instance):
    def signal_action(signum):
        if signum is signal.SIGHUP:
            applet_log.info("SIGHANDLER: caught SIGHUP")
        elif signum is signal.SIGINT:
            applet_log.info("SIGHANDLER: caught SIGINT")
        elif signum is signal.SIGTERM:
            applet_log.info("SIGHANDLER: caught SIGTERM")
        applet_instance.before_shutdown()

    def idle_handler(*args):
        applet_log.info("SIGHANDLER: handler activated (system)")
        GLib.idle_add(signal_action, priority=GLib.PRIORITY_HIGH)

    def handler(*args):
        applet_log.info("SIGHANDLER: handler activated (desktop)")
        signal_action(args[0])

    def install_glib_handler(signum):
        unix_signal_add = None
        if hasattr(GLib, "unix_signal_add"):
            unix_signal_add = GLib.unix_signal_add
        elif hasattr(GLib, "unix_signal_add_full"):
            unix_signal_add = GLib.unix_signal_add_full
        if unix_signal_add:
            applet_log.info("SIGHANDLER: register desktop handler for signal: %r" % signum)
            unix_signal_add(GLib.PRIORITY_HIGH, signum, handler, signum)
        else:
            applet_log.warning("SIGHANDLER: cannot install signal handler")

    SIGS = [getattr(signal, s, None) for s in "SIGINT SIGTERM SIGHUP".split()]
    for signum in filter(None, SIGS):
        applet_log.info("SIGHANDLER: register system handler for signal: %r" % signum)
        signal.signal(signum, idle_handler)
        GLib.idle_add(install_glib_handler, signum, priority=GLib.PRIORITY_HIGH)


def set_applet_enabled_events(evts):
    global applet_enabled_events
    applet_enabled_events = evts


def get_applet_idle_seconds():
    if applet:
        return applet.session_idle_seconds()
    else:
        return 0


def kill_existing(verbose=False, shutdown=False):
    oerr("an existing instance will be %s" % ('shut down' if shutdown else 'killed'), verbose)
    bus = dbus.SessionBus()
    interface = bus.get_object(APPLET_BUS_NAME, APPLET_BUS_PATH)
    if shutdown:
        interface.quit_instance()
    else:
        interface.kill_instance()
    oerr("instance shutdown finished", verbose)


def oerr(s, verbose=True):
    if verbose:
        sys.stderr.write("%s: %s\n" % (APPLET_NAME, s))


def install_icons(overwrite=True):
    create_desktop_file(overwrite=overwrite)
    create_autostart_file(overwrite=overwrite)


def show_box(box='about', verbose=False):
    oerr("showing %s box of currently running instance" % box, verbose)
    bus = dbus.SessionBus()
    interface = bus.get_object(APPLET_BUS_NAME, APPLET_BUS_PATH)
    interface.show_dialog(box)


def show_icon(show=True, running=True):
    if running:
        bus = dbus.SessionBus()
        interface = bus.get_object(APPLET_BUS_NAME, APPLET_BUS_PATH)
        interface.show_icon(show)
    config.set('General', 'show icon', show)
    config.save()


def clear_tasks_conditions(verbose=False):
    oerr("removing all tasks and conditions", verbose)
    try:
        tasks.load()
    except FileNotFoundError:
        tasks.save()
    try:
        conditions.load()
    except FileNotFoundError:
        conditions.save()
    l = list(conditions.names)
    for x in l:
        conditions.remove(cond_name=x)
    conditions.save()
    file_name = os.path.join(USER_CONFIG_FOLDER, 'condition.list')
    try:
        os.unlink(file_name)
    except OSError:
        oerr("could not remove condition list file", verbose)
    l = list(tasks.names)
    for x in l:
        tasks.remove(task_name=x)
    tasks.save()
    file_name = os.path.join(USER_CONFIG_FOLDER, 'task.list')
    try:
        os.unlink(file_name)
    except OSError:
        oerr("could not remove task list file", verbose)


def export_tasks_conditions(filename=None, verbose=False):
    task_dict_list = []
    condition_dict_list = []
    try:
        tasks.load()
    except FileNotFoundError:
        tasks.save()
    try:
        conditions.load()
    except FileNotFoundError:
        conditions.save()
    for name in tasks.names:
        t = tasks.get(task_name=name)
        d = Task_to_dict(t)
        task_dict_list.append(d)
    for name in conditions.names:
        c = conditions.get(cond_name=name)
        condtype = type(c)
        if condtype == EventBasedCondition:
            d = EventBasedCondition_to_dict(c)
        elif condtype == IdleTimeBasedCondition:
            d = IdleTimeBasedCondition_to_dict(c)
        elif condtype == CommandBasedCondition:
            d = CommandBasedCondition_to_dict(c)
        elif condtype == TimeBasedCondition:
            d = TimeBasedCondition_to_dict(c)
        elif condtype == IntervalBasedCondition:
            d = IntervalBasedCondition_to_dict(c)
        # TODO: add further condition type converters here
        else:
            d = Condition_to_dict(c)
        condition_dict_list.append(d)
    oerr("exporting %s tasks and %s conditions" % (
        len(task_dict_list), len(condition_dict_list)), verbose)
    json_dic = {
        'tasks': task_dict_list,
        'conditions': condition_dict_list,
    }
    if not filename:
        filename = os.path.join(USER_CONFIG_FOLDER, '%s.dump' % APPLET_NAME)
    with open(filename, 'w') as f:
        json.dump(json_dic, f, indent=2)
    oerr("items exported to file %s" % filename, verbose)


def import_tasks_conditions(filename=None, verbose=False):
    if not filename:
        filename = os.path.join(USER_CONFIG_FOLDER, '%s.dump' % APPLET_NAME)
    oerr("importing items from file %s" % filename, verbose)
    try:
        with open(filename, 'r') as f:
            json_dic = json.load(f)
    except:
        oerr("could not import from dump file")
        sys.exit(2)
    clear_tasks_conditions(verbose)
    for task_dic in json_dic['tasks']:
        task = dict_to_Task(task_dic)
        tasks.add(task)
    for condition_dic in json_dic['conditions']:
        condition = None
        condtype = condition_dic['subtype']
        if condtype == 'IntervalBasedCondition':
            condition = dict_to_IntervalBasedCondition(condition_dic)
        elif condtype == 'TimeBasedCondition':
            condition = dict_to_TimeBasedCondition(condition_dic)
        elif condtype == 'CommandBasedCondition':
            condition = dict_to_CommandBasedCondition(condition_dic)
        elif condtype == 'IdleTimeBasedCondition':
            condition = dict_to_IdleTimeBasedCondition(condition_dic)
        elif condtype == 'EventBasedCondition':
            condition = dict_to_EventBasedCondition(condition_dic)
        # TODO: add further condition loaders here
        else:
            condition = dict_to_Condition(condition_dic)
        if condition:
            conditions.add(condition)
    oerr("loaded %s tasks and %s conditions" % (
        len(json_dic['tasks']), len(json_dic['conditions'])), verbose)
    tasks.save()
    conditions.save()
    oerr("tasks and conditions successfully imported")


# Configure services and start the application
def main():
    init_signal_handler(applet)
    # signal.signal(signal.SIGINT, signal.SIG_DFL)
    preserve_pause = config.get('Scheduler', 'preserve pause')
    if not (preserve_pause and check_pause_file()):
        periodic.start()
    # the following two cases should be useless, but do some cleanup anyway
    elif not preserve_pause:
        unlink_pause_file()
    else:
        periodic.stop()
    # If we have come here, the command line had no arguments so we clear it
    applet.run([])
    if not periodic.stopped:
        periodic.stop()


def start():
    applet_log.info("MAIN: starting %s version %s" % (APPLET_FULLNAME, APPLET_VERSION))
    create_desktop_file(False)
    create_autostart_file(False)
    main()


# Build the applet and start
if __name__ == '__main__':

    if GRAPHIC_ENVIRONMENT:
        main_dbus_loop = DBusGMainLoop(set_as_default=True)
        GObject.threads_init()

    # check user folders and configure applet
    verify_user_folders()
    config = Config()
    config_loghandler()
    config_loglevel()
    tasks = Tasks()
    conditions = Conditions()
    deferred_events = DeferredEvents()

    # initialize global variables that require graphic environment
    if GRAPHIC_ENVIRONMENT:
        applet = AppletIndicator()
        history = HistoryQueue()
        periodic = Periodic(
            config.get('Scheduler', 'tick seconds'),
            periodic_condition_check,
            autostart=False,
        )

    if len(sys.argv) == 1:
        if not GRAPHIC_ENVIRONMENT:
            oerr("this program requires a graphical session")
            sys.exit(2)
        if applet.get_is_remote():
            oerr("another instance is present: leaving")
            sys.exit(2)
        start()
    else:
        parser = argparse.ArgumentParser(
            prog=APPLET_NAME,
            description=resources.COMMAND_LINE_PREAMBLE,
            epilog=resources.COMMAND_LINE_EPILOG,
        )
        parser.add_argument(
            '-v', '--verbose',
            dest='verbose', action='store_true',
            help=resources.COMMAND_LINE_HELP_VERBOSE
        )
        parser.add_argument(
            '-V', '--version',
            dest='version', action='store_true',
            help=resources.COMMAND_LINE_HELP_VERSION
        )
        parser.add_argument(
            '-s', '--show-settings',
            dest='show_settings', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHOW_SETTINGS
        )
        parser.add_argument(
            '-l', '--show-history',
            dest='show_history', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHOW_HISTORY
        )
        parser.add_argument(
            '-t', '--show-tasks',
            dest='show_tasks', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHOW_TASKS
        )
        parser.add_argument(
            '-c', '--show-conditions',
            dest='show_conditions', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHOW_CONDITIONS
        )
        parser.add_argument(
            '-R', '--reset-config',
            dest='reset_config', action='store_true',
            help=resources.COMMAND_LINE_HELP_RESET_CONFIG
        )
        parser.add_argument(
            '-I', '--show-icon',
            dest='show_icon', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHOW_ICON
        )
        parser.add_argument(
            '-C', '--clear',
            dest='clear', action='store_true',
            help=resources.COMMAND_LINE_HELP_CLEAR
        )
        parser.add_argument(
            '-T', '--install',
            dest='install', action='store_true',
            help=resources.COMMAND_LINE_HELP_INSTALL
        )
        parser.add_argument(
            '-Q', '--query',
            dest='query', action='store_true',
            help=resources.COMMAND_LINE_HELP_QUERY
        )
        parser.add_argument(
            '--shutdown',
            dest='shutdown', action='store_true',
            help=resources.COMMAND_LINE_HELP_SHUTDOWN
        )
        parser.add_argument(
            '--kill',
            dest='kill', action='store_true',
            help=resources.COMMAND_LINE_HELP_KILL
        )
        parser.add_argument(
            '--export',
            dest='export_items', metavar='FILE', nargs='?', const='*',
            help=resources.COMMAND_LINE_HELP_EXPORT
        )
        parser.add_argument(
            '--import',
            dest='import_items', metavar='FILE', nargs='?', const='*',
            help=resources.COMMAND_LINE_HELP_IMPORT
        )

        args = parser.parse_args()
        verbose = args.verbose

        running = False
        if GRAPHIC_ENVIRONMENT:
            running = applet.get_is_remote()

        if args.version:
            print("%s: %s, version %s" % (APPLET_NAME, APPLET_FULLNAME, APPLET_VERSION))
            if verbose and running:
                show_box('about', False)

        if args.show_icon:
            show_icon(True, running)

        if args.show_settings:
            if not running:
                oerr("could not find a running instance, please start it first", verbose)
                sys.exit(2)
            else:
                show_box('settings', verbose)
        elif args.show_history:
            if not running:
                oerr("could not find a running instance, please start it first", verbose)
                sys.exit(2)
            else:
                show_box('history', verbose)
        elif args.show_tasks:
            if not running:
                oerr("could not find a running instance, please start it first", verbose)
                sys.exit(2)
            else:
                show_box('task', verbose)
        elif args.show_conditions:
            if not running:
                oerr("could not find a running instance, please start it first", verbose)
                sys.exit(2)
            else:
                show_box('condition', verbose)

        if args.export_items:
            if args.export_items == '*':
                filename = None
            else:
                filename = args.export_items
            try:
                export_tasks_conditions(filename, verbose)
            except Exception as e:
                applet_log.critical("MAIN: exception %s occurred while performing 'export'" % e)
                oerr("an error occurred while trying to export items", verbose)
                sys.exit(2)
            oerr("tasks and conditions successfully exported", verbose)

        if args.shutdown:
            if running:
                kill_existing(verbose=verbose, shutdown=True)
                running = False
            else:
                oerr("could not find a running instance", verbose)
                sys.exit(1)
        elif args.kill:
            if running:
                kill_existing(verbose=verbose, shutdown=False)
                running = False
            else:
                oerr("could not find a running instance", verbose)
                sys.exit(1)

        if args.reset_config:
            if running:
                oerr("cannot reset configuration, please close instance first", verbose)
                sys.exit(2)
            else:
                try:
                    config.reset()
                    unlink_pause_file()
                except Exception as e:
                    applet_log.critical("MAIN: exception %s occurred while performing 'reset-config'" % e)
                    oerr("an error occurred while trying to reset configuration", verbose)
                    sys.exit(2)
                oerr("configuration has been reset", verbose)

        if args.clear:
            if running:
                oerr("cannot clear items, please close instance first", verbose)
                sys.exit(2)
            else:
                try:
                    clear_tasks_conditions(verbose)
                except Exception as e:
                    applet_log.critical("MAIN: exception %s occurred while performing 'clear'" % e)
                    oerr("an error occurred while trying to delete items", verbose)
                    sys.exit(2)
                oerr("tasks and conditions deleted", verbose)

        if args.import_items:
            if running:
                oerr("cannot import items, please close instance first", verbose)
                sys.exit(2)
            else:
                if args.import_items == '*':
                    filename = None
                else:
                    filename = args.import_items
                try:
                    import_tasks_conditions(filename, verbose)
                except Exception as e:
                    applet_log.critical("MAIN: exception %s occurred while performing 'import'" % e)
                    oerr("an error occurred while trying to import items", verbose)
                    sys.exit(2)

        if args.install:
            if running:
                oerr("cannot install, please close instance first", verbose)
                sys.exit(2)
            else:
                try:
                    install_icons(True)
                except Exception as e:
                    applet_log.critical("MAIN: exception %s occurred while performing 'install'" % e)
                    oerr("an error occurred while trying to install icons", verbose)
                    sys.exit(2)
                oerr("configuration has been reset", verbose)

        if args.query:
            if running:
                oerr("found a running instance", verbose)
                sys.exit(0)
            else:
                oerr("no instance could be found", verbose)
                sys.exit(1)


# end.
