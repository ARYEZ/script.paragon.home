# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the Broadlink LAN protocol (RM series IR/RF blasters).

Three stages, all UDP:

    discovery -- a 48 byte hello broadcast to 255.255.255.255:80. Devices
                 answer with their type, MAC and name.
    auth      -- command 0x0065 encrypted with a fixed key, answered with a
                 per-session key and device id that every later packet uses.
    command   -- command 0x006a carrying an encrypted payload: send a code,
                 enter learning, or collect what was learned.

Every packet is AES-128-CBC encrypted and carries a checksum seeded at 0xbeaf.
The cipher is ours (resources/lib/aes.py) because Krypton has no crypto.

Protocol reference: https://github.com/mjg59/python-broadlink/blob/master/protocol.md
"""

import socket
import struct
import time

from aes import AES

BROADCAST_ADDRESS = '255.255.255.255'
BROADCAST_PORT = 80
DEVICE_PORT = 80

# The key and IV every device accepts before authentication. Not a secret --
# they are the same in every Broadlink device and are published in the
# protocol documentation; the per-session key that auth returns is the one
# that matters.
INITIAL_KEY = bytearray([0x09, 0x76, 0x28, 0x34, 0x3f, 0xe9, 0x9e, 0x23,
                         0x76, 0x5c, 0x15, 0x13, 0xac, 0xcf, 0x8b, 0x02])
INITIAL_IV = bytearray([0x56, 0x2e, 0x17, 0x99, 0x6d, 0x09, 0x3d, 0x28,
                        0xdd, 0xb3, 0xba, 0x69, 0x5a, 0x2e, 0x6f, 0x58])

CMD_AUTH = 0x0065
CMD_DATA = 0x006a

# Payload verbs inside a CMD_DATA packet.
DATA_SEND = 0x02
DATA_LEARN = 0x03
DATA_CHECK = 0x04
DATA_LEARN_RF_SWEEP = 0x19
DATA_CHECK_RF_FOUND = 0x1a
DATA_LEARN_RF_CODE = 0x1b
DATA_CANCEL_RF_SWEEP = 0x1e

# Devices that want the newer, length-prefixed data payload. The RM4 family
# and the later "RM Mini 3" (0x5f36) share it; RM2/RM3-era hardware, including
# the RM Pro, does not. The two are otherwise identical, which is why a device
# can authenticate perfectly and then refuse every command.
#
# This list is a starting guess, not the authority: an unknown device is
# retried with the other framing and remembers whichever works, because
# hardware sold as "RM Mini 3" turns out to span both.
NEW_FRAMING_TYPES = set([
    0x5f36,
    0x51da, 0x6026, 0x6070, 0x610e, 0x610f, 0x62bc, 0x62be,
    0x6364, 0x648d, 0x6507, 0x6508, 0x6539, 0x653a, 0x653c,
])


# Device type codes seen on RM-family hardware. Anything not listed still
# works if it speaks the protocol; this only improves the label.
DEVICE_TYPES = {
    0x2712: 'RM2', 0x2737: 'RM Mini', 0x273d: 'RM Pro Phicomm',
    0x2783: 'RM2 Home Plus', 0x277c: 'RM2 Home Plus GDT',
    0x272a: 'RM2 Pro Plus', 0x2787: 'RM2 Pro Plus2',
    0x278b: 'RM2 Pro Plus BL', 0x278f: 'RM Mini Shate',
    0x27c2: 'RM Mini 3', 0x27c7: 'RM Mini 3',  0x27c3: 'RM Pro+',
    0x27d1: 'RM Mini 3', 0x27de: 'RM Mini 3',
    0x51da: 'RM4 Mini', 0x5f36: 'RM Mini 3', 0x6026: 'RM4 Pro',
    0x6070: 'RM4C Mini', 0x610e: 'RM4 Mini', 0x610f: 'RM4C Mini',
    0x62bc: 'RM4 Mini', 0x62be: 'RM4C Mini', 0x6364: 'RM4S',
    0x648d: 'RM4 Mini', 0x6539: 'RM4C Mini', 0x653a: 'RM4 Mini',
    0x653c: 'RM4 Pro',
}


class BroadlinkError(Exception):
    """The device could not be reached or refused the request."""


# Device-reported errors, as signed 16 bit values. Only the ones whose
# meaning is well established are named; anything else is reported with its
# signed value, which is what the number is written as everywhere else and so
# what a search for it will match.
ERROR_NAMES = {
    -1: 'Authentication failed',
    -2: 'You have been logged out',
    -3: 'The device is offline',
    -4: 'Unknown error',
    -9: 'Control key is expired',
}

# What to do about the one people actually hit. A Broadlink device that is
# locked to the phone app refuses LAN authentication outright, which is
# indistinguishable from a wrong key unless it is spelled out.
AUTH_ADVICE = (
    'The device is almost certainly locked to the Broadlink app. Open the '
    'Broadlink app, go to the device, and turn off "Lock device". Some '
    'firmware also refuses LAN control while the app is holding the device, '
    'so close the app afterwards and try again.')


def error_text(code):
    """Human wording for a device error code, always including the number."""
    signed = code - 0x10000 if code >= 0x8000 else code
    name = ERROR_NAMES.get(signed)
    if name:
        return '%s (error %d)' % (name, signed)
    return 'error %d (0x%04x)' % (signed, code)


def is_auth_error(code):
    signed = code - 0x10000 if code >= 0x8000 else code
    return signed in (-1, -2, -9)


def device_label(devtype):
    return DEVICE_TYPES.get(devtype, 'Broadlink %04x' % devtype)


def checksum(data, seed=0xBEAF):
    """The protocol's 16 bit checksum, seeded at 0xbeaf and wrapping."""
    total = seed
    for byte in bytearray(data):
        total += byte
    return total & 0xFFFF


def build_hello(local_ip, local_port, now=None):
    """The 48 byte discovery broadcast.

    Carries the local clock and address so the device knows where to answer
    and can set its own time; that is why it is not a fixed constant blob.
    """
    packet = bytearray(0x30)
    stamp = time.localtime(now) if now is not None else time.localtime()

    offset_hours = int(-time.timezone // 3600)
    if offset_hours < 0:
        struct.pack_into('<i', packet, 0x08, offset_hours)
    else:
        packet[0x08] = offset_hours

    packet[0x0c] = stamp.tm_year & 0xFF
    packet[0x0d] = (stamp.tm_year >> 8) & 0xFF
    packet[0x0e] = stamp.tm_min
    packet[0x0f] = stamp.tm_hour
    packet[0x10] = int(str(stamp.tm_year)[2:])
    packet[0x11] = stamp.tm_wday
    packet[0x12] = stamp.tm_mday
    packet[0x13] = stamp.tm_mon

    try:
        packet[0x18:0x1c] = bytearray(socket.inet_aton(local_ip))
    except (socket.error, TypeError):
        pass
    packet[0x1c] = local_port & 0xFF
    packet[0x1d] = (local_port >> 8) & 0xFF
    packet[0x26] = 6

    total = checksum(packet)
    packet[0x20] = total & 0xFF
    packet[0x21] = (total >> 8) & 0xFF
    return bytes(packet)


def parse_hello_response(data, address):
    """Pull the device out of a discovery reply, or None if it is not one."""
    data = bytearray(data)
    if len(data) < 0x40:
        return None

    devtype = data[0x34] | (data[0x35] << 8)
    mac = data[0x3A:0x40]
    name = bytes(data[0x40:]).split(b'\x00')[0].decode('utf-8', 'replace')

    return {
        'ip': address,
        'devtype': devtype,
        'label': device_label(devtype),
        'mac': ':'.join('%02X' % b for b in reversed(mac)),
        'mac_bytes': bytes(mac),
        'name': name.strip(),
    }


def build_auth_payload(client_id='Paragon Home'):
    """The 0x50 byte payload of an auth request."""
    payload = bytearray(0x50)
    for index in range(0x04, 0x13):
        payload[index] = 0x31
    payload[0x1E] = 0x01
    payload[0x2D] = 0x01
    name = client_id.encode('utf-8')[:0x1F]
    payload[0x30:0x30 + len(name)] = bytearray(name)
    return payload


class Session(object):
    """One authenticated conversation with one Broadlink device."""

    def __init__(self, ip, mac_bytes, devtype, timeout=5.0, log_func=None,
                 new_framing=None):
        self.ip = ip
        self.mac = bytearray(mac_bytes)
        self.devtype = devtype
        # Which data-payload layout this device wants. Guessed from the type
        # code, then corrected by experience the first time a command fails.
        self.new_framing = (devtype in NEW_FRAMING_TYPES
                            if new_framing is None else bool(new_framing))
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self.key = bytearray(INITIAL_KEY)
        self.iv = bytearray(INITIAL_IV)
        self.device_id = bytearray(4)
        self.counter = 0
        self.authenticated = False

    # -- framing -----------------------------------------------------------

    def build_packet(self, command, payload):
        """Wrap an encrypted payload in the 0x38 byte header."""
        payload = bytearray(payload)
        if len(payload) % 16:
            payload.extend(bytearray(16 - (len(payload) % 16)))

        self.counter = (self.counter + 1) & 0xFFFF

        packet = bytearray(0x38)
        packet[0x00:0x08] = bytearray([0x5A, 0xA5, 0xAA, 0x55,
                                       0x5A, 0xA5, 0xAA, 0x55])
        packet[0x24] = self.devtype & 0xFF
        packet[0x25] = (self.devtype >> 8) & 0xFF
        packet[0x26] = command & 0xFF
        packet[0x27] = (command >> 8) & 0xFF
        packet[0x28] = self.counter & 0xFF
        packet[0x29] = (self.counter >> 8) & 0xFF
        packet[0x2A:0x30] = self.mac
        packet[0x30:0x34] = self.device_id

        # The payload checksum goes in before the payload is encrypted, and
        # the whole-packet checksum after it is appended. Getting these two in
        # the wrong order is the classic way to build a packet the device
        # silently ignores.
        payload_sum = checksum(payload)
        packet[0x34] = payload_sum & 0xFF
        packet[0x35] = (payload_sum >> 8) & 0xFF

        packet.extend(bytearray(AES(self.key, self.iv).encrypt(payload)))

        total = checksum(packet)
        packet[0x20] = total & 0xFF
        packet[0x21] = (total >> 8) & 0xFF
        return bytes(packet)

    def parse_response(self, data):
        """Return the decrypted payload, raising on a device-reported error."""
        data = bytearray(data)
        if len(data) < 0x38:
            raise BroadlinkError('Short reply from %s (%d bytes)'
                                 % (self.ip, len(data)))

        error = data[0x22] | (data[0x23] << 8)
        if error:
            message = '%s: %s' % (self.ip, error_text(error))
            if is_auth_error(error):
                message += '\n\n' + AUTH_ADVICE
            raise BroadlinkError(message)

        body = data[0x38:]
        if len(body) % 16:
            body = body[:len(body) - (len(body) % 16)]
        if not body:
            return bytearray()
        return bytearray(AES(self.key, self.iv).decrypt(bytes(body)))

    # -- exchange ----------------------------------------------------------

    def send(self, command, payload):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout)
            sock.sendto(self.build_packet(command, payload),
                        (self.ip, DEVICE_PORT))
            data, _address = sock.recvfrom(2048)
        except socket.timeout:
            raise BroadlinkError('%s did not answer within %.0fs'
                                 % (self.ip, self.timeout))
        except socket.error as exc:
            raise BroadlinkError('Could not reach %s: %s' % (self.ip, exc))
        finally:
            sock.close()
        return self.parse_response(data)

    def authenticate(self, client_id='Paragon Home'):
        """Swap the shared key for a session key. Required before anything else."""
        self.key = bytearray(INITIAL_KEY)
        self.iv = bytearray(INITIAL_IV)
        self.device_id = bytearray(4)

        try:
            payload = self.send(CMD_AUTH, build_auth_payload(client_id))
        except BroadlinkError as exc:
            # Say which step failed. Authentication happens lazily on the
            # first command, so without this the error looks like the command
            # was refused rather than the handshake.
            raise BroadlinkError('Could not authenticate with %s.\n\n%s'
                                 % (self.ip, exc))
        if len(payload) < 0x14:
            raise BroadlinkError('%s sent a short auth reply' % self.ip)

        self.device_id = bytearray(payload[0x00:0x04])
        self.key = bytearray(payload[0x04:0x14])
        self.authenticated = True
        self._log('Authenticated with %s' % self.ip)
        return True

    # -- IR / RF -----------------------------------------------------------

    def build_data_payload(self, verb, data=b'', new_framing=None):
        """The payload of a data command, in whichever layout this device wants.

        Old: <I verb> + data
        New: <H length> + <I verb> + data, where length covers verb and data
        """
        if new_framing is None:
            new_framing = self.new_framing
        body = bytearray(struct.pack('<I', verb))
        body.extend(bytearray(data))
        if new_framing:
            return bytearray(struct.pack('<H', len(body))) + body
        return body

    @staticmethod
    def read_data_payload(payload, new_framing):
        """The data out of a reply, past whichever header it carries."""
        payload = bytearray(payload)
        if not new_framing:
            return bytes(payload[0x04:])
        if len(payload) < 0x06:
            return b''
        length = struct.unpack('<H', bytes(payload[0:2]))[0]
        return bytes(payload[0x06:length + 2])

    def data_command(self, verb, data=b''):
        """Send a data command, correcting the framing if it turns out wrong.

        A device given the other layout authenticates fine and then refuses
        the command, which looks like a broken feature rather than a wire
        format mismatch. Rather than trust the type-code guess, a failure is
        retried the other way and whichever works is remembered.
        """
        try:
            reply = self.send(CMD_DATA,
                              self.build_data_payload(verb, data))
            return self.read_data_payload(reply, self.new_framing)
        except BroadlinkError as first:
            other = not self.new_framing
            try:
                reply = self.send(CMD_DATA,
                                  self.build_data_payload(verb, data,
                                                          new_framing=other))
            except BroadlinkError:
                raise first          # report the original, not the retry
            self.new_framing = other
            self._log('%s wants the %s data framing; switching to it'
                      % (self.ip, 'new' if other else 'old'))
            return self.read_data_payload(reply, other)

    def send_code(self, code):
        """Emit a previously learned IR or RF code."""
        self.data_command(DATA_SEND, bytearray(code))
        return True

    def enter_learning(self):
        """Put the device into IR learning mode; it waits for a remote press."""
        self.data_command(DATA_LEARN)
        return True

    # -- RF ----------------------------------------------------------------
    #
    # Radio is learned in two passes where infrared takes one, because the
    # blaster does not know which frequency to listen on. First it sweeps for
    # one while the button is held down; then, having locked on, it listens
    # again for the code itself. The second pass reads back through
    # check_learned, exactly as infrared does -- only the finding of it
    # differs, not the code that comes out.
    #
    # Only the Pro-class blasters have the radio for this at all. A Mini
    # authenticates and then refuses the sweep, which is a BroadlinkError like
    # any other; the caller is what turns it into an explanation.

    def sweep_frequency(self):
        """Start hunting for the frequency a remote is transmitting on."""
        self.data_command(DATA_LEARN_RF_SWEEP)
        return True

    def check_frequency(self):
        """Whether the sweep has locked onto a frequency yet.

        The reply's first byte is the flag. A device still sweeping answers
        zero rather than an error, which is the opposite of how the infrared
        check behaves -- so an error here is a real failure and is left to
        raise rather than being read as "keep waiting".
        """
        payload = bytearray(self.data_command(DATA_CHECK_RF_FOUND))
        return bool(payload) and payload[0] == 1

    def find_rf_packet(self, frequency=None):
        """Listen for a code, on `frequency` in MHz if one is given.

        With no frequency this is the second half of a sweep: the blaster has
        already locked on and simply listens. With one, the sweep is skipped
        entirely -- which is worth having, because the sweep is the pass that
        needs the button held down next to the blaster, and a shade remote
        whose frequency is printed on it does not need finding.

        The number goes on the wire as kilohertz in four little-endian bytes,
        so 433.92 MHz is 433920. Only the RM4-class blasters read it; an
        older Pro answers the bare form and refuses this one, which is a
        BroadlinkError like any other and is what the caller falls back on.
        """
        data = b''
        if frequency:
            data = struct.pack('<I', int(round(float(frequency) * 1000)))
        self.data_command(DATA_LEARN_RF_CODE, data)
        return True

    def cancel_sweep(self):
        """Stop sweeping.

        Worth doing on the way out of a cancelled learn: a blaster left
        sweeping stays deaf to ordinary commands until it times out on its
        own, so giving up without this makes the next thing anyone presses
        appear broken.
        """
        self.data_command(DATA_CANCEL_RF_SWEEP)
        return True

    def check_learned(self):
        """The code captured since learning started, or None if still waiting.

        A device that has not seen a button press answers with an error rather
        than an empty payload, so that is treated as "keep waiting" rather
        than as a failure.
        """
        try:
            code = self.data_command(DATA_CHECK)
        except BroadlinkError:
            return None
        return code if code else None


class BroadlinkTransport(object):
    """Finds Broadlink devices and keeps an authenticated session per device."""

    def __init__(self, bind_address='', timeout=5.0, log_func=None):
        self.bind_address = bind_address or ''
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self._sessions = {}

    def discover(self, timeout=3.0, local_addresses=None):
        """Broadcast a hello and collect the answers. Returns a list of dicts."""
        from govee_lan import local_addresses as enumerate_addresses

        addresses = local_addresses
        if addresses is None:
            addresses = [self.bind_address] if self.bind_address \
                else enumerate_addresses()
        if not addresses:
            addresses = ['']

        found = {}
        for address in addresses:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind((address, 0))
                port = sock.getsockname()[1]
                source = address or sock.getsockname()[0]

                sock.sendto(build_hello(source, port),
                            (BROADCAST_ADDRESS, BROADCAST_PORT))

                deadline = time.time() + max(0.5, float(timeout))
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        data, sender = sock.recvfrom(2048)
                    except socket.timeout:
                        break
                    except socket.error:
                        break
                    device = parse_hello_response(data, sender[0])
                    if device and device['mac']:
                        found[device['mac']] = device
            except socket.error as exc:
                self._log('Broadlink discovery from %s failed: %s'
                          % (address or 'default', exc))
            finally:
                sock.close()

        self._log('Broadlink discovery found %d device(s)' % len(found))
        return list(found.values())

    def session(self, ip, mac_bytes, devtype):
        """An authenticated session, re-authenticating if the key went stale."""
        existing = self._sessions.get(ip)
        if existing is not None and existing.authenticated:
            return existing

        session = Session(ip, mac_bytes, devtype, timeout=self.timeout,
                          log_func=self._log)
        session.authenticate()
        self._sessions[ip] = session
        return session

    def forget_session(self, ip):
        self._sessions.pop(ip, None)
