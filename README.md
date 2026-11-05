# KODeviceSync server

Keep one profile of your KOReader add-ons on your own server, and set up any
new e-reader to match by installing a single plugin.

This repo is the **server**: a small web app with a JSON API, shipped as a
Docker image. The **KOReader plugin** lives in its own repo,
[theryanmc/kodevicesync.koplugin](https://github.com/theryanmc/kodevicesync.koplugin),
so plugin catalogs like Storefront can install it.

```
 Kobo (set up)                         KODeviceSync server                      New Kindle
 ─────────────                         ───────────────────                      ──────────
 Choose what to sync  ──── push ────▶  profile: plugins, patches,  ──── pull ──▶  restart → same
 Push                                  fonts, style tweaks                       add-ons as the Kobo
```

What gets synced: `plugins/*.koplugin`, `patches/*.lua`, `fonts/` and `styletweaks/`.
KOReader's built-in plugins and fonts are detected and left alone.

---

## Run it on Unraid

1. Open the Unraid terminal (the **>_** icon at the top right) and install the template:

   ```sh
   curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-kodevicesync.xml \
     https://raw.githubusercontent.com/theryanmc/kodevicesync/main/unraid/kodevicesync.xml
   ```

2. Go to **Docker › Add Container**. In the **Template** drop-down, pick **kodevicesync** under *User templates*.
3. Check the settings, then click **Apply**:

   | Setting           | Default                           |
   |-------------------|-----------------------------------|
   | Web UI / API port | `1379`                            |
   | Data              | `/mnt/user/appdata/kodevicesync`  |
   | Allow sign-ups    | `true` (set to `false` after creating your profile) |

4. Click the container icon and choose **WebUI**, or open `http://<unraid-ip>:1379`.

Updates arrive through Unraid's normal **Check for Updates** on the Docker tab.

## Run it with Docker

**docker run**

```sh
docker run -d --name kodevicesync --restart unless-stopped \
  -p 1379:1379 \
  -v /path/to/kodevicesync-data:/data \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  ghcr.io/theryanmc/kodevicesync:latest
```

**docker compose**: save [`docker-compose.yml`](docker-compose.yml), set `PUID`/`PGID` to the user that
should own `./data`, then run:

```sh
docker compose up -d
```

To update: `docker compose pull && docker compose up -d` (or `docker pull` and re-run).

The image is published for `linux/amd64` and `linux/arm64`, so it also runs on a Raspberry Pi or a Synology.

### Settings

| Variable             | Default     | Meaning |
|----------------------|-------------|---------|
| `KDS_ALLOW_SIGNUP`   | `true`      | Allow creating profiles from the login page. Set to `false` once you've made yours. |
| `KDS_MAX_UPLOAD_MB`  | `200`       | Largest single file or zip upload. |
| `KDS_SECURE_COOKIES` | `false`     | Set `true` if the web UI is only reached over HTTPS. |
| `PUID` / `PGID`      | `99` / `100`| User and group the server runs as and that own `/data`. |
| `KDS_PLUGIN_URL`     | latest GitHub release | Where `/plugin.zip` redirects to. |

Everything (database and stored files) lives in `/data`. Back up that folder.

---

## Set up your devices

1. Open `http://<server>:1379`, click **Create a profile**, and choose a username and password.
   There is no default account.
2. Install the KOReader plugin on each device, using any one of these:
   - **Storefront:** search for *KODeviceSync*.
   - **The latest release:** download `kodevicesync.koplugin.zip` from the
     [latest release](https://github.com/theryanmc/kodevicesync.koplugin/releases/latest).
   - **Your server:** `http://<server>:1379/plugin.zip` is a short link to the latest release.

   Unzip it so you get `koreader/plugins/kodevicesync.koplugin/`, then restart KOReader.
3. In KOReader, open **Tools (🔧) › KODeviceSync**, set **Server** to `http://<server>:1379`, and **Log in**.
4. On your main device: open **Choose what to sync**, then **Push this device's setup to server**.
   On a new device: **Pull setup from server**, then restart when asked.

### The web dashboard

- See everything in your profile, and which device last pushed each item.
- Upload add-ons from your computer. Plugin release zips and GitHub's *Download ZIP* both work.
- Download any item as a zip, or remove it from the profile.
- See signed-in devices and sign any of them out.

## Behaviour and limits

- **Nothing is deleted from devices.** Removing an item from the profile stops it syncing
  to new devices. It isn't uninstalled from devices that already have it.
- Plugin settings (`koreader/settings/`) aren't synced. They tend to be device-specific.
- Passwords are stored with scrypt. Each device gets its own revocable token.
- Traffic is plain HTTP. Keep the server on your LAN or VPN, or put it behind an HTTPS reverse proxy.
  The plugin accepts `https://` addresses.

---

## Development

```
server/app/main.py          FastAPI app: web UI + JSON API, SQLite, file storage
server/app/templates/       dashboard and login pages
Dockerfile                  the server image
unraid/kodevicesync.xml     Unraid template
.github/workflows/docker.yml  builds ghcr.io/theryanmc/kodevicesync on every push to main
```

Run locally:

```sh
cd server
python -m venv .venv && .venv/bin/pip install -r requirements.txt
KDS_DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 1379
```

`/plugin.zip` redirects to the plugin's latest GitHub release, so the server never needs
rebuilding when the plugin changes. Set `KDS_PLUGIN_URL` to point it somewhere else.

API used by the plugin: `POST /api/login` · `POST /api/logout` · `GET /api/manifest` ·
`POST /api/blobs/missing` · `PUT|GET /api/blobs/{md5}` · `POST /api/items` · `POST /api/items/delete`.
Interactive docs are at `/api/docs`.
