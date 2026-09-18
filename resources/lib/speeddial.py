# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The speed dial: eight things worth one press from a phone.

A slot holds a sequence step and nothing more. That is the whole design, and it
is why this file is short: a step already knows how to be a scene, a sequence,
a plug, a blind, an infrared code or a deadbolt, every one of those already has
a picker in the menus, and sequences.run already knows how to carry one out
along with the lock gate, the long-pause handling and the error reporting. A
second vocabulary for the same set of actions would be a second place for every
one of them to be added.

What a slot adds is a label. A step describes itself well enough for a menu row
-- "Office Plug All outlets: On" -- and badly for a button on a phone, where
there is room for "Plugs" and the point is recognising it at a glance.

Eight, fixed, numbered, and empty until filled. Named after the sequence editor
and the Transit slots for the same reason both of those are: slot 4 is slot 4,
so the thing your thumb knows is in the third position stays in the third
position when the second one is cleared.
"""

import sequences as sequence_lib

DIAL_FILE = 'speeddial.json'

# Eight is what fits on a phone at a size a thumb can hit without looking, as
# eight full-width rows, without the page scrolling.
SLOT_COUNT = 8

# What a label may run to. Longer than this and the button either shrinks the
# text until it cannot be read across a room or wraps to three lines and stops
# lining up with its neighbours.
MAX_LABEL = 18


def empty_slot():
    return {'label': '', 'step': sequence_lib.empty_step()}


def normalise_slot(raw):
    """Clamp one slot, or return an empty one if it holds nothing usable."""
    if not isinstance(raw, dict):
        return empty_slot()
    step = sequence_lib.normalise_step(raw.get('step'))
    if step.get('kind') == sequence_lib.KIND_NONE:
        # A label with nothing behind it is a button that does nothing, which
        # is worse than a gap: a gap says "empty" and that says "broken".
        return empty_slot()
    label = (raw.get('label') or '').strip()[:MAX_LABEL]
    return {'label': label, 'step': step}


def normalise(raw):
    """Exactly SLOT_COUNT slots, whatever the file holds."""
    if not isinstance(raw, list):
        raw = []
    slots = [normalise_slot(entry) for entry in raw]
    slots = slots[:SLOT_COUNT]
    while len(slots) < SLOT_COUNT:
        slots.append(empty_slot())
    return slots


def is_filled(slot):
    return (slot.get('step') or {}).get('kind') != sequence_lib.KIND_NONE


def filled(dial):
    """The slots that do something, with their numbers. [(number, slot), ...]"""
    return [(index + 1, slot) for index, slot in enumerate(dial or [])
            if is_filled(slot)]


def move(dial, frm, to):
    """One slot moved to another position, with the rest sliding along.

    The same operation the sequence editor performs on its steps, and the same
    implementation: both are a fixed-length list of numbered slots where moving
    one shuffles the others and the length never changes. A second copy of that
    would be a second place for the off-by-one to live.

    Out-of-range and no-op moves give back the dial unchanged rather than
    raising, because the honest answer to "move this nowhere" is what you
    started with.
    """
    return sequence_lib.move_step(dial, frm, to)


def label_for(slot, device_name=None):
    """What to write on the button.

    The slot's own label where it has one, and the step's description where it
    does not -- so a slot works the moment it is filled, and is worth naming
    only when the description is longer than the button.
    """
    if not is_filled(slot):
        return ''
    label = (slot.get('label') or '').strip()
    if label:
        return label
    return sequence_lib.describe_step(slot['step'], device_name)


def describe(dial):
    """One line for a menu row: how many of the eight are in use."""
    count = len(filled(dial))
    if not count:
        return 'nothing set yet'
    return '%d of %d set' % (count, SLOT_COUNT)


def as_sequence(slot, name=None):
    """The slot as a one-step sequence, which is how it is run.

    Everything that carries out a step is reached through a sequence -- the
    lock gate, a nested sequence being spliced in, a long pause being handed
    back, the way a failure is collected and reported. Running a slot as a
    sequence of one gets all of that rather than a second copy of any of it.
    """
    steps = [slot.get('step') or sequence_lib.empty_step()]
    while len(steps) < sequence_lib.STEP_COUNT:
        steps.append(sequence_lib.empty_step())
    return {'name': name or label_for(slot) or 'Speed dial',
            'description': '',
            'time': '',
            'days': [],
            'phase': 0,
            'skip_done': False,
            'steps': steps}
