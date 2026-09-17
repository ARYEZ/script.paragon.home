# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reracks: a day laid out in nine phases, each holding a sequence.

Shaped after the Paragon TV Rerack deliberately, down to the preset names, so
the two line up. There are nine presets and they always exist; an empty one
costs nothing and is simply never assigned to a day.

    Alpha
       Phase 1  maintenance          (empty)
       Phase 2  wake and tune        Curtain Up      07:00
       Phase 3  shut down            Wind Down       23:30
       Phase 5  wake and tune        Curtain Up      17:00

The point of the layer is the third and fourth lines: one sequence, written
once, used at several points in a day. A sequence knows how to do a thing; a
rerack decides when a day does it.

Where a phase's time comes from is the phase's own business, and it is said
by the time itself. A phase with a time runs at that time. A phase with none
runs when Paragon TV runs the same phase of the preset of the same name --
which is why the names match, and why nothing has to be said twice.

So one rerack can do both at once, which is the ordinary case rather than an
exotic one: hold the lights back five minutes past the television's wake, and
take its word for everything else.

A weekly table says which rerack a day gets, exactly as Paragon TV's does.
"""

import sequences as sequence_lib

# Deliberately not reracks.json or rerack_state.json: those were what
# sequences were saved in before v2.14, and are still read once to carry
# older setups over. Reusing either name would have this file read those.
RERACK_FILE = 'rerack_presets.json'
RERACK_STATE_FILE = 'rerack_phase_state.json'

# The same nine names, in the same order, as Paragon TV. Matching by name is
# how a rerack takes its times from the television's preset, so the order and
# spelling are not ours to vary.
PRESET_NAMES = ('Alpha', 'Omega', 'Delta', 'Epsilon', 'Gamma', 'Sigma',
                'Omicron', 'Theta', 'Lambda', 'Zeta')

PHASE_COUNT = 9

DAYS = sequence_lib.DAYS


def empty_phase():
    return {'time': '', 'sequence': ''}


def make_rerack(name, phases=None, follow_tv=False):
    return normalise({'name': name, 'phases': list(phases or []),
                      'follow_tv': follow_tv})


def follows_tv(phase):
    """A phase with no time of its own waits for Paragon TV."""
    return bool(phase.get('sequence')) and not phase.get('time')


def normalise_phase(raw, drop_time=False):
    """Clamp one phase.

    A phase with no sequence does nothing whatever else it holds, so it is
    stored empty rather than half filled. A blank time is not missing data:
    it means the phase takes Paragon TV's time for the same phase.
    """
    if not isinstance(raw, dict):
        return empty_phase()
    name = (raw.get('sequence') or '').strip()
    if not name:
        return empty_phase()
    return {'time': '' if drop_time
            else sequence_lib.parse_time(raw.get('time')),
            'sequence': name}


def normalise(raw):
    """Clamp one rerack, or None if it has no usable name."""
    if not isinstance(raw, dict):
        return None
    name = (raw.get('name') or '').strip()
    if not name:
        return None

    # Until v2.16 a whole rerack followed Paragon TV or did not. That switch
    # is now per phase and is said by the time itself, so a rerack that was
    # following has its times cleared -- which is exactly what following meant.
    drop = bool(raw.get('follow_tv'))
    phases = [normalise_phase(entry, drop) for entry in raw.get('phases') or []]
    phases = phases[:PHASE_COUNT]
    while len(phases) < PHASE_COUNT:
        phases.append(empty_phase())

    return {'name': name, 'phases': phases}


def default_reracks():
    """All nine, empty. They always exist, as Paragon TV's presets do."""
    return [make_rerack(name) for name in PRESET_NAMES]


def normalise_all(raw):
    """The nine presets, in order, whatever the file happened to hold.

    Anything unrecognised is dropped and anything missing is added back
    empty, so a hand-edited or older file still opens on nine presets in the
    familiar order rather than on whatever survived.
    """
    found = {}
    for entry in raw or []:
        rerack = normalise(entry)
        if rerack is not None:
            found[rerack['name']] = rerack
    return [found.get(name) or make_rerack(name) for name in PRESET_NAMES]


def find(reracks, name):
    wanted = (name or '').strip().lower()
    for rerack in reracks or []:
        if rerack.get('name', '').lower() == wanted:
            return rerack
    return None


def clean_week(raw):
    """The weekly table: seven entries, each a rerack name or ''."""
    week = ['' for _ in range(7)]
    if isinstance(raw, dict):
        raw = [raw.get(day.lower(), '') for day in DAYS]
    if not isinstance(raw, list):
        return week
    for index, entry in enumerate(raw[:7]):
        name = (entry or '').strip()
        if name in PRESET_NAMES:
            week[index] = name
    return week


def todays_rerack(week, reracks, now):
    """The rerack a day gets, or None."""
    week = clean_week(week)
    return find(reracks, week[now.weekday()])


def matching_week(tv_week):
    """Paragon TV's weekly table, as ours. The preset names are the same nine,
    so a day set to Gamma there is a day set to Gamma here."""
    return clean_week(tv_week)


def filled_phases(rerack):
    """(number, phase) for the phases that hold a sequence."""
    return [(index + 1, phase)
            for index, phase in enumerate(rerack.get('phases') or [])
            if phase.get('sequence')]


def describe(rerack):
    """One line for a menu: how many phases, and how many wait for the TV."""
    filled = filled_phases(rerack)
    if not filled:
        return 'empty'
    waiting = len([p for _n, p in filled if follows_tv(p)])
    text = '%d phase%s' % (len(filled), '' if len(filled) == 1 else 's')
    if waiting == len(filled):
        return '%s, all with Paragon TV' % text
    if waiting:
        return '%s, %d with Paragon TV' % (text, waiting)
    return '%s, own times' % text


def describe_phase_row(rerack, number, phase, tv_time=None):
    """A phase as it reads in the editor: what it does and when.

    A phase waiting on Paragon TV shows the time it resolves to today, so the
    difference between "waiting" and "waiting for nothing" is visible without
    having to go and look.
    """
    label = sequence_lib.describe_phase(number)
    if not phase.get('sequence'):
        return '%s  -  empty' % label.capitalize()

    if follows_tv(phase):
        when = ('with Paragon TV, %s' % tv_time if tv_time
                else 'with Paragon TV, which has no time for it')
    else:
        when = phase['time']
    return '%s  -  %s  (%s)' % (label.capitalize(), phase['sequence'], when)


def phase_time(rerack, number, tv_time=''):
    """When a phase runs, as 'HH:MM', or '' if it never does.

    Its own time wins outright when it has one. A phase without one takes
    Paragon TV's, and runs at no time at all when the television has none --
    there being nothing to fall back to, and an invented hour being worse
    than not firing.
    """
    phases = rerack.get('phases') or []
    if not 1 <= number <= len(phases):
        return ''
    phase = phases[number - 1]
    if not phase.get('sequence'):
        return ''
    return phase.get('time') or (tv_time or '').strip()


def stamp(rerack, number, now, at_time):
    """The key that says this phase has already run today."""
    return '%s#%d %04d-%02d-%02d %s' % (rerack.get('name'), number,
                                        now.year, now.month, now.day, at_time)


def due_phases(rerack, now, state, tv_times=None, grace=0):
    """The phases of this rerack whose time has come.

    Returns (number, sequence name, time, stamp) for each. The state is read
    rather than written here so the caller can decide when a phase counts as
    run -- which it does before running it, not after.
    """
    tv_times = tv_times or {}
    due = []
    for number, phase in filled_phases(rerack):
        at_time = phase_time(rerack, number, tv_times.get(number, ''))
        if not at_time:
            continue
        key = stamp(rerack, number, now, at_time)
        if key in state:
            continue
        if sequence_lib.due({'time': at_time, 'days': [now.weekday()]}, now,
                            schedule=(at_time, [now.weekday()]), grace=grace):
            due.append((number, phase['sequence'], at_time, key))
    return due


def needs_tv(rerack):
    """Whether any phase here is waiting on Paragon TV.

    Used to skip asking the television anything at all when nothing is
    waiting on it, which is most reracks most of the time.
    """
    return any(follows_tv(phase) for _n, phase in filled_phases(rerack))


def repoint(reracks, old_name, new_name):
    """Point every phase at a sequence's new name. Returns how many moved.

    A rename used to leave the phases naming something that was no longer
    there, and a phase whose sequence has gone does nothing and says so only in
    the log.
    """
    was = (old_name or '').strip().lower()
    now = (new_name or '').strip()
    if not was or not now:
        return 0
    moved = 0
    for rerack in reracks or []:
        for _number, phase in filled_phases(rerack):
            if phase['sequence'].strip().lower() == was:
                phase['sequence'] = now
                moved += 1
    return moved


def used_by(reracks, sequence_name):
    """Where a sequence is used, so the reuse is visible from the sequence."""
    wanted = (sequence_name or '').strip().lower()
    places = []
    for rerack in reracks or []:
        for number, phase in filled_phases(rerack):
            if phase['sequence'].strip().lower() == wanted:
                places.append('%s phase %d' % (rerack['name'], number))
    return places
