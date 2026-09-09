# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The Broadlink driver: RM-series IR/RF blasters as Paragon devices.

An RM is not a light. It has no colour, no brightness and nothing to switch;
what it has is a set of learned codes it can emit. So it claims only the
`commands` capability, and the scene engine reaches it through a scene's
actions rather than its settings.

Learned codes live in the add-on profile next to scenes and devices, keyed by
device id, so they survive upgrades and can be copied to another Kodi box.
"""

from broadlink_lan import BroadlinkError, BroadlinkTransport, Session
from devices import CAP_COMMANDS, ControlError, Device

CODE_FILE = 'broadlink_codes.json'


class BroadlinkDriver(object):
    """Discovers RM devices and fires their learned codes."""

    DRIVER_ID = 'broadlink'
    DRIVER_LABEL = 'Broadlink'

    def __init__(self, transport=None, codes=None, log_func=None,
                 save_codes=None):
        self.transport = transport or BroadlinkTransport(log_func=log_func)
        # {device_id: {command name: hex code}}
        self.codes = codes if codes is not None else {}
        self._log = log_func or (lambda message: None)
        self._save_codes = save_codes or (lambda: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        devices = []
        warnings = []
        try:
            found = self.transport.discover(timeout=timeout)
        except Exception as exc:
            return [], ['Broadlink search failed: %s' % exc]

        for entry in found:
            device = Device(
                driver=self.DRIVER_ID,
                device_id=entry['mac'],
                name=entry.get('name') or '',
                model=entry.get('label') or '',
                ip=entry.get('ip', ''),
                lan=True,
                devtype=entry.get('devtype'),
            )
            # mac_bytes is not persisted: device_id is the same six bytes as
            # a display string, so it can always be rebuilt from that.
            device.mac_bytes = entry.get('mac_bytes')
            devices.append(device)

        return devices, warnings

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def capabilities(device):
        """Emits commands, and nothing else. No colour, no state to read."""
        return set([CAP_COMMANDS])

    def commands(self, device):
        return sorted(self.codes.get(device.device_id, {}).keys())

    # -- state verbs, which an RM does not have ----------------------------

    def turn(self, device, on):
        raise ControlError('%s is a remote, not a switch' % device.name)

    def set_brightness(self, device, percent):
        raise ControlError('%s has no brightness' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s has no colour' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s has no colour' % device.name)

    def get_state(self, device):
        return None

    def get_states(self, devices, timeout=3.0):
        return dict((d.device_id, None) for d in devices)

    # -- sessions ----------------------------------------------------------

    @staticmethod
    def _mac_bytes(device):
        """The MAC as the wire wants it, rebuilt from the device id if needed."""
        mac_bytes = getattr(device, 'mac_bytes', None)
        if mac_bytes:
            return mac_bytes
        return bytearray(
            int(part, 16) for part in reversed(device.device_id.split(':'))
            if part)

    def _session(self, device):
        """An authenticated session for `device`.

        The session key is per-conversation and the device forgets it when it
        reboots, so a stale key is retried once from scratch rather than being
        reported as a dead device. A device that is locked to the Broadlink
        app refuses both attempts, and says so.
        """
        mac_bytes = self._mac_bytes(device)
        devtype = getattr(device, 'devtype', None) or 0x2712

        try:
            return self.transport.session(device.ip, mac_bytes, devtype)
        except BroadlinkError as exc:
            self.transport.forget_session(device.ip)
            self._log('Re-authenticating with %s after: %s'
                      % (device.name, exc))
            try:
                return self.transport.session(device.ip, mac_bytes, devtype)
            except BroadlinkError as retry:
                raise ControlError('%s: %s' % (device.name, retry))

    # -- commands ----------------------------------------------------------

    def send_command(self, device, name):
        stored = self.codes.get(device.device_id, {}).get(name)
        if not stored:
            raise ControlError('%s has no command called "%s"'
                               % (device.name, name))
        try:
            code = bytearray.fromhex(stored)
        except (ValueError, TypeError):
            raise ControlError('The saved code for "%s" is unreadable' % name)

        session = self._session(device)
        try:
            session.send_code(code)
        except BroadlinkError as exc:
            self.transport.forget_session(device.ip)
            raise ControlError('%s: %s' % (device.name, exc))
        self._log('Sent "%s" via %s' % (name, device.name))
        return True

    def test_connection(self, device):
        """Try to authenticate and report what happened, in words.

        Authentication is lazy -- it happens on the first real command -- so
        without this the only way to find out whether a device will talk to us
        is to try to learn a code and read the failure sideways.
        """
        try:
            session = self.transport.session(
                device.ip, self._mac_bytes(device),
                getattr(device, 'devtype', None) or 0x2712)
        except BroadlinkError as exc:
            return False, str(exc)
        return True, ('%s answered and accepted the handshake.\n\n'
                      'Device id %s' % (device.name,
                                        ''.join('%02x' % b for b
                                                in session.device_id)))

    # -- learning ----------------------------------------------------------

    def start_learning(self, device):
        session = self._session(device)
        try:
            session.enter_learning()
        except BroadlinkError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def start_rf_sweep(self, device):
        """Begin hunting for the frequency an RF remote transmits on.

        Radio takes two passes where infrared takes one -- sweep for the
        frequency, then listen on it -- so this is the first half. Only the
        Pro-class blasters have the radio; a Mini refuses, and the message
        says which it was rather than reporting a bare protocol error.
        """
        session = self._session(device)
        try:
            session.sweep_frequency()
        except BroadlinkError as exc:
            raise ControlError(
                '%s would not start an RF sweep. Only the Pro blasters have '
                'a radio -- a Mini can learn infrared only. (%s)'
                % (device.name, exc))
        return True

    def rf_frequency_found(self, device):
        """Whether the sweep has locked on. False means keep holding."""
        session = self._session(device)
        try:
            return session.check_frequency()
        except BroadlinkError:
            return False

    def start_rf_capture(self, device, frequency=None):
        """Listen for the code, on `frequency` in MHz if one is given.

        A frequency skips the sweep, which is the pass that needs the button
        held down beside the blaster. Only the RM4-class units accept one, so
        a refusal is reported as False rather than raised: the caller can then
        sweep instead, which every RF blaster can do. A refusal with no
        frequency asked for is a real failure and still raises.
        """
        session = self._session(device)
        try:
            session.find_rf_packet(frequency)
        except BroadlinkError as exc:
            if frequency:
                self._log('%s would not take a frequency directly (%s); '
                          'sweeping instead' % (device.name, exc))
                return False
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def cancel_rf_sweep(self, device):
        """Stop sweeping, so a cancelled learn does not leave it deaf.

        Never raises: this runs on the way out of a learn that has already
        gone wrong, and a failure here would replace the message explaining
        that with a less useful one.
        """
        try:
            self._session(device).cancel_sweep()
        except Exception:
            return False
        return True

    def collect_learned(self, device):
        """The code captured since learning started, as hex, or None.

        Shared by infrared and radio: once a code has been found, what comes
        back off the wire is the same either way.
        """
        session = self._session(device)
        try:
            code = session.check_learned()
        except BroadlinkError:
            return None
        if not code:
            return None
        return ''.join('%02x' % b for b in bytearray(code))

    def save_command(self, device, name, hex_code):
        name = (name or '').strip()
        if not name or not hex_code:
            return False
        self.codes.setdefault(device.device_id, {})[name] = hex_code
        self._save_codes()
        self._log('Learned "%s" on %s (%d bytes)'
                  % (name, device.name, len(hex_code) // 2))
        return True

    def forget_command(self, device, name):
        commands = self.codes.get(device.device_id) or {}
        if name in commands:
            del commands[name]
            self._save_codes()
            return True
        return False
