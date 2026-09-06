# Paragon Home

Control your LAN smart home from inside Kodi, instead of reaching for a
separate app per brand.

Started as Govee light control and grew a driver layer, so it now speaks
Govee, Broadlink, Tuya, Kasa and SwitchBot. Adding a vendor is another driver,
not a change to the menus, the scene engine or the device registry.

Part of the **Paragon TV** project, alongside
[`script.paragontv`](https://github.com/Aryez/script.paragontv),
[`script.paragonsentry`](https://github.com/Aryez/script.paragonsentry) and
[`skin.paragon`](https://github.com/Aryez/skin.paragon).

Built for **Kodi 17.6 (Krypton)** — `xbmc.python 2.25.0`, Python 2.7.

---

## What it does

* **Govee lights** over the Govee LAN protocol, the Govee cloud API, or both —
  power, brightness, RGB colour and colour temperature.
* **Broadlink RM blasters** — learn an infrared or radio code from a remote
  and send it back, over the LAN with no Broadlink account. Radio needs a
  Pro-class blaster; a Mini has no transmitter for it.
* **Tuya smart plugs** — GHome, Gosund, Smart Life and most no-name plugs,
  protocol 3.1 to 3.4. Multi-outlet plugs are listed one device per outlet.
* **Tuya roller shades and curtain motors** — opened and closed to a
  percentage, entirely on the LAN, with the position read back from the
  motor. No hub, no account, no cloud.
* **TP-Link Kasa plugs** — HS100, HS103, HS110 and the KP series. No account,
  no key, no setup: found is the same as usable.
* **SwitchBot blinds and shades** — Blind Tilt, Curtain and Roller Shade,
  opened and closed to a percentage. The one thing here that is not on the
  LAN: it goes through SwitchBot's servers, because there is no local API.
* **Scenes** — named presets (`Movie Night`, `Paused`, `All Off`, …) that you
  can edit in the add-on and re-use everywhere.
* **Playback lighting** — an optional service that dims when playback starts,
  brings the lights part-way up on pause, and restores them on stop.
* **Remote and keymap friendly** — every action is reachable via `RunScript()`,
  so it can go on a button or in Favourites.
* **A web remote** — one page on your LAN, behind a PIN, for the phone in your
  pocket. Scenes, sequences, lights and plugs.

### Why two transports

| | LAN API | Cloud API |
|---|---|---|
| Needs internet | No | Yes |
| Needs an API key | No | Yes |
| Latency | Milliseconds | Hundreds of ms |
| Rate limited | No | Yes (daily + per-device) |
| Requires | "LAN Control" on in the Govee app | Nothing on the device |

The default mode is **Automatic**: LAN when the light supports it, cloud when it
doesn't. This matters for the playback service — every play/pause would
otherwise eat cloud quota.

---

## Installing

1. Download this repository as a ZIP (**Code → Download ZIP** on GitHub).
2. In Kodi: **Settings → Add-ons → Install from zip file**.
3. Pick the ZIP.

Kodi requires the folder inside the ZIP to be named `script.paragon.home`.
GitHub's ZIP names it `script.paragon.home-main`, so either rename the folder
before zipping, or copy the folder straight into your Kodi `addons` directory
and restart Kodi.

---

## First run

### For LAN control (recommended)

1. In the **Govee Home** app, open each light → **Settings** → turn on
   **LAN Control**. Not every model has it; check Govee's supported list.
2. Make sure the lights and the Kodi box are on the same subnet, and that
   UDP ports **4001**, **4002** and **4003** aren't blocked between them.
3. Open **Paragon Home** → it offers to search. Or **Settings → Lights →
   Search for lights now**.

### For cloud control

1. In the **Govee Home** app: **Profile → About Us → Apply for API Key**. The
   key arrives by email.
2. Paste it into **Settings → Govee Cloud → Govee API key**.
3. Run a device search.

Lights found on both transports are merged into one entry, and the friendly
name you set in the Govee app is used.

---

## Using it

Launch the add-on for the control panel:

```
Govee (34)
Broadlink (2)
Tuya (5)
Scenes...
Refresh devices
Diagnose device search...
Settings
```

One row per kind of device rather than one per device. With three drivers and
forty devices a flat list ran off the screen, and a Broadlink blaster sat
among the bulbs offering a brightness it does not have. Sorting by driver
first means every menu below this point can offer only what that kind of
device actually does — a plug gets Toggle/On/Off and a status read, a blaster
opens straight into its learned codes, and only Govee rows carry a `[LAN]`
tag, because Govee is the only driver with more than one way to reach a
device.

A driver appears once it has found something. **Diagnose device search** stays
at the top level on purpose: the time you need it is when a driver found
nothing and so has no menu of its own.

Picking a driver gives you its own devices and nothing else:

```
Govee (34)
  All Govee (34)
  Back Office Left Low  [LAN]
  ...
  Manage Govee devices...
```

The title bar shows the running version, which is the quickest way to confirm
a `git pull` took effect.

Pick a light (or **All Lights**) for power, brightness, colour and colour
temperature. **Manage devices** lets you rename a light, exclude one from
"All Lights", or flash it to work out which physical unit it is
(ten flashes, cancellable, and the light is put back as it was).

#### Switch a light without styling it

**Manage devices → the light → Switch only (no colour or brightness)** for a
strip that something else drives, or one you have set by hand and want left
alone. It still goes on and off with everything else in the house; nothing in
this add-on will ever colour it or dim it again.

What it does is stop the light being a light as far as scenes are concerned —
the same footing a plug is already on, which is why every scene passes over it
at once rather than each one having to be told. "Make all the lights red"
skips it too. The menu row turns into **Let scenes control this light** to put
it back.

### Naming your lights

LAN discovery has no names to work with, so lights start out as `H6008 (1E3E)`
— the model plus the last four of the Govee id. Two ways to fix that:

**Set a Govee API key** (*Settings → Govee Cloud*) and run **Refresh
devices**. The names you already gave the lights in the Govee app are pulled
in and applied to all of them at once. The key is used only for discovery and
naming — control still goes over the LAN, so nothing about speed or rate
limits changes. This is by far the least work.

**Or name them by hand:** **Manage devices → Name lights one by one…** Each
light comes on bright magenta in turn and stays lit while the keyboard is up,
so you can look at the room and type what you see. Each one is put back to its
previous state before the walk moves on. Cancel the keyboard to stop; names
entered up to that point are kept, and the walk offers to cover only the
lights still on placeholder names.

### Where names live, and keeping them

Names are stored in `devices.json` in the add-on's profile directory:

```
Windows   %APPDATA%\Kodi\userdata\addon_data\script.paragon.home\
          (portable: <kodi>\portable_data\userdata\addon_data\...)
Linux     ~/.kodi/userdata/addon_data/script.paragon.home/
LibreELEC /storage/.kodi/userdata/addon_data/script.paragon.home/
```

They are written every time a light is renamed or a search completes, so
there is no "save" step. Copy that file somewhere if you want a backup — it
is also how you move your names to another Kodi box.

**Switching transports does not affect names.** Any name that is not the
model-plus-id placeholder is carried across every refresh, so names pulled in
from the Govee cloud stay put after switching to **LAN only** — you can drop
the API key afterwards and keep them. The same is true of names typed by hand
and of the enabled/disabled flag.

A light that does not answer a search keeps its entry rather than being
dropped, so one sleeping bulb cannot erase its own name. The refresh summary
says how many did not answer. For a light that is genuinely gone, use
**Forget this light** in its Manage devices menu.

### Colours (the speed dial)

The named colours offered whenever you pick a colour. **Manage colours…** at
the bottom of any colour menu covers the lot:

* **Add a colour…** — enter a hex code (6 or 8 digit), then name it
* **Rename**, **Change colour**, **Move up/down**, **Delete** on any entry
* **Reset to the built-in colours**

Rows show their hex, e.g. `Paragon Purple  #963CDC`. Order is the menu order,
which is what **Move up/down** is for.

The list lives in `palette.json` next to `scenes.json` and `devices.json`, so
it survives upgrades and can be copied to another box. The same colours are
offered in the scene editor, so anything added once is available everywhere.

A saved colour can also be driven by name:

```
RunScript(script.paragon.home,action=color,value=Govee Pink)
```

### Scenes

**Scenes…** applies a preset; **Manage scenes…** edits them. A scene sets any
combination of power, brightness and colour/temperature, and can target every
light or just a few. The starter set is `Movie Night`, `Paused`, `Lights Up`,
`Warm Evening`, `Paragon Purple` and `All Off`.

Scenes live in `scenes.json` in the add-on's profile directory, so they survive
upgrades and can be hand-edited or copied between boxes.

#### Lights that need their own brightness

Not every light does the same thing with the same percentage. A lightbar at
50% throws far more light than a bulb at 50%, and a strip washing a wall
behind a screen at the level that suits the room's bulbs is either glare or
nothing at all. So a scene carries two more brightnesses beside its own:

| Row | Applies to | Default |
|---|---|---|
| **Lightbar brightness** | anything whose model is a known lightbar SKU, or whose name contains "Lightbar" | same as the scene |
| **Backlight brightness** | anything whose name contains "Backlight" | same as the scene |

Both sit in the scene editor under the ordinary **Brightness** row, and both
default to *same as above* — set one only when that kind of light needs a
different figure. They override the scene's brightness for those lights and
nothing else, including in a captured scene.

A backlight is matched on its name alone, with no model list behind it. What
makes a light a backlight is where it is pointed rather than what it is: the
same bulb is a backlight behind a screen and an ordinary light in a lamp, and
the name is the only place that fact is written down. Rename a light to
include "Backlight" and it takes the backlight figure; take the word out and
it goes back to the scene's own. A light matching both rules takes the
backlight figure, the more specific of the two.

### Mixing several colours over the lights

A scene can hold a *set* of colours rather than one. In the scene editor:

**Appearance → Mix of colours (spread over the lights)…** then tick as many
saved colours as you like.

Applying the scene deals those colours across the target lights:

* **Evenly** — each colour is used `floor(n/k)` or `ceil(n/k)` times. With 25
  lights and 3 colours that is 9/8/8, not whatever chance produces.
* **Randomly** — which light gets which is shuffled, so the same scene
  arranges differently every time you apply it. Re-apply for a new pattern.
* Which colour gets the spare light is randomised too, so no colour is
  systematically favoured over repeated applications.

Brightness and power apply to every light as usual; only the colour varies.

Mix colours are **copied into the scene**, not referenced by name, so editing
or deleting a palette colour later cannot silently change a scene you already
built. A per-light entry from a **capture** still wins over the mix, so the
two compose.

### Cycling a mix

A mix scene can move the colours along on a timer. In the scene editor:

**Cycle colours →** *Every minute* (or 15s / 30s / 2m / 5m / 15m, or off).

Every light steps to the next colour in the mix on each tick. The background
service does the stepping, so it keeps going while you browse Kodi and resumes
after a Kodi restart.

Stepping **rotates** the arrangement rather than re-dealing it. That keeps the
even spread exactly — 9/8/8 becomes 8/8/9, never 12/7/6 — and guarantees every
light genuinely changes colour, which a fresh random deal would not.

Applying any other scene **stops** a running cycle, so dimming for a film is
not overwritten a minute later. While one is running the main menu shows a
**Stop cycling (…)** row.

A cycle step sends only the colour — power and brightness are already where
the previous step left them — and sends are paced a few milliseconds apart, so
a step is a manageable trickle rather than a burst of datagrams to every light
at once.

**Cycling is a LAN feature.** Every step drives every light, which is free over
the LAN and metered over the cloud. 25 cloud-driven lights on a one-minute
cycle is roughly 36,000 API calls a day against a Govee limit near 10,000 — the
lights would stop responding partway through the day. The add-on warns before
letting you set an interval that would do this.

### Govee Tap-to-Run scenes

Tap-to-Run scenes cannot be triggered through the Govee API. They are not part
of the LAN protocol, and the developer API key does not reach them either —
they run over Govee's undocumented AWS IoT channel, which needs your Govee
account email and password.

Use **Capture lights as a scene…** instead — it is on the main menu, and
also inside **Scenes…**:

1. Run the Tap-to-Run in the Govee app, so the lights are how you want them.
2. In Kodi, capture it and give it a name.

The add-on reads every light's current power, brightness, colour and colour
temperature and stores them as a scene that holds a *different setting per
light*. Replaying it is pure LAN — instant, no account credentials, no cloud,
no rate limit — which also makes it safe to drive from playback.

**The real limit — read this before relying on capture.** The Govee LAN
protocol reports only power, brightness, RGB and colour temperature. It has no
concept of a *scene*. When the Govee app drives a bulb into one of its scenes,
that happens through the cloud and the bulb's LAN status keeps reporting
whatever was last set locally — so capture faithfully records a state that is
not what you are looking at.

The tell is a capture that disagrees with the room. A real example: 25 bulbs
visibly pink, of which 22 reported `RGB 0,0,0, 3800K, brightness 1`.

This has been confirmed on hardware. An H6008 was set to RGB `255,0,255` at
40% over the LAN and reported back exactly that, so the status is honest — it
simply reflects the last state set *over the LAN*, and a cloud-applied app
scene layered on top is invisible to it.

If you suspect a different model behaves worse, pick a single light and run
**Check status reporting…** from its menu. It sets that bulb to a known colour,
reads the status back, and tells you whether the two agree. If they don't, that
model never updates its LAN status — capture cannot work on it, and Toggle
cannot tell whether a light is already on.

So capture is reliable for lights set **through this add-on** (or anything
else driving plain colour/brightness over the LAN), and unreliable for lights
set by a Govee app scene or Tap-to-Run.

What works instead — **build the look in Kodi**:

1. **Scenes… → Manage scenes… → Add a new scene…**
2. Set **Appearance** (colour or colour temperature) and **Brightness**.
   Under **Colour → Custom hex…** you can paste a code straight out of the
   Govee app — 8-digit codes are accepted as well as 6-digit ones.
3. Leave **Lights** as *all lights* for a uniform look, or pick a subset.
4. **Test this scene** until the room is right, then **Save**.

That is the whole thing for a uniform look, and it is a better result than
capture would give you: it applies over the LAN, so it is instant, needs no
account credentials, and is safe to drive from playback.

Capture then becomes useful in its own right — a way to snapshot a per-light
arrangement you built up by hand, since anything set over the LAN *is*
reported back correctly.

A capture is also a snapshot of a *static* state: an animated Govee scene
(a DIY effect, "autoplay", anything that cycles) is captured as whatever frame
the lights happened to be on.

### Colour codes

Colour entry takes `RRGGBB`, the `RGB` shorthand, and the 8-digit codes the
Govee app produces.

**Govee emits `AARRGGBB` with the alpha always `FF`** — confirmed from real
codes out of the app:

| Govee code | Colour |
|---|---|
| `FF3C447F` | `#3C447F` — deep slate blue |
| `FF7F3C3C` | `#7F3C3C` — deep brick red |

Read the other way round those would mean alphas of `7F` and `3C`, i.e. bulbs
at 50% and 23% transparency, which is meaningless for a light.

The end is still inferred rather than hardcoded, because other sources differ
— Android writes `AARRGGBB`, CSS Color 4 writes `RRGGBBAA`. An alpha in a
colour picker is almost always `FF`, so whichever end reads `FF` is taken as
the alpha; when both ends are `FF` or neither is, alpha-first wins, which is
also the right answer for Govee.

The confirmation always says how a code was read — `#FF2896 (read as
AARRGGBB)` — because dropping the alpha off the wrong end produces a
different colour, and that should be visible rather than a surprise on the
wall. If a code comes out wrong, re-enter it with the byte order swapped.

### Playback lighting

**Settings → Playback lighting → Change the lights with playback.**

Choose a scene for playing, paused and stopped. Options let you limit it to
video only, require fullscreen, and delay the dim (handy if you skip trailers).

The service only restores lights on stop if it was the thing that dimmed them —
stopping a stream will never switch on lights you deliberately left off.

### RunScript actions

For keymaps, Favourites, or other add-ons:

```
RunScript(script.paragon.home)                                  # control panel
RunScript(script.paragon.home,action=toggle)
RunScript(script.paragon.home,action=on)
RunScript(script.paragon.home,action=off)
RunScript(script.paragon.home,action=brightness,value=20)       # 1-100
RunScript(script.paragon.home,action=color,value=FF8800)        # RRGGBB, AARRGGBB,
RunScript(script.paragon.home,action=color,value=Govee Pink)    #   or a saved colour name
RunScript(script.paragon.home,action=temp,value=2700)           # Kelvin
RunScript(script.paragon.home,action=scene,name=Movie Night)
RunScript(script.paragon.home,action=refresh)
RunScript(script.paragon.home,action=diagnose)                  # why no lights?
RunScript(script.paragon.home,action=verifystatus)              # does status work?
RunScript(script.paragon.home,action=command,target=Hall RM,name=TV Power)
RunScript(script.paragon.home,action=remote)                    # web remote address + PIN
```

Add `target=<name or device id>` to any of them to hit one light:

```
RunScript(script.paragon.home,action=off,target=Bedside Lamp)
```

Example `keymaps/govee.xml` in your Kodi userdata:

```xml
<keymap>
  <global>
    <keyboard>
      <f9>RunScript(script.paragon.home,action=toggle)</f9>
      <f10>RunScript(script.paragon.home,action=scene,name=Movie Night)</f10>
    </keyboard>
  </global>
</keymap>
```

---

## Sequences

A **sequence** is fifteen ordered steps run as one. Named after the Paragon TV
preset macro system, and shaped like it on purpose: a fixed number of numbered
slots rather than a list you grow, so a sequence has the same shape every time
you open it and slot 4 is always slot 4. Empty slots are normal.

A step is three choices, in the order you would say them aloud — what kind of
thing, which one, and what to do to it:

```
Wind Down
   1. Scene: Warshade
   2. Office Plug All outlets: On  (+3s)
   3. Bedroom Broadlink: TV power
   4. Empty
   ...
  10. Empty
```

Steps run top to bottom. Each can hold a **pause afterwards**, which matters
more than it sounds: a television told to switch on and change channel in the
same breath will miss the second command, because it is still waking up.

**A failing step does not stop the ones after it.** A plug that has been
unplugged is no reason to leave the rest of the room untouched — every failure
is collected and reported together at the end.

Sequences live under **Sequences...** on the first screen, and each one is also a
`RunScript()` target:

```
RunScript(script.paragon.home,action=sequence,name=Wind Down)
```

### Copying a scene

**Duplicate...** in the scene editor makes a copy under a new name and opens
it, because a duplicate exists to be changed rather than to sit there
identical. Copy `Dawn` to `Dusk`, turn the brightness down, done.

It copies what is **on screen**, not what is saved — a change made just before
pressing it is in the copy, which is what pressing "duplicate" halfway through
an edit means. The original is untouched either way.

### Commands a scene sends

A scene sets state. A command has none to set — an infrared blaster has no
colour, it has "AVR Power". **Commands to send** in the scene editor is where
one scene picks up both, so a single "Movie Night" dims the lights and
switches the amplifier on.

Commands fire *after* the lighting changes, so the lights are already moving
when the amp clicks on. Any device that has learned codes can be picked.

### What "all" means in a scene

A scene that names no targets applies to **what it can actually describe**,
not to every device in the house. A colour scene is a statement about things
that have a colour, so it reaches the bulbs and leaves the plugs alone. A
scene that only says "off" reaches everything, because off is something a plug
can be.

This matters most inside a sequence. Before it was true, a sequence whose first
step applied a colour scene switched every plug in the house as a side effect
of the power setting that came with the colour — and any plug step beside it
appeared to be switching far more than it had been asked to.

To put a plug in a colour scene deliberately, name it in the scene's target
list. Named targets are honoured exactly.

### Changing the order

Order is the whole point of a sequence, and it is rarely right first time — a
pause belongs after the step that needs it, and that is usually discovered
after the step has been typed somewhere else.

**Reorder steps...** in the sequence editor opens a screen that stays open:
pick a step, say which slot it should go to, and repeat. The step lands where
you sent it and everything it passed slides along to make room. The
destination list names what is in each slot, because the question actually
being asked is "before the scene", not "position 6".

The same move is on each step's own menu as **Move this step...**, for when
one step is already open and only that one is in the wrong place.

A step's pause travels with it. The gap means "wait for the television", not
"wait at slot 2", so moving the television moves the wait.

The row only appears once a sequence has two steps, since one step is already
in order.

### Copying a sequence

**Duplicate...** in the sequence editor copies all fifteen slots under a new
name and opens the copy, which is the point — fifteen steps is a lot to retype
for a variant.

**The copy is not scheduled**, however the original was. Two sequences firing
at the same minute on the same days is not what anyone means by "make me a
variant of this", and it is the sort of thing that would only be noticed the
following morning. Give the copy its own under **Runs:**.

### Running one on a schedule

**Runs:** in the sequence editor gives it a time and the days it applies to:

```
Ignition - when it runs
   Time: 18:00
   Days: Sat
   Stop running it on a schedule
```

The time is read forgivingly — `18:00`, `6pm`, `6:30 PM` and `1800` all mean
the same thing, which matters when it is typed on a remote control. Days are a
toggled checklist with **Every day**, **Weekdays** and **Weekends** shortcuts.

Both halves are needed. A time with no days, or days with no time, is a
schedule that can never come round, so it counts as unscheduled until it has
both.

The background service does the firing, so **Kodi has to be running**. Three
rules govern when:

* **Once per day.** Recorded on disk, so restarting Kodi does not re-run it.
* **A few minutes late still counts.** Kodi is not always awake at the exact
  minute — it may be starting up. An hour late does not: a sequence that lifts
  the lights at six should not do it at seven because the box was off.
* **Marked as run before it runs.** A sequence that fails half way must not
  retry on the next tick and every tick after that.

Moving a schedule later in the same day lets it run again — the record holds
the time as well as the date.

### Following Paragon TV

Paragon TV has a Rerack of its own — a nine-phase preset macro system, one
preset per day, each phase pinned to a time. A sequence here can hang off one of
those phases instead of keeping its own clock:

```
Curtain Up - when it runs
   Follow Paragon TV: phase 2 (wake and tune)
   What Paragon TV says about today
   Stop running it on a schedule
```

It then runs whenever that phase falls **for whichever preset today is**. The
same sequence runs at 07:00 on an Alpha day and 08:00 on a Sigma day, because
Paragon TV's own weekly table decides. So the lights can come up when the
channel does, and the room can go dark when the box shuts down.

Following a phase replaces the sequence's own time and days rather than adding
to them — two schedules on one sequence would be two answers to one question.

Nothing runs on a day Paragon TV has no preset for, on a phase a preset does
not carry (the satellite presets have no maintenance phase at all), or while
Paragon TV's own preset system is switched off. **What Paragon TV says about
today** shows the whole of today's schedule so a phase can be checked before
it is relied on.

### How the phase times are worked out

Paragon TV does not store its phase times. It holds one **anchor** per preset
and a list of **offsets in minutes**, both hardcoded in its own source and
deliberately not read from its settings, so a master and its satellites cannot
drift apart. The time fields on its settings page are disabled copies.

A master anchors at phase 1 and its eight offsets fill phases 2 to 9. A
satellite anchors at phase 2 — it has no maintenance phase at all — and its
seven fill 3 to 9.

That leaves nothing to read, so Paragon Home holds **a copy of those tables**.
A copy can go stale, so **What Paragon TV says about &lt;rerack&gt;** compares what
it computes against Paragon TV's own disabled copies and reports any that
disagree. That is the signal Paragon TV has changed its timings and this needs
updating from it.

In **Satellite Mode**, Paragon TV asks a master box over SSH which preset today
is. There is nothing on this machine to read, so a rerack cannot follow the
week — only the phase times, which are the same everywhere.

**Paragon TV's settings are only ever read, never written.** It does not need
to know this exists, and works unchanged whether or not it does.

### A sequence is not a scene

A **scene** describes a state — how the lights should look. It can be
captured, mixed and cycled, and a sequence step can apply one.

A **sequence** describes a sequence of things to do, including things with no
state to describe at all, like an infrared button press. Steps refer to
devices by id, so renaming a device does not break a sequence that used it.

There is no starter set. A sequence refers to this house's own devices, and an
invented one would be fifteen slots pointing at nothing.

---

## Reracks

A **rerack** is a day laid out in nine phases, each holding a sequence. Shaped
after the Paragon TV Rerack down to the preset names, so the two line up.

```
Alpha  -  Mon, Tue, Wed, Sat
   Phase 1 (maintenance)        -  empty
   Phase 2 (wake and tune)      -  Curtain Up  (07:00)
   Phase 3 (shut down)          -  Wind Down   (23:30)
   Phase 5 (wake and tune)      -  Curtain Up  (17:00)
   ...
   Times: its own
   Run this rerack now
```

The nine presets — Alpha, Omega, Delta, Epsilon, Gamma, Sigma, Omicron, Theta,
Lambda — always exist. An empty one costs nothing and is simply never given a
day.

**The point of the layer is reuse.** `Curtain Up` above is written once and
used at two points in the day. Its own editor says so, under **Used by**, so
changing it is never a surprise somewhere else.

A **weekly table** says which rerack a day gets, exactly as Paragon TV's does.

### Matching the week to Paragon TV

**Days: matched to Paragon TV** takes the whole weekly table from the
television and keeps taking it:

```
Which rerack runs on which day
   Days: matched to Paragon TV
   Monday     Alpha
   Tuesday    Alpha
   Wednesday  Alpha
   Thursday   Omega
   ...
```

It is read every time rather than copied once, so changing a day in Paragon TV
changes it here with nothing to press and no way for the two to drift apart.
While matched, the days are not yours to edit — they are not yours.

Your own days are kept and come back the moment you switch it off.

**Copy Paragon TV's days once** is the other half: a one-off snapshot you can
then edit, for when you want to start from the television's week and diverge.

### Where the phase times come from

**Per phase**, not per rerack. Each phase is either:

* **Run with Paragon TV** — it goes when the television runs the same phase of
  the preset *of the same name*. Alpha here follows Alpha there, which is why
  the names match and why nothing has to be said twice.
* **Set a time of my own** — it goes at that time, and the television is
  ignored for that phase.

So one rerack does both at once, which is the ordinary arrangement rather than
an exotic one:

```
Phase 5 (wake and tune)  -  Ignition  (07:00)
Phase 6 (shut down)      -  Shutdown  (with Paragon TV, 23:30)
Phase 7 (wake and tune)  -  Ignition  (with Paragon TV, 17:00)
```

Phase 5 holds the lights back to 07:00 even though the television wakes at
06:55; phases 6 and 7 go when it goes.

A phase set to follow Paragon TV shows the time it resolves to **today**, so
"waiting" and "waiting for nothing" are told apart without going to look.

When a phase says *"which has no time for it"*, **What Paragon TV says about
&lt;rerack&gt;** in the rerack editor reports which of four things is actually the
case: Paragon TV is not installed, its Rerack system is switched off, it holds
no times for that preset, or it holds some and not others.

If it holds none at all while Paragon TV's own settings clearly show times,
open that settings page and press **OK** once. Kodi hands one add-on another's
settings out of the saved file, and a setting that has never been saved is not
in it — defaults shown on screen are not the same as values written down. A
phase the television has no time for does not run — there is nothing to fall
back to, and an invented hour is worse than not firing.

### Rerack, sequence, scene

Three layers, each answering one question:

| | Answers |
|---|---|
| **Scene** | how the lights should *look* |
| **Sequence** | what to *do*, in order — fifteen steps |
| **Rerack** | when a *day* does it — nine phases |

---

## Roller shades (Tuya)

A Tuya Wi-Fi shade motor is the one blind that stays on your network. No hub,
no token, no cloud — the same local protocol as the plugs, so it keeps working
on a dead uplink and it answers with where the shade actually is.

Setup is a plug's setup: run **Refresh devices**, then give it its **local
key** under Manage devices. Nothing else.

**Buy the motor, not the bridge.** Many shades advertised as "Tuya compatible"
are radio motors with an optional Wi-Fi bridge sold beside them; those are not
on your LAN in any useful sense, and their 433MHz is the A-OK protocol that
neither Broadlink nor Bond reproduce reliably. The listing has to say the
motor itself is Wi-Fi, **no hub required**.

Telling a shade from a plug needs no table of product ids. Tuya sends both
down the same datapoints, but datapoint 1 is a bool on a plug and one of
`open` / `stop` / `close` on a cover, so the driver reads the type and knows.
Datapoint 2 is where the shade is being sent and datapoint 3 is where it has
got to; the real position is preferred and the target is the fallback, because
the cheaper motors report only what they were last told.

A shade reports a *position*, so scenes pass over it exactly as they pass over
a plug — see the SwitchBot section below for why that matters. Put one in a
**sequence** instead. Open, Stop and Close are offered as named commands
alongside the percentage, because "stop" has no percentage: it means wherever
it has got to.

---

## Blinds (SwitchBot)

**This one is not on your LAN, and that is not a choice.** A Blind Tilt speaks
Bluetooth Low Energy, a SwitchBot hub is what bridges it to anything else, and
SwitchBot publish no local API for that bridge. Bluetooth straight from Kodi
would need a library that cannot be a standard-library import on Python 2.7.
So the route to the blinds is out to SwitchBot and back.

The practical consequence: **the blinds are the only thing in Paragon Home
that stops working when your internet does.** Govee, Tuya, Kasa and Broadlink
all keep working on a dead uplink. If that matters more than having the blinds
in here, do not set this up.

You need a **hub** — the Hub 2 or the Hub Mini both work, since all that is
being used is its bridge to the cloud, not Matter — and a token and secret
from the phone app:

**Profile → Preferences → About**, then tap the app version ten times to
reveal **Developer Options**. Copy both values into **Settings → SwitchBot**.
Both are needed: every request is signed with the secret, and a token on its
own cannot sign.

Then **Refresh devices**. Only blinds and shades are adopted — bots, sensors,
plugs and the hub itself are left alone rather than half-supported.

A blind is neither a light nor a plug, and is treated as neither. It reports a
*position*, so scenes pass over it entirely: a scene describes how a room
looks, and closing the blinds during `All Off` is not something to inherit by
accident. Put a blind in a **sequence** instead, which already switches plugs
and fires infrared. The web remote draws it a slider.

**What position means on a Blind Tilt is not what it means on a curtain.** The
slats tilt rather than travelling, so both ends of the range are shut — one
tilted up, one tilted down — and the middle is open. The menu offers the
hardware's own commands (Open, Close Up, Close Down, Pause) alongside the
percentages for exactly that reason: those do whatever the blind says they do
and do not depend on anyone's reading of the number.

---

## Smart plugs (TP-Link Kasa)

**Older hardware needs nothing at all.** Run **Refresh devices** and it
appears, already carrying the name you gave it in the Kasa app. No account, no
local key, nothing to match.

**Later hardware needs your TP-Link account.** Same model number, same box,
same app — an HS103 hardware v2 answers an open protocol on port 9999, and an
HS103 hardware v5 does not answer port 9999 at all. It speaks KLAP over HTTP
and will not say a word until both ends have proved they know the password of
the account the plug is registered to.

Check which you have in the Kasa app: **device settings → Device Info →
Hardware Version**. Both are supported; only the second needs a login, under
**Settings → Kasa**.

That login is used on your own network only — nothing is sent to TP-Link, and
the plug is switched locally as before. It is stored in Kodi's add-on settings
in plain text, which is worth knowing before you type it in. There is no way
around it: TP-Link made a local protocol check a cloud credential, and the
plug refuses to answer without it.

A search sends both generations' queries together, since which one a plug is
cannot be known before asking. Later hardware announces itself on a different
UDP port and says in that announcement which protocol it speaks, so it is
listed and named even before an account is entered — it simply says what it
needs.

Kasa's local protocol obfuscates its JSON with a running XOR rather than
encrypting it. **Anything on your network can switch these plugs** — that was
true before this add-on existed, and it is why they need no key. Worth knowing
if your network is shared.

Multi-outlet models — the KP303 and HS300 strips — report their outlets in
their system info, so they are listed one device per outlet under the names
each outlet already has. A single plug like the HS103 stays one device.

### If a search finds some but not all of them

That is almost always the access point, not the plugs. Mesh systems and guest
networks routinely drop or fail to flood broadcast traffic, so a broadcast
search reaches some devices and not others — and the ones it misses look
broken when they are not. The Kasa app broadcasts too, so it can show fewer
devices than it should for the same reason.

The search handles it: after broadcasting it addresses every host on the
subnet directly, which does not depend on broadcast working at all. When that
turns up devices the broadcast missed, it says so rather than quietly papering
over a real network fault.

Which subnet to sweep is taken from every device already known — one the
search just found, or anything already in the device list, whatever its brand.
Working it out from this machine's own addresses alone is guesswork: a VPN, a
container bridge or a hostname resolving somewhere unhelpful all give a
confident wrong answer, and sweeping the wrong subnet looks exactly like
sweeping the right one and finding nothing.

The sweep assumes a /24, which is right on essentially every home network. It
is a few hundred small datagrams to port 9999 and takes about a second.

### If a search finds nothing at all

**Diagnose device search → Kasa plugs** reports what the search *did*, not
only what it found: how many each pass turned up, which subnet was swept and
how many hosts that covered, and which addresses it went out from. A sweep
that ran and found nothing means the plugs are not there; a sweep that never
ran means the search could not tell which subnet to cover. Those need opposite
responses, so they are reported differently. In short: these plugs are
2.4GHz only and neither broadcast nor the sweep crosses subnets, so a plug on a
guest SSID or a separate VLAN from Kodi will never be heard.

A plug that works in the Kasa app but is not found here at all — by either
generation's query — is on a different subnet or SSID from Kodi.

### If some plugs accept the account and others do not

**Test connection** on a plug that works says which of two things happened:

* *"Your TP-Link account was accepted"* — the details are right, and a plug
  that refuses them is bound to a different account. Remove it in the Kasa app
  and add it again.
* *"This plug needed no account at all"* — it was never bound to an account,
  so it answers regardless and proves nothing about the details. In that case
  the plugs that refuse are the honest ones and the details are wrong.

That second case is why some can work while others fail with the same
settings, and it looks like nonsense until you know which kind each plug is.

If neither explains it, `tools/kasa_klap_probe.py` asks the plug directly
which credentials and which hash scheme it wants:

```
python3 tools/kasa_klap_probe.py 10.0.0.195 you@example.com
```

The password is prompted for, never printed, never written down and never
sent anywhere but the plug on your own network.

---

## Smart plugs (Tuya)

Everything Tuya-branded is the same platform underneath, whatever the box
says: GHome, Gosund, Smart Life, Teckin and most no-name plugs.

**Discovery needs nothing.** Tuya devices announce themselves by UDP
broadcast, so **Refresh devices** finds them with no account and no setup.

**Switching one needs a local key.** This is the one genuine hurdle, and it is
not something the add-on can work around: a Tuya device will not hand out its
own key, and the only copy lives in Tuya's cloud. Discovery is still useful
without it — it tells you the device id you need in order to go and fetch the
key.

### Getting the key

On any PC with Python 3:

```
pip install tinytuya
python -m tinytuya wizard
```

The wizard needs a free [Tuya IoT Platform](https://iot.tuya.com) account:

1. **Cloud → Create Cloud Project**. Pick the data center that matches the
   region your phone app account was *created* in — a US account is almost
   always **Western America**, and this cannot be changed on the app side
   afterwards. A mismatch here is the usual reason the next step fails.
2. Accept the default API services. Three of them matter: IoT Core,
   Authorization Token Management and Smart Home Basic Service.
3. **Devices → Link App Account → Add App Account → Tuya App Account
   Authorization**, then scan the QR from your phone app under **Me → scan**
   (not the scanner inside "Add Device", which expects a device label).
4. Run the wizard with the project's Access ID and Access Secret. It writes
   `devices.json` with a 16 character `key` per device.

Treat that file as a password file: anyone on your LAN holding the key can
switch the plug.

The cloud project is only ever used to fetch keys. Control is pure LAN, and a
key keeps working after the project's free trial lapses — you would only need
the portal again after re-pairing the plug, which regenerates its key.

### Entering it

**Manage devices → the plug → Set local key**, then **Test connection**, which
reads the plug back and says whether the key was accepted. Worth doing: 16
characters typed on a remote control is an easy thing to get one keystroke
wrong, and a wrong key otherwise shows up later as a plug that will not
switch.

### After a power cut

**Manage devices → the plug → After a power cut** sets what the relay does
when mains power comes back: **stay off**, **come back on**, or **remember how
it was**. Useful for leaving the house — a plug set to stay off will not wake
up mid-holiday because the power blinked.

It is a setting on the plug itself, so it covers every outlet on that plug and
holds whether or not Kodi is running. The current value is read from the plug
rather than remembered here, and a plug that has no such setting says so
instead of showing a made-up one.

### Multi-outlet plugs

A three-outlet strip is three independent switches in one box. Once a plug has
its key, the next **Refresh devices** asks it what it has and lists one device
per outlet — `Tuya 9ABC Outlet 1`, `Outlet 2`, `Outlet 3`, `USB` — so a scene
can switch one outlet without touching the others. Rename them as you would
any other device. The old single entry disappears at that refresh; it was the
same hardware and would no longer switch anything.

A single-outlet plug stays one device rather than being called "Outlet 1".

### The whole plug at once

Alongside the outlets there is an **All outlets** entry that switches the
whole box — the one to put on a bedtime scene or a leaving-the-house button.

There is no master relay to switch: nothing in a Tuya socket's instruction set
turns the box off as a unit, and the phone app's "all off" is simply every
outlet set in one command. This does the same. Because it is one command, the
outlets go together rather than in sequence, and it is a single round trip
instead of one per outlet.

It reads as **on when anything is drawing power**, not only when everything is
— otherwise a strip with one outlet live would report itself off, and the
button that follows would turn it further on.

A bulk action like "everything off" uses the whole-plug entry and skips its
outlets, since they would be the same instruction sent three more times. A
*scene* is left alone: a scene can tell outlets different things, so folding
them together there would silently drop one.

### Plugs in scenes

A plug has power and nothing else, so a scene that sets brightness and colour
simply switches the plug and sends it nothing it cannot do. One scene can
therefore drive bulbs and plugs together with no special handling.

### Protocol versions

The device list shows what each plug speaks (`Tuya 3.3`). Versions **3.1 to
3.4** are driven.

3.4 is a different protocol wearing the same version number: it negotiates a
session key on every connection, signs each packet with HMAC-SHA256 instead of
a CRC, renumbers both verbs, and moves the version header inside the
encryption. That is all handled — a 3.4 plug behaves like any other, and a
wrong key is caught at the handshake rather than several steps later as an
unreadable reply.

**3.5** encrypts with AES-GCM, which is a different cipher rather than a bigger
version number, and is not built. A 3.5 plug is still discovered and listed,
and says so rather than failing obscurely.

---

## Satellite mode

More than one Kodi box in the house, all reaching the same lights. The lights
are on the LAN rather than attached to any one box, so the question is not who
*can* control them but who *should decide*. Satellite mode answers it: one box
is the master and keeps the setup, the others copy it down.

Turn it on under **Settings → Satellite** and give it the master's address.

**A satellite copies** the devices, scenes, sequences, colour palette and Tuya
keys from the master — on start-up, then every 15 minutes by default, and
whenever you press **Copy from the master now**.

**A satellite runs no schedule.** No reracks, no timed sequences. That is the
whole point: three boxes running the same rerack would send every phase three
times, three "off" commands to one bulb, three sequences racing through their
steps. The Reracks menu is replaced by a Satellite one, because a rerack on a
satellite would be a copy nobody should act on.

**A satellite still does anything you ask it to** — a scene or sequence from
its menu, a keymap, Paragon TV's sidebar button. It follows the master's
schedule, not the master's remote control.

### What it needs

Passwordless SSH from each satellite to the master, as `root`. If your boxes
already have it — Paragon TV's own satellite mode uses the same thing — there
is nothing more to set up. Files are read with:

```
ssh -o BatchMode=yes root@MASTER cat /storage/.kodi/userdata/addon_data/script.paragon.home/<file>
```

`BatchMode=yes` matters: without a key it fails immediately instead of sitting
at a password prompt with the service behind it.

Nothing is ever written to the master. A satellite only reads.

### When the master is unreachable

Nothing happens, quietly. The satellite keeps the copy it already has and the
lights carry on working — they are just a little out of date. A file that
arrives truncated or unparseable is discarded rather than written, because
half a scene list is worse than yesterday's whole one.

### The master owns the setup

A satellite copies the devices, scenes, sequences, palette, Tuya keys and
learned infrared codes down from its master, and that copy is a plain
overwrite — it has no idea whether anything was changed here. So a satellite
**cannot edit any of them**, and the menus do not offer it: no new sequences,
no new scenes, no capture, no renaming or forgetting a device, no palette
edits, no Tuya keys, no learning an infrared code. Make those changes on the
master and they arrive here within the sync interval.

A satellite does not search for devices either. Discovery invents entries, and
the master decides which devices the house has — so **Refresh devices** is
replaced by **Copy from the master now**.

Being refused is the kinder answer. Before v2.32.0 all of it appeared to work
and was deleted a quarter of an hour later with nothing saying why.

Everything you *run* is unaffected — a scene, a sequence, a light, a plug, a
learned code, from the menu or a keymap or the web remote. That is what a
satellite is for.

### One thing worth knowing

The Tuya keys are copied too, so a sequence step naming a plug works from a
satellite. That does put a credential on every satellite. It is the same trust
boundary as the passwordless SSH that fetched it — anyone who can reach the
master as root can already read the key — but it is worth knowing rather than
discovering.

---

## Web remote

The lights are on the LAN, and so is your phone. **Settings → Remote → Serve
the web remote on this box** turns the background service into a small web
server: one page, mobile-shaped, with the scenes and sequences this box knows
and a row for every light and plug.

It is Kore's idea pointed somewhere else. Kore drives Kodi's playback; this
drives the room.

```
Settings → Remote
  Serve the web remote on this box   [x]
  Port                               8778
  PIN                                418302
  Allow sequences from the remote    [x]
  Show the address and PIN
```

**Show the address and PIN** puts both on the television, which saves squinting
at a settings screen while typing into a phone. Then open
`http://<that address>:8778`, enter the PIN once, and the phone stays signed in
for a month.

### How it looks

Like the rest of Paragon TV: near-black, the orange-to-red accent, the diagonal
slashes in the margins, and the same condensed uppercase type. Every panel
carries a gradient across it -- dark in one corner, warming through an ember
band about two thirds across, cooling again at the far edge.

A light that reports itself on takes both the edge and the gradient in teal
instead, so the colour is doing work rather than decorating: you can tell what
is lit from across the room without reading a word.

### On a tablet

Built for a 16:9 panel — a tablet on a wall or a shelf, in landscape. That
shape is wide and short, so height is the scarce thing and the page does not
scroll on one at all. The header stays put and the screen divides into columns
that scroll inside themselves:

```
PARAGON HOME                                    v2.27.0   SIGN OUT
------------------------------------------------------------------
 SEQUENCES           ALL LIGHTS           GOVEE 5
 [ Bedtime     ]     [ On ][ Off ][ Tog ]  [ Back Office Left Low ]
 [ Wake Up     ]     Brightness 60%        [ Lounge Strip         ]
 SCENES              colour swatches       TUYA 2
 [ Movie Night ]     warm / neutral / cool [ Desk Plug            ]
 [ Paused      ]     read / search         KASA 1
```

Three columns on a 1080p panel, two on a smaller or portrait one, one on a
phone. The scene buttons size themselves against the height of the screen: a
fixed size either leaves a third of a 1080p panel empty or pushes the sequences
off the bottom of a 720p one. Type and targets grow on a large screen too,
because a panel on a wall is read from further away than a phone in a hand.

### Running it full screen

**Add it to the tablet's home screen.** Then it launches with no browser
around it at all — no address bar, no tabs. On iOS this works as it stands. On
Android it depends: Chrome only grants a home-screen launch its own window
over HTTPS, and this is plain HTTP, so you may get a shortcut that opens in a
tab instead.

**Or press "Full screen"** in the header. That is remembered, and from then on
the panel returns to full screen on the first touch each time it is opened.

It cannot simply open full screen by itself, and no page can: browsers insist
on a gesture first, or any site could take over the display of anything that
visited it. The first touch is that gesture — on a wall panel, one you were
making anyway. Press the button again to stop it.

If you want a panel that is *never* anything but this — no way out, survives a
reboot, screen stays awake — that is a job for a kiosk browser such as Fully
Kiosk, pointed at the same address. Nothing here stops you, and nothing here
can do it for you.

The typeface is **Saira Condensed**, and it ships with the add-on under
`resources/fonts/` rather than being fetched from a font host. Two reasons: a
remote that needs the internet to look right looks wrong on the LAN it was
built for, and asking a font service for it would tell that service the address
of your house. It is served from the Kodi box, only by exact filename, and it
is under the [SIL Open Font License](resources/fonts/OFL.txt).

### The PIN, and what stands behind it

A port open on the LAN with no lock on it means anything on your network can
switch every light in the house — a guest, a phone with a bad app, one of the
smart devices themselves. So there is a PIN, six digits, and it is made for you
the first time the remote starts. **The server refuses to run without one**:
switching the remote on is never quietly the same thing as opening the house up.

Six digits is a million guesses, which a script would walk through in minutes
if nothing stopped it. Something does: five wrong answers from an address and
that address is ignored for a minute, then two, then four, up to a quarter of an
hour. That turns minutes into months.

Change the PIN whenever you like. Every phone is signed out the next time the
service picks the change up.

### For scripts, not browsers

Anything that is not a browser — `curl`, a keymap, another add-on — skips the
login and sends a token instead. It is in `remote.json` in the add-on's data
folder:

```
curl -X POST http://192.168.1.50:8778/api/action \
     -H "X-Paragon-Token: <the token from remote.json>" \
     -H "Content-Type: application/json" \
     -d '{"action": "scene", "name": "Movie Night"}'
```

The actions are the ones `RunScript` already takes — `scene`, `sequence`, `on`,
`off`, `toggle`, `brightness`, `color`, `temp`, `refresh`, `sync` — plus
`states`, which asks the lights what they are currently doing, and `command`,
which fires a learned infrared code. `target` picks one device by name or id;
leave it out for everything, except for `command`, which insists on one. `GET /api/state` returns
what the page draws itself from.

### What it is not

**There is no encryption.** This is plain HTTP, so the PIN and the token cross
your LAN in the clear. A certificate would mean a browser warning on every
phone in the house forever, and Krypton's TLS is old enough that it would be a
poor one. On a home network that is the right trade, but it is a trade: anyone
who can watch your wi-fi can read the PIN as you type it.

**Do not forward the port.** It is built for the LAN and nothing about it is
hardened for the open internet. If you want it from outside the house, put it
behind a VPN.

**It is not a second brain.** The remote asks the service to do things; it does
not decide anything by itself. Close it and everything carries on.

### Which box should serve it

The master. It owns the schedule, the state and the authoritative copy of
everything, and it is the box that is always on — a remote you cannot rely on
being there is worse than no remote.

Nothing stops a satellite serving one, and it will work: a satellite reaches
the same lights. But its copy of your scenes and sequences can be up to fifteen
minutes old, so a satellite serving the remote says **Satellite, following
&lt;master&gt;** across the top of the page, and its schedule lines are there to
read rather than to act on.

### Sequences

A sequence is the most disruptive thing a stray tap can start — fifteen steps,
and possibly an hour of pauses. **Allow sequences from the remote** turns them off
without touching anything else, which is the setting to reach for if the phone
lives in a pocket.

Starting a sequence answers straight away rather than waiting for it to finish,
and the same rule the schedule follows still applies: a second sequence cannot
start inside the first one's pauses. It is refused, and the page says so.

### What happens while a sequence is running

Nothing stops. The service checks the queue every half second, including during
a sequence's pauses, so the remote answers throughout — an hour-long pause is
not an hour of a remote that does nothing.

Nothing the phone asks for runs on the web server's own thread. Requests are put
on a queue and the service loop performs them, which is the same arrangement
playback lighting already used, and it is what keeps one thread at a time in the
scene engine. The cost is up to half a second before a command starts, which is
the tick interval.

### Long sections fold away

Tap a driver's heading to open or close it, and the count beside the name says
how much is in there either way. A section with more than six devices starts
closed: thirty Govee bulbs left open bury the plugs and the blasters under a
screen and a half of scrolling, where two plugs cost nothing to show.

What you open or close is remembered by that browser — the tablet on the wall
and the phone in your pocket each keep their own idea of it.

### Blasters

Blasters get their own section, with a button for every code they have been
taught. They have no power, brightness or colour — which is why they are still
not scene targets — but the codes are as much a thing to press as an on switch
is.

One that has learned nothing yet is listed anyway, saying so. Knowing it was
found and is reachable is most of what you wanted to know, and teaching it a
code is a job for the box it is plugged into.

There is no "all" for a code. It belongs to the blaster that learned it, so
`action=command` always wants a `target`.

---

## Moving to another box

Everything the add-on knows lives in one folder as plain JSON, so moving to
another Kodi machine is a copy. Device names, scenes, sequences, reracks, the
colour palette, learned infrared codes and the Tuya keys all come across.

```
Windows     %APPDATA%\Kodi\userdata\addon_data\script.paragon.home\
Linux       ~/.kodi/userdata/addon_data/script.paragon.home/
LibreELEC   /storage/.kodi/userdata/addon_data/script.paragon.home/
```

| File | Holds |
|---|---|
| `devices.json` | every device: name, address, driver, whether it is enabled |
| `scenes.json` | scenes |
| `sequences.json` | sequences and their schedules |
| `rerack_presets.json` | the nine reracks and the weekly table |
| `palette.json` | the speed-dial colours |
| `broadlink_codes.json` | learned infrared and radio codes |
| `tuya_keys.json` | Tuya local keys |
| `remote.json` | the web remote's API token |

A satellite copies everything above down from its master except
`remote.json`, the rerack presets and the per-box run state.
| `settings.xml` | the add-on's settings, including the Kasa account |
| `cycle.json`, `*_state.json` | what is running and what has already run today |

The add-on id was `script.paragon.govee` before v2.19. Updating an existing
installation in place carries everything over on first run — Kodi files saved
data under the id, so the old folder is copied rather than left stranded. It
is a copy: the old folder stays where it is and an older installation still
works.

**Stop Kodi on both machines before copying.** Kodi writes `settings.xml` when
it exits, so a running instance will overwrite what you just put there.

1. Install the add-on on the new box — copy the whole repository folder to
   `addons/script.paragon.home`, or install the ZIP.
2. Copy the `addon_data/script.paragon.home` folder across.
3. Start Kodi. Open the add-on and run **Refresh devices** once: addresses are
   re-checked, and a device that has moved on DHCP is found again under the
   name you gave it.

### Two things to watch

**Do not leave both machines running it.** The Govee status read needs UDP
4002 and only one program on a network segment gets it cleanly — and a
scheduled sequence would fire from both boxes at once. Disable the background
service on the old machine, or stop using the add-on there.

**Keys and passwords come across in the copy.** `tuya_keys.json` and the Kasa
account in `settings.xml` are plain text; treat the folder as you would a
password file.

---

## Settings

| Setting | Default | Notes |
|---|---|---|
| How to reach the lights | Automatic | Or force LAN-only / cloud-only |
| LAN search time | 3s | Raise it on a busy or slow network |
| Repeat each LAN command | 2 | UDP has no acknowledgement, so commands are sent more than once |
| Search for lights when Kodi starts | off | Handy if lights get new DHCP addresses |
| Govee API key | empty | Only needed for cloud control |
| Verify the Govee TLS certificate | on | Only turn off if an old box has an unusable CA bundle |
| Change the lights with playback | off | The playback service |
| Serve the web remote on this box | off | See [Web remote](#web-remote) |
| Port | 8778 | Kodi's own web server usually has 8080 |
| PIN | made for you | Six digits, made the first time the remote starts |
| Allow sequences from the remote | on | Turn off if a pocket should not be able to start Bedtime |
| Network address to send from | automatic | Set it if the box is multi-homed (VPN, docker bridge) |
| Verbose logging | off | Adds per-command detail to `kodi.log` |

---

## Troubleshooting

**No lights found.** Run **Diagnose LAN search...** from the control panel
first — it tells these apart instead of leaving you guessing, and writes the
full detail to `kodi.log`:

* *Could not open UDP port 4002* — another Govee program holds the reply port.
  Close the **Govee Desktop app**, and any Home Assistant Govee integration on
  the same machine, then search again.
* *Nothing answered* — in order of likelihood: your model does not implement
  the Govee LAN API; inbound UDP 4002 is blocked for Kodi by the firewall; or
  LAN Control is off for the lights in the Govee Home app.
* *Replies that are not Govee* — something else on the network answered; the
  raw contents are in the log.

On Windows, allow `kodi.exe` through Windows Defender Firewall on private
networks — the scan goes out fine but the replies get dropped otherwise.

**The Govee Desktop app sees my lights but the add-on doesn't.** That does not
prove the lights speak the LAN API. Govee's desktop app uses its own protocol,
and the documented LAN API covers only certain models — largely the RGBIC
strips and lamps. If your lights have no **LAN Control** toggle in the Govee
Home app, they are cloud-only: set a Govee API key in Settings and switch
**How to reach the lights** to Automatic or Cloud only.

On a multi-homed box the scan used to leave the wrong interface; since v1.0.2
it goes out on every interface plus a broadcast. If you still need to pin it,
set **Advanced → Network address to send from** to the LAN address.

**Commands work but "Show status" fails.** Status replies arrive on UDP 4002.
If another Govee integration (Home Assistant, a second Kodi) already holds that
port, status is unavailable while control still works — control needs no reply.

**Cloud says "rate limit reached".** The Govee cloud API is metered per day and
per device per minute. Enable LAN Control on the lights, or don't drive them
from playback over the cloud.

**Toggle turns lights on when they were already on.** Toggle reads state first;
if no state can be read (see above) it defaults to switching on, because a
button that appears to do nothing is worse.

---

## The television

If Paragon TV is installed on the same box, the remote grows a **TV** tab:
what is on, the artwork, the channel list, a full direction pad and playback
keys, a draggable progress bar, a box for typing into a search field on the
television, and the maintenance jobs from its utility menu.

It reads Paragon TV from the outside and writes none of its files. One thing
cannot be read from out here -- what is playing on a channel nobody is
watching, which only Paragon TV's own Overlay can work out. It writes that
down every few seconds and this reads the file; that hook is all Paragon TV
carries for the remote's sake.

No Paragon TV on the box means no TV tab. Nothing is disabled; it is absent.

## Development

```
python3 tests/test_paragon_home.py      # 702 tests
python3 tests/check_py2.py              # Python 2.7 syntax gate
python3 tests/validate_addon.py         # manifest and settings cross-check
python3 tools/make_assets.py            # regenerate icon.png / fanart.png
```

The tests run without Kodi and without hardware: `tests/kodistubs/` replaces the
Kodi API, a UDP socket on loopback stands in for a Govee light, and a local HTTP
server stands in for the Govee cloud. Broadlink and Tuya have fakes too, each
speaking its real wire format — the Tuya one negotiates a 3.4 session key and
rejects a status query framed the way a control should be. That covers protocol
encoding, transport selection, the scene engine and the menu walks.

### Why the add-on id still says "govee"

The display name is **Paragon Home**, but the add-on id is still
`script.paragon.home` and will stay that way. The id is what Kodi uses to name
the settings folder, so changing it would strand every device name, scene,
colour, learned IR code and Tuya key in an `addon_data` directory the add-on no
longer reads — and would break any keymap or favourite holding a
`RunScript(script.paragon.home,…)` line. A cosmetic rename is not worth
re-naming thirty-four bulbs over.

Inside the code, `govee_lan.py`, `govee_cloud.py` and `GoveeController` keep
their names on purpose too: those really are the Govee driver, not the add-on.

Note what the suite cannot cover: it runs on Python 3, so a Python 2-only
fault — an implicit ascii decode where text meets binary — passes here and
fails on the device. Those are guarded by invariants that *are* checkable on
both, such as every URL and header in an HTTP request being the interpreter's
own `str` type, rather than by reproducing the failure.

`tools/roborock_probe.py` is a Python 3 script for working out which
protocol a robot vacuum speaks before any driver is written for it. It asks
the network on three ports and reports what answers; it sends no commands and
needs no account. See the comment at the top of the file.

`check_py2.py` exists because Krypton runs Python 2.7 and a 2.7 interpreter is
no longer easy to come by — it walks the AST of every shipped file and fails on
anything 2.7 cannot parse (f-strings, annotations, `yield from`, Python-3-only
imports, classes with no base).

### Layout

```
addon.xml                     add-on manifest
default.py                    script entry point + RunScript actions
service.py                    playback-reactive lighting service
resources/settings.xml        Krypton-format settings
resources/lib/compat.py       Python 2/3 shims
resources/lib/addon_utils.py  logging, settings, notifications, JSON store
resources/lib/govee_lan.py    Govee LAN (UDP) client
resources/lib/govee_cloud.py  Govee Cloud (HTTPS) client
resources/lib/devices.py      device model + transport selection
resources/lib/scenes.py       scene model and application
resources/lib/paragon_home.py  the add-on session
resources/lib/gui.py          dialog-driven control panel
resources/lib/remote.py       the web remote: server, API and page
resources/fonts/              the typeface the remote's page is set in
resources/lib/satellite.py    copying the master's setup down
```

### Targeting Kodi 19+

The Python sources already run unmodified on Python 3. To move to Matrix or
later, change `xbmc.python` in `addon.xml` to `3.0.0` and convert
`resources/settings.xml` to the Kodi 18+ settings format — Kodi 19 refuses to
load the old format. The code paths for `xbmcvfs.translatePath` and the removal
of `LOGNOTICE` are already handled.

### Not included

The add-on ships English-only, with literal strings rather than a
`resources/language/` bundle. Adding one means moving every UI string to an id
in `strings.po`; worth doing if the add-on is ever translated, but it would only
cost readability today.

---

## Licence

GPL-3.0. See [LICENSE.txt](LICENSE.txt).

Govee is a trademark of its respective owner. This add-on is unofficial and not
affiliated with or endorsed by Govee.


### Learning a radio (RF) command

**Manage devices → the blaster → Learn an RF command...**

Radio takes two presses where infrared takes one, because the blaster does not
know which frequency to listen on until it has gone looking:

1. **Hold the button down**, close to the blaster, while it sweeps for the
   frequency. Holding matters — a sweep needs something to find, and a tap is
   usually over before it locks on.
2. **Press the same button again**, once, and the code is captured.

What comes out is an ordinary learned command. It appears in the same list as
the infrared ones, fires the same way, and can be used in a scene, a sequence
step or the web remote with nothing else to set up — the blaster does not care
which kind it is replaying.

**It needs a Pro-class blaster.** The RM Pro, Pro+, RM2 Pro Plus and RM4 Pro
have a radio; every Mini is infrared only. A Mini refuses the sweep and says
so rather than failing obscurely.

Two things that can go wrong and are not bugs:

* **No frequency found.** Hold rather than tap, and keep the remote within a
  foot of the blaster. The sweep is stopped on the way out, because a blaster
  left sweeping ignores ordinary commands until it times out — which would
  make the next thing you press look broken.
* **A frequency, but no code.** Some remotes — car keys, most garage doors,
  some alarms — change their code on every press by design. Those cannot be
  replayed by anything, and no amount of retrying will capture one.
