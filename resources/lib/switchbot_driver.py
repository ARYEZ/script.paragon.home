# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The SwitchBot driver: blinds, shades and deadbolts, through a SwitchBot hub.

One implementation of the driver contract the Hub routes to. What is unusual
about it is that it is the only driver here with no LAN path at all -- see
switchbot_cloud for why -- and the only one whose devices are covers rather
than lights or switches.

A cover is not a light and it is not a plug. It was worth adding CAP_POSITION
for rather than pretending: a blind reported as a plug would be offered to
every "switch everything off" path in the add-on and would be shut by scenes
that meant to turn off a lamp. It reports CAP_POSITION and CAP_STATE, so the
menus offer it a position and the scene engine passes over it entirely.

A lock is here on one condition: it locks and it cannot unlock. It reports
CAP_LOCK and CAP_STATE and nothing else -- no CAP_POWER, most of all, because a
lock offered as a switch would be reached by "switch everything off", by a
scene meaning to turn off a lamp, and by the power buttons on the web remote.
It is not that those paths would refuse a lock; it is that they never see one.

There is no unlock verb in this driver, in the Hub, or anywhere above them. A
door is not a thing this add-on can open.

A note on what "position" means on a Blind Tilt, because it is not what it
means on a curtain. The slats tilt rather than travelling: both ends of the
range are shut -- one tilted up, one tilted down -- and the middle is open.
So 50 is the light-through position and 0 and 100 are both dark. Rather than
bake that reading in, the named commands SwitchBot documents are exposed as
commands in their own right, so Open, Close Up and Close Down do whatever the
hardware says they do and do not depend on this comment being right.
"""

from devices import (CAP_COMMANDS, CAP_LOCK, CAP_POSITION, CAP_POWER,
                     CAP_STATE, ControlError, Device)
from switchbot_cloud import CloudError

# What this driver will adopt. Everything else on the account -- bots,
# sensors, plugs, the hub itself, and every infrared remote it fronts -- is
# left alone rather than half-supported.
COVER_TYPES = ('Blind Tilt', 'Curtain', 'Curtain3', 'Roller Shade')

# Deadbolts. Several spellings because SwitchBot has shipped the line under
# more than one name and the account reports whichever the hardware is; a type
# not listed here is passed over and named in the search warning, which is how
# a new one gets added rather than guessed at.
LOCK_TYPES = ('Smart Lock', 'Smart Lock Pro', 'Smart Lock Ultra', 'Lock',
              'Lock Pro', 'Lock Ultra')

# What SwitchBot reports a bolt as doing, mapped to the two words this add-on
# uses plus the one that matters most. "jammed" is its own answer and not a
# kind of unlocked: a bolt that fouled the strike plate has not locked, and
# saying so is the difference between going to bed and going to look.
LOCK_STATES = {
    'locked': 'locked',
    'lock': 'locked',
    'unlocked': 'unlocked',
    'unlock': 'unlocked',
    'jammed': 'jammed',
}

# The one command this driver will send a lock. There is no unlock here, and
# that absence is the feature -- see Hub.lock.
LOCK_COMMAND = 'lock'


def is_lock(device):
    """Whether this is a deadbolt rather than a cover."""
    model = (getattr(device, 'model', '') or '').strip()
    return model in LOCK_TYPES

# Tilt devices take even positions only; an odd one is rejected outright.
POSITION_STEP = 2

# The commands SwitchBot documents for a tilt, as (menu label, api command).
# Offered as named commands so the user has the hardware's own vocabulary and
# is not restricted to whatever this driver thinks a percentage means.
TILT_COMMANDS = (
    ('Open', 'fullyOpen'),
    ('Close Up', 'closeUp'),
    ('Close Down', 'closeDown'),
    ('Pause', 'pause'),
)

# A travelling cover has no up or down to close in.
TRAVEL_COMMANDS = (
    ('Open', 'turnOn'),
    ('Close', 'turnOff'),
    ('Pause', 'pause'),
)


def is_tilt(device):
    """Whether this is a Blind Tilt rather than a curtain or a shade."""
    model = (getattr(device, 'model', '') or '')
    return model.strip().lower() == 'blind tilt'


def clean_position(value):
    """A percentage this hardware will actually accept.

    Clamped to 0-100 and rounded to an even number, because a tilt rejects an
    odd position rather than rounding it, and a rejection three layers down
    reads as "SwitchBot refused the request" with nothing pointing at the 51
    that caused it.
    """
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise ControlError('%r is not a position' % (value,))
    number = max(0, min(100, number))
    return number - (number % POSITION_STEP)


class SwitchBotDriver(object):
    """Covers on a SwitchBot account, addressed through the hub that bridges
    them."""

    DRIVER_ID = 'switchbot'
    DRIVER_LABEL = 'SwitchBot'

    def __init__(self, transport=None, log_func=None):
        self.transport = transport
        self._log = log_func or (lambda message: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Every cover and lock on the account. Returns (devices, warnings).

        `timeout` is accepted for the contract's sake and unused: this is one
        HTTPS request whose timeout belongs to the transport, not a LAN sweep
        that can be told how long to listen.
        """
        if self.transport is None or not self.transport.configured:
            return [], []

        try:
            entries = self.transport.devices()
        except CloudError as exc:
            return [], ['SwitchBot search failed: %s' % exc]

        found = []
        # What was passed over, by the name SwitchBot calls it. Counting them
        # was not enough: "none of them a blind" leaves you with no idea
        # whether the thing you just installed is absent from the account, or
        # present under a name this driver has never heard of. The name is the
        # whole of what is needed to decide which.
        skipped = []
        for entry in entries:
            devtype = (entry.get('deviceType') or '').strip()
            if devtype not in COVER_TYPES and devtype not in LOCK_TYPES:
                skipped.append(devtype or 'unnamed type')
                continue
            device_id = entry.get('deviceId') or ''
            if not device_id:
                continue
            found.append(Device(
                driver=self.DRIVER_ID,
                device_id=device_id,
                native_id=device_id,
                name=entry.get('deviceName') or devtype,
                model=devtype,
                cloud=True,
                driver_data={'hub': entry.get('hubDeviceId') or ''},
            ))

        warnings = []
        if skipped:
            counted = {}
            for name in skipped:
                counted[name] = counted.get(name, 0) + 1
            listed = ', '.join(
                '%s x%d' % (name, count) if count > 1 else name
                for name, count in sorted(counted.items()))
            if found:
                self._log('SwitchBot: passed over %s' % listed)
            else:
                warnings.append(
                    'SwitchBot found %d device(s), none of them a blind, '
                    'shade or lock: %s' % (len(skipped), listed))
        return found, warnings

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def capabilities(device):
        """A cover opens to a percentage, reports where it is, and takes the
        named commands its own hardware documents.

        CAP_POWER is here so that "open" and "close" have somewhere to live
        for a caller that only knows how to switch things -- a sequence step,
        the web remote's on/off pair. It does not make this a light: nothing
        decides that from CAP_POWER, and CAP_BRIGHTNESS is deliberately
        absent so the scene engine passes over it.
        """
        if is_lock(device):
            # Deliberately short. No CAP_POWER, so no "switch everything off"
            # and no scene ever reaches a deadbolt; no CAP_POSITION, because a
            # door is not ajar by a percentage; no CAP_COMMANDS, because the
            # named-command path would be a way to send "unlock" by typing it.
            return set([CAP_LOCK, CAP_STATE])
        return set([CAP_POSITION, CAP_STATE, CAP_POWER, CAP_COMMANDS])

    @staticmethod
    def commands(device):
        if is_lock(device):
            return []
        table = TILT_COMMANDS if is_tilt(device) else TRAVEL_COMMANDS
        return [label for label, _command in table]

    def send_command(self, device, name):
        table = TILT_COMMANDS if is_tilt(device) else TRAVEL_COMMANDS
        for label, command in table:
            if label == name:
                self._send(device, command)
                return
        raise ControlError('%s has no command called "%s"'
                           % (device.name, name))

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def native_id(device):
        return getattr(device, 'native_id', None) or device.device_id

    def _send(self, device, command, parameter='default'):
        if self.transport is None or not self.transport.configured:
            raise ControlError('No SwitchBot token and secret have been set')
        try:
            self.transport.command(self.native_id(device), command, parameter)
        except CloudError as exc:
            raise ControlError('%s: %s' % (device.name, exc))

    # -- state verbs -------------------------------------------------------

    def set_position(self, device, percent):
        """Drive the cover to `percent`. 0 is shut, 100 fully drawn back.

        On a tilt both ends are shut and the middle is open; see the note at
        the top of this module. The value goes to the hardware as given
        either way -- reinterpreting it here would put this driver's guess
        between the user and the slider.
        """
        if is_lock(device):
            raise ControlError('%s is a lock, not a blind' % device.name)
        position = clean_position(percent)
        if is_tilt(device):
            # The API wants a direction alongside the number. "up" is the
            # side the app calls up; closeUp and closeDown are the way to
            # reach either extreme without depending on that.
            self._send(device, 'setPosition', 'up;%d' % position)
        else:
            self._send(device, 'setPosition', '0,ff,%d' % position)

    def lock(self, device):
        """Throw the bolt. There is no counterpart and there will not be one.

        Safe to send at a bolt that is already thrown, which is why nothing
        here reads the state first: the right thing to do is the same either
        way, and a read that said "already locked" wrongly would be a reason
        not to lock a door.
        """
        if not is_lock(device):
            raise ControlError('%s is not a lock' % device.name)
        return self._send(device, LOCK_COMMAND)

    def turn(self, device, on):
        """Open or shut, for callers that only know how to switch things.

        A lock is refused here rather than only being kept out by not claiming
        CAP_POWER. Not claiming it is what stops a lock being offered; this is
        what stops it being reached anyway -- Hub.turn does not check
        capabilities, and on this hardware "turnOn" at a deadbolt is an open
        front door. A rule this important is worth enforcing where it would be
        broken, not only where it is declared.
        """
        if is_lock(device):
            raise ControlError('%s is a lock. Paragon Home can lock a door '
                               'and cannot open one.' % device.name)
        if is_tilt(device):
            self._send(device, 'fullyOpen' if on else 'closeDown')
        else:
            self._send(device, 'turnOn' if on else 'turnOff')

    def set_brightness(self, device, percent):
        raise ControlError('%s is a blind, not a light' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s is a blind, not a light' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s is a blind, not a light' % device.name)

    # -- reading -----------------------------------------------------------

    @staticmethod
    def _lock_state_from_status(status):
        """A deadbolt's status, as the state dict the add-on uses.

        "jammed" is carried through as itself rather than folded into
        unlocked. A bolt that fouled the strike plate has not locked, but it is
        not the same as one that was never asked to -- and the difference is
        whether you go to bed or go and look.
        """
        raw = (status.get('lockState') or '').strip().lower()
        known = LOCK_STATES.get(raw)
        if known is None:
            return None
        state = {'lock': known}
        door = (status.get('doorState') or '').strip().lower()
        if door in ('open', 'opened'):
            state['door'] = 'open'
        elif door in ('close', 'closed'):
            state['door'] = 'closed'
        battery = status.get('battery')
        if battery is not None:
            state['battery'] = battery
        return state

    @staticmethod
    def _state_from_status(status, lock=False):
        """One SwitchBot status body, as the state dict the add-on uses."""
        if not status:
            return None
        if lock:
            return SwitchBotDriver._lock_state_from_status(status)
        raw = status.get('slidePosition')
        if raw is None:
            return None
        try:
            position = int(round(float(raw)))
        except (TypeError, ValueError):
            return None
        position = max(0, min(100, position))
        state = {'position': position}
        battery = status.get('battery')
        if battery is not None:
            state['battery'] = battery
        if status.get('moving'):
            state['moving'] = True
        return state

    def get_state(self, device):
        if self.transport is None or not self.transport.configured:
            return None
        try:
            status = self.transport.status(self.native_id(device))
        except CloudError as exc:
            self._log('Could not read %s: %s' % (device.name, exc))
            return None
        return self._state_from_status(status, lock=is_lock(device))

    def get_states(self, devices, timeout=3.0):
        """Read every listed cover.

        One request each: the API has no batch read. There are two blinds in a
        room, not twenty lights, so this is a handful of calls -- and the
        transport throttles them so a status refresh cannot burn through the
        daily quota.
        """
        states = {}
        for device in devices:
            states[device.device_id] = self.get_state(device)
        return states

    # -- setup -------------------------------------------------------------

    def test_connection(self):
        """Whether the token and secret work, as (ok, message)."""
        if self.transport is None or not self.transport.configured:
            return False, 'No SwitchBot token and secret have been set'
        try:
            entries = self.transport.devices()
        except CloudError as exc:
            return False, str(exc)
        covers = len([e for e in entries
                      if (e.get('deviceType') or '') in COVER_TYPES])
        return True, ('SwitchBot answered: %d device(s), %d blind(s) or '
                      'shade(s)' % (len(entries), covers))
