# KODeviceSync

Keep one profile of your KOReader add-ons on your own server, and set up any
new e-reader to match by installing a single plugin.

It syncs:

| Category      | Folder on the device (inside KOReader's data dir) |
|---------------|---------------------------------------------------|
| Plugins       | `plugins/*.koplugin`                              |
| User patches  | `patches/*.lua`                                   |
| Fonts         | `fonts/` (font files and font folders)            |
| Style tweaks  | `styletweaks/`                                    |

```
 ┌──────────── Kobo (set up) ───────────┐        ┌─ Unraid ───────────────┐        ┌─ New Kindle ─────────┐
 │ KODeviceSync ▸ Choose what to sync    │  push  │ kodevicesync container │  pull  │ KODeviceSync ▸ Pull  │
 │ KODeviceSync ▸ Push                   │ ─────▶ │ web UI + SQLite + files│ ─────▶ │ restart → identical  │
 └───────────────────────────────────────┘        └────────────────────────┘        └──────────────────────┘
```

## Repository layout

```
server/                     FastAPI app (web UI + JSON API), SQLite, file storage
plugin/kodevicesync.koplugin  the KOReader plugin
Dockerfile, docker-compose.yml, docker-entrypoint.sh
unraid/kodevicesync.xml     Unraid container template
.github/workflows/docker.yml  builds & publishes ghcr.io/theryanmc/kodevicesync
```

## 1. Run the server on Unraid

The container listens on **1379** and keeps everything in **/data**
(map it to `/mnt/user/appdata/kodevicesync`). It runs as `99:100`
(nobody:users), Unraid's default.

**Option A: publish an image (recommended).** Push this repo to GitHub. The
included workflow builds `ghcr.io/theryanmc/kodevicesync:latest` for amd64 and
arm64. Make the package public (or log Unraid into ghcr). Then:

1. Copy `unraid/kodevicesync.xml` to
   `/boot/config/plugins/dockerMan/templates-user/my-kodevicesync.xml`.
2. In Unraid, go to **Docker › Add Container**, pick the **kodevicesync** template, and click **Apply**.

**Option B: build on the server.** Copy the repo to Unraid, then:

```sh
cd /mnt/user/appdata/kodevicesync-src
docker build -t kodevicesync:latest .
docker run -d --name kodevicesync --restart unless-stopped \
  -p 1379:1379 -v /mnt/user/appdata/kodevicesync:/data kodevicesync:latest
```

With the Compose Manager plugin, you can run `docker compose up -d --build` instead.

### Settings (environment variables)

| Variable             | Default | Meaning |
|----------------------|---------|---------|
| `KDS_ALLOW_SIGNUP`   | `true`  | Allow creating profiles from the login page. Set to `false` once you've made yours. |
| `KDS_MAX_UPLOAD_MB`  | `200`   | Largest single file or zip upload. |
| `KDS_SECURE_COOKIES` | `false` | Set `true` if the web UI is only reached over HTTPS. |
| `PUID` / `PGID`      | `99`/`100` | User the server runs as. |

## 2. Create your profile

Open `http://<unraid-ip>:1379`, click **Create a profile**, and pick a username and password.

## 3. Install the plugin on a device

Download the plugin from the server itself at `http://<unraid-ip>:1379/plugin.zip`
(the dashboard links to it), or copy `plugin/kodevicesync.koplugin` from this repo.
Unzip it so you end up with:

- Kobo / Kindle / PocketBook: `koreader/plugins/kodevicesync.koplugin/`
- Android: `koreader/plugins/kodevicesync.koplugin/` on internal storage
- Linux desktop: `~/.config/koreader/plugins/kodevicesync.koplugin/`

Restart KOReader. The plugin is under **Tools (🔧) › KODeviceSync**.

## 4. Use it

On every device: set **Server** to `http://<unraid-ip>:1379`, then **Log in**.

**On your main, set-up device:**
1. Open **Choose what to sync**. For each category, tick individual items or turn on
   *Sync all, including ones added later*. On Kobo and Kindle, KOReader's built-in
   plugins sit in the same `plugins/` folder as yours, so tick only the ones you
   installed yourself.
2. Tap **Push this device's setup to server**.

**On a new device:**
1. Tap **Pull setup from server**. It shows what's new or changed, downloads it,
   checks every file's checksum, then offers to restart KOReader.

Pulled items are automatically ticked on that device, so later pushes from
there keep the profile up to date. Only changed files are transferred.

### The web dashboard

- See everything in your profile, with sizes and which device last pushed each item.
- **Upload** add-ons straight from your computer. Plugin release zips and
  GitHub's "Download ZIP" both work (it finds the folder containing `main.lua`).
  Patches, fonts and style tweaks can be single files or a zip.
- **Download** any item as a zip, or **Remove** it from the profile.
- See signed-in devices and sign out any of them.

## Behaviour and limits

- **Nothing is deleted from devices.** Removing an item from the profile stops it
  syncing to new devices. It isn't uninstalled from devices that already have it.
- Plugin *settings* (files in `koreader/settings/`) and `settings.reader.lua`
  aren't synced. Those tend to be device-specific.
- The plugin never syncs or overwrites itself.
- Auth: passwords are stored with scrypt. Each device gets its own revocable token.
  Traffic is plain HTTP unless you put the server behind an HTTPS reverse proxy
  (the plugin supports `https://` URLs). Keep it on your LAN or VPN, or behind a proxy.

## API (used by the plugin)

`POST /api/login` · `POST /api/logout` · `GET /api/manifest` ·
`POST /api/blobs/missing` · `PUT|GET /api/blobs/{md5}` · `POST /api/items` ·
`POST /api/items/delete`. Interactive docs are at `/api/docs`.

## Development

```sh
cd server
python -m venv .venv && .venv/bin/pip install -r requirements.txt
KDS_DATA_DIR=./data KDS_PLUGIN_DIR=../plugin/kodevicesync.koplugin .venv/bin/uvicorn app.main:app --reload --port 1379
```
