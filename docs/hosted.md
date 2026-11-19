# The KenTrackIt hosted service

One server runs KenTrackIt for many companies. Each company has its own users, units, history,
alerts, zones and settings, kept in **its own database file**. Nothing is shared between companies
except the map tiles and the phone app installer.

```
  Site A: Pi + monitor radio ──┐                       ┌── Company A's people (browser, phone app)
  Site B: Pi + monitor radio ──┼─ HTTPS ─► Cloudflare ─► hosted KenTrackIt ─┤
  Site C: Pi + monitor radio ──┘   (gateway key)       └── Company B's people ...
                                                         ASI at /hub
```

The Pi at each site is the company's **gateway**. It keeps doing everything it does now (local map,
front-panel display, recording, talk-back) and also sends the radio's data up to the hosted service.
If the internet drops, the Pi keeps the data and sends it when the connection is back, with the
original times, so the hosted history has no gaps.

| Where | What |
|---|---|
| Hosted service | Map, units, history, trip reports, zones, alerts and emergencies, users, phone app sign-in, phone location sharing |
| Site Pi only (for now) | Radio serial settings, polling, voice recording and playback, the dispatch console (talk-back), the front-panel LCD |

Recording, listening and talk-back from the hosted service are the next stage: the audio has to
travel up from (and down to) the site, which the gateway link doesn't carry yet.

---

## 1. A server

A VM on the Proxmox server is ideal. Make a full VM (not an LXC container):

- **Ubuntu Server 24.04 LTS**, 2 vCPU, 2 GB RAM, 64 GB disk.
- Storage grows by roughly **3 GB a year for a 10-radio fleet** polled every 19 s (less with fewer
  radios or a shorter "keep history" setting in each company's settings).

Install Ubuntu as in [install-nuc.md](install-nuc.md) steps 3-4 (static address, updates), then:

```sh
sudo apt install -y git
git clone https://github.com/gomeznz/kentrackit.git
cd kentrackit
sudo ./install-hub.sh
```

It prints the port (8100 unless something already uses it).

## 2. The first ASI administrator

From a PC **on the same network as the server**, open `http://<server address>:8100/hub` and create
your account. For safety this first step is refused through the public (Cloudflare) address.

Add more ASI staff later under **ASI administrators** on the same page.

## 3. A public address with Cloudflare

This gives the service an `https://` address that works from anywhere, with no ports opened on
your router. You need a domain in your Cloudflare account; the steps use `track.asiltd.co.nz`
as the example. Cloudflare moves its menus around now and then, so the names may differ slightly.

1. Log in at **dash.cloudflare.com** and open **Zero Trust** (left-hand menu). If it asks you to
   pick a plan, the **Free** plan is enough.
2. Go to **Networks → Tunnels** and press **Create a tunnel**.
3. Choose **Cloudflared**, name it `kentrackit-hub`, and save.
4. On the next screen pick **Debian** and **64-bit**. Cloudflare shows a command that starts with
   `curl -L --output cloudflared.deb`. Copy the whole block and paste it into the server's terminal.
   When it finishes, the tunnel shows as **Healthy** at the bottom of the Cloudflare page.
5. Press **Next** to add a **public hostname**:
   - **Subdomain:** `track`
   - **Domain:** pick your domain
   - **Type:** `HTTP`
   - **URL:** `localhost:8100` (the port the install printed)
6. Save. After a minute, `https://track.asiltd.co.nz/hub` opens the same page as in step 2.

## 4. Add a company

On `/hub`, press **Add a company** and fill in:

- **Company name** as people know it.
- **Short name**: lowercase, can't be changed later (it's part of everyone's sign-in key).
- **Their first administrator**: username and a starting password. Usernames are unique across the
  whole service, because people sign in without saying which company they're with.

The page then shows the **gateway key once**. Copy it straight into the site's Pi (next step) or
somewhere safe. If it's lost, **Manage → New gateway key** makes a new one (the old one stops working).

## 5. Connect the site's Pi

1. Update the Pi to this version: on the Pi, `cd kentrackit && git pull && sudo ./install.sh`.
2. On the Pi's own KenTrackIt page, open **Settings → Hosted service**.
3. Tick **Send this site's radio data to the hosted service**, enter the service address
   (`https://track.asiltd.co.nz`) and the gateway key, and press **Save**.
4. The status changes to **Connected** and shows the company's name. On `/hub` the company shows
   **Gateway online**.

The hosted service starts its history from the moment the gateway connects. The Pi keeps its own
history as before.

## 6. People sign in

Everyone uses `https://track.asiltd.co.nz` (the usual sign-in page) or the phone app with that
address. The service works out their company from their username. Each company's administrators
manage their own users, zones, alert rules and map view under **Settings**. The radio and hardware
settings aren't shown there: they live on the site's Pi, and the **Gateway** card shows whether
it's online.

**Phone app:** upload `KenTrackIt.apk` once on `/hub → Phone app`; every company's phones get it.

## 7. A company's own address (optional)

To give a company `sdc.track.asiltd.co.nz`:

1. In Cloudflare, open the tunnel (**Networks → Tunnels → kentrackit-hub → Edit → Public Hostname**),
   and add another hostname: subdomain `sdc.track`, type `HTTP`, URL `localhost:8100`.
2. On `/hub`, **Manage** the company and enter `sdc.track.asiltd.co.nz` under **Its own web addresses**.

That address shows the company's name on the sign-in page, and only that company's people can sign
in there.

## 8. Support, and switching a company off

- **Open** (on `/hub`) signs you in to a company as `asi-support` for 4 hours, in a new tab. Every
  support session is written in that company's audit log. A company can refuse support access by
  disabling the `asi-support` user in its own settings.
- **Manage → Enabled** off: the company's people can't sign in and its gateway is refused.
  Nothing is deleted; tick it again to bring it back.

## 9. Backups

Everything is in `/var/lib/kentrackit-hub`: `hub.db` (companies, ASI accounts) and
`companies/<short name>/avl.db` (each company). A nightly copy:

```sh
sudo tee /etc/cron.daily/kentrackit-hub-backup >/dev/null <<'EOF'
#!/bin/sh
D=/var/backups/kentrackit-hub/$(date +%a); mkdir -p "$D"
cd /var/lib/kentrackit-hub
for db in hub.db companies/*/avl.db; do
  mkdir -p "$D/$(dirname "$db")"
  sqlite3 "$db" ".backup '$D/$db'"
done
EOF
sudo chmod +x /etc/cron.daily/kentrackit-hub-backup
```

That keeps a week of daily copies. Copy `/var/backups/kentrackit-hub` off the server too (for example
with Proxmox's own VM backups to another disk).

## 10. Updating

```sh
cd kentrackit && git pull && sudo ./install-hub.sh
```

Companies and gateways reconnect by themselves. While it restarts, the Pis hold on to their data.

## Security notes

- Gateway keys are long random keys, stored on the server only as a hash. Each one belongs to
  exactly one company, and a gateway can only add radio data to that company.
- Sign-in keys (session cookies) carry the company's short name, and a company only ever looks up
  its own sessions. A company's own address only accepts its own people.
- Company administrators can't change anything on the server itself: ports, map sources, the
  phone app installer and restarting are ASI's (on `/hub`).
- The service listens on the LAN for the first-time setup. If the VM is ever on a network you don't
  control, firewall port 8100 so only `cloudflared` (on the same machine) reaches it.
