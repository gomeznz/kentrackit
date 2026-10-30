<p align="center"><img src="docs/logo.svg" alt="KenTrackIt" width="320"></p>

# KenTrackIt

A Raspberry Pi replacement for Kenwood KAS-20. It reads GPS reports from a Kenwood NX-series
radio's data port and shows the units on a live map in any browser on the network.

**It only listens for now.** It runs alongside KAS-20 and never transmits. Polling will be
added once the poll command has been captured (see *Roadmap*).

## What it does

- Shows live positions with names, colours, heading arrows and live/stale/lost status
- Keeps a history for each unit, with track replay (10× to 1200×) and CSV export
- Lets you name units and pick their colours (saved on the Pi)
- Shows channel busy and serial link status, and mirrors the monitor radio's display
- Caches map tiles on the Pi. Pan around the operating area once while it's online and the
  map then works offline
- Corrects its own clock from GPS time if the Pi's clock is wrong (no RTC or NTP on site)
- Logs sequence-counter gaps from the radio, which show if any serial data was lost
- **Zones and alerts:** draw circles or areas on the map; alerts when units enter or leave,
  stop reporting, or exceed a speed limit. Acknowledge them from the Alerts tab
- **Emergencies:** red banner, pulsing marker and a repeating alarm on every open screen until
  acknowledged. Triggered by words on the monitor radio's display (configurable); there's a
  test button under Settings → Alerts
- **Trip and stop reports** (`/reports`): fleet summary per day or range, then per unit a route
  map, numbered stops (named by zone) and trips with distance and top speed. Print or CSV
- **Dispatch screen:** trails (last N minutes), follow a unit, and a full-screen wall display
  (`/?wall=1` opens straight into it, handy for a kiosk screen)
- **Channel activity** (`/channel`): every transmission logged (voice, data, polls) with
  duration and unit; airtime charts, busy-hours heatmap, per-unit airtime, CSV. Live **on air**
  indicator, and the talking unit pulses on the map. Stuck-carrier alert after 2 minutes
- **Coverage map** (Coverage button on the map): built from the fleet's own traffic. Every report
  is "heard here"; missed polls (and, before KenTrackIt polls, missed reports while moving) are
  "not heard", spread along the route between where a unit went quiet and where it came back
- **Smart polling** (Settings → Polling, off by default): moving units often, parked units
  rarely, emergencies constantly, waits for a clear channel, Poll now from the map. Test mode
  plans the schedule without transmitting. Needs the poll command captured from KAS-20
- **Voice recording** (Settings → Voice recording): every voice call recorded from the monitor
  radio's audio, filed in the transmission log, played back from the Channel page or a unit's
  panel (Recent calls). Opus (~2 kB/s) or WAV, own retention period, pauses if the disk gets low.
  Dispatchers and administrators can listen; every playback is audited
- **Raw serial capture** (Settings → Diagnostics): record what the radio sends without stopping
  the service, to decode voice-call IDs, status messages, emergencies or the KAS-20 poll
- **Users and access control:** sign-in page, Administrator / Dispatcher / Viewer roles, optional
  per-user unit restrictions, audit log (see *Users* below)
- Settings page for every option (`/settings`, or the ⚙ on the map)
- Plays nicely with other services on the same Pi: checks a web port is free before using it,
  lists the ports already taken, and picks the radio's serial adapter by its stable name
- Runs at low CPU and I/O priority so it stays out of AirBridge's way

## Install on the Pi

```bash
ssh pi@<pi-address>
git clone https://github.com/gomeznz/kentrackit.git && cd kentrackit
sudo ./install.sh
```

The installer picks a free web port (8090, or the next free one if another service has it),
starts the service and prints the address. Open `http://<pi-address>:<port>/settings` and:

1. **Radio connection:** pick the radio's serial adapter and baud rate (9600 worked for the
   capture). The status box shows frames arriving.
2. **Web access:** set a password. If you change the port, the page checks it's free first and
   then moves you to the new address.
3. **Map:** choose the map source and set the start view.

Settings are saved to `/etc/radio-avl/config.json` and take effect straight away. The only
exception is changing the listen address, which needs **Restart service** (bottom of the page).

Logs: `journalctl -u radio-avl -f`

To upgrade, run `git pull` then `sudo ./install.sh` again. Your config and
history are kept.

## Users

| Role | Can do |
|---|---|
| Administrator | Everything: settings, users, backups, plus all of the below |
| Dispatcher | Live map, acknowledge alerts, draw/edit zones, name units, reports, CSV exports |
| Viewer | Live map and reports, read-only. Can silence an alarm on their own screen but not acknowledge it |

Any user can be limited to certain units. They then only see those units on the map, in
history, alerts, reports and exports (alerts not tied to a unit are shown to everyone).

- Manage users under **Settings → Users**; everyone can change their own password at `/account`.
- A fresh install opens a setup page to create the first administrator. Upgrading from v0.2/0.3
  turns the old single login into the first administrator account (same username and password).
- Sessions last 12 hours of inactivity, or 30 days with "keep me signed in" (use that for wall
  displays: create a Viewer account for the screen).
- 5 wrong passwords for a username locks it for 5 minutes (20 per address).
- Disabling a user, deleting them or changing their password signs them out immediately.
- **Settings → Audit log** lists sign-ins, failed sign-ins, settings and user changes, and who
  acknowledged alerts.
- Scripts can use HTTP Basic auth with any account, e.g. `curl -u alex:… http://pi:8090/api/units`.

## Files

| Path | What |
|---|---|
| `/opt/radio-avl/` | Program |
| `/etc/radio-avl/config.json` | Settings |
| `/var/lib/radio-avl/avl.db` | History (SQLite). Back this up |
| `/var/lib/radio-avl/tiles/` | Cached map tiles |

## Testing without the radio

Replay a capture made with `nxdn_listen.py`:

```bash
python3 -m radio_avl --config test.json --replay raw_20261030_152012.log
```

Here `test.json` contains `{"data_dir": "test-data", "web": {"port": 8091}}`. Open port 8091
afterwards. Add `--speed 10` to replay in (sped-up) real time. Parser tests:
`python3 tests/test_kenwood.py`

Simulated fleet (5 units driving around Invercargill, with stops, a speeding run, an overdue
unit, a status change and an emergency):

```bash
PYTHONPATH=. python3 tools/simulate.py --hours 3 > demo.log
python3 -m radio_avl --config test.json --replay demo.log
```

## Voice recording hardware

The NX radio decodes the NXDN voice itself, so KenTrackIt records its audio output; nothing
needs to decode AMBE+2. Connect the monitor radio's receive audio (accessory connector or speaker
line) to a USB sound card or a K5TRA board on the Pi. It must be a **different** sound card from
AirBridge's, as ALSA devices can only be opened by one program. Pick it under Settings → Voice
recording and use the level meter (aim for peaks around −12 dB; set gain with `alsamixer`).

Make sure the customer has agreed to voice logging and their staff know calls are recorded.

## Testing polling without radios

`tools/fake_radio.py` creates a virtual serial port with a pretend radio: three units driving
around that answer polls, one that never answers, a coverage hole to the north, and the odd voice
call. Point Settings → Radio connection at the port it prints, and set the poll command to
`\x02POLL{num5}\x03`.

## Status and emergency messages: still to confirm

We haven't captured a real status message or emergency yet. Until then KenTrackIt:

- raises an emergency if the monitor radio's display shows any of the configured words, and
  tries to match a unit ID in the text
- raises a "status" alert whenever the unknown `$PKNDS` fields change for a unit (the last two
  fields are likely status codes)

Send a capture of each (`nxdn_listen.py` while someone presses the emergency button and sends a
status) and the parsing can be made exact.

## Radio data format (from the 30/10/2026 capture)

Each frame is `STX` + payload + a 2-digit counter (00–99) + `ETX`.

| Payload | Meaning |
|---|---|
| `S1` / `S0` | Channel busy / clear |
| `JA` + 4 bytes | Status flags (not yet mapped) |
| `JE0` + NUL-separated text | Display text: channel, ?, zone, pop-up message |
| `$PKNDS,...` | GPS report: time, fix, lat/lon, speed (kn), course, date, ?, ?, unit ID, ?, ? |

The unknown `$PKNDS` fields (`22.70`, `W00`, `207`, `00`) are stored with every position so
they can be identified later.

## Map tiles

Every map has a **Map / Satellite** switch (bottom right); the choice is remembered per browser.
Satellite imagery comes from Esri World Imagery out of the box (no key; commercial use is under
Esri's terms) or LINZ aerial imagery, which is sharper across NZ and licensed CC BY 4.0. Set it
under **Settings → Map → Satellite imagery**.

Choose the street map source under **Settings → Map**: OpenStreetMap (default), LINZ aerial imagery (free
API key from basemaps.linz.govt.nz), a custom tile server, or none. Tiles are cached on the Pi
per source, so switching back and forth doesn't mix them. If the Pi has no internet on site, pan
around the operating area once at the workshop and the map then works offline.

## Roadmap

1. **Polling.** Capture KAS-20's poll command from a bench setup (KAS-20 PC, `kas_sniff.py`,
   spare radio), or set the units to auto-report. Then retire KAS-20.
2. Geofences and alerts (entering or leaving a site, no report for X minutes)
3. Emergency and status messages, once captured
