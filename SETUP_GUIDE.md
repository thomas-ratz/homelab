# Complete *arr Stack Setup Guide

All services share Gluetun's network, so they communicate via `localhost`.

## Quick Reference - Internal Addresses

| Service     | Address            |
|-------------|-------------------|
| qBittorrent | `localhost:8080`  |
| Sonarr      | `localhost:8989`  |
| Radarr      | `localhost:7878`  |
| Lidarr      | `localhost:8686`  |
| Bazarr      | `localhost:6767`  |
| Prowlarr    | `localhost:9696`  |

---

## Step 1: qBittorrent Configuration

Go to http://qbit.thopi.ts then **Tools → Options**:

### Downloads tab:
- Default Save Path: `/downloads`
- Keep incomplete torrents in: `/downloads/incomplete` (enable this)

### BitTorrent tab (optional):
- Enable "When seeding ratio reaches: 1.0, then Pause torrent"

---

## Step 2: Prowlarr (Indexer Manager) - Do This First!

Go to http://prowlarr.thopi.ts

### A. Add Download Client:

1. Settings → Download Clients → Click **+**
2. Select **qBittorrent**
3. Configure:
   - Name: `qBittorrent`
   - Host: `localhost`
   - Port: `8080`
   - Username: your qBit username
   - Password: your qBit password
   - Category: `prowlarr`
4. Click **Test** then **Save**

### B. Add Indexers:

1. Indexers → Click **+** → Select your trackers
2. For Cloudflare-protected sites, make sure FlareSolverr tag is applied

### C. Add Apps (connect to other *arr):

1. Settings → Apps → Click **+**

**Add Sonarr:**
- Prowlarr Server: `http://localhost:8989`
- Sync Level: Full Sync
- API Key: get from Sonarr Settings → General

**Add Radarr:**
- Prowlarr Server: `http://localhost:7878`
- Sync Level: Full Sync
- API Key: get from Radarr Settings → General

**Add Lidarr:**
- Prowlarr Server: `http://localhost:8686`
- Sync Level: Full Sync
- API Key: get from Lidarr Settings → General

---

## Step 3: Sonarr (TV Shows)

Go to http://sonarr.thopi.ts

### A. Add Root Folder:

1. Settings → Media Management → Root Folders → Add Root Folder
2. Path: `/tv`
3. Save

### B. Add Download Client:

1. Settings → Download Clients → Click **+**
2. Select **qBittorrent**
3. Configure:
   - Name: `qBittorrent`
   - Host: `localhost`
   - Port: `8080`
   - Username: your qBit username
   - Password: your qBit password
   - Category: `tv-sonarr`
4. Click **Test** then **Save**

---

## Step 4: Radarr (Movies)

Go to http://radarr.thopi.ts

### A. Add Root Folder:

1. Settings → Media Management → Root Folders → Add Root Folder
2. Path: `/movies`
3. Save

### B. Add Download Client:

1. Settings → Download Clients → Click **+**
2. Select **qBittorrent**
3. Configure:
   - Name: `qBittorrent`
   - Host: `localhost`
   - Port: `8080`
   - Username: your qBit username
   - Password: your qBit password
   - Category: `radarr`
4. Click **Test** then **Save**

---

## Step 5: Lidarr (Music)

Go to http://lidarr.thopi.ts

### A. Add Root Folder:

1. Settings → Media Management → Root Folders → Add Root Folder
2. Path: `/music`
3. Save

### B. Add Download Client:

1. Settings → Download Clients → Click **+**
2. Select **qBittorrent**
3. Configure:
   - Name: `qBittorrent`
   - Host: `localhost`
   - Port: `8080`
   - Username: your qBit username
   - Password: your qBit password
   - Category: `lidarr`
4. Click **Test** then **Save**

---

## Step 6: Bazarr (Subtitles)

Go to http://bazarr.thopi.ts

### A. Connect to Sonarr:

1. Settings → Sonarr
2. Enable Sonarr toggle
3. Address: `localhost`
4. Port: `8989`
5. API Key: paste Sonarr's API key
6. Click **Test** then **Save**

### B. Connect to Radarr:

1. Settings → Radarr
2. Enable Radarr toggle
3. Address: `localhost`
4. Port: `7878`
5. API Key: paste Radarr's API key
6. Click **Test** then **Save**

### C. Add Subtitle Providers:

1. Settings → Providers → Click **+**
2. Add providers like:
   - OpenSubtitles.com (requires free account)
   - Subscene
   - Addic7ed

### D. Set Languages:

1. Settings → Languages
2. Add your preferred subtitle languages (e.g., English, Greek)

---

## Step 7: Jellyfin Library Refresh

Once *arr services start downloading and organizing media:

1. Go to http://jellyfin.thopi.ts
2. Dashboard → Libraries
3. Click **Scan All Libraries** to pick up new content

---

## How It All Works Together

```
You search in Sonarr/Radarr/Lidarr
           ↓
Prowlarr searches your indexers (via VPN)
           ↓
Download sent to qBittorrent (via VPN)
           ↓
*arr service moves completed file to /tv or /movies or /music
           ↓
Bazarr downloads subtitles (via VPN)
           ↓
Jellyfin detects new media and adds to library
           ↓
You watch on any device via Jellyfin
```
