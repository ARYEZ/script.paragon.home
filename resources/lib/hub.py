# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Routes every device operation to the driver that owns it.

One driver speaks one vendor's LAN protocol. The Hub presents the union of
them as a single set of devices, so the registry, the scene engine and the
menus never learn what a Govee bulb is -- they ask the Hub, and the Hub asks
whichever driver discovered the device.

A driver implements:

    DRIVER_ID, DRIVER_LABEL     identity
    discover(timeout)           -> ([Device], [warning strings])
    capabilities(device)        -> set of CAP_* it supports
    turn/set_brightness/set_color/set_color_temp/set_position
                                state verbs, raising ControlError on failure
    get_state(device)           -> dict or None
    get_states(devices, timeout)-> {device_id: state or None}
    commands(device)            -> [names] it can emit, [] for most
    send_command(device, name)  fire one, raising ControlError

Only what a device claims in capabilities() is ever called on it, so a driver
for something with no colour -- a plug, an IR blaster -- implements the verbs
it has and reports the rest as absent.
"""

import time

from devices import (CAP_COMMANDS, CAP_LOCK, CAP_PLAYBACK, CAP_POSITION,
                     CAP_POWER, CAP_STATE, CAP_UNLOCK, ControlError,
                     DEFAULT_DRIVER)

# What a power-only device is allowed to be asked for. Switching, and saying
# what it is doing -- everything about how it looks is somebody else's job.
POWER_ONLY_CAPABILITIES = frozenset([CAP_POWER, CAP_STATE])

# How long a device that gave no reading is left alone before it is looked
# for again. A light switched off at the wall is silent on every sweep, and
# without this each one would cost a discovery -- three seconds for Govee,
# six for Tuya -- every time somebody pulled to refresh.
LOCATE_HOLDOFF = 300


def narrow(capabilities, device):
    """A driver's answer, cut down by what the user has said about `device`.

    A function rather than a method so that anything standing in for the Hub
    -- the test double, most of all -- narrows by calling this instead of by
    reimplementing it. A stand-in that says a device can be coloured when the
    Hub says it cannot is a stub that hides the bug it was put there to
    catch.
    """
    caps = set(capabilities or [])
    if getattr(device, 'power_only', False):
        return caps & POWER_ONLY_CAPABILITIES
    return caps


class Hub(object):
    """The set of drivers, addressed as one device layer."""

    def __init__(self, drivers=None, log_func=None, allow_unlock=False,
                 confirm_unlock=False, on_moved=None):
        # Whether this box may open a door. One flag, checked in one place, so
        # that the menus, the web remote and a sequence are all answered the
        # same way and none of them can be the exception. Off unless the box
        # has been told otherwise -- see unlock().
        self.allow_unlock = bool(allow_unlock)
        # Whether to ask first. Nothing in here reads this -- it is a question
        # about screens, and there is no screen at this depth. It is carried
        # here because the menus and the web remote both already have the
        # controller to hand and neither should be reading settings of its own,
        # which is how two halves of one preference come to disagree.
        #
        # Off by default. The permission above it is already deliberate, so a
        # prompt on every press is a keystroke rather than a decision.
        self.confirm_unlock = bool(confirm_unlock)
        self.drivers = {}
        for driver in drivers or []:
            self.drivers[driver.DRIVER_ID] = driver
        self._log = log_func or (lambda message: None)
        # What we last told each device, by device id, with the time we said
        # it. Kept here because this is the one thing every write passes
        # through -- the menus, the web remote, scenes and sequences all
        # arrive at these verbs.
        #
        # It is what we said, not what the device is doing, and the two are
        # different things: a blind can be pulled by hand, moved from the
        # SwitchBot app, or simply not have heard us. So it is shown as what
        # it is and never used to decide that a step can be skipped -- see
        # already_there in sequences.py, which takes a reading or nothing.
        #
        # In memory rather than on disk, deliberately. After a restart we
        # genuinely do not know what happened while Kodi was off, and a
        # remembered position surviving that would be a guess wearing the
        # clothes of a fact.
        self.last_told = {}
        # Told which devices were just found at a new address, so whoever
        # keeps the device list can write it down. The address is changed on
        # the device object itself, here, because this is where the silence
        # was noticed and the new address found; saving is not this layer's
        # job.
        self._on_moved = on_moved or (lambda devices: None)
        # When each device was last looked for, by device id. See
        # LOCATE_HOLDOFF.
        self._looked_for = {}

    def _remember(self, device, **what):
        """Note what a device was just told, after it accepted it."""
        entry = self.last_told.setdefault(device.device_id, {})
        entry.update(what)
        entry['at'] = time.time()
        return entry

    # -- driver lookup -----------------------------------------------------

    def driver(self, driver_id):
        return self.drivers.get(driver_id or DEFAULT_DRIVER)

    def driver_for(self, device):
        """The driver that owns `device`.

        Falls back to the default rather than failing: a devices.json written
        before drivers existed records no driver, and every entry in it is a
        Govee light.
        """
        found = self.drivers.get(getattr(device, 'driver', None)
                                 or DEFAULT_DRIVER)
        if found is None:
            found = self.drivers.get(DEFAULT_DRIVER)
        return found

    def _require(self, device):
        driver = self.driver_for(device)
        if driver is None:
            raise ControlError('%s has no driver installed for "%s"'
                               % (device.name, getattr(device, 'driver', '?')))
        return driver

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Search with every driver. Returns (devices, warnings).

        One driver failing never stops the others: a Govee search that cannot
        bind its port should not hide the IR blasters.
        """
        found = []
        warnings = []
        for driver_id in sorted(self.drivers):
            driver = self.drivers[driver_id]
            try:
                devices, driver_warnings = driver.discover(timeout=timeout)
            except Exception as exc:
                self._log('%s discovery failed: %s' % (driver_id, exc))
                warnings.append('%s search failed: %s'
                                % (driver.DRIVER_LABEL, exc))
                continue
            for device in devices:
                device.driver = driver_id
            found.extend(devices)
            warnings.extend(driver_warnings or [])
        return found, warnings

    # -- capabilities ------------------------------------------------------

    def capabilities(self, device):
        """What this device can be asked to do.

        A device marked power-only is narrowed here to switching and
        reporting, whatever its driver says it is capable of. This is the one
        place it needs doing: every decision in the add-on about what a device
        is for goes through this answer -- which controls the menus offer,
        whether a scene counts it as a light, whether the web remote draws a
        colour picker for it. Enforcing it here rather than at each of those
        means a path added later inherits it instead of having to remember it.
        """
        driver = self.driver_for(device)
        if driver is None:
            return set()
        return narrow(driver.capabilities(device), device)

    def commands(self, device):
        driver = self.driver_for(device)
        if driver is None:
            return []
        return driver.commands(device)

    def songs(self, device):
        """Which of a beacon's clips are songs. Nothing for anything else."""
        driver = self.driver_for(device)
        if driver is None or not hasattr(driver, 'songs'):
            return []
        return driver.songs(device)

    def send_command(self, device, name):
        driver = self._require(device)
        if CAP_COMMANDS not in driver.capabilities(device):
            raise ControlError('%s does not send commands' % device.name)
        return driver.send_command(device, name)

    def collapse(self, devices):
        """Drop targets a driver can cover with one command.

        Only for operations where every target gets the identical instruction.
        A driver with nothing to collapse -- which is most of them -- has no
        hook and the list passes through untouched.
        """
        result = list(devices)
        for driver_id in sorted(self.drivers):
            hook = getattr(self.drivers[driver_id], 'collapse', None)
            if hook is not None:
                result = hook(result)
        return result

    # -- state verbs -------------------------------------------------------

    def turn(self, device, on):
        answer = self._require(device).turn(device, on)
        # After the driver, never before: a write that raised is not something
        # the device was told, and remembering it would be remembering a
        # failure as a success.
        self._remember(device, power='on' if on else 'off')
        return answer

    def set_brightness(self, device, percent):
        if self._look_only(device):
            return None
        answer = self._require(device).set_brightness(device, percent)
        self._remember(device, brightness=percent)
        return answer

    def set_color(self, device, red, green, blue):
        if self._look_only(device):
            return None
        return self._require(device).set_color(device, red, green, blue)

    def set_color_temp(self, device, kelvin):
        if self._look_only(device):
            return None
        return self._require(device).set_color_temp(device, kelvin)

    def set_position(self, device, percent):
        """Drive a cover to `percent` open. 0 is shut, 100 is fully open.

        Not narrowed by _look_only. Power-only marks a light the user does not
        want coloured or dimmed; a blind is not a light, and its position is
        the only thing it has. Narrowing here would leave it with nothing.
        """
        driver = self._require(device)
        if CAP_POSITION not in driver.capabilities(device):
            raise ControlError('%s cannot be opened or closed' % device.name)
        answer = driver.set_position(device, percent)
        self._remember(device, position=percent)
        return answer

    # -- playback: a beacon ---------------------------------------------------

    def _player(self, device):
        driver = self._require(device)
        if CAP_PLAYBACK not in driver.capabilities(device):
            raise ControlError('%s does not play anything' % device.name)
        return driver

    def pause(self, device):
        return self._player(device).pause(device)

    def resume(self, device):
        return self._player(device).resume(device)

    def queue_command(self, device, name, volume=None):
        """A beacon's clip, played when what it is playing ends, at its own
        volume if given. Only a player has a turn to wait for."""
        return self._player(device).queue_command(device, name, volume)

    def set_volume(self, device, volume):
        """0 to 100. Remembered, so the phone can show it before the beacon
        is next asked."""
        settled = self._player(device).set_volume(device, volume)
        self._remember(device, volume=settled)
        return settled

    def lock(self, device):
        """Throw a deadbolt.

        Always allowed, and safe to repeat: a bolt already thrown stays thrown,
        so nothing here has to know the state to do the right thing. Locking a
        door is the one action in this add-on that cannot be a mistake.
        """
        driver = self._require(device)
        if CAP_LOCK not in driver.capabilities(device):
            raise ControlError('%s is not a lock' % device.name)
        answer = driver.lock(device)
        self._remember(device, lock='locked')
        return answer

    def unlock(self, device):
        """Withdraw a deadbolt, if this box is allowed to.

        The single most consequential thing here, so it is gated in exactly one
        place: every caller -- the menus, the web remote, a sequence step --
        arrives at this method, and none of them can be the exception.

        Off unless the box has been told otherwise. Not because the owner has
        not decided -- they have -- but because a house runs several Kodi boxes
        off one set of menus, and "this house may unlock the front door" and
        "the spare room may unlock the front door" are different sentences.
        Switched on per box under Settings.
        """
        driver = self._require(device)
        if CAP_UNLOCK not in driver.capabilities(device):
            raise ControlError('%s cannot be unlocked' % device.name)
        if not self.allow_unlock:
            raise ControlError(
                'Unlocking is switched off on this box. Settings -> Devices -> '
                'Let this box unlock doors.')
        answer = driver.unlock(device)
        self._remember(device, lock='unlocked')
        return answer

    @staticmethod
    def _look_only(device):
        """Whether this verb is one a power-only device does not accept.

        Nothing that reads capabilities first will ever reach here, and that
        is most of the add-on. What does reach here is the handful of bulk
        verbs that send the same instruction to every enabled device without
        asking -- "make all the lights red" from a remote button, say. Those
        should pass over this device rather than fail on it, so this returns
        quietly instead of raising: a colour command that skips one strip has
        not gone wrong, it has done what was asked.
        """
        return bool(getattr(device, 'power_only', False))

    def get_state(self, device):
        driver = self.driver_for(device)
        if driver is None:
            return None
        return driver.get_state(device)

    def get_states(self, devices, timeout=3.0, now=None):
        """Read many devices at once, letting each driver batch its own.

        Grouping by driver is what keeps the Govee bulk sweep -- one socket,
        one timeout for all 25 bulbs -- rather than degrading to one round
        trip per device once a second vendor is present.

        A device that gave no reading is then looked for -- see
        _find_the_silent -- so a light that came back from a router reboot
        on a different address is mended by the next read rather than by
        somebody noticing ten dead lights and pressing Refresh devices.
        """
        states = {}
        by_driver = {}
        for device in devices:
            by_driver.setdefault(getattr(device, 'driver', None)
                                 or DEFAULT_DRIVER, []).append(device)

        for driver_id, group in by_driver.items():
            driver = self.drivers.get(driver_id)
            if driver is None:
                for device in group:
                    states[device.device_id] = None
                continue
            try:
                states.update(driver.get_states(group, timeout=timeout))
            except Exception as exc:
                self._log('%s state read failed: %s' % (driver_id, exc))
                for device in group:
                    states.setdefault(device.device_id, None)
            self._find_the_silent(driver, group, states, timeout, now)
        return states

    # -- devices that moved ------------------------------------------------

    def can_locate(self, device):
        """Whether a device that went quiet is one this layer can look for.

        Three things have to hold: its driver can say where its devices are
        on the LAN, it has an address to be wrong about, and it is a device
        that answers when asked -- a blaster or a speaker reports no state,
        and silence from one of those is not a symptom.
        """
        driver = self.driver_for(device)
        return driver is not None and hasattr(driver, 'locate') \
            and bool(getattr(device, 'ip', '')) \
            and CAP_STATE in self.capabilities(device)

    def _find_the_silent(self, driver, group, states, timeout, now=None):
        """Look for the devices in `group` that gave no reading.

        A Govee command is a datagram with no reply, so a light that moved
        to a new address fails silently: the send is reported as done and
        nothing happens. The status sweep is the one place the silence can
        be heard. So a device that answered nothing is asked for by its
        driver's discovery, and if it turns up somewhere else its address is
        changed and its state read again from there. Returns what moved.

        A device found at the address it already had is left alone: it heard
        the discovery and not the status request, which is a WiFi bulb
        being a WiFi bulb. One that was not found at all is switched off, or
        gone, and a refresh will say so.
        """
        moment = now if now is not None else time.time()
        silent = []
        for device in group:
            if states.get(device.device_id) is not None:
                continue
            if not self.can_locate(device):
                continue
            last = self._looked_for.get(device.device_id)
            if last is not None and moment - last < LOCATE_HOLDOFF:
                continue
            silent.append(device)
        if not silent:
            return []

        for device in silent:
            self._looked_for[device.device_id] = moment
        self._log('%d device(s) gave no reading; looking for them'
                  % len(silent))
        try:
            where = driver.locate(silent, timeout=timeout) or {}
        except Exception as exc:
            self._log('%s could not look for its devices: %s'
                      % (driver.DRIVER_ID, exc))
            return []

        moved = []
        for device in silent:
            ip = where.get(device.device_id)
            if not ip or ip == device.ip:
                continue
            self._log('%s moved from %s to %s' % (device.name, device.ip, ip))
            device.ip = ip
            moved.append(device)
        if not moved:
            return []

        try:
            states.update(driver.get_states(moved, timeout=timeout))
        except Exception as exc:
            self._log('%s state read failed: %s' % (driver.DRIVER_ID, exc))
        self._on_moved(moved)
        return moved

    # -- Govee-specific passthrough ----------------------------------------

    def pick_transport(self, device):
        """Which transport a device would use, when its driver has the idea.

        Only Govee has a LAN/cloud split; anything else answers None. Used by
        the cloud-quota warning, which has nothing to say about a driver with
        no cloud.
        """
        driver = self.driver_for(device)
        picker = getattr(driver, 'pick_transport', None)
        if picker is None:
            return None
        return picker(device)

    @property
    def mode(self):
        """The Govee transport mode, for the diagnostics report."""
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'mode', None)

    @property
    def lan(self):
        """The Govee LAN transport, for the LAN diagnostics probe."""
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'lan', None)

    @property
    def cloud(self):
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'cloud', None)
