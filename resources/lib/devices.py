# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

One device abstraction over the two Govee transports.

Discovery runs against both the LAN and the cloud and merges the results on the
Govee device id, so a light that answers on both is a single entry the user can
act on. Which transport a command actually goes out on is decided per device at
send time: LAN when it is available (instant, unmetered), cloud otherwise.
"""

from govee_cloud import CloudError, CloudTransport, RateLimited
from govee_lan import (LANError, LANTransport, brightness_message,
                       color_message, color_temp_message, turn_message)

TRANSPORT_AUTO = 'auto'
TRANSPORT_LAN = 'lan'
TRANSPORT_CLOUD = 'cloud'

DEFAULT_TEMP_MIN = 2000
DEFAULT_TEMP_MAX = 9000

DEVICE_CACHE = 'devices.json'

# What a device can be asked to do. A driver reports these per device, and the
# scene engine skips anything a device does not claim -- which is how one
# scene can drive colour bulbs, plain plugs and IR blasters without knowing
# what any of them are.
CAP_POWER = 'power'
CAP_BRIGHTNESS = 'brightness'
CAP_COLOR = 'color'
CAP_COLOR_TEMP = 'color_temp'
CAP_STATE = 'state'        # can report what it is currently doing
CAP_COMMANDS = 'commands'  # emits named commands, e.g. a learned IR code
CAP_POSITION = 'position'  # opens and closes to a percentage, e.g. a blind
# Throws a deadbolt.
CAP_LOCK = 'lock'
# Withdraws one. Separate from CAP_LOCK rather than folded into it, because the
# two are not the same permission: locking a door is always safe and unlocking
# one is the single most consequential thing this add-on can do. Kept apart so
# that "can this be locked" and "may this be unlocked" are different questions
# with different answers, and so the second can be switched off per box while
# the first stays on.
CAP_UNLOCK = 'unlock'

# Devices cached before drivers existed have no driver recorded; they are all
# Govee, because that is all there was.
DEFAULT_DRIVER = 'govee'


def describe_state(state):
    """What a device says it is doing, as lines to show. [] if it said nothing.

    Built from what the state actually holds rather than from a fixed shape.
    The fixed shape was a bulb's -- power, brightness, colour, temperature --
    written when every device here was a Govee light, and it stayed that shape
    while blinds and then a deadbolt were added under it. Both reported
    perfectly well and both were shown as "Power: unknown", because neither has
    a power and nothing asked what they did have.

    Order is most-wanted first: what you came to the screen to find out.
    """
    if not isinstance(state, dict):
        return []
    lines = []

    bolt = state.get('lock')
    if bolt:
        # Said in capitals because it is the one answer worth crossing a room
        # over: a bolt that fouled the strike plate has not locked.
        lines.append('Lock: %s' % ('JAMMED' if bolt == 'jammed'
                                   else str(bolt).capitalize()))
    if state.get('door'):
        lines.append('Door: %s' % state['door'])
    if state.get('position') is not None:
        lines.append('Open: %s%%' % state['position'])
    if state.get('moving'):
        lines.append('Moving now')
    if state.get('power'):
        lines.append('Power: %s' % state['power'])
    if state.get('brightness') is not None:
        lines.append('Brightness: %s%%' % state['brightness'])

    color = state.get('color')
    if isinstance(color, dict) and any(color.get(k) for k in 'rgb'):
        lines.append('Colour: RGB %s, %s, %s'
                     % (color.get('r', 0), color.get('g', 0),
                        color.get('b', 0)))
    if state.get('colorTem'):
        lines.append('Temperature: %sK' % state['colorTem'])
    if state.get('battery') is not None:
        lines.append('Battery: %s%%' % state['battery'])
    if state.get('source'):
        lines.append('Read over: %s' % str(state['source']).upper())
    return lines


class ControlError(Exception):
    """A command could not be delivered to a device."""


def _short_id(device_id):
    """Last two octets of a Govee id, used to label unnamed LAN devices."""
    parts = [p for p in (device_id or '').split(':') if p]
    return ''.join(parts[-2:]) if parts else '????'


class Device(object):
    """A single Govee light, reachable over LAN, cloud, or both."""

    def __init__(self, device_id, name='', model='', ip='', lan=False,
                 cloud=False, supports=None, temp_range=None, enabled=True,
                 driver=DEFAULT_DRIVER, devtype=None, native_id=None,
                 driver_data=None, power_only=False):
        # device_id is upper-cased so matching is case-insensitive, which is
        # what a Govee or Broadlink MAC wants. Tuya ids are lower-case strings
        # that go on the wire verbatim, and upper-casing one would produce an
        # id the device does not recognise -- so the original is kept too, and
        # a driver that needs the exact bytes uses native_id.
        self.device_id = (device_id or '').upper()
        self.native_id = native_id or device_id or ''
        self.driver = driver or DEFAULT_DRIVER
        self.model = model or ''
        self.ip = ip or ''
        self.lan = bool(lan)
        self.cloud = bool(cloud)
        self.supports = list(supports or [])
        self.temp_range = temp_range or [DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX]
        self.enabled = bool(enabled)
        # Switched by this add-on, but never coloured or dimmed by it: a
        # strip that is driven by something else, or one whose colour is set
        # once by hand and meant to stay put. The device still answers to
        # power and still reports its state; it simply stops being a light as
        # far as everything that decides how the room looks is concerned, and
        # so is passed over by scenes exactly as a plug is. See
        # Hub.capabilities, which is where this is actually enforced.
        self.power_only = bool(power_only)
        # Vendor type code, when the driver needs one on the wire. Broadlink
        # puts it in every packet header, so it has to survive a restart --
        # guessing it would build packets the device ignores.
        self.devtype = devtype
        # Anything else a driver must remember about this device across a
        # restart, in that driver's own terms -- a Tuya protocol version, or
        # which outlet of a multi-outlet plug this entry is. Kept as an opaque
        # dict so a new driver needs no change here.
        self.driver_data = dict(driver_data or {})
        self.name = name or self._fallback_name()

    def _fallback_name(self):
        """LAN discovery has no friendly name, so build one from sku + id."""
        model = self.model or 'Govee'
        return '%s (%s)' % (model, _short_id(self.device_id))

    # -- capability checks -------------------------------------------------

    def supports_cmd(self, name):
        """Whether `name` is usable on this device.

        Only the cloud API reports a capability list. LAN devices are assumed
        capable: the LAN protocol has no discovery for this, and an unsupported
        command is simply ignored by the device rather than being an error.
        """
        if not self.supports:
            return True
        return name in self.supports

    def transports(self):
        available = []
        if self.lan:
            available.append(TRANSPORT_LAN)
        if self.cloud:
            available.append(TRANSPORT_CLOUD)
        return available

    # -- serialisation -----------------------------------------------------

    def to_dict(self):
        return {
            'driver': self.driver,
            'device_id': self.device_id,
            'name': self.name,
            'model': self.model,
            'ip': self.ip,
            'lan': self.lan,
            'cloud': self.cloud,
            'supports': self.supports,
            'temp_range': self.temp_range,
            'enabled': self.enabled,
            'power_only': self.power_only,
            'devtype': self.devtype,
            'native_id': self.native_id,
            'driver_data': self.driver_data,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            driver=data.get('driver') or DEFAULT_DRIVER,
            device_id=data.get('device_id', ''),
            name=data.get('name', ''),
            model=data.get('model', ''),
            ip=data.get('ip', ''),
            lan=data.get('lan', False),
            cloud=data.get('cloud', False),
            supports=data.get('supports'),
            temp_range=data.get('temp_range'),
            enabled=data.get('enabled', True),
            power_only=data.get('power_only', False),
            devtype=data.get('devtype'),
            native_id=data.get('native_id'),
            driver_data=data.get('driver_data'),
        )

    def merge(self, other):
        """Fold another discovery result for the same device into this one."""
        self.lan = self.lan or other.lan
        self.cloud = self.cloud or other.cloud
        self.ip = other.ip or self.ip
        self.model = self.model or other.model
        self.supports = other.supports or self.supports
        if other.driver_data:
            self.driver_data = dict(other.driver_data)
        if other.temp_range:
            self.temp_range = other.temp_range
        # A cloud name is the one the user chose in the Govee app, so it wins
        # over the placeholder LAN discovery had to invent.
        if other.cloud and other.name and not other.name.startswith(
                other.model + ' ('):
            self.name = other.name
        return self

    def __repr__(self):
        return '<Device %s/%s %s via %s>' % (
            self.driver, self.device_id, self.name,
            '+'.join(self.transports()) or 'none')


class GoveeController(object):
    """The Govee driver: discovers Govee devices and drives them.

    This is one implementation of the driver contract the Hub routes to --
    discover, capabilities, the state verbs, and optionally commands. A second
    vendor is a second class with the same surface, not a change here.
    """

    DRIVER_ID = 'govee'
    DRIVER_LABEL = 'Govee'
    # Govee is the only driver that can reach a device more than one way, so
    # it is the only one where showing which is in use tells you anything.
    HAS_TRANSPORTS = True

    @staticmethod
    def capabilities(device):
        """What this device can be asked to do.

        Only the Govee cloud reports a capability list. LAN devices claim
        everything: the protocol has no discovery for it, and an unsupported
        command is ignored by the bulb rather than being an error.
        """
        caps = set([CAP_STATE])
        for capability, command in ((CAP_POWER, 'turn'),
                                    (CAP_BRIGHTNESS, 'brightness'),
                                    (CAP_COLOR, 'color'),
                                    (CAP_COLOR_TEMP, 'colorTem')):
            if device.supports_cmd(command):
                caps.add(capability)
        return caps

    @staticmethod
    def commands(device):
        """Govee bulbs emit nothing; an IR blaster would list codes here."""
        return []

    def send_command(self, device, name):
        raise ControlError('%s does not send commands' % device.name)

    def __init__(self, lan=None, cloud=None, mode=TRANSPORT_AUTO,
                 log_func=None):
        self.lan = lan
        self.cloud = cloud
        self.mode = mode or TRANSPORT_AUTO
        self._log = log_func or (lambda message: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Return merged devices plus a list of human-readable warnings.

        Errors from one transport never abort the other: a missing API key
        should not stop LAN lights being found, and a firewalled UDP port
        should not hide cloud lights.
        """
        merged = {}
        warnings = []

        if self.mode in (TRANSPORT_AUTO, TRANSPORT_LAN) and self.lan:
            try:
                for raw in self.lan.discover(timeout=timeout):
                    device = Device(
                        driver=self.DRIVER_ID,
                        device_id=raw.get('device', ''),
                        model=raw.get('sku', ''),
                        ip=raw.get('ip', ''),
                        lan=True,
                    )
                    if device.device_id:
                        merged[device.device_id] = device
            except LANError as exc:
                warnings.append(str(exc))
                self._log('LAN discovery failed: %s' % exc)

        if self.mode in (TRANSPORT_AUTO, TRANSPORT_CLOUD) and self.cloud \
                and self.cloud.configured:
            try:
                for raw in self.cloud.list_devices():
                    properties = raw.get('properties') or {}
                    temp = (properties.get('colorTem') or {}).get('range') or {}
                    device = Device(
                        driver=self.DRIVER_ID,
                        device_id=raw.get('device', ''),
                        name=raw.get('deviceName', ''),
                        model=raw.get('model', ''),
                        cloud=bool(raw.get('controllable', True)),
                        supports=raw.get('supportCmds'),
                        temp_range=[temp.get('min', DEFAULT_TEMP_MIN),
                                    temp.get('max', DEFAULT_TEMP_MAX)],
                    )
                    if not device.device_id:
                        continue
                    existing = merged.get(device.device_id)
                    if existing:
                        existing.merge(device)
                    else:
                        merged[device.device_id] = device
            except (CloudError, RateLimited) as exc:
                warnings.append(str(exc))
                self._log('Cloud discovery failed: %s' % exc)

        devices = sorted(merged.values(), key=lambda d: d.name.lower())
        return devices, warnings

    def locate(self, devices, timeout=3.0):
        """Where the listed devices are on the LAN right now: {id: ip}.

        The LAN half of discovery and nothing else. The cloud does not know
        an address, so asking it would spend a request from the daily
        allowance to learn nothing -- and this is asked every time a light
        gives no reading, which a light switched off at the wall does on
        every sweep.
        """
        if not self.lan or self.mode == TRANSPORT_CLOUD:
            return {}
        wanted = set(device.device_id for device in devices)
        where = {}
        for raw in self.lan.discover(timeout=timeout):
            device_id = (raw.get('device') or '').upper()
            if device_id in wanted and raw.get('ip'):
                where[device_id] = raw['ip']
        return where

    # -- transport selection ----------------------------------------------

    def pick_transport(self, device):
        """Decide how to reach `device`, honouring the user's mode setting."""
        if self.mode == TRANSPORT_LAN:
            return TRANSPORT_LAN if device.lan else None
        if self.mode == TRANSPORT_CLOUD:
            return TRANSPORT_CLOUD if device.cloud else None
        if device.lan and device.ip and self.lan:
            return TRANSPORT_LAN
        if device.cloud and self.cloud and self.cloud.configured:
            return TRANSPORT_CLOUD
        # A LAN device with no usable cloud fallback is still worth trying.
        if device.lan and self.lan:
            return TRANSPORT_LAN
        return None

    def _send(self, device, lan_message, cloud_name, cloud_value):
        transport = self.pick_transport(device)
        if transport is None:
            raise ControlError('%s is not reachable. Run a device refresh, or '
                               'set a Govee API key for cloud control.'
                               % device.name)

        if transport == TRANSPORT_LAN:
            if not device.ip:
                raise ControlError('%s has no known IP address; refresh '
                                   'devices.' % device.name)
            if self.lan.send(device.ip, lan_message):
                return TRANSPORT_LAN
            # LAN is best-effort. If the datagram could not even be handed to
            # the OS, fall through to the cloud rather than failing outright.
            if self.mode == TRANSPORT_AUTO and device.cloud and self.cloud \
                    and self.cloud.configured:
                self._log('LAN send to %s failed, retrying via cloud'
                          % device.name)
            else:
                raise ControlError('Could not reach %s over the LAN'
                                   % device.name)

        try:
            self.cloud.control(device.device_id, device.model, cloud_name,
                               cloud_value)
        except RateLimited as exc:
            raise ControlError(str(exc))
        except CloudError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return TRANSPORT_CLOUD

    # -- commands ----------------------------------------------------------

    def turn(self, device, on):
        return self._send(device, turn_message(on), 'turn',
                          'on' if on else 'off')

    def set_brightness(self, device, percent):
        percent = max(1, min(100, int(percent)))
        return self._send(device, brightness_message(percent), 'brightness',
                          percent)

    def set_color(self, device, red, green, blue):
        rgb = [max(0, min(255, int(v))) for v in (red, green, blue)]
        return self._send(device, color_message(*rgb), 'color',
                          {'r': rgb[0], 'g': rgb[1], 'b': rgb[2]})

    def set_color_temp(self, device, kelvin):
        low, high = device.temp_range or [DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX]
        kelvin = max(int(low), min(int(high), int(kelvin)))
        return self._send(device, color_temp_message(kelvin), 'colorTem',
                          kelvin)

    @staticmethod
    def lan_state(raw):
        """Normalise a LAN devStatus payload."""
        return {
            'power': 'on' if raw.get('onOff') else 'off',
            'brightness': raw.get('brightness'),
            'color': raw.get('color'),
            'colorTem': raw.get('colorTemInKelvin'),
            'source': TRANSPORT_LAN,
        }

    @staticmethod
    def cloud_state(raw):
        """Normalise a cloud state payload onto the same keys as the LAN one.

        The cloud calls it `powerState`, the LAN calls it `onOff`. Leaving the
        two shapes different pushes that difference onto every caller, so it
        is reconciled once, here.
        """
        power = raw.get('powerState')
        if power not in ('on', 'off'):
            power = None
        return {
            'power': power,
            'brightness': raw.get('brightness'),
            'color': raw.get('color'),
            'colorTem': raw.get('colorTem'),
            'online': raw.get('online'),
            'source': TRANSPORT_CLOUD,
        }

    def get_state(self, device):
        """Best-effort current state as a dict, or None if unavailable."""
        transport = self.pick_transport(device)
        if transport == TRANSPORT_LAN and device.ip:
            state = self.lan.status(device.ip)
            if state is not None:
                return self.lan_state(state)
            if self.mode != TRANSPORT_AUTO or not device.cloud:
                return None

        if device.cloud and self.cloud and self.cloud.configured:
            try:
                state = self.cloud.state(device.device_id, device.model)
            except (CloudError, RateLimited) as exc:
                self._log('State lookup failed for %s: %s' % (device.name, exc))
                return None
            return self.cloud_state(state)
        return None

    def get_states(self, devices, timeout=3.0):
        """Read many devices at once. Returns {device_id: state or None}.

        LAN devices are swept in a single pass; anything left over falls back
        to a per-device read. With 25 bulbs that is the difference between a
        few seconds and the better part of a minute.
        """
        states = {}
        lan_devices = []

        for device in devices:
            states[device.device_id] = None
            if self.pick_transport(device) == TRANSPORT_LAN and device.ip:
                lan_devices.append(device)

        if lan_devices and self.lan:
            by_ip = self.lan.status_many([d.ip for d in lan_devices],
                                         timeout=timeout)
            for device in lan_devices:
                raw = by_ip.get(device.ip)
                if raw is not None:
                    self._log('State of %s: %s' % (device.name, raw))
                    states[device.device_id] = self.lan_state(raw)
                else:
                    self._log('No state from %s (%s)'
                              % (device.name, device.ip))

        # Only non-LAN devices fall back to an individual read. Retrying a
        # silent LAN device here would re-bind port 4002 once per device and
        # sit out a timeout each time -- with 25 bulbs that is half a minute
        # of the UI apparently hung. status_many already retries internally,
        # so a device still missing at this point is genuinely not answering.
        for device in devices:
            if states[device.device_id] is None and device not in lan_devices:
                states[device.device_id] = self.get_state(device)

        answered = len([s for s in states.values() if s])
        self._log('Read state from %d of %d device(s)'
                  % (answered, len(devices)))
        return states


def build_controller(settings):
    """Assemble the Govee driver from a plain settings dict.

    Kept free of Kodi imports so the whole control path can be exercised in
    tests without a running Kodi.
    """
    lan = LANTransport(
        bind_address=settings.get('bind_address', ''),
        retries=settings.get('command_retries', 2),
        log_func=settings.get('log_func'),
    )
    cloud = CloudTransport(
        api_key=settings.get('api_key', ''),
        timeout=settings.get('cloud_timeout', 10),
        verify_ssl=settings.get('verify_ssl', True),
        log_func=settings.get('log_func'),
    )
    return GoveeController(
        lan=lan,
        cloud=cloud,
        mode=settings.get('mode', TRANSPORT_AUTO),
        log_func=settings.get('log_func'),
    )


def build_hub(settings):
    """The device layer: every driver, addressed as one.

    A vendor is added here and nowhere else -- the registry, the scene engine
    and the menus go through the Hub and never learn what a Govee bulb is.
    """
    from broadlink_driver import BroadlinkDriver
    from broadlink_lan import BroadlinkTransport
    from hub import Hub

    drivers = [build_controller(settings)]

    if settings.get('tuya_enabled', True):
        from tuya_driver import TuyaDriver
        drivers.append(TuyaDriver(
            keys=settings.get('tuya_keys'),
            save_keys=settings.get('save_tuya_keys'),
            log_func=settings.get('log_func')))

    if settings.get('kasa_enabled', True):
        from kasa_driver import KasaDriver
        drivers.append(KasaDriver(log_func=settings.get('log_func'),
                                  known_ips=settings.get('known_ips'),
                                  username=settings.get('kasa_username', ''),
                                  password=settings.get('kasa_password', '')))

    # Only built when there are credentials to build it with. Every other
    # driver can search a network that may have nothing on it; this one would
    # be a signed request to SwitchBot that is certain to be refused.
    if settings.get('switchbot_token') and settings.get('switchbot_secret'):
        from switchbot_cloud import SwitchBotTransport
        from switchbot_driver import SwitchBotDriver
        drivers.append(SwitchBotDriver(
            transport=SwitchBotTransport(
                token=settings.get('switchbot_token', ''),
                secret=settings.get('switchbot_secret', ''),
                timeout=settings.get('cloud_timeout', 10),
                verify_ssl=settings.get('verify_ssl', True),
                log_func=settings.get('log_func')),
            log_func=settings.get('log_func')))

    if settings.get('broadlink_enabled', True):
        drivers.append(BroadlinkDriver(
            transport=BroadlinkTransport(
                bind_address=settings.get('bind_address', ''),
                timeout=settings.get('broadlink_timeout', 5),
                log_func=settings.get('log_func')),
            codes=settings.get('broadlink_codes'),
            save_codes=settings.get('save_broadlink_codes'),
            log_func=settings.get('log_func')))

    if settings.get('speaker_enabled', True):
        # A speaker is a blaster that emits sound: same capability, same
        # step, and its clips are kept the way learned codes are.
        from speaker_driver import SpeakerDriver
        from speaker_lan import SpeakerTransport
        drivers.append(SpeakerDriver(
            transport=SpeakerTransport(
                bind_address=settings.get('bind_address', ''),
                timeout=settings.get('speaker_timeout', 5),
                log_func=settings.get('log_func')),
            clips=settings.get('speaker_clips'),
            save_clips=settings.get('save_speaker_clips'),
            log_func=settings.get('log_func')))

    # Off unless this box has been told otherwise. A house has several Kodi
    # boxes and they all show the same menus, so the question is not "may this
    # house unlock the front door" but "may it be unlocked from this room".
    return Hub(drivers=drivers, log_func=settings.get('log_func'),
               allow_unlock=bool(settings.get('allow_unlock', False)),
               confirm_unlock=bool(settings.get('confirm_unlock', False)),
               on_moved=settings.get('on_moved'))
