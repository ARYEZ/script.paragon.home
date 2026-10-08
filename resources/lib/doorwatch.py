# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

doorwatch.py -- a lock's readings, turned into what happened to it.

A lock read every fifteen seconds says "unlocked" on every read while the door
stays unlocked. What a sequence wants is the moment it became so: once, when it
was unlocked, and not again until it has been something else in between.

How a lock is read is not this file's business. Today it is SwitchBot's
servers; a beacon listening to the lock's own Bluetooth would hand in the same
readings and nothing here would change.
"""

import sequences as sequence_lib


class DoorWatch(object):
    """Remembers each lock's last reading and reports the changes."""

    def __init__(self):
        self.last = {}

    def readings(self, states):
        """[(lock id, event)] for every lock that has just become unlocked or
        jammed.

        A lock's first reading is where it stands, not something that
        happened: Kodi starting while the door is unlocked must not announce
        it. A lock that gave no reading keeps the one before -- a missed read
        is not a lock that moved, and treating it as one would announce the
        door every time the internet blinked.
        """
        events = []
        for device_id, state in sorted((states or {}).items()):
            lock = (state or {}).get('lock')
            if not lock:
                continue
            before = self.last.get(device_id)
            self.last[device_id] = lock
            if before is None or before == lock:
                continue
            if lock in sequence_lib.DOOR_EVENTS:
                events.append((device_id, lock))
        return events

    def forget(self, keep=()):
        """Drop every lock not in `keep`. One that stops being watched and is
        watched again later starts from a fresh reading rather than from
        whatever it was hours ago."""
        for device_id in list(self.last):
            if device_id not in keep:
                del self.last[device_id]
