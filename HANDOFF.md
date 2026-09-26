# Handoff — where Paragon Home stands

Read this first in a new session, then `CLAUDE.md` for how to work here.

## Who and what

Aryez runs the **Paragon TV Project**: Kodi 17.6 (Krypton) add-ons, Python
2.7 on the boxes, tests on Python 3 with `tests/kodistubs`. This repo is
**Paragon Home**, the home-control add-on. `script.paragontv` is the sibling
(the television half); it may need attaching to the session to read it.

Kodi boxes: a master and satellites; the office box's web remote is on port
8778. Unraid server DIVINITY at 10.0.0.39 serves the media over NFS.

## Standing rules, in Aryez's words

* *"I only need this to be accessible while on the LAN... not remotely."*
* *"stop. Icon is perfect as is. dont mess with it"* (the Harvester icon).
* A path is never built from what a caller asked for (the remote's route
  table; on the Pi the clip name is looked up in a listing, never joined).
* Tokens, keys, and any URL carrying one never reach a log.
* *"yes push it to main"* — every release is pulled from `main`. Develop on
  the session branch, push there, then push to `main`. The permission
  classifier sometimes blocks the push to main; say so and Aryez says
  "push to main" again.
* *"yes use the narrower approach from now on"* — mutation runs narrowed
  to the tests that could catch each mutant, re-run only survivors, full
  suite once at the end, `-B` with `__pycache__` cleared. Remove equivalent
  mutants. Watch for two guards covering each other; test them apart.
* A phrase cannot unlock a door.
* Nothing user-facing says "Speaker" any more: it is **Beacon**. The driver
  id stays `speaker` (it is in every devices.json and step).

## The beacon system (Aurora)

Raspberry Pis with speakers, one script (`tools/paragon_speaker.py`), clips
are files in `~/aurora` on each Pi, one clip = one file, named by filename.
Pi user `aryez`, hostname `beacon1` (kitchen); beacon2 and beacon3 to come.
Aryez copies from Windows with `scp -r aurora aryez@beacon1:` and restarts
the systemd service `paragon-speaker`.

v2.65.0 (this session's last release): beacons are players. mpv runs for
the life of the script, driven over its IPC socket; `/status`, `/pause`,
`/resume`, `/volume` beside `/play`, `/stop`, `/clips`. Without mpv: play
and stop only, `controls: false`. The web remote has a **Beacons** tab
(cards kept between polls; polls every 3 s while open; polls are not
logged). Hub verbs `pause`, `resume`, `set_volume` gated on `CAP_PLAYBACK`.
The beacon's seconds-in is `elapsed`, never `position` (a blind's).

**Confirmed on beacon1** (2.65.0): play, pause and volume work through
mpv. 2.65.1 fixed the slider jumping back to the old number after a change
(the snapshot held the last poll's reading; the beacon is now read back
after pause, resume and volume) and turned every teal on the remote orange
-- Aryez wants no teal anywhere. 2.65.2: a device that is on is filled with
the Start Paragon TV button's gradient (`--hot`), ink inverted to #1c0a04.
2.66.0: the page reads every device when opened and when brought back to
the front (`readOnOpen`, quiet, no busy). The box skips that read if it read
everything in the last `OPENED_FRESH` (60 s), because cloud lights are
rationed requests; "Read the lights" is never skipped. 2.67.0: the
service's 10-min LAN sweep (`check_addresses(heard=...)`) hands its readings
to `RemoteServer.take_reading`, merged over the rest; a None reading keeps
the last one (WiFi bulbs miss replies); it does not reset `_states_at`,
since cloud devices are not in it. 2.68.0: Sequences, Scenes and All lights
fold like the driver sections (`FOLDS`, `foldSections`), open by default,
remembered per browser under `paragon.section.home.<body id>`. 2.69.0: a
beacon clip step (KIND_COMMAND) may carry `volume` 0-100, absent = leave as
is; set before the clip in `_run_step`; a refused volume still plays and
fails the step with "Played, but the volume was not set". Asked for in
`_step_device` (so the dial and phrases get it too), not for Stop. 2.70.0: the
Pi's /status carries `free` (bytes, `shutil.disk_usage` on the clip folder);
`clean_status` keeps it as int or None; `describe_free` words it in df -h
units; the snapshot sends `free` (words) and `ip` for beacons only; the card
shows "10.0.0.60 - 24 GB free". Needs the new paragon_speaker.py on each Pi.
2.71.0: songs live in `<folder>/songs` (or `--songs`), phrases in the clip
folder; the Pi's /clips and hello send `clips` (all names, phrases first)
and `songs`; a phrase hides a song of the same name. `POST /play
{"shuffle": true}` has the Pi pick a song, never the last one when there is
another. Kodi keeps songs in speaker_clips.json under `#songs`; the driver
offers `Random song` (RESERVED with Stop) only when a beacon has songs. The
card groups phrases, then Songs with Random song first.
2.72.0: a beacon step may be `after: True` ("in turn"): `_run_step` calls
`Hub.queue_command` (CAP_PLAYBACK) -> driver -> `POST /queue {clip|shuffle,
volume}`. The Pi keeps the queue (`Speaker.enqueue/advance`, a `serve_queue`
thread every 0.2 s, START_GRACE 1 s because mpv reads idle until a file
opens); the volume is set when the clip's turn comes. Stop or a straight-
away play clears it. /status has `queued`; the card says "N to come". An
older Pi answers /queue 404 "no such path" and the step says to update it
rather than falling back to cutting in. Checked on real mpv 0.37 (--ao=null).
Confirmed on beacon1 by Aryez: step volumes, Random song, and three beacon
steps in a row played in turn.
2.73.0: TV tab -- tv_tuneRow holds Quiet/Louder (data-press "quiet" and
"louder" -> tv.one_way: to the quiet or normal level from settings, only in
that direction, Kodi mute cleared). The old Channel down/up buttons had no
handler and did nothing. On now, Remote, Maintenance and Channels are in
FOLDS; Type on the TV deliberately is not.
2.74.0: Maintenance always shows where Paragon TV is installed. tv.ANY_TIME
= ('skin', 'reboot') may run with the TV on; run_task refuses the rest
while it runs, and task_list tells the page (`any_time`), which disables
and dims them with a line saying why. The job list redraws when the TV goes
on or off (it is in the signature).
2.74.1: Quiet/Louder light with class `on` (not `lit`, which a press
flashes) in the lit channel card's fill and ink; Louder at the normal level
via player_state()['normal'] / tv.at_normal_level.

Future, agreed in outline: a mic Pi (Pi 4) with wake word "Aurora" sending
text to a `say` endpoint on the web remote, matched by the phrase book
(`voice.py`, exact match after normalisation, aliases per action).

## Other releases this session

* 2.64.0 — a device that gives no reading on a state read is looked for
  (`Hub._find_the_silent`, driver `locate`); the service sweeps every 10
  min and on startup; hold-off 5 min per silent device.
* 2.64.1 — the web remote logs every action with the phone's address:
  `Web remote: 10.0.0.37 asked for sequence "shutdown"`. Reads are not
  logged.
* Earlier: 2.59 (dial to 10 slots), 2.60 (menus re-read files; Broadlink
  diagnostic), 2.60.1 (dial slot runs a sequence under its own name), 2.61
  (phrase book), 2.62 (speakers), 2.63 (stop and replace).

## Open threads

* Aryez was reserving DHCP addresses for every device (Govee lights moved
  after a router event; refresh fixed it). Broadlinks at 10.0.0.164 and
  10.0.0.11 were failing all evening — probably moved to their new
  reservations; Diagnose device search → Broadlink says HAS MOVED.
* Bedroom TV backlight at 10.0.0.204: "No route to host" for hours.
* Unraid: emhttpd hung (monitor exit 255, nginx upstream timeout); sshd
  fixed with `/etc/rc.d/rc.sshd start` from the web terminal. Cause not
  found; suggested syslog mirror to flash and Fix Common Problems.
* At 00:29 Paragon TV's preset Phase 1 "Initial Shutdown" ran and then the
  sequence **ignition** ran from a script invocation — looks backwards.
* Offered, not taken: a blaster looked for after a failed send (like the
  state-read heal, for devices with no state); Paragon TV NFO renamer
  dry-run and real counts; SwitchBot K11+ Pro vacuum driver (API lists
  K11+, not K11+ Pro).
* Known gap: a satellite can never have a speed dial or phrase book
  (`speeddial.json`, `phrases.json` not in `SHARED_FILES`).
