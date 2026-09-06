# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The Tuya driver: smart plugs and switches from the Tuya OEM family, which
covers GHome, Gosund, Smart Life and most no-name plugs.

A plug is the simplest device shape so far: power, and nothing else. What is
not simple is permission. Every Tuya device needs its own *local key*, which
lives in Tuya's cloud and is never offered by the device, so a plug can be
discovered and identified long before it can be switched.

That shapes the driver: a device with no key is still a real, listed,
nameable device. It simply reports that it needs one, rather than being
hidden or looking broken.

The other thing a plug is not, sometimes, is one plug. A multi-outlet strip
is several independent switches in one box, and listing it as a single device
would mean a scene could only turn all of it on or all of it off. So a keyed
plug is asked what it has and listed as one device per outlet.
"""

import tuya_lan
from devices import (CAP_COMMANDS, CAP_POSITION, CAP_POWER, CAP_STATE,
                     ControlError, Device)

KEY_FILE = 'tuya_keys.json'

# Tuya's socket instruction set is fixed across the OEM brands: mains outlets
# occupy datapoints 1-6 and USB banks 7-8, whatever the plug is sold as.
# Everything from 9 up is countdowns, child lock, relay memory and the rest --
# which is what keeps a child-lock toggle from being listed as an outlet.
OUTLET_DPS = ('1', '2', '3', '4', '5', '6')
USB_DPS = ('7', '8')
SWITCH_DPS = OUTLET_DPS + USB_DPS

# Our own marker for the whole-plug entry, not a datapoint. A multi-outlet
# plug has no master relay -- there is nothing in its instruction set that
# switches the box as a unit, and the phone app's "all off" is simply every
# outlet set in one command. This does the same, in one packet, which is both
# faster than four commands and simultaneous rather than staggered.
MASTER_DP = 'all'
MASTER_LABEL = 'All outlets'

# What the plug does when mains power comes back after a cut. Datapoint 38 in
# Tuya's socket instruction set, and a property of the whole plug rather than
# of one outlet -- there is one relay memory in the box, however many sockets
# it has.
POWER_MEMORY_DP = '38'
POWER_MEMORY = (('Stay off', 'power_off'),
                ('Come back on', 'power_on'),
                ('Remember how it was', 'last'))
POWER_MEMORY_VALUES = tuple(value for _label, value in POWER_MEMORY)

# A cover -- a roller shade, a curtain motor -- speaks a different instruction
# set through the same datapoints. Datapoint 1 is the giveaway and the reason
# no product-id table is needed: on a plug it is a bool, and on a cover it is
# one of these three strings. Reading the type is what tells the two apart,
# the same way a multi-outlet plug is found by asking rather than by model.
COVER_CONTROL_DP = '1'      # 'open' / 'stop' / 'close'
COVER_TARGET_DP = '2'       # where it is being sent, 0-100
COVER_POSITION_DP = '3'     # where it actually is, 0-100
COVER_BATTERY_DP = '13'     # percent, on the battery-powered ones

COVER_OPEN = 'open'
COVER_STOP = 'stop'
COVER_CLOSE = 'close'
COVER_CONTROLS = (COVER_OPEN, COVER_STOP, COVER_CLOSE)

# Offered as named commands as well as a position, because "stop" has no
# percentage: it means wherever it has got to.
COVER_COMMANDS = (('Open', COVER_OPEN),
                  ('Stop', COVER_STOP),
                  ('Close', COVER_CLOSE))


def looks_like_a_cover(dps):
    """Whether this device's datapoints are a shade's rather than a plug's."""
    return (dps or {}).get(COVER_CONTROL_DP) in COVER_CONTROLS


def clean_position(value):
    """A percentage a cover will accept, or a refusal saying why."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise ControlError('%r is not a position' % (value,))
    return max(0, min(100, number))


def outlet_label(dp):
    """A human name for one switchable datapoint."""
    dp = str(dp)
    if dp == MASTER_DP:
        return MASTER_LABEL
    if dp in USB_DPS:
        position = USB_DPS.index(dp)
        return 'USB' if position == 0 else 'USB %d' % (position + 1)
    if dp in OUTLET_DPS:
        return 'Outlet %d' % (OUTLET_DPS.index(dp) + 1)
    return 'Switch %s' % dp


class TuyaDriver(object):
    """Discovers Tuya devices and switches the ones that have a key."""

    DRIVER_ID = 'tuya'
    DRIVER_LABEL = 'Tuya'

    def __init__(self, keys=None, log_func=None, save_keys=None,
                 timeout=5.0):
        # {device_id: local key}
        self.keys = keys if keys is not None else {}
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self._save_keys = save_keys or (lambda: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Listen for Tuya announcements.

        Tuya devices broadcast rather than answer, so the wait is at least one
        announce interval. A short discovery timeout tuned for Govee's
        request/response sweep would simply hear nothing.
        """
        listen = max(6.0, float(timeout) * 2)
        try:
            heard = tuya_lan.discover(timeout=listen, log_func=self._log)
        except tuya_lan.TuyaError as exc:
            return [], [str(exc)]

        devices = []
        unkeyed = 0
        unsupported = []
        for entry in heard:
            found = self._devices_for(entry)
            devices.extend(found)
            if self.needs_key(found[0]):
                unkeyed += 1
            else:
                note = tuya_lan.version_note(self.version_of(found[0]))
                if note:
                    unsupported.append(found[0].name)

        warnings = []
        if unkeyed:
            warnings.append(
                '%d Tuya device(s) need a local key before they can be '
                'switched. Manage devices -> the plug -> Set local key.'
                % unkeyed)
        if unsupported:
            warnings.append(
                '%d Tuya device(s) speak a protocol version this add-on '
                'cannot drive yet: %s.'
                % (len(unsupported), ', '.join(sorted(unsupported))))
        return devices, warnings

    def _build_device(self, entry, dp=None, members=None):
        """One listed device: the whole plug, one outlet, or all of them."""
        device_id = entry['device_id']
        version = str(entry.get('version') or '3.3')
        data = {'version': version}
        name = ''
        if dp:
            data['dp'] = dp
            name = 'Tuya %s %s' % (device_id[-4:].upper(), outlet_label(dp))
        if members:
            # Recorded rather than re-derived: which outlets a plug has is
            # only knowable by asking it, and the master has to keep working
            # after a restart without another round trip.
            data['members'] = list(members)
        return Device(
            driver=self.DRIVER_ID,
            device_id='%s#%s' % (device_id, dp) if dp else device_id,
            native_id=device_id,
            name=name,
            model='Tuya %s' % version,
            ip=entry.get('ip', ''),
            lan=True,
            driver_data=data,
        )

    def _devices_for(self, entry):
        """List one entry per outlet, or one for the plug as a whole.

        Which datapoints a plug actually has can only be learnt by asking it,
        and asking needs the key -- so an unkeyed plug is listed as one device
        and splits itself on the first search after a key is entered. The
        alternative, splitting on the product id, would mean carrying a table
        of every plug ever made and being wrong about the ones not in it.

        Anything that answers with a single switch stays a single device: a
        one-outlet plug should not be called "Outlet 1".
        """
        base = self._build_device(entry)
        if self.needs_key(base) or tuya_lan.version_note(self.version_of(base)):
            return [base]

        try:
            dps = self._session(base).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            self._log('Could not read %s to find its outlets: %s'
                      % (base.name, exc))
            return [base]

        if looks_like_a_cover(dps):
            # A shade is one device however many datapoints it answers with,
            # and it is never split into outlets. Recorded on the device so
            # capabilities() -- which gets no session and cannot ask again --
            # still knows what this is after a restart.
            base.driver_data['cover'] = True
            base.name = 'Tuya %s Shade' % base.native_id[-4:].upper()
            self._log('%s is a cover, not a plug' % base.name)
            return [base]

        switches = [dp for dp in SWITCH_DPS if isinstance(dps.get(dp), bool)]
        if len(switches) < 2:
            return [base]
        self._log('%s has %d switchable outlets' % (base.name, len(switches)))
        found = [self._build_device(entry, MASTER_DP, members=switches)]
        found.extend(self._build_device(entry, dp) for dp in switches)
        return found

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def is_cover(device):
        """Whether this entry is a shade rather than a plug.

        Read off the device rather than the wire: capabilities() is asked
        constantly, by every menu and every scene, and a round trip per ask
        would put a Tuya packet behind drawing a list.
        """
        data = getattr(device, 'driver_data', None) or {}
        return bool(data.get('cover'))

    @classmethod
    def capabilities(cls, device):
        """A plug switches and reports. A shade goes to a percentage.

        CAP_BRIGHTNESS is deliberately absent from the cover set: it is what
        the scene engine reads to decide something is a light, and a shade
        being dimmed by Movie Night is not what anyone means.
        """
        if cls.is_cover(device):
            return set([CAP_POSITION, CAP_STATE, CAP_POWER, CAP_COMMANDS])
        return set([CAP_POWER, CAP_STATE])

    @classmethod
    def commands(cls, device):
        if cls.is_cover(device):
            return [label for label, _value in COVER_COMMANDS]
        return []

    def send_command(self, device, name):
        if self.is_cover(device):
            for label, value in COVER_COMMANDS:
                if label == name:
                    self._set_cover(device, {COVER_CONTROL_DP: value})
                    return
            raise ControlError('%s has no command called "%s"'
                               % (device.name, name))
        raise ControlError('%s does not send commands' % device.name)

    # -- covers ------------------------------------------------------------

    def _set_cover(self, device, dps):
        try:
            self._session(device).set_dps(dps)
        except tuya_lan.TuyaError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def set_position(self, device, percent):
        """Send a shade to `percent`. 0 is shut, 100 fully open.

        Datapoint 2 is the target; datapoint 3 is where it has got to. Writing
        the target and reading the position back is what makes the slider
        settle where the shade actually stopped rather than where it was
        asked to go.
        """
        if not self.is_cover(device):
            raise ControlError('%s is not a shade' % device.name)
        return self._set_cover(device, {COVER_TARGET_DP:
                                        clean_position(percent)})

    # -- power-cut memory --------------------------------------------------

    def power_memory(self, device):
        """What this plug does when mains power returns.

        Returns (value, [(label, value)]) or (None, []) when the plug does not
        carry the datapoint -- which is worth distinguishing from "off",
        because not every Tuya plug has a relay memory to set.
        """
        try:
            dps = self._session(device).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            self._log('Could not read the power memory of %s: %s'
                      % (device.name, exc))
            return None, []

        value = dps.get(POWER_MEMORY_DP)
        if value not in POWER_MEMORY_VALUES:
            return None, []
        return value, list(POWER_MEMORY)

    def set_power_memory(self, device, value):
        if value not in POWER_MEMORY_VALUES:
            raise ControlError('%s is not a power-cut setting' % value)
        try:
            self._session(device).set_dps({POWER_MEMORY_DP: value})
        except tuya_lan.TuyaError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def test_connection(self, device):
        """Read the device and report the result in words.

        Worth having for the moment straight after a 16 character key has been
        typed in on a remote control, which is the likeliest place for this to
        go wrong and the least obvious place to notice.
        """
        try:
            dps = self._session(device).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            return False, str(exc)

        state = self._state_from_dps(device, dps)
        if state is None:
            return False, ('%s answered, but datapoint %s is not a switch on '
                           'this device.\n\nIt reported: %s'
                           % (device.name, self.switch_dp(device),
                              ', '.join(sorted(str(k) for k in dps)) or 'nothing'))
        return True, ('%s answered and the key was accepted.\n\n'
                      'It is currently %s.' % (device.name, state['power']))

    # -- keys --------------------------------------------------------------

    @staticmethod
    def native_id(device):
        """The id Tuya itself uses -- lower case, exactly as broadcast."""
        return getattr(device, 'native_id', None) or device.device_id

    def local_key(self, device):
        return self.keys.get(self.native_id(device), '')

    def set_local_key(self, device, key):
        key = (key or '').strip()
        if not key:
            self.keys.pop(self.native_id(device), None)
            self._save_keys()
            return True
        if len(key) != 16:
            return False
        self.keys[self.native_id(device)] = key
        self._save_keys()
        self._log('Stored a local key for %s' % device.name)
        return True

    def needs_key(self, device):
        return len(self.local_key(device)) != 16

    @staticmethod
    def version_of(device):
        """The protocol version this device announced when it was found."""
        return str(getattr(device, 'driver_data', None)
                   and device.driver_data.get('version') or '3.3')

    @staticmethod
    def switch_dp(device):
        """Which datapoint this entry switches.

        A device listed before outlets were split -- or a plug with only one
        outlet -- has no datapoint recorded, and datapoint 1 is the switch on
        every single-outlet Tuya plug.
        """
        data = getattr(device, 'driver_data', None) or {}
        return str(data.get('dp') or tuya_lan.DP_SWITCH)

    def _session(self, device):
        if self.needs_key(device):
            raise ControlError(
                '%s has no local key yet.\n\nTuya devices never hand out '
                'their own key -- it has to be read from your Tuya account '
                'once. Use "Set local key" in Manage devices.' % device.name)
        return tuya_lan.Session(
            device.ip, self.native_id(device), self.local_key(device),
            version=self.version_of(device),
            timeout=self.timeout, log_func=self._log)

    # -- state verbs -------------------------------------------------------

    @classmethod
    def is_master(cls, device):
        return cls.switch_dp(device) == MASTER_DP

    @classmethod
    def member_dps(cls, device):
        """The datapoints a whole-plug entry covers.

        Falls back to every switch datapoint Tuya defines for a socket. An
        entry saved before members were recorded would otherwise switch
        nothing at all, and setting a datapoint a plug does not have is
        ignored by it rather than being an error.
        """
        data = getattr(device, 'driver_data', None) or {}
        members = [str(dp) for dp in (data.get('members') or [])]
        return members or list(SWITCH_DPS)

    def turn(self, device, on):
        # A shade has no relay to flip. "On" is open and "off" is shut, which
        # is what a caller that only knows how to switch things -- a sequence
        # step, the web remote's pair of buttons -- is asking for.
        if self.is_cover(device):
            return self._set_cover(
                device,
                {COVER_CONTROL_DP: COVER_OPEN if on else COVER_CLOSE})

        if self.is_master(device):
            wanted = dict((dp, bool(on)) for dp in self.member_dps(device))
        else:
            wanted = {self.switch_dp(device): bool(on)}
        try:
            self._session(device).set_dps(wanted)
        except tuya_lan.TuyaError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def set_brightness(self, device, percent):
        raise ControlError('%s has no brightness' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s has no colour' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s has no colour' % device.name)

    def _state_from_dps(self, device, dps):
        """This entry's switch, as the state dict the rest of the add-on uses."""
        if self.is_cover(device):
            return self._cover_state_from_dps(dps)

        if self.is_master(device):
            readings = [(dps or {}).get(dp) for dp in self.member_dps(device)]
            readings = [r for r in readings if isinstance(r, bool)]
            if not readings:
                return None
            # On if anything is drawing power. Requiring all of them would
            # report a plug with two of three outlets live as off, and the
            # button that follows would then turn it further on.
            value = any(readings)
        else:
            value = (dps or {}).get(self.switch_dp(device))
        if not isinstance(value, bool):
            return None
        # 'on'/'off' rather than a bool: that is the vocabulary the scene
        # engine reads, so a plug captured into a scene needs no special case.
        return {'power': 'on' if value else 'off', 'dps': dps}

    @staticmethod
    def _cover_state_from_dps(dps):
        """Where the shade is, as the state dict the rest of the add-on uses.

        Datapoint 3 is where it has got to; datapoint 2 is where it was last
        told to go. The real position is preferred, and the target is the
        fallback for the cheaper motors that report only what they were asked
        -- a shade that says 40 because that is what it was sent is still
        better than a slider with nothing in it.
        """
        dps = dps or {}
        for dp in (COVER_POSITION_DP, COVER_TARGET_DP):
            value = dps.get(dp)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            state = {'position': max(0, min(100, int(value))), 'dps': dps}
            battery = dps.get(COVER_BATTERY_DP)
            if isinstance(battery, (int, float)) \
                    and not isinstance(battery, bool):
                state['battery'] = int(battery)
            return state
        return None

    def get_state(self, device):
        try:
            dps = self._session(device).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            self._log('Could not read %s: %s' % (device.name, exc))
            return None
        return self._state_from_dps(device, dps)

    @classmethod
    def collapse(cls, devices):
        """Drop outlets whose whole-plug entry is in the same list.

        Only ever used where every target gets the identical instruction --
        "everything off" and the like. There the master covers its outlets in
        one packet, so sending one command per outlet as well is slower and
        no more correct. A scene, where outlets can be told different things,
        does not come through here.
        """
        masters = set(cls.native_id(d) for d in devices
                      if getattr(d, 'driver', None) == cls.DRIVER_ID
                      and cls.is_master(d))
        if not masters:
            return list(devices)
        return [d for d in devices
                if getattr(d, 'driver', None) != cls.DRIVER_ID
                or cls.is_master(d)
                or cls.native_id(d) not in masters]

    def get_states(self, devices, timeout=3.0):
        """Read every listed device, one round trip per physical plug.

        Outlets of the same plug share a status reply, so a three-outlet strip
        is one conversation rather than three -- which also keeps the three
        readings consistent with each other.
        """
        states = {}
        by_plug = {}
        order = []
        for device in devices:
            plug = self.native_id(device)
            if plug not in by_plug:
                by_plug[plug] = []
                order.append(plug)
            by_plug[plug].append(device)

        for plug in order:
            group = by_plug[plug]
            try:
                dps = self._session(group[0]).status()
            except (tuya_lan.TuyaError, ControlError) as exc:
                self._log('Could not read %s: %s' % (group[0].name, exc))
                dps = None
            for device in group:
                states[device.device_id] = self._state_from_dps(device, dps)
        return states
