# Halo Project Journal — Debugging & Build History

This is a chronological account of everything found, broken, fixed, and
decided while bringing this system from a laggy prototype to a working
end-to-end fall detection + vitals monitoring pipeline. Written to be
fed into an IDE/AI assistant to help expand the README with real
implementation detail and a "lessons learned" section, not just the
final architecture.

---

## Phase 1 — STM32-to-Python Lag (the original bug)

**Symptom:** radar detection results arrived late, in bursts — nothing
for a while, then several frames' worth all at once.

**Root causes found, in order of discovery:**
1. `Bridge.notify()` calls were too frequent (every ~2.7ms at a small
   256-byte flush threshold on a 921,600 baud stream) — RPC overhead per
   call couldn't keep pace.
2. The Python-side callback did all parsing/printing inline, blocking
   the Bridge dispatch thread from receiving the next chunk.
3. Byte-by-byte `Serial1.read()` in the Arduino loop wasted cycles vs.
   block-reading everything currently available.

**Fixes applied:**
- Increased Bridge flush threshold (256 → 1024 → eventually replaced by
  a completely different design, see Phase 3).
- Decoupled Python receive from parse: `Bridge.provide()` callback now
  only enqueues raw bytes; a separate worker thread does the actual
  parsing. This pattern was reused later for the entire 4-thread Linux
  architecture.
- Arduino-side: block reads via `Serial1.readBytes()` instead of
  looping `Serial1.read()` one byte at a time.

**Diagnostic technique used:** added `millis()` timing around
`Bridge.notify()` calls, printed to USB Serial (separate from the radar
UART) whenever a call took >20ms — this is how the intermittent stalls
were actually proven to be `notify()` itself blocking, not something
Python-side.

---

## Phase 2 — Hitting a Real Architectural Wall (DMA)

Attempted to give `Serial1` a bigger RX ring buffer to survive blocking
`notify()` calls without dropping bytes:
```cpp
Serial1.setRxBufferSize(4096);
```
**Compile error:** `'class arduino::ZephyrSerial' has no member named
'setRxBufferSize'`. This board runs a Zephyr-based core; RX buffer size
is fixed by devicetree UART config, not settable from the Arduino sketch
layer. True DMA ingestion would need Zephyr driver/devicetree-level
work, out of reach for a `.ino` file. This limitation was **confirmed
real and never worked around** — it's an honest, permanent limitation of
this board at the sketch level, documented rather than papered over.

---

## Phase 3 — The Real Fix: Parse On-Device, Send Only Results

Realized the actual bottleneck was sending **raw radar bytes** through
the Bridge/RPC hop at all. Redesigned: the STM32 does full TLV parsing
itself (magic word sync, header parse, TLV loop) in fast C++, and only
sends small, already-decoded structs over the bridge — target ID/X/Y/Z
per frame, not the raw 921,600-baud firehose. This is the single biggest
architectural decision in the whole project: **the bridge carries
results, not raw telemetry.** Reduced per-frame bridge payload from
multi-KB to ~20 bytes/target.

---

## Phase 4 — Data Corruption From the Physical Link

Even after the architecture fix, garbage values kept appearing:
absurd floats like `-43537399017609727839392999746884861952.00m`.

**Diagnosis:** classic bit-flip corruption from a plain jumper-wire
connection at 921,600 baud — no shielding, easy to pick up noise. Not a
software bug. Confirmed by the pattern: garbage appeared as isolated
single-field corruption within an otherwise-valid frame (structural
framing/magic-word sync kept working), consistent with a bit error, not
systematic desync.

**Mitigation (not elimination):**
- Physical: shortened the wire, recommended twisted/shielded pair.
- Software: added per-field sanity bounds (physically-plausible ranges
  for X/Y/Z/target ID) that silently reject a corrupted single value
  without discarding the whole frame.

This same sanity-filtering pattern was later reapplied to vitals data
(Phase 11) after the same class of corruption showed up there too — it
hadn't been added there initially, which was a real gap.

---

## Phase 5 — Tracker Tuning: From "Ceiling-Mount Elderly Profile" to Reality

The original vendor config (`radarConfig[]`) was tuned for a very
different deployment than what was actually being tested:
ceiling-mounted, ~2m up, tilted down, expecting slow/near-static elderly
movement. Actual setup: **table-mounted, ~0.75m height, flat, ~0.6m
range, active/fast test movements.**

Symptoms this mismatch caused, and the fixes, found one at a time by
testing, reading TI's own tracker tuning guide sections the user
supplied, and never guessing blind on undocumented fields:

| Symptom | Cause | Fix |
|---|---|---|
| Seated target undetected | `boundaryBox` min-depth `0.5m` excluded a 0.6m seated distance with any lean | Lowered to `0.3m` |
| Track lost on any walk | `maxAcceleration 0.1` tuned for near-static elderly shuffling | Raised to `2.0` |
| New track ID every few seconds | `stateParam`'s `active2freeThre=6` (later `12`) gave ~300ms-650ms miss tolerance | Raised to `80` (~4.4s) |
| Coordinates completely wrong | `sensorPosition 2 0 15` assumed ceiling geometry | Set to `0.75 0 0` matching real table mount |

**Discipline established here and followed for the rest of the
project:** never guess a tracker parameter's meaning — get the actual
field-by-field documentation before changing it. This caught and
prevented at least one near-miss where a plausible-sounding but
undocumented change could have broken tracking silently.

---

## Phase 6 — Fall Detection Logic v1 (Threshold-Based)

Built before any ML model existed: watch each target's Z (height) over
a rolling window, flag a fall when Z drops ≥0.4m within that window and
then stays ≤0.3m (floor threshold) for a sustained number of frames —
distinguishing an actual fall from a quick crouch that recovers.

**Real limitation surfaced immediately:** at the table-mount geometry,
observed Z range during normal activity was already only ~0-0.3m —
the same range the "floor" threshold sits at. This mount cannot produce
the wide standing-to-floor Z arc a ceiling-mounted sensor would see.
Documented as an accepted limitation rather than chasing a fix that the
physical setup can't actually support.

---

## Phase 7 — TinyML vs. Current Approach (decision made, not switched)

Discussed moving inference onto the STM32 directly (TinyML/TFLite
Micro) vs. keeping it on the Linux MPU. Decided to stay with the
Linux-side PyTorch approach: the MCU is already busy with the parse
loop, real board specs for a safe TinyML port weren't independently
verified, and the Linux side has real compute headroom. TinyML flagged
as a legitimate follow-on project, not a swap-in replacement.

---

## Phase 8 — Switching to the Vitals + Tracking Binary

Flashing this binary meant abandoning several previously-answered
questions and re-establishing them against new, real vendor
documentation:
- Confirmed (TI E2E forum + product literature) that vitals genuinely
  requires this separate prebuilt binary — not obtainable from the
  standard tracking binary.
- Got the real vitals TLV struct from the user's own uploaded
  `parseTLVs.py` reference code (type `1040`, confirmed 136-byte
  length) rather than guessing — an earlier fabricated architecture doc
  had claimed type `1015` with a different, wrong field layout, which
  was caught during a structured review (see Phase 9) before it ever
  reached real code.
- Adapted the vendor's own `.cfg` file for this binary, carrying over
  the same tracker tuning validated in Phase 5.

---

## Phase 9 — Catching Fabricated/Unverified Claims in an AI-Generated Doc

A separately-produced "architecture" document (styled very differently
from the sober TI documentation used throughout this project — heavy
emoji headers, a "4-Thread Edge Architecture," specific CPU/RAM
percentage tables) was reviewed critically rather than accepted at
face value. Findings:

- **True, verified:** the vitals-binary requirement, and its bpm
  accuracy figures (±5 heart, ±2 breath) — both independently confirmed
  against real TI sources.
- **False:** a claim of "DMA UART Ingestion, zero buffer overflows" —
  contradicted directly by Phase 2's compile error. Never implemented,
  never claimed as done afterward.
- **Wrong, uncaught until reviewed:** TLV type `1015` for vitals with an
  invented field layout — corrected to `1040` with the real struct
  before any code was written against it.
- **Misleading:** described Thread 3 as performing its own micro-Doppler
  phase-shift DSP to compute heart/breath rate — that work is already
  done on the radar chip; Thread 3's real job is just consuming and
  thresholding already-computed values.
- **Unverifiable, downgraded:** a table of specific per-thread CPU/RAM
  percentages presented as "rigorously cross-referenced" — no code
  existed yet to measure, relabeled as unverified planning estimates.

This review process — treat any doc's specific factual claims as
needing independent verification, not authority from formatting or
confidence of tone — was applied consistently to every subsequent
"architecture" or "here's how it should work" document introduced later
in the project too, catching further issues each time (see Phase 13,
"emergency architecture" second review).

---

## Phase 10 — Building the Real 4-Thread Linux Application

Reused the receive/parse decoupling pattern from Phase 1: each `Bridge`
channel callback only enqueues raw bytes; four dedicated worker threads
handle Targets/Fall (Thread 1), Point-cloud/Classifier (Thread 2),
Vitals (Thread 3), and Event routing (Thread 4). Thread 4 initially used
a minimal honest placeholder (JSON-lines log + a barebones stdlib HTTP
endpoint) rather than assuming FastAPI/MQTT existed — those were only
built later, deliberately, once actually needed (Phase 14).

---

## Phase 11 — First Real-Hardware Bugs (from actual test runs, not review)

Running the built system against real hardware surfaced bugs no amount
of code review had caught:

1. **`No module named 'torch'`** — environment gap, not code; resolved
   by installing dependencies properly (see Phase 14 for the
   `requirements.txt` bug that also affected this).
2. **Vitals corruption with no filtering** — `Breath Dev:
   2052689190705117215236030464.000`. The Phase 4 sanity-filter pattern
   had never been extended to vitals. Fixed on both STM32 and Python
   sides.
3. **Degenerate `Y:0.00 Z:0.00` target records** — recurring pattern,
   cause not confirmed against documentation, filtered out defensively
   regardless since it carries no reliable signal either way.
4. **`'collections.OrderedDict' object has no attribute 'eval'`** — the
   model had been correctly saved as a `state_dict`, but the loading
   code treated it as a full model object. Real bug, introduced by an
   incomplete earlier fix, caught only once real inference was
   attempted. Fixed by defining `MyCNN`'s class identically in both the
   training notebook and the deployed app, then `load_state_dict()`-ing
   into an instance of it.

---

## Phase 12 — dtype Migration (float64 → float32)

Deliberate change, not a bug: the notebook originally used
`dtype=torch.double` throughout. Changed to `float32` — no accuracy
benefit from double precision at this model's size, and float32 is the
correct starting point for any future TinyML/int8 quantization path.
**Required syncing in two places that must always agree:** the
notebook's `MyCNN` definition and the deployed app's copy of the same
class — a mismatch here reproduces the exact same class of load-time
crash as Phase 11, item 4.

---

## Phase 13 — The Point Cloud Mystery (biggest single debugging arc)

**Symptom:** `radar_pointcloud` channel delivered zero data — confirmed
by an added `"First pointcloud batch received"` log line that never
printed.

**Investigation:** rather than guess again, added a debug logger to the
STM32 sketch that printed every *distinct* TLV type seen, once each.
First attempt returned a long list of obvious garbage (`4294967295`,
`1879048195`, etc) — diagnosed as a single UART-bit-flip-corrupted frame
producing nonsense header values for one frame before the parser
naturally resynced on the next magic word (same corruption class as
Phase 4, now shown to also occasionally corrupt frame/TLV headers, not
just payload values).

**Second, cleaner debug run** (after physical wire improvements from
Phase 4) surfaced the real signal: `1020, length: 60`. Type `1020` is
`MMWDEMO_OUTPUT_MSG_COMPRESSED_POINTS` — a **compressed** point-cloud
format, completely different from the raw `x,y,z,doppler` float struct
that had been assumed (type `1`, sourced from the *other* demo's
reference code, which didn't apply to this vitals-combined binary).

**Real fix:** pulled `parseCompressedSphericalPointCloudTLV` from the
user's own reference `parseTLVs.py`, implemented the real compressed
struct (5-float unit header, then 8-byte-per-point records needing
decompression via those units, then spherical-to-cartesian conversion)
in C++ on the STM32 side. This is the deepest "verify before coding"
example in the whole project — an earlier guess (type `1`) had already
been written, tested, and shipped before hard evidence proved it wrong.

---

## Phase 14 — Second Architecture Doc Review, and Building What Was Actually Missing

A second version of the "emergency architecture" doc was reviewed with
the same rigor as Phase 9, confirming it was now accurate on the parts
that had been fixed (triple TLV parsing, bounding-box pruning) while
still correctly flagging DMA as unimplemented — nothing new fabricated
this round, a good sign the correction process was working.

This prompted actually building the previously-deferred pieces:
**on-device bounding-box pruning for the point cloud stream** (drop
out-of-zone points before they cross the bridge, extending the same
bandwidth-discipline established in Phase 3) and the **real Thread 4**
(MQTT + webhook + notification, replacing the earlier honest
placeholder).

---

## Phase 15 — Notebook Bugs Found During "How Do I Train This"

Reading the notebook in detail (not just trusting it worked because it
was uploaded) surfaced three real, separate bugs:

1. **No `torch.save()` anywhere** — every training run only ever
   produced a model in memory. Added the missing save step.
2. **`fc1`'s input size hardcoded** to the notebook's own example
   dataset shape (`32*5*1=160`, derived from `max_detobj=21`). Not
   portable to different training data. Derived the general formula
   from the conv/pool layer arithmetic so it can be recomputed for any
   dataset: `H1=floor((H_in-1)/2)+1, H2=floor(H1/2), fc1_in=32*H2`.
3. **Inconsistent point-padding width** between training (`.max()`) and
   single-file scoring (`.min()`) — these will differ for almost any
   real dataset. Fixed to use the stored `max_detobj` consistently in
   both places.

The user's own training run confirmed `max_detobj=21` (22 points/frame
after padding), which happened to already match the deployed app's
hardcoded value — a lucky coincidence, not something that should be
assumed for any future retrain.

---

## Phase 16 — MQTT + Home Assistant Integration (five distinct bugs, all found via live testing)

1. **`requirements.txt` scoping bug**: `--index-url
   https://download.pytorch.org/whl/cpu` silently replaced the default
   package registry for the *entire file*, not just `torch` — this is
   why `paho-mqtt` failed to resolve despite being spelled correctly.
   Fixed: `--extra-index-url` (adds, doesn't replace).
2. **Docker loopback trap**: `MQTT_BROKER_IP = "127.0.0.1"` failed with
   `Connection refused` because the Python app runs in its own Docker
   container, separate from `mosquitto`'s container — loopback means
   "this container," not the host or a sibling container. Fixed by
   finding the host's real LAN IP via `hostname -I`.
3. **The actual "HA not updating" root cause**: heart rate, breath rate,
   and fall probability were only ever published on rare
   alert-threshold events, never as a continuous stream — the
   dashboard entities had genuinely never received a single payload in
   the shape they expected. Fixed by pushing every valid reading, not
   just alerts, and giving continuous data its own bare-numeric MQTT
   topics separate from the JSON-blob alert topics.
4. **HA sensor config mismatch**: even after the above fix, the
   existing `configuration.yaml` still pointed `state_topic` at the old
   topic with a `value_template` expecting JSON — now receiving a bare
   number instead, causing "non-numeric" errors via a different path
   than before. Fixed by updating `state_topic` to the new topics and
   removing the now-unnecessary `value_template`.
5. **paho-mqtt callback API deprecation** persisted even after
   specifying `CallbackAPIVersion.VERSION1` — the installed version
   deprecated that too. Fixed with `VERSION2`.

---

## Phase 17 — Emergency Notifications Didn't Exist

The webhook had a real destination the whole time, but **no Home
Assistant automation existed to receive it** — confirmed by an empty
"Start automating" screen in the HA UI. Built a real automation:
webhook trigger, branching by event type (`fall_cnn` vs `vitals_alert`),
`persistent_notification.create` for the in-app bell icon. A follow-up
emoji-encoding bug (`⚠️` corrupted to `??`, likely a `nano`
locale/encoding issue during copy-paste onto the device) was fixed by
just dropping the emoji rather than debugging the encoding chain
further. Push-to-phone was added via `ntfy.sh` (chosen deliberately over
the official HA app's push, which now requires a paid Nabu Casa
subscription) — a `rest_command` POSTing to a randomly-generated,
effectively-secret topic name.

---

## Phase 18 — Git and Filesystem Cleanup

A remote push restructured the repository layout (moved `config/`,
`mosquitto/`, `docker-compose.yml` into a `homeassistant/` subfolder)
while the locally-running Home Assistant container still had the
old-path config files open, so git could not fully unlink them during
the merge (`Permission denied`) — leaving stale duplicate copies
alongside the new tracked structure. Root cause traced further: Home
Assistant's Docker container writes its own config/runtime files as
`root`, while git commands run as the regular `arduino` user.

Resolved carefully rather than force-deleting anything blind:
`diff`-checked both duplicate copies before removing either, used
`cp -a` to bring the live runtime state (database, logs, `.storage`)
into the properly-tracked folder before deleting the untracked
original, used `sudo -E` (preserving user identity/credentials) for git
operations that specifically needed root's file permissions, followed
by `chown -R` back to the regular user so normal git commands worked
again afterward. A later real merge conflict (two independently-made
changes to the same `configuration.yaml`) was resolved by inspecting
the actual conflict markers rather than guessing which side to keep.

Added `.gitignore` entries inside `config/` and `mosquitto/` to exclude
runtime state (`.storage/`, `*.db`, logs, `.cache/`) from ever being
committed — important since this repo is public and `.storage/`
contains real auth tokens.

---

## Summary: What Changed Between "It Compiles" and "It Actually Works"

Almost none of the real bugs in this project were caught by reading code
— they were caught by running it against real hardware and treating
every unexpected log line as a lead to chase, not noise to ignore. The
recurring pattern worth calling out explicitly: **guessing a struct
layout, TLV type, or tuning parameter without a sourced reference
document consistently produced bugs that only surfaced under real
testing** — whereas every time a claim was checked against the user's
own reference code, a TI forum thread, or actual product literature
before writing code against it, that part worked correctly the first
time. The one deliberate exception (Phase 13's initial TLV type 1
assumption) is the clearest illustration of why that discipline matters
even when a "close enough" reference exists.
