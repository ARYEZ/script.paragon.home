# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Sequences: fifteen ordered steps, run as one.

A fixed number of numbered slots rather than a list you grow, so a sequence
has the same shape every time you open it and slot 4 is always slot 4. Empty
slots are normal and cost nothing. That shape is borrowed from the Paragon TV
Rerack, whose phases work the same way.

The slots are fixed but not frozen: a step can be moved to any other slot,
and the ones it passes slide along to make room. Order is the whole point of
a sequence -- a pause belongs after the step that needs it, not wherever the
step happened to be typed -- so getting the order wrong should not mean
retyping the steps in a different order.

A step is three choices: what kind of thing, which one, and what to do to it.

    1. Scene       Warshade
    2. Tuya        Office Plug All outlets      On
    3. Broadlink   Bedroom Broadlink            TV power

Steps run in order, top to bottom. Each can hold a pause afterwards, which
matters more than it sounds: a television told to switch on and change channel
in the same breath will miss the second command, because it is still waking up.

A sequence is deliberately not a scene. A scene describes a state -- how the
lights should look -- and can be captured, mixed and cycled. A sequence is an
ordered list of things to do, including things with no state to describe at
all, like an infrared button press.
"""

import datetime
import re
import time as time_module

SEQUENCE_FILE = 'sequences.json'

# What sequences were called before, and the file they were saved in. Read
# once when there is no sequences.json, so a rename does not cost anyone the
# ones they had already built.
LEGACY_FILE = 'reracks.json'
LEGACY_STATE_FILE = 'rerack_state.json'

# Fixed, like the phases of the system this is named after, and a fixed count
# is what lets the editor show every slot including the empty ones. Fifteen
# because ten ran out: a sequence that wakes a television, waits for it, tunes
# it and then settles the lights around it spends its slots quickly.
STEP_COUNT = 15

KIND_NONE = 'none'
KIND_SCENE = 'scene'
KIND_POWER = 'power'
KIND_COMMAND = 'command'
KIND_POSITION = 'position'   # open a blind to a percentage
KIND_SEQUENCE = 'sequence'   # run another sequence's steps here
KIND_LOCK = 'lock'           # throw a deadbolt
KIND_UNLOCK = 'unlock'       # withdraw one, where the box is allowed to

# Power actions a step can carry.
ACTION_ON = 'on'
ACTION_OFF = 'off'
ACTION_TOGGLE = 'toggle'
POWER_ACTIONS = (ACTION_ON, ACTION_OFF, ACTION_TOGGLE)

# A target of this stands for every enabled device of the step's driver,
# rather than one of them.
TARGET_ALL = '*'

MAX_PAUSE = 3600

# How deep one sequence may reach into others, and how many steps the whole
# thing may come to once they are spliced together. A loop is already refused
# outright, so neither of these is what stops A calling B calling A -- they
# stop the other runaway, where a handful of sequences each holding the next
# one twice come to thousands of steps and a Kodi on a small box stops
# answering while it works them out.
MAX_NESTING = 5
MAX_EXPANDED_STEPS = 200

# How near a blind counts as already being where a step wants it. A cover told
# to shut reports 0 most of the time and 1 or 2 sometimes, and running the
# motor to take two percent off is the noise this exists to avoid.
POSITION_TOLERANCE = 2

# A pause longer than this is not waited out in place. The sequence writes down
# where it got to and returns, and the service carries it on when the wait is
# over -- so a coffee maker brewing for twelve minutes is twelve minutes this
# box spends free, rather than twelve minutes nothing else can be run.
#
# Shorter pauses still block. A light settling or an amplifier coming up is a
# wait measured in seconds, and handing those to the scheduler would cost more
# than it saves and put a five-second gap where a one-second one was asked for.
LONG_PAUSE_SECONDS = 30

SEQUENCE_STATE_FILE = 'sequence_state.json'

# Sequences part way through a long pause: what to carry on, from which step,
# and when. On disk rather than in memory because the step waiting on the far
# side of the pause is usually the one that turns something off, and a Kodi
# restart must not be what leaves the coffee maker heating all day.
PENDING_FILE = 'pending_sequences.json'

# Index 0 is Monday, to match datetime.weekday(). Nothing is gained by
# picking a different origin from the standard library's.
DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
        'Sunday')
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)

# How late a sequence may still run. Kodi is not always awake at the minute a
# sequence is due -- it may be starting up, or mid-way through something -- and
# a few minutes late is what was wanted. An hour late is not: a sequence that
# lifts the lights at six should not do it at seven because the box was off.
CATCH_UP_SECONDS = 300

# Paragon TV's Rerack has nine phases.
TV_PHASE_COUNT = 9

_TIME_PATTERNS = (
    re.compile(r'^(\d{1,2}):(\d{2})\s*([ap]m?)?$', re.I),
    re.compile(r'^(\d{1,2})\s*([ap]m?)$', re.I),
    re.compile(r'^(\d{2})(\d{2})$'),
)


def parse_time(text):
    """Read a time of day into 'HH:MM', or '' if it is not one.

    Deliberately forgiving about how it is typed. This is entered on a remote
    control, where "6pm" is a great deal less work than "18:00", and both mean
    the same thing.
    """
    if not text:
        return ''
    text = str(text).strip()

    for pattern in _TIME_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groups()
        hour = int(groups[0])
        if len(groups) == 3 and groups[1] is not None and ':' in text:
            minute, suffix = int(groups[1]), groups[2]
        elif pattern is _TIME_PATTERNS[1]:
            minute, suffix = 0, groups[1]
        else:
            minute, suffix = int(groups[1]), None

        if suffix:
            suffix = suffix[0].lower()
            if hour == 12:
                hour = 0
            if suffix == 'p':
                hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return '%02d:%02d' % (hour, minute)
    return ''


def clean_days(raw):
    """Days of the week as a sorted list of 0-6, ignoring anything else."""
    days = set()
    for entry in raw or []:
        try:
            day = int(entry)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            days.add(day)
    return sorted(days)


def follows_tv(sequence):
    """Whether this sequence hangs off a Paragon TV phase instead of a clock."""
    return bool(sequence.get('phase'))


def clean_phase(raw):
    """A Paragon TV phase number, or 0 for not following one."""
    try:
        phase = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return phase if 1 <= phase <= TV_PHASE_COUNT else 0


def describe_phase(phase):
    """Named here rather than imported, so this module stays importable
    without Kodi -- the label list is small and does not change."""
    labels = ('maintenance', 'wake and tune', 'shut down',
              'push to satellites', 'wake and tune', 'shut down',
              'wake and tune', 'shut down', 'wake and tune')
    if not 1 <= phase <= TV_PHASE_COUNT:
        return 'phase %s' % phase
    return 'phase %d (%s)' % (phase, labels[phase - 1])


def scheduled(sequence):
    """Whether this sequence runs itself, by either route.

    Its own time needs both halves -- a time with no days, or days with no
    time, is a schedule that can never come round. Following Paragon TV needs
    neither, because Paragon TV supplies both.
    """
    if follows_tv(sequence):
        return True
    return bool(sequence.get('time') and sequence.get('days'))


def describe_schedule(sequence):
    """The schedule in words, the way it would be said."""
    if follows_tv(sequence):
        return 'Paragon TV %s' % describe_phase(sequence['phase'])
    if not scheduled(sequence):
        return 'only when you run it'

    days = tuple(sequence['days'])
    if len(days) == 7:
        when = 'every day'
    elif days == WEEKDAYS:
        when = 'weekdays'
    elif days == WEEKEND:
        when = 'weekends'
    else:
        when = ', '.join(DAYS[day][:3] for day in days)
    return '%s at %s' % (when, sequence['time'])


def stamp(sequence, now, at_time=None):
    """The key that says this sequence has run today.

    Includes the time as well as the date, so moving a schedule later in the
    same day lets it run again rather than being counted as already done. The
    time is passed in when it came from somewhere else -- a Paragon TV phase
    that moved should re-arm for the same reason.
    """
    if at_time is None:
        at_time = sequence.get('time') or ''
    return '%04d-%02d-%02d %s' % (now.year, now.month, now.day, at_time)


def own_schedule(sequence):
    """The (time, days) a sequence keeps for itself."""
    return sequence.get('time') or '', list(sequence.get('days') or [])


def due(sequence, now, last='', schedule=None, grace=0):
    """Whether this sequence should run right now.

    Three separate questions, and all of them have to be yes: is today one of
    its days, is the time here or just past, and has it not already run.

    `schedule` is a resolved (time, days) pair for a sequence whose schedule
    comes from elsewhere. Resolving it outside keeps this function free of
    any knowledge of where a time came from.

    `grace` widens the catch-up window for one check. The service cannot look
    at the clock while it is part way through a sequence, so an hour-long
    pause would otherwise push everything due in that hour past the catch-up
    window and skip it outright. The service passes the time it was busy, so
    the allowance covers exactly the period it could not have noticed -- which
    is not the same as making the window permanently wider, since that is what
    stops a Kodi restart replaying this morning's sequences.
    """
    at_time, days = own_schedule(sequence) if schedule is None else schedule
    if not at_time or not days:
        return False
    if now.weekday() not in days:
        return False

    hour, minute = [int(part) for part in at_time.split(':')]
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    late = (now - at).total_seconds()
    if late < 0 or late > CATCH_UP_SECONDS + max(0, grace):
        return False
    return last != stamp(sequence, now, at_time)


def now():
    """The current local time, in one place so tests can hand in their own."""
    return datetime.datetime.now()


def empty_step():
    return {'kind': KIND_NONE, 'driver': '', 'target': '', 'action': '',
            'pause': 0}


def make_sequence(name, steps=None, time=None, days=None, phase=None,
                  skip_done=False):
    """A sequence with its full complement of slots, however few are filled."""
    return normalise({'name': name, 'steps': list(steps or []),
                      'time': time, 'days': days, 'phase': phase,
                      'skip_done': skip_done})


def one_step(step, name):
    """One step, as a sequence, so that it can be run like anything else.

    The speed dial and the phrase book both hold a single step and both have
    to run it. Everything that carries a step out is reached through a
    sequence -- the lock gate, a nested sequence being spliced in, a long
    pause handed back, the way a failure is collected -- so a sequence of one
    gets all of that rather than a second copy of any of it.

    The name is the caller's, and it matters: it is the key a long pause is
    written down under, and the key the resume, the countdown and the last-run
    record all look up. A caller with a real sequence to run should run that,
    under its own name, rather than wrapping it in one of these.
    """
    steps = [step or empty_step()]
    while len(steps) < STEP_COUNT:
        steps.append(empty_step())
    return {'name': name, 'description': '', 'time': '', 'days': [],
            'phase': 0, 'skip_done': False, 'steps': steps}


def _clean_int(value, low, high, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _volume(value):
    """0 to 100 from whatever was written down, or None for none."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, number))


def normalise_step(raw):
    """Clamp one step, or return an empty slot if it is not usable.

    A step naming a kind it cannot carry out -- a power action with no target,
    a command with no name -- becomes empty rather than being kept and failing
    later. A half-filled slot that looks filled is worse than a blank one.
    """
    if not isinstance(raw, dict):
        return empty_step()

    kind = raw.get('kind')
    step = empty_step()
    step['pause'] = _clean_int(raw.get('pause'), 0, MAX_PAUSE)

    if kind == KIND_SCENE:
        name = (raw.get('target') or '').strip()
        if not name:
            return empty_step()
        step['kind'] = KIND_SCENE
        step['target'] = name
        return step

    if kind == KIND_POWER:
        action = (raw.get('action') or '').strip().lower()
        target = (raw.get('target') or '').strip()
        if action not in POWER_ACTIONS or not target:
            return empty_step()
        step['kind'] = KIND_POWER
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        step['action'] = action
        return step

    if kind == KIND_COMMAND:
        target = (raw.get('target') or '').strip()
        action = (raw.get('action') or '').strip()
        if not target or not action:
            return empty_step()
        step['kind'] = KIND_COMMAND
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        step['action'] = action
        # A beacon's clip can say how loud: the volume is set first, so the
        # clip starts at it rather than jumping part way in. Absent is "leave
        # it where it is", which is what every step written before this
        # means, so it is left off rather than stored as a blank. One that
        # is not a number is dropped rather than emptying the slot: the clip
        # is the step, the volume is how it is said.
        volume = _volume(raw.get('volume'))
        if volume is not None:
            step['volume'] = volume
        return step

    if kind == KIND_SEQUENCE:
        name = (raw.get('target') or '').strip()
        if not name:
            return empty_step()
        step['kind'] = KIND_SEQUENCE
        step['target'] = name
        return step

    if kind in (KIND_LOCK, KIND_UNLOCK):
        # No action of its own: the kind is the whole instruction. Two kinds
        # rather than one with a direction, so that a sequence holding an
        # unlock cannot be turned into one by changing a single word.
        target = (raw.get('target') or '').strip()
        if not target:
            return empty_step()
        step['kind'] = kind
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        return step

    if kind == KIND_POSITION:
        target = (raw.get('target') or '').strip()
        if not target:
            return empty_step()
        # Held as a string like every other field on a step, so a sequence
        # written by hand and one written by the editor read the same. The
        # number is what matters, so an unparseable one empties the slot
        # rather than being kept as a step that cannot run.
        try:
            percent = int(round(float(raw.get('action'))))
        except (TypeError, ValueError):
            return empty_step()
        step['kind'] = KIND_POSITION
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        step['action'] = str(max(0, min(100, percent)))
        return step

    return empty_step()


def move_step(steps, frm, to):
    """One step moved to another slot, with the rest sliding along.

    Returns a new list rather than reordering in place, so a caller that
    decides against the move still has the original. The length never
    changes: a step leaving slot 2 for slot 7 pulls slots 3 to 7 up one, and
    the sequence still has its full complement of slots afterwards.

    Out-of-range and no-op moves return the steps unchanged rather than
    raising. There is nothing a caller could usefully do about it, and the
    honest answer to "move this nowhere" is the list you started with.
    """
    moved = list(steps or [])
    if not (0 <= frm < len(moved)) or not (0 <= to < len(moved)) or frm == to:
        return moved
    moved.insert(to, moved.pop(frm))
    return moved


def normalise(raw):
    """Clamp one sequence, or None if it has no usable name.

    Always exactly STEP_COUNT slots: a file written by an older version, or
    edited by hand, is padded or trimmed rather than being rejected.
    """
    if not isinstance(raw, dict):
        return None
    name = (raw.get('name') or '').strip()
    if not name:
        return None

    steps = [normalise_step(entry) for entry in (raw.get('steps') or [])]
    steps = steps[:STEP_COUNT]
    while len(steps) < STEP_COUNT:
        steps.append(empty_step())

    return {'name': name,
            'description': (raw.get('description') or '').strip(),
            'time': parse_time(raw.get('time')),
            'days': clean_days(raw.get('days')),
            'phase': clean_phase(raw.get('phase')),
            'skip_done': bool(raw.get('skip_done')),
            'steps': steps}


def normalise_all(raw):
    if not isinstance(raw, list):
        return []
    cleaned = []
    for entry in raw:
        sequence = normalise(entry)
        if sequence is not None:
            cleaned.append(sequence)
    return cleaned


def find(sequences, name):
    """Look a sequence up by name, ignoring case and surrounding space."""
    wanted = (name or '').strip().lower()
    for sequence in sequences or []:
        if sequence.get('name', '').lower() == wanted:
            return sequence
    return None


def filled_steps(sequence):
    """The steps that actually do something, in order."""
    return [step for step in sequence.get('steps') or []
            if step.get('kind') != KIND_NONE]


def describe(sequence):
    """One line for a menu: how many steps, and what the first one does."""
    steps = filled_steps(sequence)
    if not steps:
        return 'no steps yet'
    count = '%d step%s' % (len(steps), '' if len(steps) == 1 else 's')
    # Not lower-cased: the first step names a scene or a device, and those
    # are the user's own names to spell as they chose.
    return '%s, first: %s' % (count, describe_step(steps[0]))


def describe_step(step, device_name=None):
    """One line for a step, in the order it was chosen: kind, target, action."""
    kind = step.get('kind')
    if kind == KIND_NONE:
        return 'Empty'

    target = device_name or step.get('target') or ''
    if kind == KIND_SCENE:
        text = 'Scene: %s' % target
    elif kind == KIND_SEQUENCE:
        text = 'Sequence: %s' % (step.get('target') or '')
    elif kind == KIND_LOCK:
        text = '%s: Lock' % target
    elif kind == KIND_UNLOCK:
        # Shouted, because a sequence listing is read at a glance and this is
        # the one step in it that opens a door.
        text = '%s: UNLOCK' % target
    elif kind == KIND_POWER:
        if step.get('target') == TARGET_ALL:
            target = 'all %s devices' % (step.get('driver') or 'listed')
        text = '%s: %s' % (target, step.get('action', '').title())
    elif kind == KIND_COMMAND:
        text = '%s: %s' % (target, step.get('action'))
        if step.get('volume') is not None:
            text += ' at %d%%' % step['volume']
    elif kind == KIND_POSITION:
        text = '%s: %s%% open' % (target, step.get('action'))
    else:
        text = 'Empty'

    pause = step.get('pause') or 0
    if pause:
        text += '  (+%ds)' % pause
    return text


def _last_doing_something(steps):
    """The index of the last step that is not an empty slot, or None."""
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get('kind') != KIND_NONE:
            return index
    return None


def uses(sequence):
    """The sequences this one runs directly, in the order it runs them."""
    names = []
    for step in sequence.get('steps') or []:
        if step.get('kind') == KIND_SEQUENCE:
            name = (step.get('target') or '').strip()
            if name:
                names.append(name)
    return names


def used_by_sequences(sequences, name):
    """Which sequences run `name`, so the reuse is visible from both ends."""
    wanted = (name or '').strip().lower()
    return [other['name'] for other in sequences or []
            if wanted in [used.strip().lower() for used in uses(other)]]


def repoint(sequences, old_name, new_name):
    """Point every nested step at a sequence's new name. Returns how many moved.

    Called when a sequence is renamed. Without it a rename is a quiet way to
    break every sequence that runs this one -- they would go on naming
    something that is not there and fail at the step, which on a shutdown means
    finding out in the morning.
    """
    was = (old_name or '').strip().lower()
    now = (new_name or '').strip()
    if not was or not now:
        return 0
    moved = 0
    for sequence in sequences or []:
        for step in sequence.get('steps') or []:
            if step.get('kind') != KIND_SEQUENCE:
                continue
            if (step.get('target') or '').strip().lower() == was:
                step['target'] = now
                moved += 1
    return moved


def would_loop(sequences, host_name, candidate_name):
    """Whether host running candidate would make a sequence reach itself.

    Asked before a step is saved rather than only caught when it runs. A loop
    is refused at the point it is chosen, where there is something to say about
    it, rather than at six in the morning when the shutdown gives up half way.
    """
    host = (host_name or '').strip().lower()
    candidate = (candidate_name or '').strip().lower()
    if not host or not candidate:
        return False
    if host == candidate:
        return True

    by_name = dict((seq['name'].strip().lower(), seq)
                   for seq in sequences or [] if seq.get('name'))
    # Walk out from the candidate: if the host is anywhere below it, adding it
    # to the host closes a ring.
    seen = set()
    pending = [candidate]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == host:
            return True
        found = by_name.get(current)
        if found is None:
            continue
        pending.extend(used.strip().lower() for used in uses(found))
    return False


def expand(sequence, sequences, _depth=0, _seen=None, _budget=None):
    """This sequence's steps with any nested sequence's steps spliced in.

    Done before anything runs rather than while it runs, so everything below --
    the runner, the resume index a long pause writes down, the progress dialog
    -- goes on working with one flat list of steps and knows nothing about
    nesting.

    A step naming a sequence that cannot be run is left as it is rather than
    dropped: a missing name and a loop are both worth reporting when the
    sequence runs, and _run_step raises on the kind for exactly that reason. A
    dropped step would be a shutdown that quietly did less than it was told to.

    The nesting step's own pause belongs after everything it brought in, so it
    moves to the last of the spliced steps. An empty nested sequence brings in
    nothing, pause included: that pause exists to let an action land, and no
    action happened.
    """
    seen = set(_seen or ())
    budget = [MAX_EXPANDED_STEPS] if _budget is None else _budget
    by_name = dict((seq['name'].strip().lower(), seq)
                   for seq in sequences or [] if seq.get('name'))
    here = (sequence.get('name') or '').strip().lower()
    seen.add(here)

    out = []
    for step in sequence.get('steps') or []:
        if budget[0] <= 0:
            break
        if step.get('kind') != KIND_SEQUENCE:
            out.append(step)
            budget[0] -= 1
            continue

        wanted = (step.get('target') or '').strip().lower()
        nested = by_name.get(wanted)
        if (nested is None or wanted in seen or _depth >= MAX_NESTING):
            # Left to fail at run time, with the reason it could not be run.
            out.append(step)
            budget[0] -= 1
            continue

        inner = expand(nested, sequences, _depth + 1, seen, budget)
        # The last slot of a sequence is almost always empty -- fifteen of them
        # and most unused -- and the pause has to land on the last step that
        # does something, not on the blank after it, where nothing would ever
        # wait for it.
        last = _last_doing_something(inner)
        if last is None:
            continue
        pause = step.get('pause') or 0
        if pause:
            # A copy, because the step being carried over belongs to the nested
            # sequence and must not be given the host's pause on disk.
            inner = (inner[:last] + [dict(inner[last], pause=pause)]
                     + inner[last + 1:])
        out.extend(inner)
    return out


# What became of a step. One vocabulary, so the runner, the menus and the phone
# all say the same four words about the same four things -- and so that
# "skipped" never has to be inferred from "not done and not failed".
DID = 'done'
SKIPPED = 'skipped'      # nothing left to do; see already_there
FAILED = 'failed'
WAITING = 'waiting'      # stopped here for a long pause, will carry on
CANCELLED = 'cancelled'  # somebody backed out of the progress dialog


def describe_run(record, now=None):
    """One line for what became of a run, or '' if there has not been one.

    Counts rather than a verdict, because "3 done" and "3 done, 2 failed" are
    different answers to the question being asked and a single word for both
    would be the thing this was built to stop.
    """
    if not isinstance(record, dict) or not record.get('steps'):
        return ''
    bits = []
    for key, word in ((DID, 'done'), (SKIPPED, 'already done'),
                      (FAILED, 'FAILED')):
        count = record.get(key) or 0
        if count:
            bits.append('%d %s' % (count, word))
    if record.get('waiting'):
        bits.append('still waiting')
    if not bits:
        bits.append('nothing ran')
    return '%s  (%s)' % (', '.join(bits), describe_when(record.get('at'), now))


def describe_when(stamp, now=None):
    """How long ago, said the way a person would.

    Relative rather than a clock time: "2 hours ago" answers "did this morning's
    one work" without anyone having to work out what time it is now.
    """
    if not stamp:
        return 'at some point'
    seconds = (now if now is not None else time_module.time()) - stamp
    # Covers a clock that went backwards too: a negative gap is under ninety
    # seconds, and "just now" is the right answer for both.
    if seconds < 90:
        return 'just now'
    minutes = int(seconds // 60)
    if minutes < 60:
        return '%d min ago' % minutes
    hours = int(minutes // 60)
    if hours < 24:
        return '%d hour%s ago' % (hours, '' if hours == 1 else 's')
    days = int(hours // 24)
    return '%d day%s ago' % (days, '' if days == 1 else 's')


def describe_outcome(outcome):
    """The one word a person would use for it."""
    return {DID: 'done', SKIPPED: 'already done', FAILED: 'FAILED',
            WAITING: 'waiting', CANCELLED: 'cancelled'}.get(outcome, outcome)


def describe_wait(seconds):
    """A pause said the way a person would say it: "12 minutes", "90 seconds"."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return '%d second%s' % (seconds, '' if seconds == 1 else 's')
    minutes, rest = divmod(seconds, 60)
    if rest:
        return '%d min %d sec' % (minutes, rest)
    return '%d minute%s' % (minutes, '' if minutes == 1 else 's')


def work_left(steps, index):
    """Whether any step from `index` on would actually do something.

    A sequence is fifteen slots and most of them are empty, so "is there a step
    after this one" is not a question about the length of the list. Without
    this, a pause on the last real step would be deferred and come back to find
    nothing but blanks.
    """
    for step in steps[index:]:
        if step.get('kind') != KIND_NONE:
            return True
    return False


def already_there(step, state):
    """Whether `state` says this step's device is already as the step wants.

    None means the question cannot be answered, which is not the same as no:
    a device that does not report, a state that failed to read, an infrared
    command that nothing can confirm, a toggle that has no target state at all.
    Everything that cannot be answered is done rather than skipped, because a
    blind left open because a bulb lied is worse than a motor running for two
    seconds.
    """
    if not isinstance(state, dict):
        return None

    kind = step.get('kind')
    if kind == KIND_POSITION:
        where = state.get('position')
        if not isinstance(where, (int, float)) or isinstance(where, bool):
            return None
        try:
            wanted = int(step.get('action'))
        except (TypeError, ValueError):
            return None
        return abs(float(where) - wanted) <= POSITION_TOLERANCE

    if kind == KIND_POWER:
        action = step.get('action')
        if action not in (ACTION_ON, ACTION_OFF):
            # A toggle is asking for the other one, whatever it is now.
            return None
        power = state.get('power')
        if power not in (ACTION_ON, ACTION_OFF):
            return None
        return power == action

    # A scene sets several lights to a colour and a brightness; "already there"
    # is a judgement rather than a comparison, and getting it wrong leaves the
    # room wrong. Infrared is never confirmable at all.
    return None


def still_needed(step, targets, states):
    """The targets a step still has work to do on.

    `states` maps device id to what it last said, or None where nothing is
    known. A device that cannot be asked is always included: see already_there.
    """
    if not states:
        return list(targets)
    remaining = []
    for device in targets:
        if already_there(step, states.get(device.device_id)) is not True:
            remaining.append(device)
    return remaining


def checkable(step):
    """Whether this kind of step could ever be skipped as already done.

    Only a position and an explicit on or off. Everything else falls through to
    False, and a door is the one worth saying out loud: a bolt does report
    whether it is thrown, so the comparison would work, and it is still never
    asked. A reading that wrongly says "already locked" leaves a front door
    open all night, where throwing a bolt that is already thrown costs nothing.
    The trade that makes skipping worth it for a blind is upside down at a
    door, so a lock is not even asked where it is.
    """
    if step.get('kind') == KIND_POSITION:
        return True
    return (step.get('kind') == KIND_POWER
            and step.get('action') in (ACTION_ON, ACTION_OFF))


def resolve_targets(step, devices):
    """Which devices a step acts on. Empty means the step cannot run.

    Matched on device id first and name second, so renaming a device does not
    break a sequence that referred to it, and a sequence written by hand with a
    friendly name still works.
    """
    target = step.get('target') or ''
    driver = step.get('driver') or ''

    if target == TARGET_ALL:
        return [d for d in devices
                if d.enabled and (not driver or d.driver == driver)]

    wanted = target.strip().lower()
    matches = [d for d in devices if d.device_id.lower() == wanted]
    if not matches:
        matches = [d for d in devices if d.name.strip().lower() == wanted]
    return matches


def run(app, sequence, log_func=None, sleep_func=None, on_step=None,
        start=0, defer=None, states=None, on_outcome=None):
    """Run one sequence from `start` on. Returns (steps done, [failures]).

    One step failing does not stop the rest. A sequence is a list of separate
    intentions -- lights, plugs, a television -- and a plug that has been
    unplugged is no reason to leave the rest of the room untouched. Every
    failure is collected and reported together at the end.

    Pass `defer` -- called as defer(next_index, seconds) -- to be given back a
    long pause instead of sleeping through it. The sequence stops there and the
    caller is expected to call again with `start=next_index` once the wait is
    over. Without it the pause is slept, which is what it always did; that is
    why a caller with nowhere to write the wait down can go on passing nothing.

    `start` skips the steps already done. It is an index into the whole slot
    list, not a count of the filled ones, so it survives an empty slot in the
    middle.

    Pass `states` -- {device id: what it last said} -- to have a step leave
    alone whatever is already as it wants it. A step with nothing left to do is
    not done, it is skipped: it does not count toward the total, and its pause
    does not happen either, because that pause is there to let an action land
    and no action happened.

    `on_outcome(index, step, outcome, why)` hears what became of every step it
    reaches, in the vocabulary above. It is how anything downstream knows that
    a step was skipped by design rather than quietly not run -- which is the
    whole difference between "the blinds were already shut" and "the blinds did
    not close", and is not recoverable from the (done, errors) pair.
    """
    from devices import ControlError

    log = log_func or (lambda message: None)
    sleep = sleep_func or time_module.sleep
    done = 0
    errors = []

    steps = list(sequence.get('steps') or [])
    for index in range(max(0, start), len(steps)):
        step = steps[index]
        if step.get('kind') == KIND_NONE:
            continue
        def said(outcome, why=''):
            if on_outcome is not None:
                on_outcome(index, step, outcome, why)

        if on_step is not None and on_step(index, step) is False:
            said(CANCELLED)
            log('Sequence "%s" cancelled at step %d'
                % (sequence.get('name'), index + 1))
            break

        try:
            if _run_step(app, step, states) is False:
                said(SKIPPED)
                log('Sequence "%s" step %d: already done'
                    % (sequence.get('name'), index + 1))
                # No pause either: it is there to let an action land.
                continue
            done += 1
            said(DID)
        except ControlError as exc:
            errors.append('Step %d: %s' % (index + 1, exc))
            said(FAILED, str(exc))
            log('Sequence "%s" step %d failed: %s'
                % (sequence.get('name'), index + 1, exc))
        except Exception as exc:
            errors.append('Step %d: %s' % (index + 1, exc))
            said(FAILED, str(exc))
            log('Sequence "%s" step %d raised: %s'
                % (sequence.get('name'), index + 1, exc))

        pause = step.get('pause') or 0
        if not pause:
            continue

        # A long pause with something still to do on the far side of it is
        # handed back rather than slept. A long pause with nothing after it is
        # slept like any other -- coming back to a sequence to run no steps
        # would be bookkeeping for its own sake.
        if (defer is not None and pause >= LONG_PAUSE_SECONDS
                and work_left(steps, index + 1)):
            # No outcome said here. This step already reported what became of
            # it; the waiting belongs to the sequence, and the caller knows
            # about it because its own defer was the thing that just fired.
            defer(index + 1, pause)
            log('Sequence "%s": %d step(s) done, %d failed, waiting %s'
                % (sequence.get('name'), done, len(errors),
                   describe_wait(pause)))
            return done, errors

        sleep(pause)

    log('Sequence "%s": %d step(s) done, %d failed'
        % (sequence.get('name'), done, len(errors)))
    return done, errors


def _run_step(app, step, states=None):
    """Carry out one step. False if there was nothing left to do.

    Raises ControlError if it cannot be done. False is not a failure: it is the
    blind that was already shut, and the caller counts it apart from both.
    """
    from devices import ControlError

    kind = step.get('kind')

    if kind == KIND_SCENE:
        if not app.apply_scene_by_name(step['target'], announce=False):
            raise ControlError('No scene called "%s"' % step['target'])
        return

    if kind == KIND_SEQUENCE:
        # Everything runnable was spliced in by expand() before any of this
        # started, so a step of this kind still standing here is one that could
        # not be: a name that is not there, a ring, or nesting too deep. Which
        # one is worth saying, so it is worked out here rather than guessed at.
        name = step.get('target') or ''
        known = find(getattr(app, 'sequences', None) or [], name)
        if known is None:
            raise ControlError('No sequence called "%s"' % name)
        raise ControlError('"%s" cannot run here: it would reach itself, or '
                           'the nesting is more than %d deep'
                           % (name, MAX_NESTING))

    targets = resolve_targets(step, app.devices)
    if not targets:
        raise ControlError('Nothing matches "%s"' % step.get('target'))

    # Narrowed rather than all-or-nothing: with four plugs already off and one
    # on, the step is for that one. An empty list is the whole step skipped.
    wanted = still_needed(step, targets, states)
    if not wanted:
        return False
    targets = wanted

    if kind == KIND_POWER:
        action = step.get('action')
        if action == ACTION_TOGGLE:
            _report(app.toggle_all(targets))
        else:
            _report(app.power_all(action == ACTION_ON, targets))
        return

    if kind == KIND_COMMAND:
        volume = step.get('volume')
        missed = []
        for device in targets:
            if volume is not None:
                # Before the clip, so it starts at this volume. A beacon that
                # will not take one -- no mpv on the Pi -- still plays: the
                # announcement is the point of the step. It is reported after,
                # so the run says the clip went out at the wrong volume rather
                # than that it went out fine.
                try:
                    app.controller.set_volume(device, volume)
                except ControlError as exc:
                    missed.append('%s: %s' % (device.name, exc))
            app.controller.send_command(device, step['action'])
        if missed:
            raise ControlError('Played, but the volume was not set (%s)'
                               % '; '.join(missed))
        return

    if kind == KIND_POSITION:
        for device in targets:
            app.controller.set_position(device, int(step['action']))
        return

    if kind == KIND_LOCK:
        for device in targets:
            app.controller.lock(device)
        return

    if kind == KIND_UNLOCK:
        for device in targets:
            app.controller.unlock(device)
        return

    raise ControlError('Unknown step type "%s"' % kind)


def _report(result):
    """Turn an (applied, errors) pair into a raise or a return."""
    from devices import ControlError

    applied, errors = result
    if not applied and errors:
        raise ControlError(errors[0])
    if not applied:
        raise ControlError('Nothing accepted the command')
