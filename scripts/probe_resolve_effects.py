#!/usr/bin/env python3
"""Read-only diagnostic probe of Resolve's Timeline and TimelineItem objects.

Run this on the Mac with Resolve Studio already open and the project loaded,
then paste the whole output back. It gathers facts about how the scripting API
exposes OpenFX / ResolveFX effects. It implements no features.

    python3 scripts/probe_resolve_effects.py

THIS PROBE MUST NOT MODIFY THE RESOLVE PROJECT. It calls no ``Set*``, no
``Add*``, no ``Create*``, no ``Delete*``, and nothing else that mutates state.

``SetCurrentTimeline`` IS A WRITE and is never called. The target timeline is
found by iterating ``GetTimelineByIndex`` over ``GetTimelineCount`` and reading
``GetName`` — the active timeline is left exactly as it was.

The one loop that calls methods discovered at runtime (section 5) is fenced by
:func:`is_safe_reader`: a name is invoked only if it starts with ``Get``, and
never if it starts with any mutating prefix. Nothing else in this file calls a
method it did not name literally, and every literal name here begins with
``Get``. Where a value could only be discovered by writing, this script reports
UNKNOWN and says which write would have been required.

Two rules govern every line of output:

* Every return value is checked. This API signals failure by returning ``None``
  or ``False`` — no exception, no message, nothing in any log.
* Nothing is printed that did not come back from the API. Gaps print as
  ``UNKNOWN — <reason>``, never as a plausible-looking method name. A
  wrong-but-plausible name is worse than no answer: it fails silently later.

The connection is not reimplemented here. It is imported wholesale from
``probe_resolve_render.py``, which must sit beside this file, so there is
exactly one connection path in the repository.

Standard library only. Deliberately standalone: it does not import from
``src/footage_pipeline/``, and nothing in ``src/`` imports it.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# The sibling probe owns the connection and the output helpers. Importing it
# executes no work — everything there is behind ``if __name__ == "__main__"``.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from probe_resolve_render import (
        RESOLVE_SCRIPT_API,
        api_call,
        connect,
        grep_lines,
        heading,
        print_mapping,
        print_names,
        read_api_readme,
        sub,
        unknown,
    )
except ImportError as exc:
    print(f"Could not import probe_resolve_render: {exc}")
    print(
        "probe_resolve_render.py must sit in the same directory as this file — "
        "it owns the single connection path. Run both from a full checkout of "
        "the repository rather than copying one script on its own."
    )
    raise SystemExit(1)


#: The bin the target timeline is named after or filed under.
TARGET_BIN = "Segment 1"
#: Words that make a method name worth calling in section 5.
EFFECT_WORDS = (
    "fusion",
    "ofx",
    "effect",
    "plugin",
    "filter",
    "preset",
    "version",
    "take",
    "property",
    "node",
)
#: Words used for the Q1 name sweep.
Q1_WORDS = ("ofx", "effect", "plugin", "filter", "fusion", "preset")
#: A method whose name starts with any of these is never called by this probe.
MUTATING_PREFIXES = (
    "Set",
    "Add",
    "Create",
    "Delete",
    "Import",
    "Export",
    "Append",
    "Insert",
    "Remove",
    "Link",
    "Unlink",
    "Load",
    "Save",
    "Start",
    "Stop",
    "Apply",
    "Clear",
    "Update",
    "Refresh",
)
#: How many items to scan when looking for one that carries an effect. A cap so
#: a feature-length timeline cannot make the probe crawl; reported when hit.
ITEM_SCAN_CAP = 200


# --------------------------------------------------------------------------
# The only gate through which a runtime-discovered method may be called
# --------------------------------------------------------------------------


def is_public(name: str) -> bool:
    """True for real API names. Dunders and privates are attributes, not API."""
    return not name.startswith("_")


def is_safe_reader(name: str) -> bool:
    """True only for zero-risk reader names: ``Get*`` and nothing mutating."""
    if not name.startswith("Get"):
        return False
    if name.startswith(MUTATING_PREFIXES):
        return False
    return True


def matches_effect_word(name: str, words: tuple[str, ...] = EFFECT_WORDS) -> bool:
    """True when a public API name mentions one of the given words."""
    return is_public(name) and any(word in name.lower() for word in words)


def call_reader(obj: Any, name: str) -> tuple[str, Any]:
    """Call a zero-argument reader, classifying every outcome.

    Returns ``(outcome, value)`` where outcome is one of ``"value"``,
    ``"needs-args"``, ``"none"``, ``"false"`` or ``"raised"``. Arguments are
    never guessed: a method that wants them is reported and skipped.
    """
    if not is_safe_reader(name):
        return "blocked", None
    method = getattr(obj, name, None)
    if method is None:
        return "missing", None
    try:
        value = method()
    except TypeError as exc:
        return "needs-args", exc
    except Exception as exc:
        return "raised", exc
    if value is None:
        return "none", None
    if value is False:
        return "false", None
    return "value", value


def print_reader_result(name: str, outcome: str, value: Any) -> None:
    if outcome == "value":
        print(f"  {name}() -> {value!r}")
    elif outcome == "needs-args":
        print(
            f"  {name}() -> SKIPPED — it requires arguments "
            f"(TypeError: {value}). No argument was guessed."
        )
    elif outcome == "none":
        print(f"  {name}() -> None (silent failure, or nothing applies here)")
    elif outcome == "false":
        print(f"  {name}() -> False (silent failure, or nothing applies here)")
    elif outcome == "raised":
        print(f"  {name}() -> raised {type(value).__name__}: {value}")
    elif outcome == "missing":
        print(f"  {name}() -> UNKNOWN — the attribute vanished between dir() and the call")
    else:
        print(f"  {name}() -> SKIPPED — not a Get* reader, so it was never called")


# --------------------------------------------------------------------------
# Read-only lookups
# --------------------------------------------------------------------------


def normalise(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def find_folder(folder: Any, wanted: str, path: str = "") -> tuple[Any, str]:
    """Depth-first search for a bin whose name matches ``wanted``."""
    if folder is None:
        return None, ""
    name = folder.GetName() if hasattr(folder, "GetName") else None
    here = f"{path}/{name}" if path else str(name)
    if name is not None and normalise(name) == normalise(wanted):
        return folder, here
    for child in folder.GetSubFolderList() or []:
        found, found_at = find_folder(child, wanted, here)
        if found is not None:
            return found, found_at
    return None, ""


def video_item_count(timeline: Any) -> tuple[int | None, str]:
    """Total items across every video track. Returns (count, note)."""
    track_count = timeline.GetTrackCount("video")
    if not isinstance(track_count, int):
        return None, f"GetTrackCount('video') returned {track_count!r}"
    total = 0
    for track in range(1, track_count + 1):
        items = timeline.GetItemListInTrack("video", track)
        if items:
            total += len(items)
    return total, ""


def effect_reader_names(item: Any) -> list[str]:
    """Zero-argument-looking Get* readers on an item whose names sound effecty."""
    return [
        name
        for name in sorted(dir(item))
        if is_safe_reader(name) and matches_effect_word(name)
    ]


def score_item(item: Any, readers: list[str]) -> tuple[int, list[tuple[str, Any]]]:
    """How many effect-ish readers return something non-empty for this item."""
    hits: list[tuple[str, Any]] = []
    for name in readers:
        outcome, value = call_reader(item, name)
        if outcome == "value" and value not in ("", [], {}, 0):
            hits.append((name, value))
    return len(hits), hits


# --------------------------------------------------------------------------
# README section parsing for Q6
# --------------------------------------------------------------------------


def top_level_headers(lines: list[str]) -> list[tuple[int, str]]:
    """Lines that look like an object heading: a bare word at column zero."""
    found = []
    for number, line in enumerate(lines, 1):
        if not line or line[:1].isspace():
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", line.strip()):
            found.append((number, line.strip()))
    return found


def find_section(lines: list[str], name: str) -> tuple[int | None, int | None, str]:
    """Locate the README section for ``name``. Returns (start, end, note).

    ``start``/``end`` are 1-based inclusive line numbers. The best candidate is
    the heading followed by the most signature-looking indented lines, so a
    table-of-contents entry does not win over the real section.
    """
    headers = top_level_headers(lines)
    candidates = [number for number, text in headers if text == name]
    if not candidates:
        return None, None, f"no bare '{name}' heading at column zero was found"

    def signature_score(start: int) -> int:
        window = lines[start : start + 12]
        return sum(
            1 for line in window if line[:1].isspace() and "(" in line
        )

    best = max(candidates, key=signature_score)
    note = ""
    if len(candidates) > 1:
        note = (
            f"{len(candidates)} lines read as a '{name}' heading "
            f"({', '.join(str(c) for c in candidates)}); chose line {best}, the "
            "one followed by the most method signatures"
        )

    end = None
    for number, _text in headers:
        if number > best:
            end = number - 1
            break
    if end is None:
        end = len(lines)
        note = (note + "; " if note else "") + (
            "no following heading was found, so the section is echoed to the "
            "end of the file rather than truncated"
        )
    return best, end, note


def print_readme_signature(name: str, lines: list[str]) -> None:
    """Quote the README's own line(s) for a method, so its signature is exact.

    Nothing is inferred: if the README does not mention the name, that is what
    is printed.
    """
    if not lines:
        print("      (no README available, so no signature can be quoted)")
        return
    hits = grep_lines(lines, re.escape(name))
    if not hits:
        print(f"      (the README never mentions {name}, so its signature is UNKNOWN)")
        return
    for number, line in hits:
        print(f"      README {number:>5} | {line}")


def echo_lines(lines: list[str], start: int, end: int) -> None:
    for number in range(start, end + 1):
        print(f"  {number:>5} | {lines[number - 1]}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    resolve = connect()

    project_manager = api_call(
        "GetProjectManager()", resolve, "GetProjectManager", show=False
    )
    project = api_call(
        "GetCurrentProject()", project_manager, "GetCurrentProject", show=False
    )
    if project is None:
        print(
            "No current project. Resolve returns None here when no project is "
            "open — open the project, then re-run. Everything below needs it."
        )
        return 1
    api_call("project.GetName()", project, "GetName")

    # ----------------------------------------------------------------- 1
    heading("1. EVERY TIMELINE IN THE PROJECT")
    print(
        "Found by index only. SetCurrentTimeline is a write and is never "
        "called: the active timeline is left exactly as it was."
    )
    timeline_count = api_call("GetTimelineCount()", project, "GetTimelineCount")
    timelines: list[tuple[int, str, Any]] = []
    if not isinstance(timeline_count, int):
        unknown(
            "Timeline list",
            "GetTimelineCount() did not return an integer, so no timeline could "
            "be reached by index",
        )
    elif timeline_count == 0:
        print("The project reports zero timelines.")
    else:
        for index in range(1, timeline_count + 1):
            timeline = api_call(
                f"GetTimelineByIndex({index})",
                project,
                "GetTimelineByIndex",
                index,
                show=False,
            )
            if timeline is None:
                continue
            name = timeline.GetName()
            video_tracks = timeline.GetTrackCount("video")
            audio_tracks = timeline.GetTrackCount("audio")
            clips, note = video_item_count(timeline)
            print(
                f"  [{index}] name={name!r} "
                f"video_tracks={video_tracks!r} audio_tracks={audio_tracks!r} "
                f"video_clips={clips if clips is not None else 'UNKNOWN'}"
                + (f" ({note})" if note else "")
            )
            timelines.append((index, str(name), timeline))

    # ------------------------------------------------- locate the target
    heading(f"TARGET SELECTION — a timeline named or filed under {TARGET_BIN!r}")
    media_pool = api_call("project.GetMediaPool()", project, "GetMediaPool", show=False)
    root_folder = api_call(
        "mediaPool.GetRootFolder()", media_pool, "GetRootFolder", show=False
    )
    bin_folder, bin_path = find_folder(root_folder, TARGET_BIN)
    bin_clip_names: list[str] = []
    if bin_folder is None:
        print(f"No bin named {TARGET_BIN!r} was found anywhere in the media pool.")
    else:
        print(f"Bin found at: {bin_path!r}")
        clips = bin_folder.GetClipList()
        if not clips:
            print("  The bin reports no clips.")
        else:
            print(f"  {len(clips)} item(s) in the bin:")
            for clip in clips:
                clip_name = clip.GetName()
                bin_clip_names.append(str(clip_name))
                print(f"    {clip_name!r}")

    wanted = {normalise(name) for name in bin_clip_names}
    candidates = [
        entry
        for entry in timelines
        if normalise(TARGET_BIN) in normalise(entry[1]) or normalise(entry[1]) in wanted
    ]
    if candidates:
        print(f"\nTimelines matching the bin: {[entry[1] for entry in candidates]}")
    else:
        print(
            f"\nNo timeline name contains {TARGET_BIN!r} or matches a clip in "
            "that bin."
        )

    target: Any = None
    target_name = ""
    for _index, name, timeline in candidates:
        clips, _note = video_item_count(timeline)
        if clips:
            target, target_name = timeline, name
            print(f"Target: {name!r} — the first matching timeline that has clips.")
            break
    if target is None:
        for _index, name, timeline in timelines:
            clips, _note = video_item_count(timeline)
            if clips:
                target, target_name = timeline, name
                print(
                    f"FALLBACK target: {name!r} — no timeline matched the bin, so "
                    "this is simply the first timeline in the project that has "
                    "any clips. Read the results with that in mind."
                )
                break
    if target is None:
        print(
            "\nNo timeline in this project has any clips on a video track. "
            "Section 1 above lists every timeline the project reports, with its "
            "clip count. Sections 2-6 and Q1-Q5 need a clip and are skipped; "
            "Q6 and Q7 read the README and still run."
        )

    # ----------------------------------------------------------------- 2
    heading("2. sorted(dir(timeline)) FOR THE TARGET TIMELINE — COMPLETE")
    timeline_names: list[str] = []
    if target is None:
        unknown("dir(timeline)", "no target timeline was found")
    else:
        print(f"Target timeline: {target_name!r}")
        timeline_names = sorted(dir(target))
        print_names("dir(timeline)", timeline_names)

    # ----------------------------------------------------------------- 3
    heading("3. GetItemListInTrack('video', 1) AND ('video', 2)")
    tracks: dict[int, list[Any]] = {}
    if target is None:
        unknown("GetItemListInTrack", "no target timeline was found")
    else:
        for track in (1, 2):
            items = api_call(
                f"GetItemListInTrack('video', {track})",
                target,
                "GetItemListInTrack",
                "video",
                track,
                show=False,
            )
            if items is None:
                continue
            tracks[track] = list(items)
            print(f"GetItemListInTrack('video', {track}): {len(items)} item(s)")
            for position, item in enumerate(items):
                item_name = item.GetName()
                if item_name is None:
                    print(f"  [{position}] UNKNOWN — GetName() gave nothing back")
                else:
                    print(f"  [{position}] {item_name!r}")

    # --------------------------------------------- pick an item with an effect
    heading("4. sorted(dir(timelineItem)) FOR ONE ITEM WITH AN EFFECT — COMPLETE")
    all_items = [item for track in sorted(tracks) for item in tracks[track]]
    sample: Any = None
    sample_hits: list[tuple[str, Any]] = []
    readers: list[str] = []
    if not all_items:
        unknown(
            "dir(timelineItem)",
            "video tracks 1 and 2 reported no items, so there is no "
            "TimelineItem to inspect",
        )
    else:
        readers = effect_reader_names(all_items[0])
        print(
            "Effect-ish zero-argument readers discovered on TimelineItem, used "
            f"to find an item carrying an effect: {readers or '(none)'}"
        )
        scanned = all_items[:ITEM_SCAN_CAP]
        if len(all_items) > len(scanned):
            print(
                f"Scanning the first {ITEM_SCAN_CAP} of {len(all_items)} items "
                "(cap, so a long timeline cannot stall the probe)."
            )
        best_score = 0
        for item in scanned:
            score, hits = score_item(item, readers)
            if score > best_score:
                sample, sample_hits, best_score = item, hits, score
        if sample is None:
            sample = all_items[0]
            print(
                "NOTE: no item returned anything from an effect-ish reader, so "
                "no item could be shown to carry an effect. Falling back to the "
                "first item on the earliest track. If the keyer is on a clip "
                "here, then the API does not expose it through any Get* reader "
                "whose name mentions "
                f"{', '.join(EFFECT_WORDS)} — which is itself the finding."
            )
        else:
            print(
                f"Chose an item on which {best_score} effect-ish reader(s) "
                "returned something:"
            )
            for name, value in sample_hits:
                print(f"  {name}() -> {value!r}")
        api_call("sample item GetName()", sample, "GetName")
        print_names("dir(timelineItem)", sorted(dir(sample)))

    # ----------------------------------------------------------------- 5
    heading("5. EVERY EFFECT-RELATED ZERO-ARGUMENT Get* READER ON THAT ITEM")
    print(
        "Only names starting with Get are called, and only with no arguments. "
        "A method that wants arguments is reported and skipped — no argument is "
        "ever guessed."
    )
    if sample is None:
        unknown("effect readers", "no TimelineItem was available")
    else:
        matched = [
            name
            for name in sorted(dir(sample))
            if is_safe_reader(name) and matches_effect_word(name)
        ]
        print(f"Matched on {', '.join(EFFECT_WORDS)}: {len(matched)} method(s)")
        if not matched:
            print("  (none — no Get* name on this object contains any of those words)")
        for name in matched:
            outcome, value = call_reader(sample, name)
            print_reader_result(name, outcome, value)

        skipped = [
            name
            for name in sorted(dir(sample))
            if not name.startswith("Get") and matches_effect_word(name)
        ]
        if skipped:
            print()
            print(
                "Effect-related names that are NOT Get* readers. These were "
                "NEVER CALLED — they are listed so their exact spelling is on "
                "the record:"
            )
            for name in skipped:
                print(f"  {name}")

    # ----------------------------------------------------------------- 6
    heading("6. GetProperty() WITH NO ARGUMENT, FOR THAT ITEM")
    if sample is None:
        unknown("GetProperty()", "no TimelineItem was available")
    elif not hasattr(sample, "GetProperty"):
        unknown("GetProperty()", "the method is not present on this TimelineItem")
    else:
        outcome, value = call_reader(sample, "GetProperty")
        if outcome == "value" and isinstance(value, dict):
            print_mapping("GetProperty()", value)
        else:
            print_reader_result("GetProperty", outcome, value)

    # =================================================================
    heading("ANSWERS")

    # Read once here: Q2, Q4 and Q5 quote the README's signature for every
    # method they name, and Q6/Q7 echo it wholesale.
    readme_path, readme_lines = read_api_readme()

    listings: dict[str, list[str]] = {}
    if timeline_names:
        listings["timeline"] = timeline_names
    if sample is not None:
        listings["timelineItem"] = sorted(dir(sample))

    # ----------------------------------------------------------------- Q1
    sub("Q1. Every method containing ofx/effect/plugin/filter/fusion/preset")
    if not listings:
        unknown("Q1", "neither dir() listing could be collected")
    else:
        missing = [n for n in ("timeline", "timelineItem") if n not in listings]
        if missing:
            print(f"NOT searched (unavailable): {', '.join(missing)}")
        total = 0
        for listing_name, names in listings.items():
            hits = [
                name
                for name in names
                if matches_effect_word(name, Q1_WORDS)
            ]
            print(f"{listing_name}: {len(hits)} hit(s)")
            for name in hits:
                matched_words = [w for w in Q1_WORDS if w in name.lower()]
                print(f"  {listing_name}.{name}   [matched: {', '.join(matched_words)}]")
            total += len(hits)
        if total == 0:
            print("No method name in either listing contains any of those words.")

    # ----------------------------------------------------------------- Q2
    sub("Q2. Any method that ADDS an OpenFX / ResolveFX effect to an item?")
    print("Names only — nothing here was called; calling one would be a write.")
    if not listings:
        unknown("Q2", "neither dir() listing could be collected")
    else:
        adders = {
            listing_name: [
                name
                for name in names
                if re.search(r"(?i)(add|apply|insert|create|new)", name)
                and matches_effect_word(name, Q1_WORDS)
            ]
            for listing_name, names in listings.items()
        }
        found = False
        for listing_name, names in adders.items():
            for name in names:
                print(f"  {listing_name}.{name}")
                print_readme_signature(name, readme_lines)
                found = True
        if not found:
            unknown(
                "Q2",
                "no name in either listing combines an add/apply/insert/create "
                "verb with an effect word. Nothing is named speculatively; read "
                "the complete listings in sections 2 and 4, and the README "
                "sections in Q6, to confirm",
            )

    # ----------------------------------------------------------------- Q3
    sub("Q3. Any method that READS the effects applied to an item?")
    if sample is None or "timelineItem" not in listings:
        unknown("Q3", "no TimelineItem was available")
    else:
        candidates_q3 = [
            name
            for name in listings["timelineItem"]
            if is_safe_reader(name)
            and matches_effect_word(name, ("ofx", "effect", "plugin", "filter", "fusion"))
        ]
        if not candidates_q3:
            unknown(
                "Q3",
                "no Get* name on TimelineItem contains ofx, effect, plugin, "
                "filter or fusion",
            )
        else:
            print(
                "Candidate readers, each called with no arguments against the "
                "target item (which you say already carries a keyer):"
            )
            for name in candidates_q3:
                outcome, value = call_reader(sample, name)
                print_reader_result(name, outcome, value)
            print(
                "\nRead the values above literally. A reader that returned None "
                "or an empty list did not report the keyer — that is a finding "
                "about the API, not a guess about the clip."
            )

    # ----------------------------------------------------------------- Q4
    sub("Q4. Any method that SETS a parameter on an applied effect?")
    print("Names only — nothing here was called; calling one would be a write.")
    if not listings:
        unknown("Q4", "neither dir() listing could be collected")
    else:
        setters_found = False
        for listing_name, names in listings.items():
            for name in names:
                if name.lower().startswith("set") and matches_effect_word(
                    name,
                    ("ofx", "effect", "plugin", "filter", "fusion", "param", "property"),
                ):
                    print(f"  {listing_name}.{name}")
                    print_readme_signature(name, readme_lines)
                    setters_found = True
        if not setters_found:
            unknown(
                "Q4",
                "no name in either listing starts with a set verb and mentions "
                "an effect or parameter word",
            )

    # ----------------------------------------------------------------- Q5
    sub("Q5. Any method that applies a SAVED effect preset by name?")
    print("Names only — nothing here was called; calling one would be a write.")
    if not listings:
        unknown("Q5", "neither dir() listing could be collected")
    else:
        preset_hits = [
            (listing_name, name)
            for listing_name, names in listings.items()
            for name in names
            if matches_effect_word(name, ("preset",))
        ]
        if preset_hits:
            for listing_name, name in preset_hits:
                print(f"  {listing_name}.{name}")
                print_readme_signature(name, readme_lines)
            print(
                "Whether any of these applies a SAVED EFFECT preset (as opposed "
                "to a render or grade preset) is not stated by the name alone. "
                "Cross-read the README sections in Q6 before relying on one."
            )
        else:
            unknown(
                "Q5",
                "no name in either listing contains 'preset'",
            )

    # ----------------------------------------------------------------- Q6/Q7
    sub("Q6. The COMPLETE Timeline and TimelineItem README sections, verbatim")
    if readme_path is None:
        unknown(
            "Q6",
            f"no README* file was found anywhere under {RESOLVE_SCRIPT_API}",
        )
    else:
        print(f"README: {readme_path} ({len(readme_lines)} lines)")
        headers = top_level_headers(readme_lines)
        print(
            "Top-level headings detected, so you can check the parser: "
            + ", ".join(f"{text}@{number}" for number, text in headers)
        )
        for section_name in ("Timeline", "TimelineItem"):
            print()
            start, end, note = find_section(readme_lines, section_name)
            if start is None:
                unknown(f"  '{section_name}' section", note)
                continue
            if note:
                print(f"  NOTE: {note}")
            print(f"  '{section_name}' section — lines {start}-{end}, untruncated:")
            echo_lines(readme_lines, start, end)

    sub("Q7. Every README line mentioning OFX / OpenFX / ResolveFX or presets")
    if readme_path is None:
        unknown("Q7", "no README was found (see Q6)")
    else:
        fx_hits = grep_lines(readme_lines, r"ofx|openfx|resolvefx")
        print(f"Lines matching ofx/openfx/resolvefx: {len(fx_hits)}")
        for number, line in fx_hits:
            print(f"  {number:>5} | {line}")
        if not fx_hits:
            print("  (none — the README never mentions OFX in any spelling)")

        preset_hits_readme = grep_lines(readme_lines, r"preset")
        print(f"\nLines matching 'preset': {len(preset_hits_readme)}")
        for number, line in preset_hits_readme:
            print(f"  {number:>5} | {line}")
        if not preset_hits_readme:
            print("  (none)")

    heading("END OF PROBE — nothing above modified the project")
    return 0


if __name__ == "__main__":
    sys.exit(main())
