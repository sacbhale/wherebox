# Deep Interview Spec: Household inventory webapp with AI capture

## Metadata
- Interview ID: b7e3c1a2-5d4f-4e8a-9c6b-2f1a0d9e7c31
- Rounds: 14 (plus Round 0 topology)
- Final Ambiguity Score: 16.5%
- Type: greenfield
- Generated: 2026-09-29
- Threshold: 0.2
- Threshold Source: default
- Initial Context Summarized: no
- Status: PASSED — pending approval

## Clarity Breakdown
Overall scores are the minimum across active components.

| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.85 | 0.40 | 0.34 |
| Constraint Clarity | 0.80 | 0.30 | 0.24 |
| Success Criteria | 0.85 | 0.30 | 0.255 |
| **Total Clarity** | | | **0.835** |
| **Ambiguity** | | | **0.165** |

## Topology

| Component | Status | Description | Coverage / Deferral Note |
|-----------|--------|-------------|--------------------------|
| Inventory core | active | Unified Items in a nested Location tree; search, browse, quick actions, review queue, permanent location URLs for QR/NFC | AC-1 to AC-12 |
| Item capture | active | AI-assisted entry: item/shelf photo, receipt photo/upload, chat/voice text, re-photo reconcile; pluggable AI provider with cost controls | AC-13 to AC-26 |
| Household and accounts | active | Single self-hosted household, one shared password, mobile + desktop | AC-27 to AC-33 |
| AI assistant | **deferred** | Conversational Q&A and proactive suggestions (restock, expiry alerts, shopping lists) | Deferred in Round 4 (2026-09-29): user judged search + filters sufficient for "where is X / do we have Y" in v1 |

## Goal
Build a self-hosted web app for one household that keeps an accurate inventory of both consumables (food, toiletries, supplies) and durable belongings (tools, electronics, seasonal items). Every item lives in a nested location tree (room > shelf > box). Entering data should take less effort than typing, because AI turns photos, receipts and short text or voice notes into inventory entries. Every location has a permanent URL, so a QR code or NFC tag stuck on a physical container opens that container's contents directly.

## Constraints
- **Deployment:** one Docker container on the user's home server or NAS. SQLite database plus a photos directory on a mounted volume; backing up means copying that directory.
- **Stack:** the implementer picks a simple, mainstream stack. The UI must be mobile-first and responsive, and able to open the phone's rear camera from the browser.
- **Users:** one household. No multi-tenancy, per-person accounts, roles or billing.
- **Access:** one shared household password. The app may be exposed to the internet behind the user's reverse proxy (Caddy, Traefik or Tailscale), which terminates HTTPS. The app serves plain HTTP inside the container.
- **AI:** a provider interface, with a single cloud vision-capable adapter in v1. API key and model come from env vars. The interface must allow a local model (e.g. Ollama) to be added later without changing callers.
- **AI cost:** images are downscaled before sending (long edge around 1500px). Usage is recorded per capture. An optional `MONTHLY_AI_BUDGET` env var hard-stops AI capture when reached.
- **Photo privacy:** photos are sent to the configured cloud provider (the user accepted this in exchange for accuracy). Stored photos stay local.
- **Permanent URLs:** location URLs must never change after creation, regardless of renames, moves within the tree, or restarts. The public base URL comes from config (e.g. `PUBLIC_BASE_URL`) so printed codes don't depend on the request hostname.

## Non-Goals (v1)
- Chat assistant, natural-language Q&A, recipe suggestions (AI assistant component deferred)
- Proactive notifications: low-stock alerts, expiry push alerts, generated shopping lists (an "expiring within N days" **filter** is in scope)
- Email ingestion: forwarding, IMAP polling, or pasting email text
- Per-person accounts, roles, audit of who changed what
- Multiple households / public sign-up / billing
- Native mobile apps
- Local AI model adapter (the interface is designed for it, but no adapter ships in v1)
- Writing NFC tags from within the app (the user writes the URL with any NFC app; see AC-11)
- Barcode scanning (not requested)

## Acceptance Criteria

### Inventory core
- [ ] AC-1: A single `Item` type with name, quantity, category, kind (consumable | belonging), location, and optional expiry date, purchase date, price, warranty/notes, photo reference, `confidence`, `needs_review`.
- [ ] AC-2: Locations form an arbitrary-depth tree (e.g. Garage > Shelf 2 > Blue box); locations can be created, renamed, moved under another parent, and deleted. Moving a location never creates a cycle.
- [ ] AC-3: Typing "batt" in search returns "AA batteries" in under 1s and shows its full location path.
- [ ] AC-4: Filters: location (including sub-locations), category, kind, needs-review, expiring within N days. They can be combined.
- [ ] AC-5: Browsing a location shows its direct items, its child locations, and the option to view all items in its subtree.
- [ ] AC-6: Quick actions on any item (+1, -1, used up, move) each take at most 2 taps from the item row.
- [ ] AC-7: The review queue lists AI-added low-confidence items and reconcile "missing?" proposals. Each can be resolved in one tap (keep / edit / delete or confirm removal).
- [ ] AC-8: Manual add/edit form available for every field, as a fallback to AI capture.
- [ ] **AC-9: Every location gets a permanent, unique short code at creation (e.g. `/l/7KQ2M`). The URL keeps resolving to the same location after rename, move, and app restart or upgrade. Codes are never reused, even after deletion.**
- [ ] **AC-10: Opening a location URL while logged out goes to login and then redirects back to that location. Opening it while logged in shows that location's contents with capture and reconcile buttons for that location.**
- [ ] **AC-11: The location page shows its full permanent URL (built from `PUBLIC_BASE_URL`) with a copy button, for writing to an NFC tag, and a QR code of that URL that can be downloaded or printed.**
- [ ] **AC-12: The URL of a deleted location shows a "this location was removed" page instead of an error or another location.**

### Item capture
- [ ] AC-13: Photo capture always happens in a location: the one being browsed (including one opened from a QR/NFC tag) or one picked beforehand. Resulting items are placed there automatically.
- [ ] AC-14: Each photo-captured item gets name, quantity, category, kind and confidence. Items below the confidence threshold get `needs_review = true`. Items are saved immediately ("auto-add, fix later").
- [ ] AC-15: A 12-item shelf photo produces saved items within about 20s.
- [ ] AC-16: The original photo is stored locally and linked to the items it produced.
- [ ] AC-17: Receipts can be captured by photo or uploaded as an image, PDF or screenshot. Product line items become Items with price and purchase date, placed in a system "Inbox" location and flagged for review/move. Non-product lines (tax, bags, totals) are skipped.
- [ ] AC-18: Chat text such as "bought 2 milk and a drill, drill goes in garage" gives 2x milk (placed in Inbox or the current location) and drill (placed in Garage).
- [ ] AC-19: Voice input uses browser speech-to-text to fill the same chat text box. No audio is sent to the server.
- [ ] AC-20: Reconcile: re-photographing a location auto-applies newly seen items. Recorded items not seen appear as "missing?" proposals in the review queue and are removed only after the user confirms.
- [ ] AC-21: All AI calls go through a provider interface. v1 ships one cloud adapter, configured by env vars (API key, model).
- [ ] AC-22: Images are downscaled (long edge around 1500px) before being sent to the provider.
- [ ] AC-23: Each capture records its token or cost usage. The settings page shows the current month's total.
- [ ] AC-24: With `MONTHLY_AI_BUDGET` set, AI capture is disabled once the monthly total reaches it, with a clear message. Manual entry keeps working.
- [ ] AC-25: If an AI call fails or times out, the photo or receipt is kept as a "pending" capture with a retry button. No input is lost.
- [ ] AC-26: The provider interface has a documented contract, and a test double implementing it is used by the test suite.

### Household and accounts
- [ ] AC-27: Every page and API call without a valid session redirects to the login screen (pages) or returns 401 (API).
- [ ] AC-28: The household password is set via env var or a first-run setup screen, and stored hashed.
- [ ] AC-29: Sessions last about 30 days per device.
- [ ] AC-30: Failed logins are throttled (e.g. 5 attempts per 15 min per IP).
- [ ] AC-31: Runs behind a reverse proxy that terminates HTTPS. Secure-cookie and forwarded-header handling are correct when proxied.
- [ ] AC-32: Core flows (search, quick actions, photo capture) are usable one-handed on a phone. Camera capture opens the rear camera.
- [ ] AC-33: `docker compose up` with a data volume and env vars gives a working app. Copying the data directory is a complete backup and restore.

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| "With AI" means a chat assistant | Round 4 (contrarian): would you miss chat if search were good? | No. The AI goes into capture; the assistant is deferred |
| Consumables and belongings are different things | Round 8 (ontologist): what is an entry, really? | One Item model with optional fields, in a location tree |
| A household app needs user accounts | Round 6 (simplifier): what's the simplest access that works? | One shared password |
| AI capture must be reviewed before saving | Round 5: what does a good capture look like? | Auto-add, flag low-confidence, fix later |
| Auto-add applies to removals too | Round 11: items missing from a re-photo | No. Removals need confirmation |
| Order emails need ingestion | Round 12: a mail server is heavy for self-hosting | Photo/upload only |
| AI must run locally for privacy | Round 7 | Cloud by default behind a pluggable interface |
| Location identity is just a DB row | Round 14 (user-added) | Permanent short-code URLs so physical QR/NFC tags keep working |

## Technical Context
Greenfield: `/var/ai/git-repos/inventory-app` is empty and not yet a git repo. The stack is the implementer's choice within these constraints: single container, SQLite plus a local photos directory, mobile-first web UI with camera access, and QR generation in the app. SQLite full-text search (FTS5) or plain `LIKE` meets the under-1s search target at household scale (thousands of items). Location short codes should be random (for example 5 to 6 characters of Crockford base32) rather than derived from name or path, so renames and moves can't change them. `PUBLIC_BASE_URL` must be set before any codes are printed.

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Item | core domain | name, quantity, category, kind, expiry?, purchase_date?, price?, warranty/notes?, confidence, needs_review, photo_ref? | belongs to one Location; optionally produced by one CaptureInput |
| Location | core domain | name, parent_id, **short_code (permanent, unique, never reused)**, deleted flag | tree: has many child Locations; has many Items; system "Inbox" location |
| Category | supporting | name | has many Items |
| Household | supporting (singleton) | password_hash, settings, monthly AI usage/budget | owns everything |
| CaptureInput | supporting | type (photo, receipt, text, reconcile), status (pending, done, failed), source file, target location, usage/cost | produces Items; reconcile produces "missing?" proposals |
| Receipt | supporting | file, purchase date, merchant? | a kind of CaptureInput; produces Items in Inbox |
| AIProvider | external system | adapter name, model, API key (env) | used by CaptureInput |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|-------------|-----|---------|--------|----------------|
| 1 | 7 | 7 | - | - | - |
| 2 | 9 | 2 (CaptureInput, Receipt) | 0 | 7 | 78% |
| 3–5 | 9 | 0 | 0 | 9 | 100% |
| 6 | 8 | 0 (User removed) | 0 | 8 | 100% |
| 7 | 9 | 1 (AIProvider) | 0 | 8 | 89% |
| 8 | 7 | 0 (Consumable and Belonging merged into Item) | 1 | 6 | 100% |
| 9–14 | 7 | 0 | 0 | 7 | 100% |

## Interview Transcript
<details>
<summary>Full Q&A (Round 0 + 14 rounds)</summary>

### Round 0 — Topology
**Q:** 4 components: Inventory core, Item capture, AI assistant, Household and accounts. Is that right?
**A:** Looks right.

### Round 1 — Inventory core / Goal
**Q:** What kind of things are you tracking, and what goes wrong today?
**A:** Both consumables and belongings, equally.
**Ambiguity:** 91%

### Round 2 — Item capture / Goal
**Q:** Main way items should get into the app?
**A:** All of the above (photo, receipt, chat/voice).
**Ambiguity:** 89%

### Round 3 — Household and accounts / Constraints
**Q:** Who is this for, and where does it run?
**A:** My household, self-hosted.
**Ambiguity:** 84.5%

### Round 4 — AI assistant / Criteria (contrarian)
**Q:** Would you miss a chat assistant if search were good?
**A:** Search is enough; defer the assistant.
**Ambiguity:** 75%

### Round 5 — Item capture / Criteria
**Q:** A 12-item shelf photo: what result means capture is working?
**A:** Auto-add, fix later.
**Ambiguity:** 75%

### Round 6 — Household and accounts / Criteria (simplifier)
**Q:** Simplest access setup that works?
**A:** One shared password.
**Ambiguity:** 64.5%

### Round 7 — Item capture / Constraints
**Q:** Where should the AI run?
**A:** Pluggable, cloud default.
**Ambiguity:** 61.5%

### Round 8 — Inventory core / Goal (ontologist)
**Q:** What is an inventory entry, really?
**A:** One Item plus a location tree.
**Ambiguity:** 53%

### Round 9 — Inventory core / Criteria
**Q:** How does the app learn items are used up or moved?
**A:** Quick taps plus re-photo reconcile.
**Ambiguity:** 46.5%

### Round 10 — Inventory core / Constraints
**Q:** Stack and deployment?
**A:** You pick, keep it simple (one container, SQLite plus photos).
**Ambiguity:** 37.5%

### Round 11 — Item capture / Criteria
**Q:** Reconcile doesn't see 3 recorded items. What happens?
**A:** Propose, you confirm.
**Ambiguity:** 33.7%

### Round 12 — Item capture / Goal
**Q:** Receipt and order inputs for v1?
**A:** Photo/upload only.
**Ambiguity:** 32.5%

### Round 13 — Inventory core + Household / Criteria
**Q:** Confirm the drafted search, browse and access acceptance tests.
**A:** Yes, use these.
**Ambiguity:** 25.5%

### Round 14 — Item capture / Constraints + Criteria
**Q:** Confirm the drafted capture tests and limits.
**A:** Mostly, with one addition: containers must have fixed URLs so optional QR codes or NFC tags attached to them keep working.
**Ambiguity:** 16.5%
</details>
