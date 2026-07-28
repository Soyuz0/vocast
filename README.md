# Vocast

Convert articles to audio using local TTS models — one at a time by hand, or
continuously from your RSS feeds.

![Vocast demo: add an article from a URL, list the library, and serve it as a podcast feed](https://raw.githubusercontent.com/cnrmurphy/vocast/main/assets/demo.gif)

The clip above is sped up through the synthesis step. Once the feed is served, add its URL to a podcast app on your phone. Here it is playing in Downcast:

https://github.com/user-attachments/assets/89de6fe0-136e-4c34-9430-ea075080cb3c

## Why

I wanted a way to convert articles into audio that I could stream to my mobile device while on the go. This seemed straightforward enough to build myself
and I didn't want to pay for an app. 

## How

Vocast uses Kokoro for TTS. It can fetch articles from a given URL or local text file. Audio files are saved to `~/.vocast/library`. It provides an HTTP server
that exposes an RSS feed allowing for podcast apps to discover the converted audio files. You can use Tailscale to allow connections between the server and client devices
like your mobile phone.

It can also run as a **self-hosted service that watches your feeds** and turns
every new article into a podcast episode by itself:

```
FreshRSS or any RSS/Atom feed
        ↓  poll on an interval
   deduplicate into a persistent queue
        ↓  extract and clean the article
   Kokoro TTS  →  MP3
        ↓
 subscribable podcast feed
```

Both modes share the same library, the same feed, and the same pipeline, so
`vocast add` and an auto-ingested article produce exactly the same kind of
episode. If you only want the manual workflow, nothing below changes for you.

For a large ingested library, discovery and playback can be separated:

```
search and filter /library
        -> add selected articles to Listen Later
        -> subscribe to /feeds/listen-later.xml
        -> listen in a normal podcast app
```

Jump to [Running as a service](#running-as-a-service).

## Requirements

- Python 3.10–3.12 (Kokoro does not yet support 3.13). If your default Python is newer, point pipx at a compatible one: `pipx install --python python3.12 vocast`.
- `espeak-ng` on PATH (used by Kokoro as a fallback phonemizer)

ffmpeg is bundled (via `imageio-ffmpeg`), so the only system dependency is `espeak-ng`:

```
sudo dnf install espeak-ng     # Fedora
sudo apt install espeak-ng     # Debian/Ubuntu
brew install espeak-ng         # macOS
```

## Install

The easiest way is [pipx](https://pipx.pypa.io), which installs `vocast` into an isolated environment and puts the command on your PATH so you can run it from anywhere:

```
pipx install vocast
```

If you don't have pipx, install into a virtual environment with pip:

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install vocast
```

With this option `vocast` is only available while that venv is active.

The first run downloads the Kokoro weights (~300 MB) and a small spaCy model into the cache. Subsequent runs are immediate.

### From source (development)

```
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Usage

Vocast has subcommands; run `vocast --help` to see them all.

### Add an article to the library

```
vocast add https://example.com/article    # fetch and synthesize a URL
vocast add notes.txt                      # synthesize a local text file
vocast add ... --title "Custom title"     # override the title
vocast add ... --voice af_bella           # use a different Kokoro voice
vocast add ... --quiet                    # suppress per-chunk progress
```

URLs are fetched and cleaned with `trafilatura`. Code blocks (`<pre>` elements) are stripped before synthesis since they don't translate well to audio. Each entry is stored under `~/.vocast/library/<id>/` as `audio.mp3` plus `meta.json` with title, source URL, duration, and voice.

### List the library

```
vocast list
```

### Serve the library as a podcast feed

```
vocast serve                                # 127.0.0.1:8080 by default
vocast serve --host 0.0.0.0 --port 8000     # custom host/port
```

Exposes `GET /feed.xml` (podcast RSS) and `GET /audio/<id>.mp3` (audio enclosures). Long articles are split on sentence boundaries during synthesis and concatenated with short silence between chunks.

### Expose the feed to your phone over Tailscale

```
vocast init
```

A guided checklist that walks you through installing Tailscale, signing into your tailnet, and proxying `vocast serve` over HTTPS via `tailscale serve`. Re-run after each step until it prints your feed URL, then add that URL to a podcast app on your phone.

> [!NOTE]
> Make sure the URL you add uses `https`. The app must communicate with your vocast server directly. Apps that proxy feeds through their own servers (Overcast, Pocket Casts, etc.) can't reach your tailnet. On iPhone, the built-in Apple Podcasts app works and is free. Downcast also works well, although it costs about $2.

### Synthesize directly to a file (skip the library)

```
vocast synth article.txt              # writes article.mp3
vocast synth article.txt -o out.wav   # WAV output
```


---

# Running as a service

The service polls each configured feed, queues articles it has not seen before,
narrates them one at a time, and publishes the results as a podcast feed. State
lives in SQLite, so restarts pick up exactly where they left off and an article
is never narrated twice.

## Architecture

```
                    ┌──────────┐
   feeds  ─────────▶│  poller  │──── inserts new entries ────┐
                    └──────────┘                             ▼
                                                    ┌──────────────────┐
                                                    │ SQLite           │
                                                    │ sources, entries │
                                                    └──────────────────┘
                                                             ▲
                    ┌──────────┐   claims one pending entry   │
   article ────────▶│  worker  │──────────────────────────────┘
     HTML           └──────────┘
                          │  reuses the existing vocast pipeline:
                          │  extract → clean → chunk → Kokoro → mp3
                          ▼
                 ~/.vocast/library/<id>/{audio.mp3, meta.json}
                          │
                    ┌──────────┐
                    │  server  │  /feed.xml  /feeds/all.xml
                    └──────────┘  /feeds/source/<id>.xml  /api/*
```

Design points worth knowing:

- **The poller never synthesizes.** A slow or broken feed delays discovery but
  cannot stall episode generation, and one dead feed never stops the others.
- **The database is the queue.** Workers claim an entry inside a transaction, so
  two of them can never pick up the same article.
- **The library is the authority on what audio exists.** The database only adds
  provenance, which is why manual and ingested episodes share one feed.
- **Episode GUIDs are stable forever.** They are library entry ids, written once
  when the audio is created, so re-rendering a feed or renaming an episode never
  makes a podcast app re-download anything.

| Component | Where |
|---|---|
| Source adapters (RSS, Atom, FreshRSS) | `src/vocast/ingest/adapters/` |
| Guarded HTTP fetching | `src/vocast/ingest/nethttp.py` |
| Schema and migrations | `src/vocast/ingest/db.py` |
| Poller | `src/vocast/ingest/poller.py` |
| Worker and retries | `src/vocast/ingest/worker.py` |
| Pipeline seam | `src/vocast/ingest/generator.py` |
| Feed rendering | `src/vocast/ingest/feeds.py` |
| HTTP surface | `src/vocast/ingest/api.py` |

## Quick start (no Docker)

```
vocast source add --name "Simon Willison" --url https://simonwillison.net/atom/everything/
vocast run
```

`vocast run` starts the HTTP server, the poller, and the worker together. The
first run downloads the Kokoro weights (~300 MB). Then subscribe a podcast app
to `http://<host>:8080/feeds/all.xml`.

To check things without waiting for the poll interval:

```
vocast poll                 # fetch every enabled source right now
vocast worker --once        # drain the queue, then exit
vocast entry list           # see what was discovered
```

## Docker Compose

```
mkdir -p data
cp config.example.yaml data/config.yaml   # then edit it
docker compose up -d
docker compose logs -f
```

`./data` holds the database, the generated audio, and the model cache — it is
the only thing you need to back up.

Compose binds to `127.0.0.1:8000` on purpose. Podcast apps need a reachable
URL, so put a reverse proxy in front (below) rather than exposing the port
directly, and set `VOCAST_ADMIN_TOKEN` if the admin API will be reachable from
anywhere but localhost:

```
VOCAST_PUBLIC_BASE_URL=https://podcast.example.com
VOCAST_ADMIN_TOKEN=$(openssl rand -hex 32)
```

Put those in a `.env` file next to `docker-compose.yml`.

Notes on the image:

- It is roughly 1.6 GB, almost entirely PyTorch. That is the cost of running TTS
  locally. The build installs the CPU-only torch wheel; installing the default
  wheel instead would add several GB of unused CUDA libraries.
- It runs as an unprivileged user (uid 10001) and only needs to write `/data`.
- Health is reported at `/api/health`; the healthcheck allows a 5 minute start
  period for the first-run model download.
- `docker stop` is graceful: the poller stops immediately and the worker
  finishes the episode it is on, so no half-written MP3 is left behind.
- CPU-only Linux is the target. It works on arm64 (including a Raspberry Pi 5)
  but synthesis is several times slower; expect a long article to take minutes.
  No GPU support is wired up.

## Adding a normal RSS or Atom source

```
vocast source add --name "Example" --url https://example.com/feed.xml
vocast source add --name "Slow Feed" --url https://example.com/feed.xml --interval 120
vocast source list
vocast source disable 2
vocast source enable 2
vocast source remove 2
```

Feeds behind HTTP Basic Auth or needing a particular header:

```
vocast source add --name "Members" --url https://members.example.com/feed.xml \
  --username ada --password hunter2

vocast source add --name "Picky" --url https://picky.example.com/feed.xml \
  --header 'User-Agent: vocast (self-hosted)'
```

Sources can equally be declared in `config.yaml` (see
[config.example.yaml](config.example.yaml)); that block is reconciled into the
database on every start, so it can be the single source of truth for a
deployment. Sources added later by CLI or API are kept, never removed by it.

## Adding a FreshRSS feed

FreshRSS can publish any category as an ordinary Atom feed, which is all vocast
needs.

1. In FreshRSS, open **Subscription management**, pick a category, and use its
   **RSS feed** link. You get a URL like
   `https://freshrss.example.com/i/?a=rss&get=c_1&token=YOUR_TOKEN`.
2. Add it with `kind: freshrss_feed`:

```
vocast source add --name "FreshRSS Tech" --kind freshrss_feed \
  --url 'https://freshrss.example.com/i/?a=rss&get=c_1&token=YOUR_TOKEN'
```

The token in the URL is usually all the authentication needed. If your instance
is behind a protected reverse proxy, add `--username` / `--password` too.

For a FreshRSS on your own LAN, allow the private address explicitly:

```
vocast source add --name "FreshRSS" --kind freshrss_feed \
  --url 'http://192.168.1.10/i/?a=rss&get=c_1&token=...' --allow-private
```

FreshRSS items link to the original publisher, so vocast narrates the real
article rather than FreshRSS's copy, and FreshRSS's stable per-entry id is used
for deduplication.

### Working through a large unread backlog

A FreshRSS RSS document is a **capped window** — 20 items by default, and its
`page` parameter is ignored for RSS output. That is fine for keeping up with new
articles, but it cannot enumerate a backlog: point vocast at it with 10,000
unreads and you get the newest 20, while the rest are never discovered.

For that, use `kind: freshrss_api`, which talks to FreshRSS's Google Reader
compatible API and pages through the whole backlog with `continuation` cursors:

```yaml
sources:
  - name: FreshRSS Unreads
    kind: freshrss_api
    # The instance base URL, not a feed URL.
    url: https://freshrss.example.com
    username: your-freshrss-user
    # Settings > Profile > API management. NOT your login password.
    api_password: ${FRESHRSS_API_PASSWORD}
    unread_only: true
    page_size: 200
    max_entries_per_poll: 20000
```

This needs **API access enabled** in FreshRSS (Settings > Authentication) and an
API password set. To set one from the host:

```
docker exec freshrss php /var/www/FreshRSS/cli/update-user.php \
  --user your-user --api-password 'generated-secret'
```

The adapter stops paging as soon as it reaches a page of articles it already
tracks, so only the first poll walks the full backlog; later polls fetch a
single page. Without that it would re-download everything every 15 minutes.

Pair it with newest-first narration, or the queue will start with your oldest
unread article:

```yaml
worker:
  newest_first: true
  concurrency: 4
```

The API stream is ordered by *crawl* time (when FreshRSS fetched an article),
which is what makes pagination reliable; `newest_first` then narrates in true
newest-*published* order, which is usually what you actually want.

> Only unread enumeration is implemented. Vocast never marks anything read in
> FreshRSS, so articles stay unread there until you deal with them yourself.

## Configuring the public base URL

This is the setting people get wrong most often.

Enclosure URLs in the feed must be absolute and reachable by the podcast app.
By default they are derived from the incoming request, which breaks behind a
reverse proxy that terminates TLS: the request arrives as plain HTTP on an
internal hostname, and the app is handed URLs it cannot fetch.

Set it explicitly:

```yaml
server:
  public_base_url: https://podcast.example.com
```

or `VOCAST_SERVER_PUBLIC_BASE_URL=https://podcast.example.com`. Verify with:

```
curl -s https://podcast.example.com/feeds/all.xml | grep enclosure
```

Every URL in that output must be fetchable from outside your network.

## Private feeds and apps that crawl server-side

Some podcast apps fetch feeds from **their own servers** rather than from your
device: Overcast, Pocket Casts, and Spotify all work this way. They cannot reach
a LAN or tailnet address at all, so the feed has to be reachable from the public
internet for them to work. Apps that fetch on-device (Apple Podcasts, Downcast,
AntennaPod) have no such requirement.

"Reachable from the internet" need not mean "public". Set a feed token and the
feeds, audio, and cover art all require it, exactly like a paid private podcast:

```yaml
server:
  public_base_url: https://podcast.example.com
  feed_token: ${VOCAST_FEED_TOKEN}
```

```
https://podcast.example.com/feeds/all.xml?token=YOUR_TOKEN
```

Anything without a valid token gets `401`. The token is injected into the
enclosure and cover URLs inside the feed, so clients keep working; rotate the
value to revoke access. `/api/health` stays open so container health checks work.

A podcast client cannot send an `Authorization` header, which is why the secret
travels in the query string. Treat the URL itself as the credential.

### Keeping the audio off the public internet

If the app only needs to *crawl* the feed, the episodes themselves can stay on a
private network. `audio_base_url` points enclosures somewhere other than the
feed's own host:

```yaml
server:
  # Publicly reachable, so a server-side crawler can read the feed.
  public_base_url: https://box.tailnet.ts.net
  # Enclosures resolve only inside the tailnet, so no audio leaves it.
  audio_base_url: http://100.64.0.1:3402
  feed_token: ${VOCAST_FEED_TOKEN}
```

Whether this is enough depends on the app: it works only if that app downloads
audio on the device. If episodes fail to download, drop `audio_base_url` so the
enclosures use the public host too.

## Subscribing from a podcast app

| Feed | Contents |
|---|---|
| `/feed.xml` | Everything. Unchanged from earlier vocast versions. |
| `/feeds/all.xml` | Everything. Identical to `/feed.xml`. |
| `/feeds/source/<id>.xml` | One source only, as its own show. |
| `/feeds/listen-later.xml` | Only ready episodes selected in the web library. |

Feeds carry the newest `server.feed_max_items` episodes (default 300); set it to
`unlimited` for no cap. Rendering costs nothing per episode -- duration and size
are read from the database, not from the audio files -- so an uncapped feed is
affordable even with thousands of episodes. Episodes finished before this was
recorded still read their metadata from disk, which on a network share costs
around 17 ms each.

Use `vocast source list` to get source ids.

## Searchable library and Listen Later

Open `http://<vocast-host>:<port>/library` while the ingestion service is
running. This page is the discovery interface: it shows pending, processing,
ready, failed, ignored, and expired articles without turning the podcast feed
into a browsing interface. It uses server-rendered HTML and remains usable on a
phone; search, filters, and pagination do not require JavaScript.

Search matches article title, publication, Vocast source, and author without
regard to case. Filters cover publication, source, processing status, Listen
Later state, downloaded state, publication date, and duration. Results can be
sorted by publication date, title, or duration. FreshRSS API sources can contain
many publications; Vocast groups those by the persisted publication name,
normalized for filtering. The upstream stream/feed identifier is not currently
stored, so a renamed publication can appear as a new filter value.

Use **Add to Listen Later** on a card, then subscribe your podcast app to:

```
https://podcast.example.com/feeds/listen-later.xml
```

Use **Remove from Listen Later** to withdraw it. Pending or failed articles may
be selected, but they stay out of the podcast feed until their status is
`ready`. Explicit playlist positions are listed first; otherwise the feed is
newest-added first, with stable entry ids as tie-breakers. Each episode keeps
its original article publication date and its existing library id as the
podcast GUID.

When `VOCAST_ADMIN_TOKEN` is configured, Listen Later changes require that same
bearer token. The page asks for it on first use and stores it only in the current
browser tab's session storage; Vocast never writes the token into the HTML or a
URL. Direct `/library` is intentionally tokenless for access through a trusted
tailnet address; do not expose that route directly to the internet. The bundled
Funnel script maps public `/library` to the protected internal `/public/library`
route instead. Open the public page as `/library?token=...`; Vocast validates the
feed token, stores it in an HttpOnly same-site session cookie, and redirects to
a clean `/library` URL so filters and pagination do not carry the secret. The
Listen Later feed follows the same feed-token rule as existing feeds.

Removing an episode from Listen Later removes it from future feed responses but
cannot delete a copy that a podcast app already downloaded. A download request
is the only consumption signal Vocast receives; ordinary podcast apps do not
report playback completion, so Vocast cannot detect that an episode was truly
heard and does not remove it automatically.

> [!NOTE]
> Many podcast apps fetch feeds through **their own servers**, not your phone.
> Overcast and Pocket Casts work this way and cannot reach a LAN-only or
> tailnet-only URL — the feed must be publicly reachable over HTTPS. Apps that
> fetch directly from the device (Apple Podcasts, Downcast) work fine with
> Tailscale. This is also why `vocast init` exists for the Tailscale setup.

## Reading on a phone

`/m` is a second, phone-sized view of the same library, laid out like a native
iOS reader rather than a narrower version of `/library`. It exists alongside
`/library`, which is unchanged.

- `/m` lists where you can go, in three groups: **Library** (everything) and
  **Listen Later**; then the pipeline **statuses**; then every publication.
- `/m/articles` lists the articles for whatever you tapped, as dense rows.
  `?origin_id=` selects a publication, `?playlist=listen-later` the queue,
  `?status=` one pipeline status, and `?search=` narrows within the selection.

The bottom toolbar carries a three-way **unread / read / all** segmented
control. It is the one filter that applies everywhere: every count on `/m` —
Library, Listen Later, each status and each publication — is counted under it,
so the numbers always add up to the number beside Library. Unread is the
default. Status and the read filter compose, so "ready and unread" is one tap
from "ready". Ready, processing, pending and failed are always listed, even at
zero; ignored and expired appear only when they hold something. Publications
with nothing matching drop out of the list entirely.

Each row shows how long its narration runs, once there is one: a duration for a
finished episode, a progress bar while it is being narrated, and nothing at all
for an article the worker has not reached. Tapping a row opens the original
article in a new tab.

Swipe a row right to toggle read, left to toggle Listen Later. Both actions are
also plain buttons — the unread dot on the left, the star on the right — so
neither needs a gesture, and an Undo appears beside the row after either one.
The gesture keeps whichever direction it started in, so dragging back cancels
rather than performing the other action, and it will not start within about
28px of either screen edge, which is left to Safari's own back and forward
swipes. Rows never disappear on their own: marking one read while the unread
filter is showing leaves it in place, dimmed, so the list does not resequence
under your thumb. **Refresh**, top right on both pages, pulls read state from
FreshRSS and reloads, which is what reconciles the page with the filter.

There is no player here. This view is for triage; listening happens in a podcast
app subscribed to the feed. The list, the filters, search and refresh are
server-rendered and work without JavaScript; the swipes, the Undo and forcing
the FreshRSS pull are what need it.

Access follows the same rule as everything else: nothing is required from the
tailnet, and a request arriving through Funnel needs the feed token. Open
`/m?token=...` once and Vocast stores it in the same HttpOnly cookie `/library`
uses, then redirects to a clean `/m` so links between the two pages never carry
the secret. `scripts/enable-public-feed.sh` does not publish `/m` through
Funnel, so out of the box this view is reachable from the tailnet only.

## Running a manual poll

```
vocast poll                      # every enabled source, ignoring intervals
vocast poll --source-id 3        # just one
vocast poll --due-only           # respect intervals, as the scheduler does
```

Or over HTTP:

```
curl -X POST -H "Authorization: Bearer $VOCAST_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/sources/3/poll
```

## Retrying failed entries

```
vocast entry list --status failed -v     # what failed and why
vocast entry show 42                     # one entry in detail
vocast entry retry 42                    # put it back in the queue
```

Transient failures (timeouts, 5xx, rate limits) retry automatically with
exponential backoff — 5, 10, 20, 40 minutes and so on, capped at 6 hours, for 5
attempts by default. Permanent failures (404, a blocked URL, a page that
extracts to almost nothing) are parked immediately as `failed`; retrying those
only helps once the underlying problem is fixed.

Entry states: `pending` → `processing` → `ready`, plus `failed`, `ignored`, and
`expired` (removed by retention).

Some failures are correct and permanent. Comics, video links, podcast episode
pages, and paywalled teasers extract to almost nothing, and vocast refuses to
publish an episode of a navigation menu. Check what a URL actually yields before
assuming extraction is at fault:

```
vocast entry show 42        # the URL, error, retry count and schedule
```

## Controlling narration

Synthesis is CPU-bound and will use every core it is given. It can be stopped
and resumed without restarting anything:

```
vocast pause      # stop narrating; CPU drops to idle
vocast resume
vocast status     # queue progress and whether it is paused
```

`pause` interrupts the article in progress rather than waiting for it. Long
articles can take hours, so waiting would not be a pause in any useful sense.
The article returns to the queue with its attempt count untouched and no partial
audio is written; it restarts from the beginning when you resume. The state is
stored in the database, so a restart does not silently resume.

`vocast status` reports progress:

```
narration : running
pending   : 10489
ready     : 13
failed    : 18
progress  : 31/10523 (0.3%)
```

### Re-narrating existing episodes

After changing anything about how narration sounds -- a different voice, engine,
or the title and byline intro -- existing episodes keep their old audio until
regenerated:

```
vocast regenerate            # every finished episode
vocast regenerate 42         # just one
vocast regenerate --limit 20
```

This happens **in place**: the episode keeps its id, and therefore its podcast
GUID, and the new audio is swapped in only once complete. Subscribers see the
episode update rather than the old one vanish and a new one appear, and the feed
never goes empty. Clients will re-download the audio, which is unavoidable since
it genuinely changed.

`vocast backfill-text` re-extracts article text for episodes generated before
that text was stored. It only fetches and extracts -- nothing is re-synthesized.

## Choosing a TTS engine

Two engines ship, both running the same Kokoro model and voices:

```yaml
tts:
  engine: kokoro-onnx    # or: kokoro
  voice: af_heart
```

`kokoro` uses PyTorch. `kokoro-onnx` runs the same weights under ONNX Runtime and
is faster on CPU. Measured on a Coffee Lake i5, one thread, same article:

| Engine | Synthesis | Model load | Notes |
|---|---|---|---|
| `kokoro` (PyTorch) | 273.5s | 7.4s | |
| `kokoro-onnx` | 196.9s | 1.4s | **1.39x faster**, fp32 |

The ONNX engine uses fp32 weights, so it is numerically equivalent to the
PyTorch path: switching engines cannot change how anything sounds. Model files
download on first use to `VOCAST_TTS_MODEL_DIR` (default `~/.vocast/models`).

> Quantized variants exist and were measured too. On this CPU generation int8 is
> **2.2x slower** than fp32, because int8 inference needs VNNI instructions that
> Coffee Lake lacks and otherwise pays dequantize overhead in software. fp16 is
> ~10% faster than fp32, winning on memory bandwidth rather than arithmetic, but
> it is lossy. Newer CPUs with VNNI would likely favour int8; measure before
> assuming.

## Tuning throughput

```yaml
worker:
  concurrency: 3           # articles narrated in parallel
  threads_per_worker: 1    # compute threads each may use
  nice: 15                 # yield to interactive work
  newest_first: true       # narrate recent articles before old ones
  reclaim_on_start: true   # requeue work abandoned by a restart
```

**`threads_per_worker` matters more than it looks.** Left unset it is
CPUs / concurrency, which is almost always what you want. Left to the TTS
library's own default, *every* worker takes *every* core: four workers on a
four-CPU quota means twenty-four compute threads contending for four cores,
which reads as fully-busy CPU while producing almost nothing.

`nice` deprioritizes only the synthesis threads -- on Linux `nice` is per-thread,
so the HTTP server stays responsive. It does not reduce throughput on an
otherwise idle machine.

`newest_first` claims the most recently *published* article rather than the
longest-queued one. With a large backlog that is the difference between starting
on today's news and starting on a four-year-old post. New articles therefore go
to the front of the queue as they are discovered.

`reclaim_on_start` requeues anything left mid-synthesis by a restart, instead of
waiting out `processing_timeout_minutes`. Enable it only when this process is the
sole worker: a separate `vocast worker` running alongside would have its live
claims stolen.

> On a thermally limited machine, more workers is not always faster. Measure
> *sustained* throughput, not a burst: a cooled-down benchmark on one deployment
> preferred four workers, while an eleven-minute run with temperature and clock
> recorded showed four and three converging once heat-soaked, with four
> periodically collapsing to the CPU's minimum frequency.

## Retention

Off by default — nothing is ever deleted unless you ask.

```yaml
retention:
  enabled: true
  max_age_days: 90
  max_episodes: 1000
```

Either limit can trigger removal. Episodes added by hand with `vocast add` are
protected unless you set `include_manual: true`, since they cannot be
regenerated from a feed.

```
vocast retention apply --dry-run     # show what would go
vocast retention apply               # do it
```

The database row survives as an `expired` marker. That is deliberate: deleting
it would let the next poll rediscover the article and narrate it again.

> [!NOTE]
> Removing an episode from the feed does **not** delete it from podcast apps
> that already downloaded it. Those copies live on the device.

## Configuration

Precedence: built-in defaults → `config.yaml` → environment variables.

The config file is looked up at `$VOCAST_CONFIG`, then `~/.vocast/config.yaml`,
then `/data/config.yaml`. A full annotated example is in
[config.example.yaml](config.example.yaml). Inspect what is actually in effect
with `vocast config show` (secrets are masked).

Environment variables follow `VOCAST_<SECTION>_<KEY>`:

| Variable | Default |
|---|---|
| `VOCAST_CONFIG` | auto-discovered |
| `VOCAST_SERVER_HOST` | `127.0.0.1` |
| `VOCAST_SERVER_PORT` | `8080` |
| `VOCAST_SERVER_PUBLIC_BASE_URL` | derived per request |
| `VOCAST_SERVER_FEED_TOKEN` | unset (feeds open) |
| `VOCAST_SERVER_AUDIO_BASE_URL` | same as the feed host |
| `VOCAST_SERVER_FEED_MAX_ITEMS` | `300` (`unlimited` for no cap) |
| `VOCAST_DATABASE_PATH` | `~/.vocast/vocast.db` |
| `VOCAST_STORAGE_LIBRARY_PATH` | `~/.vocast/library` |
| `VOCAST_STORAGE_REQUIRE_MARKER` | `false` |
| `VOCAST_POLLING_DEFAULT_INTERVAL_MINUTES` | `15` |
| `VOCAST_WORKER_CONCURRENCY` | `1` |
| `VOCAST_WORKER_PROCESSING_TIMEOUT_MINUTES` | `60` |
| `VOCAST_WORKER_MAX_RETRIES` | `5` |
| `VOCAST_WORKER_BASE_RETRY_MINUTES` | `5` |
| `VOCAST_WORKER_MAX_RETRY_MINUTES` | `360` |
| `VOCAST_WORKER_NICE` | `0` |
| `VOCAST_WORKER_THREADS_PER_WORKER` | CPUs / concurrency |
| `VOCAST_WORKER_RECLAIM_ON_START` | `false` |
| `VOCAST_WORKER_NEWEST_FIRST` | `false` |
| `VOCAST_RETENTION_ENABLED` | `false` |
| `VOCAST_RETENTION_MAX_AGE_DAYS` | `90` |
| `VOCAST_RETENTION_MAX_EPISODES` | `1000` |
| `VOCAST_RETENTION_INCLUDE_MANUAL` | `false` |
| `VOCAST_TTS_ENGINE` | `kokoro` |
| `VOCAST_TTS_VOICE` | engine default (`af_heart`) |
| `VOCAST_TTS_MODEL_DIR` | `~/.vocast/models` (ONNX engine) |
| `VOCAST_ADMIN_TOKEN` | unset (admin API open) |
| `VOCAST_LOG_LEVEL` | `INFO` |
| `VOCAST_ALLOW_PRIVATE_URLS` | `false` |

**Secrets never need to be written into the YAML.** Any string value may
reference the environment as `${VAR}`, or `${VAR:-fallback}`:

```yaml
sources:
  - name: FreshRSS
    kind: freshrss_feed
    url: https://freshrss.example.com/i/?a=rss&get=c_1&token=${FRESHRSS_TOKEN}
```

A `${VAR}` with nothing set is a startup error rather than being sent literally
as a credential.

## HTTP API

Feeds and `/api/health` are always public — podcast clients cannot send an
`Authorization` header. Everything else requires
`Authorization: Bearer $VOCAST_ADMIN_TOKEN` when a token is configured.

```
GET    /api/health
GET    /api/sources
POST   /api/sources
PATCH  /api/sources/{id}
DELETE /api/sources/{id}
POST   /api/sources/{id}/poll
GET    /api/entries?status=failed&source_id=1&limit=100
POST   /api/entries/{id}/retry
POST   /api/playlists/listen-later/entries/{id}
DELETE /api/playlists/listen-later/entries/{id}
```

`/api/health` reports application and database status, whether the worker and
poller are running, pending and failed counts, and the last successful poll:

```
curl -s http://127.0.0.1:8000/api/health
{"status":"ok","database":"ok","worker":"running","poller":"running",
 "sources":1,"pending":0,"failed":0,"last_successful_poll":"2026-07-25T09:55:48Z"}
```

## Reverse proxy

Caddy is not required, but it is the shortest path to a working HTTPS feed:

```caddy
podcast.example.com {
    reverse_proxy vocast:8000
}
```

Then set `VOCAST_SERVER_PUBLIC_BASE_URL=https://podcast.example.com`, or the
feed will advertise unreachable internal URLs.

If you would rather not expose anything publicly, `vocast init` walks through
serving the feed over your tailnet with `tailscale serve` — but see the note
above about apps that proxy feed fetches through their own servers.

## Data locations and backups

| What | Default | In Docker |
|---|---|---|
| Ingestion state | `~/.vocast/vocast.db` | `/data/vocast.db` |
| Episodes | `~/.vocast/library/<id>/` | `/data/library/<id>/` |
| Model cache | `~/.cache/huggingface` | `/data/cache/huggingface` |

### Storing episodes on a network share

Audio can live anywhere; only the database needs local disk.

```yaml
database:
  path: /data/vocast.db     # keep local: SQLite over CIFS/NFS risks corruption
storage:
  library_path: /audio      # a bind mount of the share
  require_marker: true
```

`require_marker` guards the case where the share is not mounted yet — at boot,
or after a network blip. An unmounted bind mount looks like an empty directory,
so episodes would be written somewhere they are never served from. With it set,
vocast refuses to start unless `<library_path>/.vocast-storage` exists:

```
touch /mnt/your-share/.vocast-storage
```

Under `restart: unless-stopped` that turns a not-yet-ready mount into a retry
loop that heals itself. Also make sure the container user can write to the
share — a CIFS mount forces its own uid/gid, so match it (`user: "1000:964"`).

Each episode directory holds `audio.mp3` and `meta.json` — unchanged from
earlier vocast versions.

Back up the database and the library together. The database is SQLite in WAL
mode, so copy it with `sqlite3` rather than `cp` while the service is running:

```
sqlite3 /data/vocast.db ".backup '/backup/vocast.db'"
tar czf /backup/library.tar.gz -C /data library
```

The model cache is disposable; it re-downloads.

On first startup with this version, Vocast applies an additive database
migration that creates `playlists` and `playlist_entries`, then creates the
built-in `listen-later` system playlist. Existing sources and entries are not
rewritten or removed, and reopening an already migrated database is a no-op.
Back up the SQLite database and media library together before upgrading, as for
any stateful service. The migration runs when Vocast opens the database; there
is no separate production migration command.

If you lose the database but keep the library, existing episodes still appear in
`/feed.xml` (the library is what the feed is built from), but articles will be
rediscovered and narrated again, since the dedup records are gone.

## Security considerations

**SSRF.** Source and article URLs are supplied by you, and articles can
redirect anywhere, so every outbound request is a request your server makes on
someone else's behalf. All of them go through one guarded layer that:

- allows only `http` and `https`;
- refuses loopback, private, link-local, reserved, and multicast addresses by
  default, which blocks cloud metadata endpoints such as `169.254.169.254`
  (including their IPv4-mapped IPv6 forms);
- checks *every* address a hostname resolves to, not just the first;
- re-validates each redirect hop, so a public URL cannot bounce to localhost;
- caps response size (10 MB default) and applies timeouts to every request.

`allow_private_urls`, and the per-source `--allow-private`, deliberately switch
the address check off so you can reach a LAN FreshRSS. **Only point it at hosts
you trust.** With it enabled for a source, a malicious feed in that source can
make your server fetch internal addresses. Prefer the per-source flag over the
global one.

**Admin API.** Write endpoints are unauthenticated unless `VOCAST_ADMIN_TOKEN`
is set. That is fine on a loopback bind and not fine otherwise; the service logs
a warning when it binds a non-loopback interface without a token. The bundled
Compose file binds to `127.0.0.1` for this reason.

**Credentials.** Feed credentials are never logged and are never returned by the
API; `vocast config show` masks them. Prefer `${VAR}` references over literals
in the config file.

**Other.** Titles and URLs from third-party feeds are XML-escaped on the way
into the feed. Episode ids from URL paths are validated, so they cannot escape
the library directory. Retention refuses to delete anything outside the
configured library path. Article content is only ever parsed and narrated, never
executed.

## Known limitations

- **Narration is literal.** The article body is read as extracted. No
  summarizing, rewriting, or translation, and no LLM is involved.
- **Extraction is imperfect.** Paywalls, consent walls, and
  JavaScript-rendered pages often yield too little text. Vocast fails those
  loudly (they land in `failed`) rather than publishing an empty episode.
- **Code blocks are dropped**, as they were before — they do not translate to
  audio.
- **One voice.** No per-source voices, no multiple narrators, no diarization.
- **Single process.** The poller, worker, and server share one process and one
  SQLite file. Fine for a homelab; there is no distributed queue and no
  Kubernetes story.
- **`worker.concurrency > 1` multiplies memory**, because each worker loads its
  own copy of the TTS model.
- **No played/read synchronization.** Marking an episode played in a podcast app
  does not feed back to FreshRSS, by design.
- **Single-user library.** The web library has one built-in Listen Later queue;
  there are no user accounts, shared queues, or playback-completion sync.
- **Python 3.10–3.12**, because Kokoro does not support 3.13 yet.

## Troubleshooting

**Nothing is being discovered.** Check the source is enabled and when it was
last polled: `vocast source list` shows a `LAST OK` column and flags errors.
Force a fetch with `vocast poll` and read the log line — it names the source,
the URL, the stage, and the error.

**`private, loopback, and link-local addresses are blocked`.** The feed or
article is on a private address. Add `--allow-private` to that source, or set
`allow_private_urls: true`. See the security note first.

**Entries stay `pending`.** Nothing is running the queue. Use `vocast run`, or
`vocast worker`, and confirm with `curl /api/health` that `worker` is
`running`. Entries with a future `next_retry_at` are waiting on backoff — check
`vocast entry show <id>`.

**`extracted only N characters ... below the 400 character minimum`.** The page
was a paywall, consent screen, or navigation stub. Confirm with
`vocast add <url>`; if that also fails, the page is not extractable and the
article cannot be narrated.

**Episodes generate but the podcast app shows nothing.** Almost always
`public_base_url`. Fetch the feed from *outside* your network and check that the
`enclosure` URLs resolve. If the app is Overcast or Pocket Casts, the feed must
be publicly reachable — those fetch through their own servers.

**Episodes appear but will not play.** Fetch an enclosure URL directly; it
should return `200` with `Content-Type: audio/mpeg`. A `404` means the audio
file is missing from the library while the metadata remains.

**An entry is stuck in `processing`.** A worker died mid-synthesis. It is
requeued automatically after `worker.processing_timeout_minutes` (60 by
default); `vocast entry retry <id> --force` does it now.

**`ModuleNotFoundError: No module named 'kokoro'`.** Installed without
dependencies. The ingestion CLI and tests run without Kokoro, but synthesis
needs it: `pip install vocast`.

**Container is killed mid-episode.** PyTorch needs headroom; raise
`mem_limit` past 2 GB. The entry returns to `pending` and is retried, so nothing
is lost.

**Weights re-download on every container start.** `HF_HOME` must land on the
`/data` volume. The bundled Compose file handles this.
