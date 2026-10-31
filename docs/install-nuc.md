# Installing KenTrackIt on an Intel NUC (or any small PC)

KenTrackIt runs the same on a NUC as on a Pi: it's plain Python with no Pi-only parts. These steps go
from a blank NUC to a running system, using **Ubuntu Server 24.04 LTS**. Debian 12 works the same way.

Allow about 45 minutes.

---

## 1. What you need

| Item | Notes |
|---|---|
| NUC (any Intel/AMD mini PC) | 4 GB RAM and a 64 GB SSD are plenty. Wired Ethernet recommended. |
| USB stick, 8 GB or more | For the Ubuntu installer. |
| USB-to-RS-232 adapter | For the monitor radio's data. **FTDI-based** adapters are the most reliable; CH340 ones work but see step 5. |
| USB sound card or K5TRA (optional) | For voice recording, from the radio's AFO pin. |
| USB-to-I2C adapter (optional) | Only for the front-panel LCD; a NUC has no I2C pins (step 11). |
| Keyboard and screen | Just for the install; after that everything is over the network. |

## 2. BIOS settings

Press **F2** while the NUC powers on.

- **Power → After Power Failure: Power On** (wording varies: "AC recovery", "Restore on AC power loss").
  This makes it start by itself after a power cut, like the Pi does.
- **Boot:** USB first for now. Secure Boot can stay on (Ubuntu supports it).
- Save and exit (**F10**).

## 3. Install Ubuntu Server

1. Download **Ubuntu Server 24.04 LTS** from ubuntu.com/download/server and write it to the USB stick
   with balenaEtcher or Raspberry Pi Imager ("Use custom").
2. Boot the NUC from the stick and step through the installer:
   - **Network:** leave on DHCP for now (step 4 fixes the address).
   - **Storage:** use the entire disk.
   - **Profile:** server name `kentrackit`, your username (e.g. `admin`) and a strong password.
   - **SSH:** tick **Install OpenSSH server**.
   - **Featured snaps:** none needed.
3. Remove the stick when asked and let it reboot. Log in and note its address:

   ```bash
   hostname -I
   ```

From here on you can work from your own PC: `ssh admin@<address>`.

## 4. Fixed address, time zone, updates

**Fixed address.** Easiest: give the NUC a DHCP reservation on the router. Or set it on the NUC:

```bash
ip -br link            # find the Ethernet name, e.g. enp86s0
sudo nano /etc/netplan/50-cloud-init.yaml
```

```yaml
network:
  version: 2
  ethernets:
    enp86s0:
      dhcp4: false
      addresses: [192.168.1.50/24]
      routes: [{to: default, via: 192.168.1.1}]
      nameservers: {addresses: [192.168.1.1, 1.1.1.1]}
```

```bash
sudo netplan apply
```

**Time and updates:**

```bash
sudo timedatectl set-timezone Pacific/Auckland
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git i2c-tools
```

Unlike a Pi, the NUC keeps time with its own clock battery, and Ubuntu syncs it over the internet.
KenTrackIt still corrects itself from GPS time if the clock is ever wrong.

## 5. Stop Ubuntu grabbing the serial adapter (important)

Two Ubuntu services interfere with USB serial adapters:

- **brltty** (for Braille displays) takes over CH340/CH341 adapters, so the port vanishes.
- **ModemManager** probes new serial ports by sending modem commands down them, which would go
  straight to the radio.

Neither is needed on this box:

```bash
sudo apt remove -y brltty
sudo systemctl disable --now ModemManager
```

## 6. Plug in the radio and install KenTrackIt

Plug the serial adapter in first, so the installer can pick it up, then:

```bash
ls /dev/serial/by-id/         # should list the adapter, e.g. usb-FTDI_FT232R_USB_UART_A10K...-if00-port0
git clone https://github.com/gomeznz/kentrackit.git
cd kentrackit
sudo ./install.sh
```

The installer:

- installs Python's serial library, the audio tools and the Opus encoder;
- creates the `radio-avl` service user with access to serial ports, audio and I2C;
- copies the program to `/opt/radio-avl`, config to `/etc/radio-avl/config.json`,
  data to `/var/lib/radio-avl`;
- picks the first USB serial adapter and a free web port (8090 unless taken);
- starts the `radio-avl` service, which then starts on every boot.

It finishes by printing the address, e.g. `http://192.168.1.50:8090/`.

## 7. First login and settings

1. Open that address in a browser. A new install asks you to **create the administrator account**.
2. **Settings → Radio connection:** check the serial port and baud rate (9600). The status box should
   show frames arriving within a poll cycle.
3. **Settings → Map:** choose the map source and set the start view.
4. **Settings → Users:** add the dispatchers and viewers.

Everything else is the same as on the Pi; the README covers each page.

## 8. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8090/tcp        # or the port the installer printed
sudo ufw enable
```

If you only use the Cloudflare tunnel (step 9), skip the 8090 rule: the tunnel connects from inside.

## 9. Remote access through Cloudflare (optional)

```bash
curl -L https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
sudo cloudflared service install <token from the Cloudflare dashboard>
```

In the tunnel's public hostname, set the service to **HTTP** and `localhost:8090`.
**Not HTTPS**: KenTrackIt speaks plain HTTP behind the tunnel, and HTTPS gives "502 Bad Gateway".

## 10. Voice recording (optional)

Plug in the USB sound card or K5TRA, wire the radio's **AFO** pin to its input, then:

1. **Settings → Voice recording:** tick **Record voice calls** and pick the sound card.
2. Talk on a radio and set the level with `alsamixer` (F6 to pick the card) so peaks sit around −12 dB.

With more than one USB sound card, give each a fixed name so they can't swap over at boot:

```bash
for c in /sys/class/sound/card*; do echo "$c: $(cat $c/id)  port $(udevadm info -a $c | grep -m1 -oP 'KERNELS=="\K[0-9]+-[0-9.]+')"; done
echo 'SUBSYSTEM=="sound", KERNEL=="card*", KERNELS=="3-2", ATTR{id}="KENTRACK"' | sudo tee /etc/udev/rules.d/85-sound-names.rules
sudo reboot
```

(Use the port the first command showed.) Then choose `plughw:CARD=KENTRACK,DEV=0`.

## 11. Front-panel LCD (optional)

A NUC has no I2C pins, so use a **USB-to-I2C adapter**. The **CP2112** board (about $10) works with no
drivers to install: Linux shows it as an extra I2C bus.

1. Wire the LCD backpack to the adapter: GND, VCC (5 V), SDA, SCL.
2. Load the I2C device driver, now and on every boot:

   ```bash
   echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
   sudo modprobe i2c-dev
   ```
3. Plug in the adapter and find its bus number:

   ```bash
   sudo i2cdetect -l          # look for "CP2112 SMBus Bridge on hidraw…", e.g. i2c-9
   sudo i2cdetect -y 9        # should show 27 or 3f
   ```
4. Run `sudo ./install.sh` again (from the kentrackit folder), so the service can use the I2C bus,
   then **Settings → Front-panel display**: pick that bus, **Find it**, tick the box and save.

The NUC's own I2C buses (graphics, memory) also appear in the list. **Find it** only checks LCD
addresses, so it's safe to try them, but the CP2112 bus is the one you want.

## 12. Moving from the Pi (keeping history, users and settings)

On the **Pi**:

```bash
sudo systemctl stop radio-avl
sudo tar czf ~/kentrackit-move.tgz -C / etc/radio-avl var/lib/radio-avl
```

Copy it across and unpack on the **NUC** (after step 6):

```bash
scp admin@<pi-address>:kentrackit-move.tgz .
sudo systemctl stop radio-avl
sudo tar xzf kentrackit-move.tgz -C /
sudo chown -R radio-avl:radio-avl /etc/radio-avl /var/lib/radio-avl
sudo systemctl start radio-avl
```

Then in **Settings → Radio connection**, pick the NUC's serial adapter: its name will differ from the
Pi's. Users, zones, history, recordings and the map tile cache all come across.

Leave the Pi's service stopped (`sudo systemctl disable radio-avl`), or both will poll the radios
once polling is live.

## 13. Day to day

| Task | Command |
|---|---|
| Upgrade KenTrackIt | `cd ~/kentrackit && git pull && sudo ./install.sh` |
| Live log | `journalctl -u radio-avl -f` |
| Restart | `sudo systemctl restart radio-avl` (or Settings → Restart service) |
| Ubuntu security updates | Automatic (unattended-upgrades is on by default) |
| Backup | Settings → History and data → **Download backup**, or copy `/var/lib/radio-avl` |

The service file keeps KenTrackIt at low priority (`Nice=10`, 200 MB memory cap) because it shared
a Pi with AirBridge. On a NUC of its own, you can remove those lines from
`/etc/systemd/system/radio-avl.service`, but there's no need: it uses a few percent of one core.

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| No serial port listed | `ls /dev/serial/by-id/`. Empty with a CH340 adapter: brltty is still installed (step 5). |
| Port listed, but "Permission denied" | Run `sudo ./install.sh` again, then restart the service. |
| Frames arrive garbled | Check the baud rate (9600), and that it's an RS-232 adapter, not TTL. |
| Page doesn't load from another PC | Firewall (step 8), or the port: `sudo ss -ltnp \| grep python`. |
| Cloudflare shows 502 | Tunnel service must be **HTTP**, not HTTPS (step 9). |
| Sound card missing in Settings | `arecord -l` should list it; replug, or check the USB cable. |
| LCD: "/dev/i2c-N doesn't exist" | `sudo modprobe i2c-dev`, and pick the CP2112 bus (step 11). |
| Doesn't come back after a power cut | BIOS "After Power Failure" setting (step 2). |
