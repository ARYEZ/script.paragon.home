# Working on Paragon Home

## Commands

```
python3 tests/test_paragon_home.py     # the suite, ~105s for 1000+
python3 tests/check_py2.py             # Kodi 17.6 runs Python 2.7
python3 tests/validate_addon.py        # addon.xml and settings.xml
```

Run one class or one test by name:
`python3 tests/test_paragon_home.py TestTheSpeedDial.test_a_dial_always_has_eight_slots`

## Constraints

* **Kodi 17.6, Python 2.7.** `check_py2.py` is the gate; the tests themselves
  run on Python 3 with the stubs in `tests/kodistubs`.
* **Never push to `main` without asking.** Development happens on the branch
  named in the session.
* **Nothing that is a secret goes in a log.** Tokens, keys, and any URL
  carrying one.

## Testing practice

Every behaviour gets a **revert-check**: break it deliberately, confirm a test
fails. A test that passes against broken code is not a test. This has caught
real defects repeatedly — silent skips, guards covering each other, a test that
slept for twelve minutes instead of failing.

**Keep the mutation runs narrow.** Select the tests that could plausibly catch
each mutant, not a whole class. `TestWebRemote` takes ~36 seconds because it
starts a real HTTP server per test; running it against a mutant in
`speeddial.py` buys nothing and costs most of the wall clock. When mutants
survive, **re-run only the survivors**, not the whole set. Run the full suite
once at the end rather than after each file.

Two failure modes worth watching for, both seen here:

* **Two guards covering each other.** If the Hub and the driver both refuse the
  same thing, each makes the other look tested. Test them apart.
* **A test that asserts the outcome but not the reason.** A refusal that passes
  whichever guard fired is a test that would not notice the wrong one firing.

**Remove equivalent mutants rather than keeping them.** Code no test can
distinguish is dead, and dead code shaped like a safety check is worse than
none — it invites someone to rely on it.

## Shape of the thing

`hub.py` is the one place every device write passes through, from the menus,
the web remote, scenes and sequences alike. Rules that must hold everywhere go
there, once. `sequences.py` is the one place a step is carried out — the speed
dial runs a slot as a one-step sequence rather than carrying it out itself, for
that reason.

The web remote's page is a plain triple-quoted string in `remote.py`, so a
backslash-n in it becomes a real newline by the time a browser sees it. Inside a
JavaScript string literal that is a syntax error that blanks the whole page.
There is a test that walks the script for it.
