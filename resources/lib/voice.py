# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The phrase book: what somebody said, and what the house does about it.

A phrase book rather than an interpreter. "Make me a coffee" runs the sequence
called barista because somebody wrote that down, not because anything worked
out that coffee is a drink and barista makes drinks. Nothing here guesses, and
"I'm thirsty" does nothing at all.

That is the design, and it is worth being clear about why, because the other
kind is what people expect. A closed list of phrases can be read: every phrase
is one somebody chose, so the answer to "why did the kitchen light come on" is
always a line in a file. It is also what makes local speech recognition good
enough -- recognising one of forty known phrases is a far smaller job than
transcribing English, and it can be done on a Raspberry Pi with nothing
leaving the house.

An entry is one action and every phrase that triggers it. Grouped that way
rather than one row per phrase because aliases are the whole trick: speech
recognition will hear "make me a coffee" as "make me coffee" often enough that
a single exact phrase feels broken. Three spellings of the same request,
pointing at one action, stay exact -- which is to say predictable -- while
absorbing what the microphone actually does.

What an entry holds is a sequence step, for the same reason a speed dial slot
does: every kind a step can be is a kind a phrase can trigger, every one has a
picker in the menus already, and running it goes through sequences.run along
with the lock gate, the long pauses and the error reporting.
"""

import re

import sequences as sequence_lib
from compat import string_types

PHRASE_FILE = 'phrases.json'

# What somebody heard that matched nothing. This is the file that makes the
# book converge: a phrase that missed is exactly the phrase worth adding as an
# alias, and without it the only way to find out what the microphone really
# heard is to go and read a log on another machine.
HEARD_FILE = 'unheard.json'
MAX_UNHEARD = 30

# Longer than this is not something anybody says to a room; it is a sentence
# that got away from the speech recogniser. Refusing it keeps one runaway
# transcript out of the phrase book and out of the unmatched list.
MAX_PHRASE = 80

# Words dropped from both ends before matching, so "paragon lights out" and
# "lights out please" reach the same entry as "lights out". Only at the ends:
# a filler in the middle of a phrase is part of it.
POLITENESS = ('please', 'thanks', 'thank you')

_PUNCTUATION = re.compile(r'[^\w\s]', re.UNICODE)
_SPACES = re.compile(r'\s+', re.UNICODE)


def normalise(text):
    """One spelling of a phrase, for comparing two of them.

    Case, punctuation and spacing are things a speech recogniser decides and a
    person does not, so a phrase book that treats "Lights out." and "lights
    out" as different entries is one that fails for reasons nobody can see.
    """
    if not text:
        return ''
    text = _PUNCTUATION.sub(' ', text.lower())
    text = _SPACES.sub(' ', text).strip()
    for word in POLITENESS:
        # Looped rather than done once: "please thanks" is two, and a phrase
        # that ends up empty is caught by the caller either way.
        while text.endswith(' ' + word):
            text = text[:-(len(word) + 1)].strip()
        while text.startswith(word + ' '):
            text = text[len(word) + 1:].strip()
    return text


def clean_phrases(raw):
    """The usable phrases in `raw`, normalised, in order, without repeats."""
    if isinstance(raw, string_types):
        # One phrase given as a bare string rather than a list of one. A file
        # written by hand will do this, and refusing it teaches nothing.
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    seen, kept = set(), []
    for item in raw:
        try:
            phrase = normalise(item)[:MAX_PHRASE].strip()
        except (AttributeError, TypeError):
            continue
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        kept.append(phrase)
    return kept


def normalise_entry(raw):
    """One entry, or None if it holds nothing that could ever fire.

    Both halves have to be there. A step with no phrase can never be reached,
    and a phrase with no step is a thing the house appears to understand and
    then ignores -- which is worse than not knowing it, because it is
    indistinguishable from a broken device.
    """
    if not isinstance(raw, dict):
        return None
    step = sequence_lib.normalise_step(raw.get('step'))
    if step.get('kind') == sequence_lib.KIND_NONE:
        return None
    phrases = clean_phrases(raw.get('phrases'))
    if not phrases:
        return None
    return {'phrases': phrases, 'step': step}


def normalise_book(raw):
    """Every usable entry in the file, in order, with duplicate phrases dropped.

    A phrase claimed by two entries is resolved here rather than at the
    microphone, so that what fires is decided once, by the file, and is the
    same on every box that reads it. The first entry keeps it; the later one
    loses that phrase and survives on its others.
    """
    if not isinstance(raw, list):
        return []
    book, taken = [], set()
    for item in raw:
        entry = normalise_entry(item)
        if entry is None:
            continue
        entry['phrases'] = [p for p in entry['phrases'] if p not in taken]
        if not entry['phrases']:
            continue
        taken.update(entry['phrases'])
        book.append(entry)
    return book


def match(book, text):
    """The entry `text` triggers, or None.

    Exact, on the normalised phrase, and nothing else. No nearest match and no
    part of one: a house that acts on an approximation of what it heard is a
    house that does something nobody asked for, and the failure that teaches
    you it does that is the one where it was the door.

    Silence matches nothing without needing to be checked for here, because
    clean_phrases keeps empty phrases out of the book in the first place.
    """
    wanted = normalise(text)
    for entry in book or []:
        if wanted in entry['phrases']:
            return entry
    return None


def says(book):
    """Every phrase the book knows, sorted. What the recogniser can expect."""
    return sorted(p for entry in (book or []) for p in entry['phrases'])


def find_phrase(book, text):
    """Where a phrase already lives: (index, entry), or (None, None)."""
    wanted = normalise(text)
    for index, entry in enumerate(book or []):
        if wanted in entry['phrases']:
            return index, entry
    return None, None


def describe_entry(entry, device_name=None):
    """One line for a menu row: what it does, and how many ways to ask."""
    if not entry:
        return ''
    does = sequence_lib.describe_step(entry['step'], device_name)
    count = len(entry['phrases'])
    if count == 1:
        return '"%s"  ->  %s' % (entry['phrases'][0], does)
    return '"%s" (+%d more)  ->  %s' % (entry['phrases'][0], count - 1, does)


def describe(book):
    """One line for the menu above it: how much the house has been taught."""
    entries = len(book or [])
    if not entries:
        return 'nothing said yet'
    phrases = len(says(book))
    if phrases == entries:
        return '%d phrase%s' % (phrases, '' if phrases == 1 else 's')
    return '%d phrases, %d action%s' % (phrases, entries,
                                        '' if entries == 1 else 's')


# -- what was heard and did not match ---------------------------------------

def normalise_unheard(raw):
    """The misses file, trimmed and in order, newest first."""
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = normalise(item.get('text'))[:MAX_PHRASE].strip()
        if not text:
            continue
        try:
            count = int(item.get('count') or 1)
        except (TypeError, ValueError):
            count = 1
        try:
            at = float(item.get('at') or 0)
        except (TypeError, ValueError):
            at = 0.0
        kept.append({'text': text, 'count': max(1, count), 'at': at})
    return kept[:MAX_UNHEARD]


def remember_miss(misses, text, now=None):
    """Note a phrase that matched nothing. Returns the new list.

    Counted rather than listed twice, and moved to the front, because the
    thing worth knowing is which miss keeps happening -- said once, it was a
    slip of the tongue; said five times, it is a phrase this house should
    know.
    """
    text = normalise(text)[:MAX_PHRASE].strip()
    if not text:
        return list(misses or [])
    stamp = now if now is not None else 0.0
    kept, count = [], 1
    for entry in (misses or []):
        if entry.get('text') == text:
            count = int(entry.get('count') or 1) + 1
            continue
        kept.append(entry)
    return [{'text': text, 'count': count, 'at': stamp}] + kept[:MAX_UNHEARD - 1]


def forget_miss(misses, text):
    """Drop one miss, which is what adding it as a phrase should do."""
    text = normalise(text)
    return [e for e in (misses or []) if e.get('text') != text]


def describe_miss(entry):
    """One line for a menu row, saying how often rather than when."""
    if not entry:
        return ''
    count = int(entry.get('count') or 1)
    if count == 1:
        return '"%s"' % entry.get('text', '')
    return '"%s"  (heard %d times)' % (entry.get('text', ''), count)
