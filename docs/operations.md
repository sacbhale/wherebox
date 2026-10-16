# Operations

How to install, expose, secure, back up and restore Wherebox. Everything the
app stores lives in one directory, `/data` in the container (`./data` next to
`docker-compose.yml` by default):

| Path | Contents |
|---|---|
| `inventory.db` (+ `-wal`, `-shm`) | SQLite database in WAL mode |
| `photos/YYYY/MM/` | Original uploads, plus an upright `.display.jpg` copy of each photo (long edge 2000 px) |
| `backups/latest.db` | The latest consistent snapshot of the database |

## Install

```sh
cp .env.example .env        # then edit .env, at least PUBLIC_BASE_URL (see below)
docker compose up -d        # pulls the published image; `--build` builds this checkout instead
docker compose logs -f app  # shows the one-time setup token when no password is set
curl -fsS http://127.0.0.1:8000/healthz
```

The shipped `.env.example` works as-is for a first try on the same machine
(`PUBLIC_BASE_URL=http://localhost:8000`, published on `127.0.0.1:8000`). For real use,
put the app behind an HTTPS reverse proxy (below) and set `PUBLIC_BASE_URL` to the URL the
household will use.

`uvicorn` runs with exactly one worker process, and one background thread handles AI
captures and backups. Do not scale the service to more replicas or workers: the
single-worker rule is what keeps AI spending within one call of the budget.

### File ownership: PUID/PGID or compose `user:`

- **Default.** The container starts as root. The entrypoint changes the owner of any file
  in `/data` that is not owned by `PUID:PGID` (default `1000:1000`), then drops to that
  user with `setpriv`. A root-owned `./data`, as created by `docker compose up` itself,
  works. Set `PUID`/`PGID` in `.env` to the host user that should own the files.
- **Compose `user:`.** Uncomment `user: "1000:1000"` in `docker-compose.yml` (use your
  uid:gid) to start unprivileged. The entrypoint then skips the ownership fix and the
  privilege drop, so `./data` must already be writable by that user
  (`mkdir -p data && sudo chown 1000:1000 data`). Use this with rootless Docker, Podman,
  and hardened hosts.
- **NFS/CIFS with root_squash.** Root in the container cannot change owners on such mounts.
  The entrypoint prints a warning and continues. Set compose `user:` to the uid:gid that
  owns the share instead.

## Reverse proxy and network exposure

The app speaks plain HTTP on port 8000 and expects a reverse proxy to terminate HTTPS.

### Publish on loopback, or `expose` on a shared network

- **Proxy on the host** (Caddy or nginx installed on the machine, Tailscale serve): keep
  the shipped `ports: ["127.0.0.1:8000:8000"]`. Never publish on `0.0.0.0` (a bare
  `"8000:8000"`): that bypasses the proxy and, with Docker's default firewall rules, is
  reachable from the LAN even when a host firewall seems to block it.
- **Proxy in Docker** (Caddy or Traefik containers): remove `ports:` and use
  `expose: ["8000"]` on a network shared with the proxy. Then only containers on that
  network can reach the app.
- **Direct LAN access, no proxy** (a trusted home network only): set `BIND_ADDRESS=0.0.0.0`
  in `.env`. Also set `PUBLIC_BASE_URL` to the address phones will use (for example
  `http://192.168.1.20:8000`) so QR/NFC tags point there. Set `FORWARDED_ALLOW_IPS=127.0.0.1`
  so no LAN client can spoof `X-Forwarded-For` to dodge the login throttle. Traffic,
  including the password, is unencrypted plain HTTP. The browser microphone (voice input)
  needs HTTPS, so it stays hidden; keyboard dictation still works.

### Trusted proxies: `FORWARDED_ALLOW_IPS`

uvicorn runs with `--proxy-headers` and honours `X-Forwarded-For` and `X-Forwarded-Proto`
only from addresses in `FORWARDED_ALLOW_IPS`. The client IP drives the login throttle, and
the scheme drives the Secure cookie flag and the CSRF origin check. The default is
`127.0.0.1,172.16.0.0/12`: loopback plus the Docker bridge range, where a proxy container
or the Docker gateway normally appears.

> **Warning: trusting `172.16.0.0/12` is unsafe with Docker's userland-proxy, hairpin NAT,
> rootless Docker (slirp4netns/pasta), or Podman.** In those setups every client,
> including LAN or internet clients, can reach the app from a bridge or gateway address.
> Any client could then send its own `X-Forwarded-For`, pick an IP to evade the per-IP
> login throttle, and claim `https`. Set `FORWARDED_ALLOW_IPS` to the proxy's exact
> address instead, for example `FORWARDED_ALLOW_IPS=172.30.0.2`. Find it with
> `docker network inspect <network>` (proxy container) or `docker network inspect
> <project>_default --format '{{(index .IPAM.Config 0).Gateway}}'` (proxy on the host,
> seen through the gateway). Never use `*`.

A request from an untrusted address is still served. Its forwarded headers are just
ignored, so it counts as the direct peer IP over plain HTTP.

### Caddy

On the host, with the app published on `127.0.0.1:8000`:

```caddyfile
wherebox.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

As a container on a network shared with the app (`expose: ["8000"]` on the app):

```caddyfile
wherebox.example.com {
    reverse_proxy app:8000
}
```

Caddy sets `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` by default.
Set `PUBLIC_BASE_URL=https://wherebox.example.com`.

### Traefik

The app joins Traefik's network, and Traefik discovers it by labels. Give that network a
fixed subnet, so `FORWARDED_ALLOW_IPS` can name Traefik's exact address:

```yaml
services:
  app:
    # ...as in docker-compose.yml, but with `expose` instead of `ports`:
    expose: ["8000"]
    environment:
      FORWARDED_ALLOW_IPS: 172.30.0.2 # Traefik's address on the proxy network
    networks: [proxy]
    labels:
      traefik.enable: "true"
      traefik.http.routers.wherebox.rule: Host(`wherebox.example.com`)
      traefik.http.routers.wherebox.entrypoints: websecure
      traefik.http.routers.wherebox.tls.certresolver: letsencrypt
      traefik.http.services.wherebox.loadbalancer.server.port: "8000"
  traefik:
    # ...your Traefik service...
    networks:
      proxy:
        ipv4_address: 172.30.0.2
networks:
  proxy:
    ipam:
      config:
        - subnet: 172.30.0.0/24
```

Traefik sets the `X-Forwarded-*` headers by default.

### Tailscale serve

On the Docker host, with the app published on `127.0.0.1:8000`:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

Set `PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net`. The connection reaches the
container through the Docker gateway, which the default `FORWARDED_ALLOW_IPS` trusts. Only
local processes can reach a `127.0.0.1` publish, so this is safe unless the host runs
untrusted software. To be exact, set `FORWARDED_ALLOW_IPS` to the gateway address (see the
warning above). If the phone gets no session cookie behind Tailscale, set
`COOKIE_SECURE=true`, because every Tailscale serve URL is HTTPS.

### `COOKIE_SECURE`

- `auto` (default): the session cookie gets the Secure flag when the effective scheme,
  after trusted-proxy handling, is https. Plain-HTTP LAN access such as
  `http://nas.local:8000` still gets a working cookie.
- `true`: always Secure. Use it when the app is only ever reached over HTTPS.
- `false`: never Secure. Only for plain-HTTP setups whose proxy mis-reports the scheme.

### `PUBLIC_BASE_URL`: set it before printing tags

Every location's QR code and NFC tag holds `PUBLIC_BASE_URL/l/<code>`. Choose the final
URL (`scheme://host[:port]`, no path) **before printing any tags**. Changing it later
orphans every printed QR/NFC tag, because the old URL no longer reaches the app. Settings
repeats this warning. The browser's `Origin` must match the `PUBLIC_BASE_URL` host, the
request's Host, or one of the comma-separated names in `ALLOWED_HOSTS`. Otherwise forms
return 403.

## Login name, passwords and login

The household shares **one login**: a login name and a password (no per-person accounts).

- **First start without a password.** Every page redirects to `/setup`, which asks for a
  one-time **setup token** printed in the container logs (`docker compose logs app`, look
  for `SETUP TOKEN`), the login name and the password. After setup the token is discarded
  and `/setup` returns 404.
- **The login name** is 1-64 characters (surrounding spaces are trimmed) without control
  characters. It is compared **case-insensitively** (Unicode NFKC + casefold), so a phone
  that capitalises the first letter still logs in. Set it with **`HOUSEHOLD_USERNAME` in
  `.env`**: on start, a value that differs from the stored name replaces it and signs out
  all sessions once (an invalid value stops the app with an error naming the rule). Or set
  it with the CLI, which also signs out all sessions (`HOUSEHOLD_USERNAME` wins on the
  next restart if it differs):

  ```sh
  docker compose exec app python -m app.cli set-username            # prompts
  docker compose exec app python -m app.cli set-username --name Kim
  ```

- **Why a name helps:** an attacker must guess the name *and* the password, and a wrong
  name gets the same "Wrong login name or password." answer, after the same password
  check, as a wrong password. This only adds strength if the name is **not guessable**: the
  household's surname, "admin" or "home" add little. The long passphrase remains the main
  defence.
- **Legacy mode (upgrades):** an instance set up before login names existed has no name
  stored. Until one is set, the password alone logs in (the name field is ignored) and
  Settings warns: "No login name is set". Existing sessions stay valid until the name is
  set; then everyone logs in again with name + password. The login page looks the same
  either way.
- **`HOUSEHOLD_PASSWORD` in `.env`** is hashed at startup and **replaces the stored
  password on every restart**. It overrides anything set with `set-password`, so remove
  it from `.env` if you manage the password with the CLI.
- **Change the password with the CLI:**

  ```sh
  docker compose exec app python -m app.cli set-password
  ```

  Every password or login-name change (setup, env override or CLI) signs out all sessions.
- **Sessions** last `SESSION_DAYS` (30) and are extended while in use.

### Login throttle and its trade-off

- Every failed login counts, whether the name, the password or both were wrong.
- One client IP gets 5 failed logins per 15 minutes, then HTTP 429 with `Retry-After`.
  Other IPs are unaffected.
- Once more than 50 logins fail across all IPs in 15 minutes, every attempt (right or
  wrong, from any IP) waits `min(2s × (failures − 50), 30s)` before the password is
  checked. It **never locks the household out**. A correct password still works, just
  slowly, during an attack.
- The trade-off: a hard lock would let an attacker lock the household out. The progressive
  delay still lets a distributed attacker make about one guess every 30 seconds at the cap.
  That is negligible against an argon2id-hashed **long passphrase**, so use one (four or
  more random words).
- The counters live in memory and **reset on restart** (accepted: restarts are rare, and
  argon2 slows guessing anyway).
- The throttle is only as good as the client IP. See the `FORWARDED_ALLOW_IPS` warning.

## AI capture: provider, cost and budget

- `AI_PROVIDER=anthropic` with `AI_API_KEY` and `AI_MODEL` (a vision-capable Claude
  model). Without a key, AI capture is disabled with a message, and manual entry still
  works.
- **Prices and budget are in USD.** `AI_INPUT_PRICE_PER_MTOK` and
  `AI_OUTPUT_PRICE_PER_MTOK` are USD per million tokens (copy them from the provider's
  price list for your model). `MONTHLY_AI_BUDGET` is USD per calendar month in `TZ` and
  requires both prices (except with OpenRouter, see below). Once the month's total reaches it, capture buttons are disabled
  and the capture endpoints return 409 until next month. Manual entry keeps working.
  Overshoot is at most the one call already in flight.
- **Timeouts.** A call that hits its timeout (a limit on the whole call:
  `AI_TIMEOUT_SECONDS`, default 60, plus 10 seconds for every photo after the first, so a
  6-photo capture gets 110 s and a 12-photo extension 170 s) reports no usage. With Anthropic it is recorded with 0 tokens, although it may still be
  billed, so reported usage and the budget check can undercount. With OpenRouter, a timed-out
  or dropped call (after the request was sent) is charged a conservative estimate: the
  typical input tokens for that many photos plus the full output cap (16,384 tokens for photo,
  reconcile and "another angle" captures, 4,096 for receipts and text) at the model's catalog
  price. Settings says so as well.
- **Stopping during a call.** `stop_grace_period: 15s` lets uvicorn finish HTTP requests,
  but it is shorter than the 60s provider timeout. `docker compose stop` can therefore
  interrupt an in-flight AI call. The capture stays in the queue, is retried on the next
  start (its upload is on disk), and at most that one call's usage goes unrecorded.
- Failed captures keep their input. **Retry** on the capture resets it. A capture that
  fails 3 attempts in a row stops. A failed "another angle" capture's photos stop counting
  toward its group, so Retry checks the group again: if the group has filled up since ("This
  capture is full now — add the photos as a new angle") or already holds the same photos
  ("Those photos are already in this capture"), Retry is refused with that message (409,
  shown next to the button, also in the review queue) and the capture stays failed. As a last
  safety net the worker fails any capture whose call would send more than 12 photos.

### Several photos per capture, and "add another angle"

- **Several photos.** A photo or reconcile capture takes **1 to 6 photos** in one
  submission (the `photos` form field; the older single `photo` field still works). They are
  stored before any AI call and sent to the model together, in the order taken, as views of
  the same place: the model lists each object once, however many photos show it. Receipts
  stay one photo or PDF. PDFs are refused for photo and reconcile captures ("Use a photo;
  PDFs are for receipts"), and so is the same photo twice ("That photo is already in this
  capture").
- **Image formats.** Only what phones and browsers produce is decoded: JPEG, PNG, WebP, GIF
  and HEIC/HEIF (plus PDF for receipts). Anything else (TIFF, BMP, …), even renamed `.jpg`, is
  stored but never decoded: the capture fails with "not a readable image" (keep the original
  and send a JPEG instead). Large photos are decoded at a reduced scale for the display copy,
  which keeps the worker's memory low.
- **Add another angle.** A finished photo capture (or one of its extensions) can get more
  photos later: `POST /captures/<id>/extend` creates an *extension* capture of the group's
  first capture, at its location, queued like any capture and behind the same budget and
  AI-disabled gates. The model gets every photo of the group (old ones first) and the items
  already recorded there; it reports which ones it sees again. Items already there are
  **never changed**: they only gain boxes on the photos and a "last seen" time, and a
  higher count than recorded becomes a quantity proposal in the review queue (a lower count
  is ignored — an angle can hide things). Group items that were moved or used up since are
  sent as "not here any more" and are never added again. An object the model reports as new
  but whose name equals an item still at the location is treated as that item and flagged
  for review; one whose name equals only a moved or used-up item is added as a new item,
  flagged for review (a replacement bought after the old one was used up, say). Missing items
  are not proposed: that is what reconcile is for. "Added X new, Y already known" counts as
  known every item still at the location that the model matched, boxed or not; matches to
  moved or used-up items are not counted.
- **Limits.** At most 6 photos per submission and **12 per capture group** (photos of queued,
  processing and finished captures count; failed ones do not). Each file is limited to
  `MAX_UPLOAD_MB`; a request larger than 6 × `MAX_UPLOAD_MB` + 1 MB is refused (413) before it
  is read, and so is an upload without a Content-Length (411; browsers always send one for a
  form upload, so a reverse proxy in front must pass it on rather than re-chunk the body —
  Caddy, Traefik and nginx do by default). A body that is not a multipart file upload is
  refused (415) without being parsed. Refused uploads are logged with their file count and
  sizes.
- **Boxes.** The model also marks where each item is on each photo (numbered boxes). They are
  best effort: an invalid box is dropped without failing the capture, and an item without a
  box is still saved ("not marked"). Every photo gets an upright display copy (EXIF rotation
  applied, long edge 2000 px) that the boxes are drawn on; the original is kept untouched.
  Receipts and text captures have no boxes. Boxes are replaced **per photo**: when a later
  call (another angle, a reconcile) marks an item on a photo, its older boxes on *that*
  photo are replaced; on photos where the model did not mark it this time, its older boxes
  stay (leaving a box out does not show the item is gone from that photo).
- **Cost.** A capture with more photos costs more (roughly per photo). Settings shows the
  estimate "per 1-photo capture". Ledger rows from before photo counts were recorded count as
  no photo for text captures (left out of the per-photo average) and one photo otherwise (PDF
  receipts included: their page count was not recorded).
- **Out of scope** for now: deleting individual photos, and backfilling display copies or
  boxes for photos taken before this feature (modern browsers apply the EXIF rotation to
  those originals themselves).

#### Using it on a phone

- **Taking several photos.** The photo and reconcile forms have a photo tray. **Take photo**
  opens the camera for one photo; tap it again for the next angle (iPhones take one photo per
  camera trip). **Choose photos** picks several from the library at once. Each photo shows
  as a thumbnail with a **×** to remove it, and the count says how many more fit (6 per
  send). Nothing is uploaded until you tap **Send (N photos)**, which stays greyed out while
  the tray is empty. If the server refuses the upload (too large, a PDF, the same photo twice,
  budget reached), the message appears right under that form and the photos stay in the tray,
  so you can remove the bad one and send again. Capture needs JavaScript (it always has).
- **Android caveat.** Some Android phones close the browser tab while the camera app is open
  (low memory); the tray is then empty when you come back. Use **Choose photos** to add the
  photos you just took from the gallery instead.
- **Reading the boxes.** When a photo capture finishes, its photos appear with a numbered box
  around each item the model found, and the item list below uses the same numbers. Swipe
  sideways for the other photos. Tap an item in the list to highlight its boxes (thick dashed
  yellow outline; the other boxes fade) and scroll to the first one; tap it again to clear.
  Tap a box to highlight its item. Items listed as "not marked" were found but not boxed;
  items moved or used up since are listed with that state. The full view is the capture's
  page, `/captures/<id>` (**View** on the result, or "N photos · View" on **Recent
  captures**).
- **Add another angle.** On a finished photo capture's page (not a reconcile's), **Add another
  angle** has the same tray (it says how many photos still fit: 6 per send, or "this capture
  has room for N" when fewer than 6 of the 12 per capture are left; it updates as angles are
  added). When it is done the result
  reads "Added X new, Y already known" and the photos above, the older ones included, show the
  new boxes.
- **Elsewhere.** An item's edit page shows **Where it was seen**: the photos with that item's
  box ("Seen in Garage, 2026-09-30"); tap one to open the capture with the item highlighted.
  The review queue shows a crop of the box for items to check (tap it for the whole photo):
  a square around the box, in the photo's own proportions (nothing is stretched; a box near
  the edge is shifted inside the photo rather than cut). Photos without a recorded display
  size keep the plain padded crop. Items from a PDF receipt show no photo (there is no image
  to show), and a comparison report shows no boxes strip for PDF receipt cases.
  A comparison report shows each case's photos with one model's boxes at a time (the buttons
  above the photos switch models; the numbers match that model's item lines, missed items
  have none). Boxes are shown, not scored.

#### Location photo, item thumbnails and fixing outlines

- **Location photo.** A location's page opens with the picture of its contents: during a
  photo, reconcile or "another angle" capture the model also names the photo that shows the
  most of the place (the widest view; if it does not, the photo with the most boxes, then the
  first). The page shows that photo of the location's **latest finished photo or reconcile
  capture**, with the outlines of the items directly in the location; tap an outline to jump
  to (and flash) its row. **All photos** opens that capture. To choose a different photo, open
  a capture of the location and tap **Use as location photo** under a photo; the location
  then keeps that photo until you tap **Pick automatically** on its page. The Inbox has no
  location photo, and captures made before this feature use their photo with the most
  outlines.
- **Thumbnails.** Item rows (location page, search, home search) show a small crop of the
  item when it has an outline; tap it to open the **item photo view**: the big photo with the
  item highlighted and, when several photos show it, a strip to switch between them.
- **Fix outline.** In the item photo view, **Fix outline**, then drag a rectangle around the
  item on the photo (finger or mouse; the page does not scroll while you draw) and tap
  **Save** (Cancel or Esc to stop). The new outline replaces the item's outline on that photo
  and becomes its thumbnail. A later "another angle" or reconcile never replaces an outline
  you drew. Outlines cannot be removed, only redrawn.
- **Rename in the list.** Tap an item's name to edit it in place: Enter saves, Esc or Cancel
  keeps the old name. The full edit form is still under **⋯ → Edit**.
- **Delete.** **⋯ → Delete → Confirm delete** removes an item that was detected by mistake
  (the row shows "Deleted: … · Undo"). It is a soft delete like **Used up** (photos and
  outlines are kept, Undo restores it), but a deleted item is left out of capture galleries,
  location photos and comparison prefills, and "another angle" still tells the model not to
  add it again. The review queue's **Delete** does the same. The export marks it with
  `deleted_reason: "deleted"` (`"used_up"` for used-up items).
- **Tag section.** The location page's QR code, permanent link, downloads and label links
  are under **Tag: QR code, link and labels**, closed until you tap it.

### Using OpenRouter

[OpenRouter](https://openrouter.ai) gives one key for hundreds of models from many vendors,
so you can pick a model by price and quality instead of a fixed Claude subscription. The
direct Anthropic provider stays available.

- **Key.** Create a key at <https://openrouter.ai/settings/keys> and add credits. In `.env`
  set `AI_PROVIDER=openrouter`, `OPENROUTER_API_KEY=<key>` and a default `AI_MODEL` in
  OpenRouter's `vendor/model` form, for example `google/gemini-3.1-flash-lite` (reads
  images, supports tool calls, inexpensive). Restart the container. Without the key, or with
  an `AI_MODEL` that is not an OpenRouter id (such as the Anthropic default
  `claude-sonnet-5-5`), AI capture is disabled with a message on Settings and manual entry
  keeps working. The key only ever leaves the server in the `Authorization` header to
  openrouter.ai; it is never shown, logged or sent to the browser. Requests carry the
  `X-Title: Wherebox` header and no `HTTP-Referer`, so your hostname is not
  shared.
- **Choosing a model.** Settings → **AI model** shows the active model with its prices, and
  a search over OpenRouter's model list (fetched by the server, cached for an hour, and kept
  in `DATA_DIR/cache/openrouter_models.json` so a restart or an outage still has a list).
  By default only models that read images are listed; tick the box to see all. Each row
  shows the price per million input/output tokens, a **rough** cost per 1-photo capture
  (from the per-photo average tokens of your last 20 successful AI calls, or 1,500 in /
  400 out before the first one; more photos cost roughly proportionally more),
  and badges: **tools** (forced tool call, the most reliable), **JSON** (strict JSON
  output), **best effort** (the model is only asked for JSON in the prompt). **Use** makes
  it active for the next capture, without a restart. **Reset to .env default** goes back
  to `AI_MODEL`. A model that cannot read images may be chosen, with a warning: photo,
  receipt and reconcile captures then fail and text captures still work. Models whose
  price OpenRouter does not publish (such as `openrouter/auto`) are not offered.
- **When a model disappears** from OpenRouter, Settings warns, and captures fail with
  "Model … is no longer available on OpenRouter — pick another in Settings".
- **Real costs.** OpenRouter reports what each call actually cost (in credits, 1 credit =
  1 USD); the ledger records that amount, so Settings totals and the budget use real spend.
  `AI_INPUT_PRICE_PER_MTOK` / `AI_OUTPUT_PRICE_PER_MTOK` are not needed. If a response ever
  lacks the cost, the model's catalog prices are used instead.
- **Budget.** `MONTHLY_AI_BUDGET` works as above and needs no price variables with
  OpenRouter. "Insufficient credits" (HTTP 402) fails the capture with
  "OpenRouter: insufficient credits": top up your OpenRouter balance and press Retry.
  Rate limits (429) and provider outages (5xx) are retried automatically: a capture waits
  5 seconds (a rate limit without `Retry-After`: 30 s, then 60 s), or as long as the
  provider's `Retry-After` asks (at most 5 minutes), up to 3 attempts in all.
- **Models without forced tool calls.** Some models list tool calls (or structured output)
  in the catalog although no provider behind them accepts them right now; OpenRouter then
  answers "No endpoints found that support the provided 'tool_choice'". The app retries at
  once, in the same call and without extra cost, with the next way of asking for the answer
  (tool call → JSON schema → JSON described in the prompt; the generic "No endpoints found
  that can handle the requested parameters." is treated the same), and remembers what worked
  for that model for an hour (the log shows an INFO line), then tries its best mode again.
  This fallback is silent: such a model may run in a weaker output mode (JSON described in
  the prompt is "best effort") than its badge in the picker says, which can lower its
  results in a comparison. When none works, the call fails with "No provider for <model>
  supports structured output right now" (OpenRouter's own message is kept as the details).
- **Timeouts** (the whole call is limited to `AI_TIMEOUT_SECONDS` + 10 s per extra photo) and dropped connections
  are charged a conservative estimate, as described above, because OpenRouter may still bill
  them. A capture that times out is retried like before; a comparison call is not.
- **The model list** is refreshed at most once an hour; the page or capture that triggers a
  refresh can wait up to 10 seconds for it.

#### Comparing models on your own captures

Settings → **Model comparison** (or `/compare`) re-runs captures you already made on 2–5
OpenRouter models and ranks them against the items you say each capture really contains.
It needs `OPENROUTER_API_KEY`; `AI_PROVIDER` can stay `anthropic`, so you can try OpenRouter
models before switching.

- **Building good cases.** On **Captures**, a finished photo, receipt or text capture has
  **Use for model comparison**. The form is pre-filled with the items that capture created
  (receipts also with prices); open the original photo or read the text, then fix names and
  counts, remove wrong rows and add what was missed. If the items were edited or used up
  since, the form says so. Pick captures that are typical for you (a busy shelf, a long
  receipt, a spoken list) and a few hard ones; 10 or more cases make the ranking
  trustworthy, below 10 the report marks "best value" as indicative. Reconcile captures
  cannot be cases. A case made from a capture with extra angles belongs to the group's first
  capture and uses **all the group's photos** at the time the case is made (all sent in one
  call, with the plain shelf prompt); its pre-filled items are the whole group's. A case whose
  photo/PDF (any of its photos) was deleted is flagged "input missing" and skipped. Deleting
  a case hides it from new comparisons; old reports keep it. Each model's boxes are stored
  with its answer (shown, not scored).
- **Cost.** Every case is one call per model, charged to your OpenRouter credits and
  counted in the monthly budget (Settings shows the comparison part separately). The page
  shows a **rough** estimate (per case, the estimate for its number of photos; × models) and refuses to
  start when the month's spend plus **twice** that estimate would exceed
  `MONTHLY_AI_BUDGET`. The budget is also re-checked before every call: when it runs out,
  the comparison stops with "monthly budget reached" and the remaining calls fail.
- **While it runs.** One comparison runs at a time and can be cancelled. Captures always go
  first: the worker makes one comparison call only when no capture is waiting, so a new
  capture waits for at most one comparison call, which can take up to
  `AI_TIMEOUT_SECONDS`. A rate limit (429: the model's provider is busy, common with free or
  new models) is retried up to 4 attempts in all, waiting 30 s, 60 s, then 120 s (or longer
  when the provider's `Retry-After` asks, counted up to 10 minutes); the other calls go on
  meanwhile. Other outages are retried once, 30 seconds later (or after `Retry-After`); a
  timeout is not retried (it may have been billed); any other error fails that model's call
  for that case only, and the other models carry on.
- **Rate limits: what to expect.** Free variants (model ids ending in `:free`, marked in the
  picker) and cheap "flash" models are often rate-limited by their providers. Each throttled
  result can add up to about 3.5 minutes (30 s + 60 s + 120 s of pauses) before it succeeds
  or fails, and a provider's `Retry-After` can stretch a single pause to 10 minutes. Other
  models' calls go on meanwhile, and **Cancel** stops the run at any time (results so far
  stay in the report).
- **What the page shows.** Tapping **Start comparison** shows the run's status right below
  the button (the page scrolls to it) and locks the form ("A comparison is running…") until
  the run ends. The status updates every 2 seconds: overall calls done, the time elapsed, and
  one row per model with its calls done, how many succeeded or failed, how many are
  **waiting to retry** and its latest error in plain words, for example "Rate-limited by the
  model's provider — busy right now; retried 3 times." "Waiting to retry" means a call hit a
  rate limit or outage and is scheduled again; the row says when ("Trying again in about
  25 s", recomputed on every update). Tap **Details** under a row for OpenRouter's own
  message (the report shows it under **Details** too). **Cancel** stops the run. When it ends, the status shows the best
  model (F1 and cost per case) and **See the report**, the run appears under **Past
  comparisons** and the form unlocks, without reloading the page. Each case's item list is fixed when the
  comparison starts: editing a case meanwhile does not change how the running comparison
  scores the models, and its report keeps showing the list it used.
- **Reading the report.** Predicted items are matched to your list one-to-one by name
  (case, punctuation and simple plurals ignored; a name whose words all appear in the other
  also matches, so "Coca-Cola Zero" matches "Coca-Cola Zero 330ml can"; names of 4 letters
  or fewer must match exactly or be a whole word of the other name, so "Eggs" matches
  "Free-range eggs" and "Tape" matches "Duct tape", but "milk" never matches "silk"). **F1** combines found
  (recall) and extra (precision) items, from 0 to 1; a failed call counts as 0 and is also
  listed under **Failures**. **Qty acc.** is the share of found items with the right count;
  when the run has receipt cases, **Price acc.** is the share of found items priced within
  1 cent. The table also shows cost per case (the spend on the scored cases divided by
  their number), total cost (everything spent, including calls of a cancelled run), median
  time and **n** (cases), and marks the model that made the cases' original captures.
  Models are ranked by mean F1, ties by lower cost per case; **best value** is the cheapest
  model per case within 0.05 F1 of the best. Open a case to see your list next to each model's items, marked ✓ found,
  + extra and − missed, and which model made the original capture. **Use this model** makes
  that model active for the next capture (with `AI_PROVIDER=openrouter`).
- **Restarts.** A running comparison resumes after a restart. A crash between a call
  returning and its result being saved repeats that call (and bills it again); a call that
  keeps crashing the app is given up after 6 attempts. Timed-out calls are charged the
  estimate described above.

## Backups and restore

### What is backed up automatically

Every `BACKUP_INTERVAL_HOURS` (24), and shortly after startup when no recent backup
exists, the background worker writes `backups/latest.db`:

1. It deletes a stale `backups/latest.db.tmp` left by an interrupted run.
2. It runs `VACUUM INTO 'backups/latest.db.tmp'`. This is a read transaction, so web
   requests and captures continue meanwhile.
3. It fsyncs the file and atomically renames it over `backups/latest.db`.
4. It records the time as `last_backup_at`, which Settings shows. Settings warns when the
   last backup is older than twice the interval.

`latest.db` is therefore always a complete, consistent snapshot, even when `/data` is
copied while the app is writing. To write one now:

```sh
docker compose exec app python -m app.cli backup
```

### Back up

Copy the **whole data directory**, including `photos/` and `backups/`:

```sh
docker compose stop            # best: a quiet copy
cp -a data /path/to/backup/wherebox-data-$(date +%F)
docker compose start
```

Copying while the app runs also works. The live `inventory.db` might then be caught
mid-write, but `backups/latest.db` in the copy is always consistent (restore step 3).
`python -m app.cli export` (or `GET /api/export.json` while logged in) prints a JSON
document of all locations (with codes), items and photo paths with sha256. It is useful
as a readable extra copy and for comparing two installs, but restore uses the data
directory.

### Restore

1. Stop the app and put the copied directory in place as `./data` (or wherever the
   compose volume points).
2. Check the database:

   ```sh
   docker compose run --rm --no-deps --entrypoint python app -m app.cli check
   ```

   This runs `PRAGMA integrity_check` on `inventory.db`, prints `ok: ...` and exits 0 when
   the database is healthy. Then start the app with `docker compose up -d`.
3. If the check fails, it exits non-zero, and it names a usable `backups/latest.db` if
   there is one. With the app still stopped, repair:

   ```sh
   docker compose run --rm --no-deps --entrypoint python app -m app.cli check --repair
   ```

   `--repair` only acts when the check fails and `backups/latest.db` itself passes the
   check. It moves the damaged `inventory.db` (and its `-wal`) aside as
   `inventory.db.corrupt-<timestamp>`, deletes `-shm`, and copies `latest.db` into place.
   **Changes made after that backup are lost.** Then start the app. Without `--repair`,
   `check` never modifies anything. The manual equivalent: delete `inventory.db-wal` and
   `inventory.db-shm` first, then copy `backups/latest.db` over `inventory.db`.

   `docker compose run --entrypoint python` runs as root, so the restored file is
   root-owned until the next normal start fixes ownership. With compose `user:`, add
   `--user <uid>:<gid>`.
4. A backup from before login names has no name, so the app runs in legacy mode (password
   alone logs in, Settings warns). Set `HOUSEHOLD_USERNAME` in `.env` (recommended: it is
   re-applied on every start), or run `set-username` after restoring. With
   `HOUSEHOLD_USERNAME` set, the env value always wins on restart, and `/setup` uses it too.

`bash tests/scripts/restore_check.sh` tests this path end to end. It seeds a container,
copies its data directory, starts a second container on the copy, and checks that the
exports are identical and that `backups/latest.db` holds the same data.

## Adding many locations and printing label sheets

**Add several locations** (home page) or **Add several sub-locations** (a location page)
opens a form that takes one name per line, up to 50 at a time. The numbered-series helper
fills the list for you: name `Shelf`, from `1` to `10` gives `Shelf 1` … `Shelf 10`; type
`01` to get leading zeros. Edit the list before saving. Names that already exist in that
place, or appear twice, are skipped and listed on the result page. If any name is over 200
characters, nothing is added.

**Print labels for these** (result page), **Print labels for sub-locations** and **Add to
label sheet** (location page) open a sheet for A4 sticker paper with 21 labels (3 × 7, each
63.5 × 38.1 mm, the "L7160"/"5029" layout). Each label has the QR code, the name, the parent
path and the URL. Up to 100 labels fit in one print; they continue onto more sheets.

- Print from a computer if you can. In the print dialog choose paper size **A4**, scale
  **100% / Actual size** (not "Fit to page"), margins None or Default. On a phone: Share →
  Print → Save as PDF (or save the page as PDF), then print the PDF at 100%.
- On a part-used sheet, tap the cell to start at (or type the row and column). The cells
  before it stay blank.
- If the labels come out shifted, open **Printer offset**, print a **test sheet** (label
  outlines only), hold it against a sticker sheet up to the light, and adjust right/down in
  0.25 mm steps (±5 mm). The offset is saved in that browser; **Reset to 0** clears it.
- **Choose labels** lists every location of the sheet with a checkbox and whether its label
  was printed. Only the labels never printed are ticked at first (all of them when every
  one was printed), so after adding a few sub-locations, **Print labels for sub-locations**
  picks just the new ones. **All**, **None** and **Not printed only** change the ticks.
- After printing, tap **Mark these N as printed** (the app never marks them by itself);
  **Mark as not printed** undoes it. The location page's Tag section shows the status.
  Printing the single label (`/l/<code>/label`) does not mark it.

## Scan-to-move in a different browser

Scan-to-move ("Scan destination tag" in an item's `⋯` sheet) stores the pending move in the
**server session of the browser you started it in**. It expires after 10 minutes. The move
applies when that same browser opens the destination's `/l/<code>` URL.

Some phone QR scanners and NFC handlers open URLs in a **different browser** from the one
the household uses, or outside an **installed home-screen web app**, which has its own
cookie jar. That browser has no session cookie. It shows the login page, and after login
it redirects back to the location. The pending move is **not** visible there, because it
belongs to the other browser's session. What to do:

- Open the scanned URL in the browser where the move was started, or scan from inside that
  browser. The move then applies, with undo. If it expired, start it again.
- Or set the phone's default browser to the one the household uses for Wherebox.
- Use a normal browser tab rather than an installed home-screen app when you rely on
  scan-to-move.

## Manual verification record

### AC-15: real-key capture timing

AC-15 requires a p50 of 20 seconds or less over 5 real-key shelf-photo captures of about 12
items each, with an empty queue. The automated part (pipeline overhead under 2s with the
fake provider) is covered by `tests/integration/test_capture_photo.py`. This real-key run
is a manual verification item: it needs a real API key and real shelves, so it cannot run
in CI.

**Status: manual check — not yet recorded.**

Procedure:

1. Configure `AI_PROVIDER=anthropic`, `AI_API_KEY`, `AI_MODEL` and the prices, then
   restart. Confirm that no capture is queued or processing (the `/captures` page).
2. On a phone, over the real proxy URL, open a location page and photograph a shelf with
   about 12 distinct items. Start a stopwatch when you tap upload, and stop it when the
   status line shows the result.
3. Wait for each capture to finish before the next one. Repeat for 5 different shelves.
4. Read the server-side timings. `duration_ms` is the provider call alone;
   `total_s` runs from upload to done:

   ```sh
   docker compose exec app python -c "import sqlite3; c = sqlite3.connect('/data/inventory.db'); [print(r) for r in c.execute(\"SELECT id, duration_ms, round((julianday(finished_at) - julianday(created_at)) * 86400, 1) AS total_s, (SELECT count(*) FROM items WHERE capture_id = captures.id) AS items FROM captures WHERE type = 'photo' ORDER BY id DESC LIMIT 5\")]"
   ```

5. Fill in the table and the p50 (the 3rd value when sorted). Replace the status line
   above with the date and a pass or fail result.

| Run | Date | Model | Items found | `duration_ms` | `total_s` | Stopwatch (s) |
|---|---|---|---|---|---|---|
| 1 | not yet recorded | | | | | |
| 2 | not yet recorded | | | | | |
| 3 | not yet recorded | | | | | |
| 4 | not yet recorded | | | | | |
| 5 | not yet recorded | | | | | |
| **p50** | | | | | | |

The other manual checks from the plan are a real iPhone opening the rear camera over HTTPS
through the proxy (AC-32) and a printed QR scanned by a phone (AC-11). Record them the same
way when done.

### Multi-photo tray on real phones

**Status: manual check — not yet recorded.** The e2e tests drive the tray in Chromium and
WebKit with injected files; a real camera cannot be automated. On an **iPhone (Safari)** and
an **Android phone (Chrome)**, over the real proxy URL:

1. On a location page, tap **Take photo** twice (two camera trips) and **Choose photos** once
   with two library photos: the tray shows 4 thumbnails; remove one with ×; **Send (3
   photos)**.
2. Cancel the camera once: nothing is sent and the tray is unchanged.
3. When the capture is done, the numbered boxes sit on the right objects on every photo
   (portrait and landscape shots), tapping a list row highlights its boxes and tapping a box
   its row, and the page does not scroll sideways.
4. On the capture's page, **Add another angle** with one photo: "Added X new, Y already known".

| Phone | Browser | Date | Result |
|---|---|---|---|
| iPhone | Safari | not yet recorded | |
| Android | Chrome | not yet recorded | |
