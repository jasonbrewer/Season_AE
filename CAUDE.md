# Season Assistant Editor — Functional & UI Specification

**Repo:** `Season_AE`
**Platform:** macOS desktop (local FastAPI server + browser UI)
**Companion to:** DaVinci Resolve Studio

**Purpose of this document:** a complete description of what the Season Assistant Editor app does and the screens it presents. This is the *target* end-state (all features). Some of it is already built; most is planned. This document is the source of truth for behavior. Where this document and any wireframe disagree, see **Section 10 — Decisions to reconcile**; items there are NOT settled and must not be implemented until the operator has ruled on them.

---

## 1. What the app is

Season Assistant Editor automates the repetitive post-production plumbing for a video series ("Season 1 I Love Virginia"). The human does the creative and organizational thinking; the app does the mechanical work of backing up footage, getting it into DaVinci Resolve, and assembling per-segment timelines with green-screen compositing and music.

The operator's only manual creative act inside the app is **triage** — deciding which clips belong to which segment. Everything before triage (backup, detection, import) and everything after it (timelines, transcription, keying, backgrounds, music) is automated.

The app is a **companion to DaVinci Resolve Studio**, not a replacement. It drives Resolve through Resolve's official Python scripting API. Resolve Studio must be installed, running, and have the target project open for the app's Resolve features to work.

---

## 2. Core principles (non-negotiable)

- **Read/write only in Resolve — never delete.** The app must never delete or remove anything inside the Resolve project (no clips, bins, timelines, or media pool items). It only reads the project and adds to it. This is a hard safety rule and should be visible in the product's behavior and messaging.
- **Non-destructive to source footage.** The backup never moves, renames, or deletes source files, and never overwrites existing files at the backup destination.
- **Integrity-verified backup.** Every backed-up file is checksum-verified (xxHash64), not just copied.
- **The app discovers Resolve structure; it does not create it.** The operator creates and names the bins in Resolve. The app reads them. It never creates or renames bins.
- **Idempotent.** Re-running any stage on already-processed material must not duplicate work or produce duplicate clips, timelines, or placements.
- **Fail loud, fail early.** If a precondition is missing (Resolve not running, project not open, required bin absent, drive not mounted), the app stops and names the exact missing thing. It does not partially proceed.

---

## 3. Global status header

A persistent header across all screens showing the five preconditions for a run. Each is an **indicator** with a clear set/not-set state, and each is clickable to go to where it's configured.

1. **Media Drive** — the source drive holding the camera cards/footage.
2. **Media Folder** — the folder within that drive to crawl.
3. **Backup Drive** — the verified-copy destination.
4. **DaVinci Project** — connection state to Resolve and the name of the currently open project.
5. **Set Up** — sequence settings (frame rate, resolution, scaling).

**Indicator states to design:** unset / set-and-valid / set-but-unreachable (e.g. drive not mounted, Resolve not running, project not open). The unreachable state is distinct from unset — it means "you configured this, but it isn't there right now."

The header is the app's single answer to "why can't I press Start?"

---

## 4. Screen A — Backup / Source setup

**Purpose:** get footage from the camera media onto the backup drive, verified.

**Controls (per wireframe 2a):**
- **Choose Source** — picker for the media drive/folder.
- **Choose Backup** — picker for the backup destination.
- **Start Backup** — runs the verified backup.

**Progress and Information panel:** live progress bar plus a running log line (e.g. "Scanning backup drive for new clips…"). On completion it leads into the Triage screen.

**Backup engine behavior (built):**
- Straight-mirror path structure — the source's relative folder structure is reproduced under the backup root.
- xxHash64 streaming verification of every file after copy.
- `O_EXCL` on create, so an existing destination file is never overwritten. A name collision with differing content is a **conflict**, surfaced to the operator, not silently resolved.
- Per-run JSON manifest plus a human-readable log.
- Re-running is idempotent: already-verified files are skipped.

**States to design:** nothing chosen / ready to start / copying (with per-file and overall progress) / verifying / complete / conflict detected / error.

---

## 5. Detection — how "new clips" are found

After backup, the app crawls the **media folder on the backup drive**, recursively, for MXF files. A clip is "new" if it is present there and not already in the Resolve project's `ALL MEDIA` bin.

Detection is what populates the Triage screen. Its result is a count and a list. See **10.4** — the detection/import source is one of the open items.

---

## 6. Screen B — Triage / New Clips Detected

**Purpose:** the operator assigns each newly detected clip to a segment. This is the only manual decision in the pipeline.

**Header:** "NEW CLIPS DETECTED (n)" with an **Assign selected ▾** control and a **Start** button.

**Core interactions:**
- Each clip row/card shows the filename, a thumbnail, and its current assignment as a tag.
- **Unassigned** = dashed red tag/outline. **Assigned** = solid green tag naming the segment.
- **Batch assignment is a primary workflow, not a convenience.** Checkbox- or shift-select multiple clips, then one action assigns all selected clips at once — not one-by-one. Via the *Assign selected* control and via right-click → "Assign to Segment ▸" submenu.
- The submenu lists the existing segments, shows a ✓ on the current assignment when the selection shares one, and offers **New segment…**.
- Assignment is **exclusive**: each clip belongs to exactly one segment.
- **Start** is enabled only when every clip is assigned (or the operator explicitly chooses to proceed with a subset — needs a decision if we want that).

**Where segments come from:** the segment choices are **read from the Resolve project's bins** — specifically bins whose names begin with the prefix `SEG_`. On **Sync**, the app reads the project and populates the segment picker from those bins. The list is dynamic and grows over time as the operator adds `SEG_` bins in Resolve. The app never creates them.

**Layout options presented in the wireframe** (one to be chosen for the design):
- **1a** Two-column row list grouped by segment, checkbox multi-select, right-click bulk assign.
- **1b** Single-column card grid with large thumbnails; unassigned cards get a dashed red outline and an inline "Assign to… ▾" dropdown; shift-click multi-select raises a bulk-assign bar at the bottom.
- **1c** Grouped table with an **UNASSIGNED (n)** section pinned to the top, then a section per segment — so the operator immediately sees what still needs a decision.
- **1d** Empty state: "No new clips found — backup drive was scanned, nothing new since last sync."

**States to design:** no source/backup set / scanning / no new clips (1d) / clips listed with a mix of assigned and unassigned / all assigned and ready to Start / build in progress. An at-a-glance count of what's left to triage is important.

---

## 7. Screen C — Progress: the pipeline

**Purpose:** show the automated build running, per stage and per segment, and provide a way into Resolve when done.

**Layout (per wireframe 2b):** a matrix. Each **row is a pipeline stage**; each **column is a segment** (Seg 1…N). Each cell shows that stage's status for that segment via color: **green = complete**, **black/blank = not started**, **red = failed**. A **PROGRESS** heading with an overall status dot sits on top; an **Open Davinci Project** button sits at the bottom. Read-only — no delete actions, consistent with the read/write-only rule.

**Pipeline stages, in order (rows):**
1. **Clips moved to segment folders** *(see 10.1 — contested)*
2. **New sequences created** — one timeline per segment, created inside that segment's `SEG_` bin.
3. **Clips placed on sequences** — the segment's assigned clips appended to its timeline, in order.
4. **Transcriptions finished** — each assigned clip's audio transcribed in Resolve.
5. **Green screen keys applied** — a chroma key applied to the foreground clips.
6. **Keys removed** *(see 10.2 — intent unknown, do not implement)*
7. **Backgrounds placed on timelines** — a randomly chosen background placed under each clip.
8. **Music placed** — a randomly chosen music track placed.

**Clip ordering on a segment timeline:** by **filename, natural/numeric-aware sort** (default; correct for single-camera shoots where filenames increment with recording order). A settings toggle switches the sort key to **actual recording date+time** for the multi-card/multi-camera case.

**Timeline track layout (the green-screen composite):**
- **V2** — the camera clip (foreground), carrying the green-screen key.
- **V1** — the randomly chosen background, below V2 so it shows through the keyed-out green. A **different background per clip**.
- **A4** — the randomly chosen music.
- The camera clip's own audio remains on its default track (assumed A1 — see **10.3**).

**Source of backgrounds and music:** the `BACKGROUNDS` bin and the `MUSIC` bin. Selection is random per clip / per segment.

**States to design:** per-cell not started / running / complete / failed; a failed cell should be able to surface why (tooltip). Plus an overall done state that surfaces **Open Davinci Project**.

---

## 8. SET UP screen

**Purpose:** tell the app how to build sequences so timelines match the project's delivery spec.

**Settings:**
- **Sequence/timeline frame rate** (e.g. 23.98).
- **Resolution** and related sequence settings.
- **Clip scaling behavior** — e.g. "Fill to frame" so clips that don't match the timeline resolution scale to fill.
- **Clip-order toggle** (filename natural sort vs. recording date+time).
- Resolve connection env/paths, if surfaced.

These settings apply when the app creates the per-segment timelines.

**Persistence:** a single JSON settings file. **No database.**

---

## 9. The Resolve project model

One Resolve project. Under the master, the operator maintains these bins (the app reads, never creates):

- **ALL MEDIA** — every imported camera clip lives here permanently, single home. *Required.*
- **BACKGROUNDS** — compositing backgrounds. *Required.*
- **MUSIC** — music tracks, used by the Music stage.
- **`SEG_*` bins** — one per segment; each holds that segment's timeline. Discovered by the `SEG_` prefix. Grows over time.
- **STOCK IMAGES**, **FINAL ASSEMBLY** — operator-managed; the app does not touch these.

If a **required** bin (ALL MEDIA or BACKGROUNDS) is missing, the app **stops** and tells the operator exactly which bin to create. It does not create it and does not proceed.

**Segment membership is expressed by timeline placement only.** A clip belonging to Segment 3 means it appears on Segment 3's timeline. The master clip stays in `ALL MEDIA`. Nothing is duplicated into or moved between bins.

**Connection method:** Resolve's Python scripting API. The required environment variables (`RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB`, `PYTHONPATH`) are set **inside Python before importing `DaVinciResolveScript`**, so the app does not depend on the user's shell environment.

---

## 10. Decisions to reconcile — OPEN, do not implement

These five items are **not settled**. They do not block UI design. They **do** block the build stages that touch them. A recommended reading is given for each; none is authoritative until the operator confirms.

**10.1 "Clips moved to segment folders" vs. "clips live permanently in ALL MEDIA."**
A literal media-pool *move* would remove the clip from `ALL MEDIA` and conflicts with the no-deletes rule. *Recommendation:* treat this stage as "clip placed on the segment's timeline" (the timeline living in the `SEG_` bin), master clip untouched in `ALL MEDIA`. **Needs confirmation.**

**10.2 "Keys removed" stage — intent unknown.**
The pipeline applies green-screen keys (stage 5) then has a "Keys removed" stage (6). Could be (a) verification that green was successfully keyed out, (b) flattening/baking the composite and removing the keyer node, or (c) a leftover that shouldn't be there. **Do not implement until clarified.**

**10.3 Audio routing for the clip's own sound.**
Music goes on A4. The camera clip's own audio (what gets transcribed) presumably stays on its default track, A1. **Needs confirmation of the exact audio track layout.**

**10.4 Import/detection source: backup drive vs. a picked folder.**
The wireframe detects new clips by crawling the media folder on the **backup drive** (the verified copy). An earlier build step imported from a *picked source folder*. *Recommendation:* standardize on the backup drive media folder so Resolve always ingests the checksum-verified copy. **Needs confirmation.**

**10.5 Music in scope.**
MUSIC was earlier treated as operator-managed/hands-off; the wireframe makes it an automated stage (random music on A4). Currently written as an active, app-used bin. **Needs confirmation.**

---

## 11. Build slices

1. **Slice 1 — Backup engine + app skeleton.** Complete and verified (real footage, empty folder, re-run idempotency, conflict detection).
2. **Slice 2 — Resolve wiring + media import.** Connect via the Python API, verify required bins, discover `SEG_*` bins, crawl recursively for MXF, import into `ALL MEDIA`.
3. **Slice 3 — Triage UI** for clip-to-segment assignment.
4. **Slice 4 — Timeline building:** sequences, clip placement, transcription, green-screen keying, per-clip backgrounds, music.

---

## 12. Stack

- **Language/runtime:** Python.
- **Server:** FastAPI + uvicorn, local, browser-based UI.
- **Settings:** single JSON file. **No database.**
- **Hashing:** xxHash64, streaming.
- **NLE:** DaVinci Resolve Studio via official Python scripting API.

---

## 13. Glossary

- **Segment** — a unit of the show; one `SEG_` bin and one timeline in Resolve.
- **Sync** — the app reads the Resolve project and refreshes the segment picker from the current `SEG_` bins.
- **Triage** — the operator's act of assigning detected clips to segments.
- **New clips detected** — clips found on the backup media folder that are not yet in the project.
- **Conflict** — a backup destination filename that already exists with different content.
