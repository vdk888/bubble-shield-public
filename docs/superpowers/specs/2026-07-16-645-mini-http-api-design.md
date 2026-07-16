# #645 — Mini tier: live HTTP API over Tailscale (read-distribution + miss-path)

**Status:** DRAFT spec — key choices surfaced to Joris before build.
**Decision base (Joris, 2026-07-16 09:59):** option (b) live HTTP API on the mini over
Tailscale. Degraded mode: if the mini is down/unreachable, **clients keep working —
serve raw, accepted leak**. Availability wins; no refuse-mode, no blocking waits.

## Problem (from card #645)

Mini tier = Mac mini is the SOLE indexer (sweeps the shared Dropbox vault, writes
shadow.db); client Macs read masked shadows and never run models. Two breaks today:

1. **read-MISS serves RAW** (`bubble_shield_mcp.py:1464`, the B1 accepted gap) — fine
   single-Mac (local sweep catches up), but on a client Mac it means raw client PII in
   Cowork context, which the mini tier exists to prevent.
2. **`mark_pending` is client-local** — keyed by the client's local path, written to the
   client's local shield.db. The mini never sees it → the miss recurs forever.

Verified good news: `get_shadow` is keyed by `content_hash` (path-independent) → HITs
resolve from any client regardless of local Dropbox path.

## Design

### Mini side — `bubble_shield_minid.py` (new, stdlib http.server or the existing daemon pattern)

Read-only-ish HTTP API, bound to the **Tailscale interface only** (never 0.0.0.0):

| Endpoint | Method | Behavior |
|---|---|---|
| `/health` | GET | `{status, version, shadow_count, last_sweep_at}` |
| `/shadow/{content_hash}` | GET | HIT → `{clean_text}` (gazetteer exact-string net applied **server-side** — the gazetteer lives on the mini). MISS → 404 `{status:"miss"}` |
| `/index_request` | POST | body `{content_hash, rel_path}` → mini resolves `rel_path` against ITS Dropbox root, `mark_pending`s **its own** store, 202. Not-found-yet (Dropbox lag) → still 202, queued with retry (sweep re-checks). |

- No raw document content EVER travels client→mini (the mini has the file via Dropbox
  sync; the client only sends a hash + relative path). Only masked shadows travel
  mini→client. Clean story: the tailnet never carries unmasked PII.
- Singleton via port-bind (same pattern as nerd/gemmad, v1.23.23).
- launchd KeepAlive on the mini.

### Client side — `_read_with_shadow` gains a mini branch

Config: `bubble-shield.json` → `"mini_url": "http://<mini-tailscale-ip>:8377"`
(presence of the key = mini-mode; absent = current single-Mac behavior, zero change).

```
mini-mode read:
  h = content_hash(local file)
  1. local shadow.db get_shadow(h)        # free, still useful as a warm cache
  2. HIT? serve.
  3. GET {mini_url}/shadow/{h}  timeout=2s
     HIT  → put_shadow locally (cache) → serve
     MISS → POST /index_request {h, rel_path} (fire-and-forget, 1s timeout)
            → serve RAW local extract        # Joris-accepted leak, mini learns
     DOWN/timeout/any error
          → serve RAW local extract          # Joris-accepted leak
          → mark_pending locally (harmless) + log one status line
```

- Fire-and-forget on the index request — **never block a read** (explicit Joris
  constraint: "clients need to be able to work without it").
- `rel_path` = path relative to the configured protected-folder root (the shared
  Dropbox tree both machines sync). Local-only files that haven't synced yet: the
  mini 202-queues and its sweep retries until Dropbox catches up.
- Client in mini-mode runs **reader-mode**: its own sweep launchd job is not
  installed / disabled (the mini is sole writer). Ties to the known reader-mode
  flag gap (4 write paths) — those paths gate on the same `mini_url` config key.

### Security

- Bind to the Tailscale interface IP only + `Authorization: Bearer <token>` shared
  token in config (defense in depth on top of tailnet ACLs). Tailscale already gives
  WireGuard encryption + device auth; the token guards against anything else on the
  tailnet (e.g. a client's other software).
- Shadows are masked text — worst case on the wire is already-anonymised content.

### Non-goals (v1)

- No sync-wait on miss (Joris: availability over fail-closed here).
- No mini→client push/replication (the API IS the distribution).
- No multi-mini, no failover chain.
- Restores keep pointing at the shared vault read-only (prior decision).

## Test plan (maker≠checker)

1. Unit: client branch — HIT-local, HIT-remote (+local cache write), MISS→raw+index_request, DOWN→raw (mock server; each terminal state asserted).
2. Unit: minid — endpoint contracts, tailnet-bind refusal of other interfaces, token required, rel_path traversal rejection (`..`, absolute paths).
3. Live: two-machine test over the real tailnet (this Mac ↔ mini when provisioned); until the mini exists, loopback simulation with two BUBBLE_SHIELD_HOME stores.
4. PII scan on everything; synthetic docs only in tests.

## Open design choices for Joris (blocking build start)

- **Q-A port**: default 8377 ok? (avoids 8723 nerd / gemmad ports)
- **Q-B token**: auto-generate on mini first-run + operator copies to each client's
  config (one-time, printed by onboarding) — ok? (alternative: no token, pure tailnet
  trust)
- **Q-C local cache**: client caches remote HITs into its local shadow.db (faster
  repeat reads, works if mini later down) — ok? (alternative: always ask mini, no
  local copy of shadows on client Macs — stricter data-locality story for clients
  who don't want shadows persisted locally)
