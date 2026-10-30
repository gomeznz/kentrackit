# KenTrackIt

A Raspberry Pi replacement for Kenwood KAS-20. It reads GPS reports from a Kenwood NX-series
radio's data port and shows the units on a live map in any browser on the network.

**Version 0.1 only listens.** It runs alongside KAS-20 and never transmits. Polling will be
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
- Runs at low CPU and I/O priority so it stays out of AirBridge's way

## Install on the Pi

```bash
ssh pi@<pi-address>
git clone https://github.com/gomeznz/kentrackit.git && cd kentrackit
sudo ./install.sh
```

The installer lists the serial adapters it can see. Edit the config file:

```bash
sudo nano /etc/radio-avl/config.json
```

- `serial.port`: use the `/dev/serial/by-id/...` path, not `/dev/ttyUSB0`. The by-id path
  doesn't change when other USB devices are plugged in or the Pi reboots.
- `serial.baud`: the same value you used with `nxdn_listen.py` (9600 worked).
- `web.password`: set one. The username defaults to `admin`.
- `map.center`: where the map opens before any unit has reported.

Then run `sudo systemctl restart radio-avl` and open `http://<pi-address>:8090`.

Logs: `journalctl -u radio-avl -f`

To upgrade, run `git pull` then `sudo ./install.sh` again. Your config and
history are kept.

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

By default tiles come from OpenStreetMap through the Pi's cache, which is fine for one
dispatch screen. If the Pi has no internet on site, load the area once at the workshop.
For aerial imagery, LINZ Basemaps works with a free API key:

```json
"tiles": { "upstream": "https://basemaps.linz.govt.nz/v1/tiles/aerial/WebMercatorQuad/{z}/{x}/{y}.webp?api=YOUR_KEY",
           "attribution": "&copy; LINZ CC BY 4.0" }
```

If you change the tile source, delete `/var/lib/radio-avl/tiles/`.

## Roadmap

1. **Polling.** Capture KAS-20's poll command from a bench setup (KAS-20 PC, `kas_sniff.py`,
   spare radio), or set the units to auto-report. Then retire KAS-20.
2. Geofences and alerts (entering or leaving a site, no report for X minutes)
3. Emergency and status messages, once captured
