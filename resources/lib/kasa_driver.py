# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The TP-Link Kasa driver: HS100, HS103, HS110 and the KP series.

The second plug driver, and the one that shows how much of Tuya's difficulty
was Tuya's rather than the shape of the problem. There is no local key to
fetch, no cloud account to link and no protocol version to negotiate: a plug
found is a plug that can be switched. Which means, unlike Tuya, discovery and
control arrive together -- there is no useful half-state where a device is
listed but not usable.

A Kasa device already knows its own name. Whatever it is called in the Kasa
app comes back in its system info, so these arrive named rather than as
"Kasa 1E3E" waiting to be identified one by one.
"""

import kasa_klap
import kasa_lan
from devices import CAP_POWER, CAP_STATE, ControlError, Device


class KasaDriver(object):
    """Discovers Kasa devices and switches them."""

    DRIVER_ID = 'kasa'
    DRIVER_LABEL = 'Kasa'

    def __init__(self, log_func=None, timeout=5.0, known_ips=None,
                 username='', password=''):
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        # A callable rather than a list: the device cache is loaded lazily and
        # the drivers are built before it is read.
        self._known_ips = known_ips or (lambda: [])
        # Only later hardware needs these, and only because TP-Link made a
        # local protocol check a cloud credential.
        self.username = username or ''
        self.password = password or ''
        self._last_session = None

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Broadcast for Kasa devices.

        The wait is longer than the caller's default: a broadcast that goes
        out of several interfaces wants time for the slowest to answer, and a
        plug behind a mesh access point is not always prompt.
        """
        listen = max(5.0, float(timeout) + 2.0)
        try:
            heard, counts = kasa_lan.search(timeout=listen,
                                            log_func=self._log,
                                            hints=self._known_ips())
        except kasa_lan.KasaError as exc:
            return [], [str(exc)]

        devices = []
        for entry in heard:
            devices.extend(self._devices_for(entry))

        warnings = []
        locked = [d for d in devices
                  if self.is_klap(d) and not self.has_credentials()]
        if locked:
            warnings.append(
                '%d Kasa device(s) are later hardware that needs your '
                'TP-Link account before they can be switched. Settings -> '
                'Kasa.' % len(locked))
        if counts.get('sweep'):
            # Worth saying rather than silently papering over: a network that
            # drops broadcast will keep doing it, and it explains why the Kasa
            # app -- which also broadcasts -- may show fewer devices too.
            warnings.append(
                '%d Kasa device(s) answered only when addressed directly. '
                'Your access point is dropping broadcast traffic.'
                % counts['sweep'])
        return devices, warnings

    def _devices_for(self, entry):
        """One entry per outlet, or one for a single-outlet plug.

        Multi-outlet Kasa hardware -- the KP303 and HS300 strips -- reports
        its outlets as `children` in system info, each with its own name and
        its own state. A single plug has no children and stays one device
        rather than being called "Outlet 1".
        """
        children = entry.get('children') or []
        if not children:
            return [self._build_device(entry)]

        found = [self._build_device(entry, child) for child in children]
        self._log('%s has %d outlets' % (entry.get('alias') or 'Kasa device',
                                         len(children)))
        return found

    def _build_device(self, entry, child=None):
        device_id = entry['device_id']
        data = {}
        # The name the device already answers to. Kasa devices are named in
        # the app at pairing time, so unlike a Govee bulb there is nothing to
        # work out by flashing it.
        name = entry.get('alias') or ''
        if child:
            data['child'] = child['id']
            name = child.get('alias') or ''
            if not name:
                name = '%s outlet' % (entry.get('alias') or 'Kasa')
        protocol = entry.get('protocol') or kasa_lan.PROTOCOL_LEGACY
        data['protocol'] = protocol
        if protocol == kasa_lan.PROTOCOL_KLAP:
            data['http_port'] = entry.get('http_port') or kasa_klap.HTTP_PORT
        if not name:
            # A KLAP announcement carries no alias -- the name lives behind
            # the very handshake we may not be able to complete yet.
            name = '%s (%s)' % (entry.get('model') or 'Kasa',
                                device_id[-4:].upper())
        return Device(
            driver=self.DRIVER_ID,
            device_id='%s#%s' % (device_id, child['id']) if child
            else device_id,
            native_id=device_id,
            name=name,
            model=entry.get('model') or 'Kasa',
            ip=entry.get('ip', ''),
            lan=True,
            driver_data=data,
        )

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def capabilities(device):
        """A plug switches, and reports whether it is on. That is all."""
        return set([CAP_POWER, CAP_STATE])

    @staticmethod
    def commands(device):
        return []

    def send_command(self, device, name):
        raise ControlError('%s does not send commands' % device.name)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def native_id(device):
        return getattr(device, 'native_id', None) or device.device_id

    @staticmethod
    def child_id(device):
        """Which outlet this entry is, or None for a whole plug."""
        data = getattr(device, 'driver_data', None) or {}
        return data.get('child') or None

    @staticmethod
    def is_klap(device):
        data = getattr(device, 'driver_data', None) or {}
        return data.get('protocol') == kasa_lan.PROTOCOL_KLAP

    def has_credentials(self):
        return bool(self.username and self.password)

    def _session(self, device):
        """The right client for whichever protocol this device speaks.

        Two generations of the same model number need entirely different
        transports, which is exactly why the protocol is recorded when the
        device is found rather than worked out again at each command.
        """
        if not device.ip:
            raise ControlError('%s has no address. Run a device refresh.'
                               % device.name)

        if not self.is_klap(device):
            return kasa_lan.Session(device.ip, timeout=self.timeout,
                                    log_func=self._log)

        if not self.has_credentials():
            raise ControlError(
                '%s is later Kasa hardware, which will not answer without '
                'the TP-Link account it is registered to.\n\nEnter that '
                'account under Settings -> Kasa. It is used on your own '
                'network only; nothing is sent to TP-Link.' % device.name)

        data = getattr(device, 'driver_data', None) or {}
        session = kasa_klap.Session(device.ip, self.username, self.password,
                                    port=data.get('http_port'),
                                    timeout=self.timeout, log_func=self._log)
        self._last_session = session
        return _KlapAdapter(session)

    # -- state verbs -------------------------------------------------------

    def turn(self, device, on):
        try:
            self._session(device).set_relay(bool(on), self.child_id(device))
        except (kasa_lan.KasaError, kasa_klap.KlapError) as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def set_brightness(self, device, percent):
        raise ControlError('%s has no brightness' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s has no colour' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s has no colour' % device.name)

    def _state_from_info(self, device, info):
        """This entry's switch, as the state dict the rest of the add-on uses."""
        if not info:
            return None
        child = self.child_id(device)
        if child:
            for entry in info.get('children') or []:
                found = entry.get('id') or ''
                if found == child or child.endswith(found):
                    value = entry.get('state')
                    break
            else:
                return None
        else:
            value = info.get('relay_state')

        if value not in (0, 1, True, False):
            return None
        return {'power': 'on' if value else 'off'}

    def get_state(self, device):
        try:
            info = self._session(device).info()
        except (kasa_lan.KasaError, kasa_klap.KlapError,
                ControlError) as exc:
            self._log('Could not read %s: %s' % (device.name, exc))
            return None
        return self._state_from_info(device, info)

    def locate(self, devices, timeout=3.0):
        """Where the listed devices are right now: {device_id: ip}.

        The same search a refresh runs, kept to the addresses. Nothing here
        leaves the LAN, so it costs only the wait.
        """
        found, _warnings = self.discover(timeout=timeout)
        wanted = set(device.device_id for device in devices)
        return dict((device.device_id, device.ip) for device in found
                    if device.ip and device.device_id in wanted)

    def get_states(self, devices, timeout=3.0):
        """Read every listed device, one round trip per physical plug.

        Outlets of the same strip share a reply, which also keeps their
        readings consistent with each other.
        """
        states = {}
        groups = {}
        order = []
        for device in devices:
            plug = self.native_id(device)
            if plug not in groups:
                groups[plug] = []
                order.append(plug)
            groups[plug].append(device)

        for plug in order:
            group = groups[plug]
            try:
                info = self._session(group[0]).info()
            except (kasa_lan.KasaError, kasa_klap.KlapError,
                ControlError) as exc:
                self._log('Could not read %s: %s' % (group[0].name, exc))
                info = None
            for device in group:
                states[device.device_id] = self._state_from_info(device, info)
        return states

    def _account_note(self, device):
        """Whether the entered account was what got in, for a KLAP device.

        Worth saying plainly. When some plugs work and others do not, the
        useful question is whether the ones that work actually needed the
        account -- a plug never bound to one answers regardless, so it proves
        nothing about the details being right.
        """
        if not self.is_klap(device) or self._last_session is None:
            return ''
        if self._last_session.used_account:
            return 'Your TP-Link account was accepted.'
        return ('This plug needed no account at all, so it does not tell you '
                'whether the details in Settings are right.')

    def test_connection(self, device):
        """Read the device and report the result in words."""
        try:
            info = self._session(device).info()
        except (kasa_lan.KasaError, kasa_klap.KlapError,
                ControlError) as exc:
            return False, str(exc)

        state = self._state_from_info(device, info)
        summary = '%s answered.\n\nModel %s, firmware %s.' % (
            device.name, info.get('model') or '?', info.get('sw_ver') or '?')
        note = self._account_note(device)
        if note:
            summary += '\n\n%s' % note
        if state is None:
            return True, summary + '\n\nIt did not report a switch state.'
        return True, summary + '\n\nIt is currently %s.' % state['power']


class _KlapAdapter(object):
    """Presents a KLAP session with the same surface as the legacy one.

    The JSON inside the envelope is identical between the two generations --
    the same get_sysinfo, the same set_relay_state. Only the way it travels
    changed, so only that is adapted here and the driver above stays free of
    the distinction.
    """

    def __init__(self, session):
        self.session = session

    def info(self):
        reply = self.session.request(kasa_lan.INFO_COMMAND)
        info = (reply.get('system') or {}).get('get_sysinfo')
        if not isinstance(info, dict):
            raise kasa_klap.KlapError(
                '%s did not report its system info' % self.session.ip)
        return info

    def set_relay(self, on, child_id=None):
        body = {'system': {'set_relay_state': {'state': 1 if on else 0}}}
        if child_id:
            body = dict(body)
            body['context'] = {'child_ids': [child_id]}
        reply = self.session.request(body)
        result = (reply.get('system') or {}).get('set_relay_state') or {}
        if result.get('err_code'):
            raise kasa_klap.KlapError(
                '%s refused the request (error %s)%s'
                % (self.session.ip, result['err_code'],
                   ': %s' % result['err_msg'] if result.get('err_msg') else ''))
        return True
