# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reading Paragon TV's Rerack schedule.

Paragon TV has its own Rerack -- a nine-phase preset macro system, one preset
per day of the week, each phase pinned to a time. This module reads that
schedule so a rerack here can be hung off one of those phases: the lights come
up when the channel does, the room goes dark when the box shuts down.

Nothing is written. Paragon TV's settings are read exactly as Paragon TV reads
them and never changed, so this cannot disturb a working television setup.
Paragon TV does not need to know this exists, and works unchanged whether or
not it does.

The one detail that matters: the day settings hold an *index*, not a name.
"3" means the third preset, and "0" means no preset today. Reading the value
as a name would silently find nothing.
"""

import ast
import os
import re

import xbmcaddon

from addon_utils import _translate

ADDON_ID = 'script.paragontv'

# Order matters: the day settings store a 1-based index into this list, and
# it must match Paragon TV's own order exactly.
PRESET_NAMES = ('Alpha', 'Omega', 'Delta', 'Epsilon', 'Gamma', 'Sigma',
                'Omicron', 'Theta', 'Lambda', 'Zeta')

# Satellite presets have no maintenance phase, and anchor at phase 2 rather
# than phase 1.
SATELLITE_PRESETS = ('Sigma', 'Omicron', 'Theta', 'Lambda', 'Zeta')

# ---------------------------------------------------------------------------
# Mirrored from Paragon TV's ptv_preset_timer.py
#
# Paragon TV does not store its phase times. It holds one anchor time per
# preset and a list of offsets in minutes, both hardcoded in its own source
# and deliberately not read from its settings, so that a master and its
# satellites can never drift apart. The time fields on its settings page are
# disabled and kept only as a readable copy.
#
# That leaves nothing to read, so these are a copy. A copy can go stale, so
# the report checks the computed times against those disabled settings fields
# and says so when they disagree -- which is the signal that Paragon TV has
# moved and these tables need updating from it.
# ---------------------------------------------------------------------------

PHASE_OFFSETS = {
    'Alpha':   (40, 60, 65, 105, 255, 795, 855, 885),
    'Omega':   (40, 60, 65, 105, 315, 855, 885, 930),
    'Delta':   (40, 60, 65, 105, 435, 525, 735, 765),
    'Epsilon': (40, 60, 65, 105, 315, 435, 675, 705),
    'Gamma':   (30, 50, 60, 65, 385, 475, 685, 725),
    'Sigma':   (10, 20, 170, 710, 770, 800),
    'Omicron': (10, 30, 240, 780, 810, 855),
    'Theta':   (10, 30, 360, 450, 660, 690),
    'Lambda':  (10, 30, 240, 360, 600, 630),
    'Zeta':    (20, 35, 355, 445, 655, 695),
}

ANCHOR_TIMES = {
    'Alpha': '03:00', 'Omega': '05:00', 'Delta': '05:00',
    'Epsilon': '05:00', 'Gamma': '05:50', 'Sigma': '04:25',
    'Omicron': '06:15', 'Theta': '06:15', 'Lambda': '06:15',
    'Zeta': '06:20',
}

# Paragon TV's own initial shutdown, which sits outside the numbered phases.
SHUTDOWN_TIMES = {
    'Alpha': '00:30', 'Omega': '01:30', 'Delta': '03:30',
    'Epsilon': '03:30', 'Gamma': '03:30', 'Sigma': '01:30',
    'Omicron': '03:30', 'Theta': '04:30', 'Lambda': '04:30',
    'Zeta': '03:30',
}

DAY_SETTINGS = ('MondayPreset', 'TuesdayPreset', 'WednesdayPreset',
                'ThursdayPreset', 'FridayPreset', 'SaturdayPreset',
                'SundayPreset')

PHASE_COUNT = 9

# What each phase is for, in Paragon TV's own terms. Shown when picking one,
# because "phase 6" means nothing on its own.
PHASE_LABELS = (
    'maintenance',
    'wake and tune',
    'shut down',
    'push to satellites',
    'wake and tune',
    'shut down',
    'wake and tune',
    'shut down',
    'wake and tune',
)

# The two phases a satellite does not have. Maintenance is the master's
# work, done once for everyone; the push is by definition the thing a master
# does TO a satellite. A rerack hung off either will simply never come round
# on a satellite day, which is the honest answer -- better than a time that
# arrives and does nothing.
MASTER_ONLY_PHASES = (1, 4)

# Which phases a satellite's offsets fill, being the rest of them.
SATELLITE_PHASES = tuple(phase for phase in range(2, PHASE_COUNT + 1)
                         if phase not in MASTER_ONLY_PHASES)


def _addon():
    """Paragon TV's add-on handle, or None when it is not installed."""
    try:
        return xbmcaddon.Addon(ADDON_ID)
    except Exception:
        # Kodi raises for an add-on that is not installed, and the exception
        # type has varied across versions.
        return None


def installed():
    return _addon() is not None


def _setting(key):
    addon = _addon()
    if addon is None:
        return ''
    try:
        return addon.getSetting(key) or ''
    except Exception:
        return ''


def enabled():
    """Whether Paragon TV's own preset system is switched on."""
    return _setting('EnablePresetSystem') == 'true'


def preset_for_day(weekday):
    """The preset name for a weekday (0 = Monday), or '' for none.

    The setting holds a 1-based index into PRESET_NAMES, with 0 meaning no
    preset that day -- which is how Paragon TV reads it, and reading it any
    other way finds nothing without saying so.
    """
    if not 0 <= weekday <= 6:
        return ''
    raw = _setting(DAY_SETTINGS[weekday]) or '0'
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return ''
    if 1 <= index <= len(PRESET_NAMES):
        return PRESET_NAMES[index - 1]
    return ''


def satellite_mode():
    """Whether this Paragon TV follows a master box rather than its own week."""
    return _setting('SatelliteMode') == 'true'


def todays_preset(now):
    """Today's preset, or '' when it cannot be known from here.

    In Satellite Mode, Paragon TV asks the master box over SSH which preset
    today is and keeps the answer in memory. There is nothing on this machine
    to read, so this says it does not know rather than reading the local
    weekly table, which Paragon TV is deliberately ignoring.
    """
    if satellite_mode():
        return ''
    return preset_for_day(now.weekday())


def week():
    """Paragon TV's whole weekly table, as seven names or blanks."""
    if satellite_mode():
        return ['' for _ in range(7)]
    return [preset_for_day(day) for day in range(7)]


def compute_phase_times(preset):
    """Every phase time for a preset, worked out as Paragon TV works it out.

    One anchor plus offsets in minutes. A master anchors at phase 1 and its
    eight offsets fill phases 2 to 9. A satellite anchors at phase 2 and its
    six fill phases 3 and 5 to 9, having neither the maintenance phase nor
    the push -- both of which belong to the master alone.
    """
    anchor = ANCHOR_TIMES.get(preset)
    offsets = PHASE_OFFSETS.get(preset)
    if not anchor or not offsets:
        return {}

    try:
        hour, minute = [int(part) for part in anchor.split(':')]
    except (ValueError, AttributeError):
        return {}
    start = hour * 60 + minute

    satellite = preset in SATELLITE_PRESETS
    # A satellite's offsets skip the push; a master's run straight through.
    fills = SATELLITE_PHASES[1:] if satellite else tuple(range(2, PHASE_COUNT + 1))

    times = {SATELLITE_PHASES[0] if satellite else 1: anchor}
    for index, offset in enumerate(offsets):
        # Wrapped rather than allowed past 24:00: an offset that crosses
        # midnight is a time the following morning, which is what Paragon TV
        # gets from adding a timedelta to a datetime.
        minutes = (start + offset) % (24 * 60)
        times[fills[index]] = '%02d:%02d' % (minutes // 60, minutes % 60)
    return times


def phase_time(preset, phase):
    """When a phase of a preset runs, as 'HH:MM', or '' if it does not."""
    if not preset or not 1 <= phase <= PHASE_COUNT:
        return ''
    return compute_phase_times(preset).get(phase, '')


def shutdown_time(preset):
    """Paragon TV's initial shutdown, which is not one of the nine phases."""
    return SHUTDOWN_TIMES.get(preset, '')


def describe_phase(phase):
    """'phase 3 (shut down)', for a menu."""
    if not 1 <= phase <= PHASE_COUNT:
        return 'phase %s' % phase
    return 'phase %d (%s)' % (phase, PHASE_LABELS[phase - 1])


def setting_ids(match=''):
    """Every setting id Paragon TV's own settings page declares.

    Read from its settings.xml on disk, because Kodi offers no way to ask an
    add-on what settings it has -- only to ask for one by name, which is no
    help when the name is the thing in doubt. Paragon TV has been ahead of
    its published source more than once, and a scheme that was renamed there
    is invisible from here until something looks.
    """
    addon = _addon()
    if addon is None:
        return []
    root = _translate(addon.getAddonInfo('path'))
    if not root:
        # Without this an empty path joins to a relative one, which reads
        # whichever settings.xml happens to be under the working directory --
        # ours, as it turned out.
        return []
    try:
        path = os.path.join(root, 'resources', 'settings.xml')
        handle = open(path, 'r')
        try:
            text = handle.read()
        finally:
            handle.close()
    except Exception:
        return []

    found = re.findall(r'id="([^"]+)"', text)
    if match:
        needle = match.lower()
        found = [name for name in found if needle in name.lower()]
    seen = []
    for name in found:
        if name not in seen:
            seen.append(name)
    return seen


def _literal_after(text, name, open_char, close_char):
    """The bracketed literal that follows an assignment, ast-parsed.

    Finds `name`, then the first opening bracket after it, and reads to the
    matching close so a multi-line dict or tuple comes back whole.
    """
    start = text.index(name)
    start = text.index(open_char, start)
    depth = 0
    for end in range(start, len(text)):
        char = text[end]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return ast.literal_eval(text[start:end + 1])
    raise ValueError('unbalanced %r after %r' % (open_char, name))


def read_source_tables():
    """Paragon TV's real timing tables, parsed from its own source.

    Paragon TV runs off hardcoded anchor and offset tables in
    ptv_preset_timer.py, deliberately NOT off its settings page -- the time
    fields there are a disabled display copy it ignores and Kodi never purges,
    so a box carries stale ones for years after a change. To tell whether this
    mirror has fallen behind, read what Paragon TV actually runs on.

    Returns {'offsets', 'anchors', 'shutdowns', 'satellite_phases'}, or None
    when the file cannot be found, read, or parsed.
    """
    addon = _addon()
    if addon is None:
        return None
    root = _translate(addon.getAddonInfo('path'))
    if not root:
        return None
    try:
        path = os.path.join(root, 'resources', 'lib', 'ptv_preset_timer.py')
        handle = open(path, 'r')
        try:
            text = handle.read()
        finally:
            handle.close()
    except Exception:
        return None
    try:
        return {
            'offsets': _literal_after(text, 'self.phase_offsets', '{', '}'),
            'anchors': _literal_after(text, 'self.anchor_times', '{', '}'),
            'shutdowns': _literal_after(text, 'self.shutdown_times', '{', '}'),
            'satellite_phases': _literal_after(text, 'self.satellite_phases',
                                               '(', ')'),
        }
    except Exception:
        return None


def source_phase_times(preset, tables):
    """Paragon TV's real phase times for a preset, from its source tables.

    Worked out exactly as Paragon TV works them: a master anchors at phase 1
    with its offsets filling phases 2-9; a satellite anchors at phase 2 with
    its offsets filling the phases named in satellite_phases (which skip the
    push), the maintenance phase and push having no time at all.
    """
    if not tables:
        return {}
    anchor = tables['anchors'].get(preset)
    offsets = tables['offsets'].get(preset)
    if not anchor or not offsets:
        return {}
    try:
        hour, minute = [int(part) for part in anchor.split(':')]
    except (ValueError, AttributeError):
        return {}
    start = hour * 60 + minute

    if preset in SATELLITE_PRESETS:
        fills = tuple(tables['satellite_phases'])
        times = {2: anchor}
    else:
        fills = tuple(range(2, PHASE_COUNT + 1))
        times = {1: anchor}
    for index, offset in enumerate(offsets):
        if index >= len(fills):
            break
        minutes = (start + offset) % (24 * 60)
        times[fills[index]] = '%02d:%02d' % (minutes // 60, minutes % 60)
    return times


def report(preset, now):
    """Everything Paragon Home can see about one preset, and why not.

    Written because "with Paragon TV, which has no time for it" is true but
    says nothing about which of four quite different things went wrong.
    """
    lines = []
    if not installed():
        return ('Paragon TV is not installed on this machine, or its add-on '
                'id is not "%s".' % ADDON_ID)

    lines.append('Paragon TV is installed.')
    lines.append('Its Rerack system is %s.'
                 % ('on' if enabled() else 'OFF -- switch it on in Paragon '
                    'TV\'s own settings'))

    today = todays_preset(now)
    lines.append('Today it runs: %s' % (today or 'nothing'))
    lines.append('')

    if not preset:
        return '\n'.join(lines)

    if satellite_mode():
        lines.append('It is in Satellite Mode, so it takes today from the '
                     'master box over SSH. Nothing here can read that, so a '
                     'rerack cannot follow its week -- only its phase times.')
        lines.append('')

    anchor = ANCHOR_TIMES.get(preset, '')
    kind = 'satellite' if preset in SATELLITE_PRESETS else 'master'
    lines.append('%s is a %s preset, anchored at %s.'
                 % (preset, kind, anchor or '(unknown)'))
    lines.append('Its initial shutdown is %s.'
                 % (shutdown_time(preset) or '(none)'))
    lines.append('')

    tables = read_source_tables()
    source = source_phase_times(preset, tables) if tables else None

    lines.append('Times it holds for %s:' % preset)
    found = 0
    drifted = []
    for phase in range(1, PHASE_COUNT + 1):
        at_time = phase_time(preset, phase)
        if at_time:
            found += 1
        lines.append('  Phase %d  %s' % (phase, at_time or '(none)'))

        # Checked against Paragon TV's OWN source of truth -- the anchor and
        # offset tables in ptv_preset_timer.py -- not its settings page, whose
        # time fields are a disabled display copy Paragon TV ignores and Kodi
        # never purges, so a box reports stale ones long after a change.
        if source is not None:
            real = source.get(phase, '')
            if real != (at_time or ''):
                drifted.append('Phase %d: Paragon TV runs %s, this says %s'
                               % (phase, real or '(none)', at_time or '(none)'))

    if source is not None:
        real_sd = tables['shutdowns'].get(preset) or ''
        mine_sd = shutdown_time(preset) or ''
        if real_sd != mine_sd:
            drifted.append('Initial shutdown: Paragon TV runs %s, this says %s'
                           % (real_sd or '(none)', mine_sd or '(none)'))

    if source is None:
        lines.append('')
        lines.append("(Paragon TV's own schedule could not be read from its "
                     'source, so this could not be checked against it.)')
    elif drifted:
        lines.append('')
        lines.append('These no longer agree with Paragon TV:')
        for line in drifted:
            lines.append('  %s' % line)
        lines.append('Paragon TV has changed its timings and Paragon Home '
                     'needs updating to match.')
    else:
        lines.append('')
        lines.append("These match Paragon TV's own schedule.")

    if not found:
        lines.append('')
        lines.append('None at all. Paragon Home looks for settings named '
                     '%sPhase1Time and so on.' % preset)

        declared = setting_ids(preset)
        if declared:
            lines.append('')
            lines.append('What this Paragon TV actually declares for %s:'
                         % preset)
            for name in declared[:14]:
                lines.append('  %s' % name)
            if len(declared) > 14:
                lines.append('  ... and %d more' % (len(declared) - 14))
        elif setting_ids():
            lines.append('')
            lines.append('This Paragon TV declares no setting mentioning '
                         '%s at all, so its phase times are not stored the '
                         'way Paragon Home expects.' % preset)
        else:
            lines.append('')
            lines.append('Its settings page could not be read, so this '
                         'cannot say which names it uses.')
    return '\n'.join(lines)


def status(now):
    """A short account of what Paragon TV says about today, for a dialog."""
    if not installed():
        return 'Paragon TV is not installed.'
    if not enabled():
        return 'Paragon TV is installed, but its Rerack system is switched off.'
    preset = todays_preset(now)
    if not preset:
        return 'Paragon TV has no preset scheduled for today.'

    times = []
    for phase in range(1, PHASE_COUNT + 1):
        when = phase_time(preset, phase)
        if when:
            times.append('%s  %s' % (when, describe_phase(phase)))
    if not times:
        return "Today's preset is %s, which has no phase times set." % preset
    return "Today's preset is %s:\n\n%s" % (preset, '\n'.join(times))
