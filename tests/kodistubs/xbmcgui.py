# -*- coding: utf-8 -*-
"""Minimal xbmcgui stub.

Dialog answers are driven by queues the tests fill in, so a menu walk can be
scripted end to end without a running Kodi.
"""

INPUT_ALPHANUM = 0
INPUT_NUMERIC = 1
INPUT_DATE = 2
INPUT_TIME = 3
INPUT_IPADDRESS = 4
INPUT_PASSWORD = 5

# Scripted answers, consumed left to right.
SELECT_QUEUE = []
INPUT_QUEUE = []
YESNO_QUEUE = []

# Everything the code showed the user.
NOTIFICATIONS = []
OK_DIALOGS = []
SELECT_CALLS = []
PROGRESS_CALLS = []

# When non-empty, DialogProgress.iscanceled() returns True once this many
# update() calls have been made.
CANCEL_AFTER = []


def reset():
    del SELECT_QUEUE[:]
    del INPUT_QUEUE[:]
    del YESNO_QUEUE[:]
    del NOTIFICATIONS[:]
    del OK_DIALOGS[:]
    del SELECT_CALLS[:]
    del PROGRESS_CALLS[:]
    del CANCEL_AFTER[:]


def getCurrentWindowId():
    return 10000


class Dialog(object):
    def select(self, heading, options, autoclose=0):
        SELECT_CALLS.append((heading, list(options)))
        if not SELECT_QUEUE:
            return -1
        return SELECT_QUEUE.pop(0)

    def input(self, heading, default='', type=0, option=0, autoclose=0):
        if not INPUT_QUEUE:
            return ''
        return INPUT_QUEUE.pop(0)

    def yesno(self, heading, line1, line2='', line3='', nolabel='',
              yeslabel=''):
        if not YESNO_QUEUE:
            return False
        return YESNO_QUEUE.pop(0)

    def ok(self, heading, line1, line2='', line3=''):
        OK_DIALOGS.append((heading, line1))
        return True

    def notification(self, heading, message, icon='', time=5000, sound=True):
        NOTIFICATIONS.append((heading, message))


class DialogProgress(object):
    """Foreground progress dialog. Tests set CANCEL_AFTER to cancel early."""

    def __init__(self):
        self._updates = 0

    def create(self, heading, line1='', line2='', line3=''):
        PROGRESS_CALLS.append((heading, line1))

    def update(self, percent=0, line1='', line2='', line3=''):
        self._updates += 1

    def iscanceled(self):
        if CANCEL_AFTER and self._updates >= CANCEL_AFTER[0]:
            return True
        return False

    def close(self):
        pass


class DialogProgressBG(object):
    def create(self, heading, message=''):
        pass

    def update(self, percent=0, heading='', message=''):
        pass

    def close(self):
        pass


class Window(object):
    _properties = {}

    def __init__(self, window_id=0):
        self.window_id = window_id

    def setProperty(self, key, value):
        self._properties[key] = value

    def getProperty(self, key):
        return self._properties.get(key, '')
