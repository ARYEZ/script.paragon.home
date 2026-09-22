# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Satellite mode: one box owns the setup, the others follow it.

A house has one master Kodi box and some number of satellites. All of them can
reach the same lights -- the devices are on the LAN, not attached to any one
box -- so the question is not who *can* control them but who *should decide*.
Satellite mode answers it: the master keeps the devices, scenes, sequences and
reracks, and a satellite copies them down and runs no schedule of its own.

That last part is the point. Three boxes running the same rerack would send
every phase three times: three "off" commands to the same bulb, three sequences
racing each other through their steps. A satellite still runs anything asked
for by hand -- from the menu, from a keymap, from Paragon TV's sidebar -- it
just never decides on its own that something is due.

Files are read over SSH, the same way Paragon TV reads its master's settings,
because that is what these boxes already have set up between them. Nothing is
written to the master: a satellite only ever reads.
"""

import json
import os
import subprocess

import addon_utils as utils
from compat import to_native

# The master keeps its data under the same add-on id this box uses, so the
# remote path is derived rather than written out twice.
MASTER_PROFILE = ('/storage/.kodi/userdata/addon_data/%s' % utils.ADDON_ID)

# What a satellite copies down, in the order it is copied. Devices first: a
# scene or a sequence naming a device is meaningless without it.
#
# tuya_keys.json is included because a Tuya plug cannot be switched without
# its local key, so a sequence step naming one would fail on a satellite that
# did not have it. It is a credential, and this copies it to every satellite
# -- which is the same trust boundary as the passwordless SSH that fetched it.
#
# broadlink_codes.json is here for exactly that reason and was missed: a
# sequence step that fires a learned infrared code fails just as flatly on a
# satellite that never learned it, and a satellite cannot learn one for
# itself. A satellite is a copy of the master, and half a copy is a sequence
# that works in one room and not the next.
SHARED_FILES = (
    'devices.json',
    'scenes.json',
    'sequences.json',
    'palette.json',
    'tuya_keys.json',
    'broadlink_codes.json',
    # Which clips each speaker holds. A sequence step naming one is checked
    # against this before anything goes on the wire, so a satellite without
    # it would refuse every announcement the master can make.
    'speaker_clips.json',
)

# What a satellite never copies: the master's reracks and its record of what
# has already run today. A satellite runs no schedule, so both would be dead
# weight, and phase state especially is about one box's day rather than the
# house's.
NEVER_SHARED = (
    'rerack_presets.json',
    'rerack_phase_state.json',
    'sequence_state.json',
    'rerack_state.json',
    'cycle.json',
)

SSH_TIMEOUT = 5


def _run(command):
    """Run `command` and return its output as text, or raise.

    Split out so a test can hand in its own runner rather than reaching for a
    real network and a real ssh binary.
    """
    output = subprocess.check_output(command, stderr=subprocess.STDOUT)
    return utils.to_text(output)


def fetch(master_ip, name, run=None):
    """Read one file from the master. Returns its text, or None.

    None covers every way this can fail -- no route, no key, no such file --
    on purpose. The caller cannot do anything different about any of them, and
    a satellite that cannot reach its master must carry on with what it
    already has rather than stopping.
    """
    runner = run or _run
    command = ['ssh', '-o', 'ConnectTimeout=%d' % SSH_TIMEOUT,
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
               'root@%s' % to_native(master_ip),
               'cat %s' % os.path.join(MASTER_PROFILE, name)]
    try:
        return runner(command)
    except Exception as exc:
        utils.debug('Satellite: could not read %s from %s: %s'
                    % (name, master_ip, exc))
        return None


def pull(master_ip, run=None, write=None, wanted=None):
    """Copy the shared files down from the master.

    Returns (names copied, [problems]). A file that cannot be read, or that
    does not parse, is left exactly as it is here: overwriting a good scene
    list with a truncated one because the network hiccuped mid-copy would be
    worse than being a few minutes out of date. That is also why this parses
    before it writes rather than streaming the bytes into place.
    """
    if not master_ip:
        return [], ['No master address set']

    writer = write or utils.write_json
    copied = []
    problems = []

    for name in (wanted or SHARED_FILES):
        text = fetch(master_ip, name, run=run)
        if text is None:
            # tuya_keys.json is absent on a house with no Tuya plugs, and
            # palette.json until the colours are first edited. Neither is a
            # problem worth showing.
            if name in ('tuya_keys.json', 'palette.json',
                        'broadlink_codes.json', 'speaker_clips.json'):
                continue
            problems.append('%s could not be read' % name)
            continue

        try:
            payload = json.loads(text)
        except ValueError:
            problems.append('%s from the master is not readable JSON' % name)
            continue

        if writer(name, payload):
            copied.append(name)
        else:
            problems.append('%s could not be saved here' % name)

    if copied:
        utils.log('Satellite: copied %s from %s'
                  % (', '.join(copied), master_ip))
    return copied, problems


def describe(master_ip, last=None):
    """A line for the menu saying where this box stands."""
    if not master_ip:
        return 'no master address set'
    if not last:
        return '%s, not yet copied' % master_ip
    return '%s, last copied %s' % (master_ip, last)
