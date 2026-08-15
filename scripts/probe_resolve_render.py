#!/usr/bin/env python3
"""Read-only diagnostic probe of the DaVinci Resolve scripting API.

Run this on the Mac with Resolve Studio already open, then paste the whole
output back. It gathers facts so the proxy-render spec can be written against
real values instead of guesses. It implements no features.

    python3 scripts/probe_resolve_render.py

THIS PROBE MUST NOT MODIFY THE RESOLVE PROJECT IN ANY WAY. It calls no
``Set*`` method, no ``AddRenderJob``, no ``StartRendering``, and nothing else
that mutates state. The one place ``SetRenderSettings`` is named at all is a
``__doc__`` attribute read for Q5 — the method object is never invoked. Where
a value could only be discovered by writing, this script reports UNKNOWN and
says which write would have been required.

Two rules govern every line of output:

* Every return value is checked. The Resolve API signals failure by returning
  ``None`` or ``False`` — no exception, no message, nothing in any log — so a
  silent failure is reported here as a silent failure.
* Nothing is printed that did not come back from the API. Gaps are printed as
  ``UNKNOWN — <reason>``, never as a plausible-looking guess.

Standard library only. Deliberately standalone: it does not import from
``src/footage_pipeline/``, and nothing in ``src/`` imports it.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Connection paths (macOS)
# --------------------------------------------------------------------------

RESOLVE_SCRIPT_API = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
RESOLVE_SCRIPT_LIB = (
    "/Applications/DaVinci Resolve/DaVinci Resolve.app"
    "/Contents/Libraries/Fusion/fusionscript.so"
)

#: How many README lines to echo after the first SetRenderSettings mention.
README_BLOCK_LINES = 140
#: Cap on the sample-clip dir()/property dumps is deliberately absent; these
#: listings are the point of the probe and are never truncated.


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def sub(text: str) -> None:
    print()
    print(f"--- {text} " + "-" * max(0, 73 - len(text)))


def unknown(what: str, reason: str) -> None:
    print(f"{what}: UNKNOWN — {reason}")


def api_call(label: str, obj: Any, method_name: str, *args: Any, show: bool = True) -> Any:
    """Call ``obj.method_name(*args)``, reporting every failure mode explicitly.

    Returns the value on success, or ``None`` on any failure — after printing
    what went wrong. ``None`` and ``False`` are treated as the API's silent
    failure signals; an empty dict or list is a real (if empty) answer and is
    returned as such.
    """
    shown_args = ", ".join(repr(a) for a in args)
    if obj is None:
        unknown(label, f"the owning object is None, so {method_name}() was never called")
        return None
    method = getattr(obj, method_name, None)
    if method is None:
        unknown(label, f"{method_name!r} is not present on this object")
        return None
    try:
        value = method(*args)
    except Exception as exc:  # the API can also raise out of the C module
        unknown(label, f"{method_name}({shown_args}) raised {type(exc).__name__}: {exc}")
        return None
    if value is None:
        print(f"{label}: the API returned None — silent failure, no reason available.")
        return None
    if value is False:
        print(f"{label}: the API returned False — silent failure, no reason available.")
        return None
    if show:
        print(f"{label}: {value!r}")
    return value


def print_mapping(label: str, mapping: Any) -> None:
    """Print a dict in full — every pair, repr'd, never truncated."""
    if mapping is None:
        return
    if not isinstance(mapping, dict):
        print(f"{label}: not a dict; got {type(mapping).__name__} = {mapping!r}")
        return
    print(f"{label}: {len(mapping)} entries")
    if not mapping:
        print("  (the dict is empty — that is what the API returned)")
        return
    for key in sorted(mapping, key=lambda k: str(k).lower()):
        print(f"  {key!r} -> {mapping[key]!r}")


def print_names(label: str, names: Any) -> None:
    if names is None:
        return
    print(f"{label}: {len(names)} entries")
    for name in names:
        print(f"  {name}")


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


def fail(step: str, detail: str) -> None:
    """Print a clear diagnostic naming the failed step, then exit non-zero."""
    print()
    print("!" * 78)
    print(f"CONNECTION FAILED AT: {step}")
    print(detail)
    print("!" * 78)
    sys.exit(1)


def connect() -> Any:
    """Wire up the environment, import the module, and get the Resolve app."""
    heading("0. CONNECTION")

    print(f"python            : {sys.version.split()[0]} ({sys.executable})")
    print(f"platform          : {sys.platform}")
    if sys.platform != "darwin":
        print(
            "NOTE: the hard-coded paths below are macOS-specific, so the checks "
            "that follow are expected to fail on this platform."
        )

    modules_dir = os.path.join(RESOLVE_SCRIPT_API, "Modules")

    # Set the documented environment variables before the import. PYTHONPATH is
    # only read by the interpreter at startup, so setting it here affects child
    # processes only — sys.path is what makes the import work in THIS process,
    # and both are done deliberately.
    os.environ["RESOLVE_SCRIPT_API"] = RESOLVE_SCRIPT_API
    os.environ["RESOLVE_SCRIPT_LIB"] = RESOLVE_SCRIPT_LIB
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (os.environ.get("PYTHONPATH", ""), modules_dir) if part
    )
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)

    print(f"RESOLVE_SCRIPT_API: {RESOLVE_SCRIPT_API}")
    print(f"RESOLVE_SCRIPT_LIB: {RESOLVE_SCRIPT_LIB}")
    print(f"PYTHONPATH        : {os.environ['PYTHONPATH']}")

    # Step 1 — the scripting folder.
    if not os.path.isdir(RESOLVE_SCRIPT_API):
        fail(
            "step 1: locating RESOLVE_SCRIPT_API",
            f"Not a directory: {RESOLVE_SCRIPT_API}\n"
            "DaVinci Resolve's Developer/Scripting folder is not where this "
            "script expects it. Check the Resolve installation.",
        )
    print("step 1 OK: RESOLVE_SCRIPT_API directory exists.")

    # Step 2 — the fusionscript library.
    if not os.path.isfile(RESOLVE_SCRIPT_LIB):
        fail(
            "step 2: locating RESOLVE_SCRIPT_LIB",
            f"Not a file: {RESOLVE_SCRIPT_LIB}\n"
            "fusionscript.so was not found inside the Resolve app bundle. If "
            "Resolve is installed somewhere other than /Applications, this "
            "path needs to change.",
        )
    print("step 2 OK: fusionscript.so exists.")

    # Step 3 — the Modules folder holding DaVinciResolveScript.py.
    if not os.path.isdir(modules_dir):
        fail(
            "step 3: locating the Modules folder",
            f"Not a directory: {modules_dir}\n"
            "DaVinciResolveScript.py lives here and cannot be imported without it.",
        )
    print("step 3 OK: Modules directory exists.")

    # Step 4 — import.
    try:
        import DaVinciResolveScript as dvr_script  # noqa: N813  (vendor module name)
    except Exception as exc:
        fail(
            "step 4: importing DaVinciResolveScript",
            f"{type(exc).__name__}: {exc}\n"
            f"sys.path[0] is {sys.path[0]!r}.\n"
            "A common cause is an architecture mismatch: fusionscript.so must "
            "match the Python running this script (both arm64, or both x86_64 "
            "under Rosetta).",
        )
    print(f"step 4 OK: imported DaVinciResolveScript from {dvr_script.__file__}")

    # Step 5 — connect to the running application.
    try:
        resolve = dvr_script.scriptapp("Resolve")
    except Exception as exc:
        fail(
            "step 5: scriptapp('Resolve')",
            f"{type(exc).__name__}: {exc}",
        )
    if resolve is None:
        fail(
            "step 5: scriptapp('Resolve')",
            "scriptapp('Resolve') returned None.\n"
            "Resolve returns None here without any error message. The usual "
            "causes are: Resolve is not running; or External Scripting is not "
            "enabled (Preferences > System > General > External scripting "
            "using = Local).",
        )
    print("step 5 OK: connected to a running Resolve instance.")
    return resolve


# --------------------------------------------------------------------------
# Media pool traversal (read-only)
# --------------------------------------------------------------------------


def find_first_clip(folder: Any, path: str = "") -> tuple[Any, str]:
    """Depth-first search for any one clip. Returns ``(clip, folder_path)``.

    Returns ``(None, "")`` when the pool holds no clips at all. Reads only:
    GetClipList and GetSubFolderList never modify anything.
    """
    if folder is None:
        return None, ""
    name = folder.GetName() if hasattr(folder, "GetName") else "?"
    here = f"{path}/{name}" if path else str(name)

    clips = folder.GetClipList()
    if clips:
        return clips[0], here

    for sub_folder in folder.GetSubFolderList() or []:
        clip, found_in = find_first_clip(sub_folder, here)
        if clip is not None:
            return clip, found_in
    return None, ""


# --------------------------------------------------------------------------
# Q5 support: whatever documentation the installation actually ships
# --------------------------------------------------------------------------


def read_api_readme() -> tuple[Path | None, list[str]]:
    """Find and read the README shipped alongside the scripting API."""
    root = Path(RESOLVE_SCRIPT_API)
    if not root.is_dir():
        return None, []
    for candidate in sorted(root.rglob("README*")):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            return candidate, text.splitlines()
    return None, []


def grep_lines(lines: list[str], pattern: str) -> list[tuple[int, str]]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [(n, line) for n, line in enumerate(lines, 1) if rx.search(line)]


def capture_help(obj: Any) -> str:
    """Capture help() output without letting it paginate or escape to stdout."""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            help(obj)
    except Exception as exc:
        return f"UNKNOWN — help() raised {type(exc).__name__}: {exc}"
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Matching helpers for the ANSWERS section
# --------------------------------------------------------------------------


def normalise(text: Any) -> str:
    """Lowercase and strip everything that is not a letter or digit."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def entries_matching(mapping: dict, predicate) -> list[tuple[Any, Any]]:
    """Every (key, value) pair where the predicate accepts either side."""
    return [(k, v) for k, v in mapping.items() if predicate(str(k)) or predicate(str(v))]


def names_containing(listings: dict[str, list[str]], needle: str) -> list[tuple[str, str]]:
    """Every (listing_name, method_name) whose method name contains needle."""
    hits = []
    for listing_name, names in listings.items():
        for name in names:
            if needle.lower() in name.lower():
                hits.append((listing_name, name))
    return hits


def report_hits(label: str, hits: list[tuple[str, str]]) -> None:
    if hits:
        print(f"{label}: {len(hits)} hit(s)")
        for listing_name, name in hits:
            print(f"  {listing_name}.{name}")
    else:
        print(f"{label}: no hits in any of the dir() listings that were collected.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    resolve = connect()

    # ----------------------------------------------------------------- 1
    heading("1. RESOLVE PRODUCT AND VERSION")
    product_name = api_call("GetProductName()", resolve, "GetProductName")
    api_call("GetVersionString()", resolve, "GetVersionString")
    api_call("GetVersion()", resolve, "GetVersion")
    if product_name is None:
        unknown(
            "Studio?",
            "GetProductName() gave nothing back, so Studio cannot be determined",
        )
    elif "studio" in str(product_name).lower():
        print(f"Studio? YES — the product name {product_name!r} contains 'Studio'.")
    else:
        print(
            f"Studio? NO — the product name {product_name!r} does not contain "
            "'Studio'. Studio-only API surface may be missing below."
        )

    # ----------------------------------------------------------------- 2
    heading("2. CURRENT PROJECT")
    project_manager = api_call(
        "GetProjectManager()", resolve, "GetProjectManager", show=False
    )
    if project_manager is None:
        print("GetProjectManager() failed; everything below that needs it is UNKNOWN.")
    else:
        print("GetProjectManager(): OK")
    project = api_call(
        "GetCurrentProject()", project_manager, "GetCurrentProject", show=False
    )
    if project is None:
        print(
            "GetCurrentProject() failed. Resolve returns None here when no "
            "project is open — open the project first, then re-run."
        )
    else:
        print("GetCurrentProject(): OK")
    api_call("project.GetName()", project, "GetName")

    # ----------------------------------------------------------------- 3
    heading("3. TOP-LEVEL BINS IN THE MEDIA POOL")
    media_pool = api_call("project.GetMediaPool()", project, "GetMediaPool", show=False)
    if media_pool is not None:
        print("project.GetMediaPool(): OK")
    root_folder = api_call(
        "mediaPool.GetRootFolder()", media_pool, "GetRootFolder", show=False
    )
    if root_folder is not None:
        print("mediaPool.GetRootFolder(): OK")
        api_call("root folder name", root_folder, "GetName")
    sub_folders = api_call(
        "rootFolder.GetSubFolderList()", root_folder, "GetSubFolderList", show=False
    )
    if sub_folders is not None:
        if not sub_folders:
            print(
                "rootFolder.GetSubFolderList(): empty list — the media pool has "
                "no top-level bins."
            )
        else:
            print(f"rootFolder.GetSubFolderList(): {len(sub_folders)} top-level bin(s)")
            for index, folder in enumerate(sub_folders):
                name = folder.GetName() if hasattr(folder, "GetName") else None
                if name is None:
                    print(f"  [{index}] UNKNOWN — GetName() gave nothing back")
                else:
                    print(f"  [{index}] {name!r}")

    # ----------------------------------------------------------------- 4
    heading("4. project.GetRenderFormats()")
    render_formats = api_call(
        "GetRenderFormats()", project, "GetRenderFormats", show=False
    )
    print_mapping("GetRenderFormats()", render_formats)

    # ----------------------------------------------------------------- 5
    heading("5. project.GetRenderCodecs('mov') — COMPLETE, UNTRUNCATED")
    mov_codecs = api_call(
        "GetRenderCodecs('mov')", project, "GetRenderCodecs", "mov", show=False
    )
    print_mapping("GetRenderCodecs('mov')", mov_codecs)

    # If the literal 'mov' token is not what this build expects, the answer to
    # Q1 is still discoverable read-only from whatever token GetRenderFormats
    # actually reported. Every fallback below is another read-only call.
    codec_source = "GetRenderCodecs('mov')"
    if not mov_codecs and isinstance(render_formats, dict) and render_formats:
        sub("FALLBACK: 'mov' returned nothing, retrying with tokens from GetRenderFormats()")
        tokens: list[str] = []
        for key, value in render_formats.items():
            for token in (key, value):
                token = str(token)
                if token != "mov" and token not in tokens and (
                    "mov" in token.lower() or "quicktime" in token.lower()
                ):
                    tokens.append(token)
        if not tokens:
            print(
                "No key or value in GetRenderFormats() contains 'mov' or "
                "'quicktime', so there was no alternative token to try."
            )
        for token in tokens:
            attempt = api_call(
                f"GetRenderCodecs({token!r})",
                project,
                "GetRenderCodecs",
                token,
                show=False,
            )
            print_mapping(f"GetRenderCodecs({token!r})", attempt)
            if attempt and not mov_codecs:
                mov_codecs = attempt
                codec_source = f"GetRenderCodecs({token!r})"

    # ----------------------------------------------------------------- 6
    heading("6. project.GetCurrentRenderFormatAndCodec()")
    current_fmt = api_call(
        "GetCurrentRenderFormatAndCodec()",
        project,
        "GetCurrentRenderFormatAndCodec",
        show=False,
    )
    print_mapping("GetCurrentRenderFormatAndCodec()", current_fmt)

    # ----------------------------------------------------------------- 7
    heading("7. project.GetCurrentRenderMode()")
    render_mode = api_call("GetCurrentRenderMode()", project, "GetCurrentRenderMode")
    if render_mode is not None:
        print(
            "The integer above is printed exactly as returned. What each value "
            "means is not something the API reports; see the README excerpt in "
            "the Q5 section for whatever this installation documents."
        )

    # ----------------------------------------------------------------- 8
    heading("8. project.GetRenderPresetList()")
    presets = api_call("GetRenderPresetList()", project, "GetRenderPresetList", show=False)
    if presets is not None:
        print(f"GetRenderPresetList(): {len(presets)} preset(s)")
        for preset in presets:
            print(f"  {preset!r}")

    # ----------------------------------------------------------------- 9/10
    listings: dict[str, list[str]] = {}

    heading("9. sorted(dir(project)) — COMPLETE")
    if project is None:
        unknown("dir(project)", "there is no project object")
    else:
        listings["project"] = sorted(dir(project))
        print_names("dir(project)", listings["project"])

    heading("10. sorted(dir(mediaPool)) AND sorted(dir(resolve)) — COMPLETE")
    if media_pool is None:
        unknown("dir(mediaPool)", "there is no mediaPool object")
    else:
        listings["mediaPool"] = sorted(dir(media_pool))
        print_names("dir(mediaPool)", listings["mediaPool"])

    sub("dir(resolve)")
    listings["resolve"] = sorted(dir(resolve))
    print_names("dir(resolve)", listings["resolve"])

    # ----------------------------------------------------------------- 11
    heading("11. sorted(dir(mediaPoolItem)) FOR ONE SAMPLE CLIP")
    clip, clip_folder = find_first_clip(root_folder)
    if clip is None:
        print(
            "The media pool contains no clips (searched the root folder and "
            "every sub-folder), so there is no MediaPoolItem to inspect. "
            "Sections 11, Q2 and Q6 are skipped for that reason."
        )
    else:
        print(f"Sample clip found in bin: {clip_folder!r}")
        api_call("clip.GetName()", clip, "GetName")
        listings["mediaPoolItem"] = sorted(dir(clip))
        print_names("dir(mediaPoolItem)", listings["mediaPoolItem"])

    # =================================================================
    heading("ANSWERS")

    # ----------------------------------------------------------------- Q1
    sub("Q1. Exact codec strings for Apple ProRes 422 Proxy and 422 LT")
    if not mov_codecs:
        unknown(
            "Q1",
            "the codec dict came back empty or failed, so no string can be "
            "reported. Nothing is inferred from naming patterns.",
        )
    else:
        print(f"Source of the dict below: {codec_source}")
        print("Every ProRes-family entry, exactly as returned (key -> value):")
        prores = entries_matching(mov_codecs, lambda text: "prores" in normalise(text))
        if not prores:
            print(
                "  (none — no entry contains 'prores' on either side; the "
                "ProRes codecs may be named differently in this build, so "
                "read the complete section 5 dump above)"
            )
        for key, value in sorted(prores, key=lambda pair: str(pair[0]).lower()):
            print(f"  {key!r} -> {value!r}")

        print()
        proxy = entries_matching(mov_codecs, lambda text: "proxy" in normalise(text))
        if proxy:
            print("ProRes 422 Proxy — entries containing 'proxy':")
            for key, value in proxy:
                print(f"  key   = {key!r}")
                print(f"  value = {value!r}")
        else:
            unknown(
                "ProRes 422 Proxy",
                "no entry in the codec dict contains 'proxy' on either side. "
                "Not guessed from the other ProRes entries.",
            )

        print()

        def is_lt(text: str) -> bool:
            flat = normalise(text)
            return bool(re.search(r"(?i)\blt\b", text)) or "422lt" in flat

        lt_entries = entries_matching(mov_codecs, is_lt)
        if lt_entries:
            print("ProRes 422 LT — entries matching 'LT' as a distinct token:")
            for key, value in lt_entries:
                print(f"  key   = {key!r}")
                print(f"  value = {value!r}")
        else:
            unknown(
                "ProRes 422 LT",
                "no entry matches 'LT' as a distinct token on either side. "
                "Not guessed from the other ProRes entries.",
            )

        print()
        print(
            "NOTE: which side of each pair is the argument SetCurrentRender"
            "FormatAndCodec expects is not stated by the API and was not "
            "tested here, because testing it means writing. Both sides are "
            "printed above verbatim."
        )

    # ----------------------------------------------------------------- Q2
    sub("Q2. Do LinkProxyMedia / UnlinkProxyMedia appear on MediaPoolItem?")
    item_names = listings.get("mediaPoolItem")
    if item_names is None:
        unknown(
            "Q2",
            "no MediaPoolItem was available (see section 11), so its dir() "
            "listing could not be collected",
        )
    else:
        for wanted in ("LinkProxyMedia", "UnlinkProxyMedia"):
            present = wanted in item_names
            print(f"  {wanted}: {'PRESENT' if present else 'ABSENT'}")

    # ----------------------------------------------------------------- Q3
    sub("Q3. Every method name containing 'roxy' (case-insensitive)")
    print(f"Listings searched: {', '.join(sorted(listings)) or '(none)'}")
    missing = [name for name in ("project", "mediaPool", "resolve", "mediaPoolItem")
               if name not in listings]
    if missing:
        print(f"Listings NOT searched because they were unavailable: {', '.join(missing)}")
    report_hits("'roxy'", names_containing(listings, "roxy"))

    # ----------------------------------------------------------------- Q4
    sub("Q4. Every method name containing 'ptimiz' (case-insensitive)")
    report_hits("'ptimiz'", names_containing(listings, "ptimiz"))

    # ----------------------------------------------------------------- Q5
    sub("Q5. Discovering valid SetRenderSettings keys, read-only")

    print("(a) The method object's own docstring — attribute read, never called:")
    if project is None:
        unknown("  SetRenderSettings.__doc__", "there is no project object")
    else:
        set_render_settings = getattr(project, "SetRenderSettings", None)
        if set_render_settings is None:
            unknown(
                "  SetRenderSettings",
                "the attribute is not present on the project object",
            )
        else:
            doc = getattr(set_render_settings, "__doc__", None)
            if doc:
                print(f"  {doc!r}")
            else:
                print(
                    "  __doc__ is empty. The Resolve API objects are remote "
                    "proxies and carry no per-method docstrings."
                )

    print()
    print("(b) help(project) output, captured:")
    if project is None:
        unknown("  help(project)", "there is no project object")
    else:
        help_text = capture_help(project)
        if help_text.strip():
            for line in help_text.splitlines():
                print(f"  {line}")
        else:
            print("  help() produced no output.")

    print()
    print("(c) The README shipped with this installation of the scripting API:")
    readme_path, readme_lines = read_api_readme()
    filename_hits: list[tuple[int, str]] = []
    documents_keys = False
    if readme_path is None:
        unknown(
            "  README",
            f"no README* file was found anywhere under {RESOLVE_SCRIPT_API}",
        )
    else:
        print(f"  Found: {readme_path} ({len(readme_lines)} lines)")
        mentions = grep_lines(readme_lines, r"SetRenderSettings")
        if not mentions:
            print("  'SetRenderSettings' does not appear in the README at all.")
        else:
            documents_keys = True
            first = mentions[0][0]
            last = min(len(readme_lines), first + README_BLOCK_LINES)
            print(
                f"  First mention at line {first}; echoing lines {first}-{last} "
                "verbatim:"
            )
            for number in range(first, last + 1):
                print(f"  {number:>5} | {readme_lines[number - 1]}")

        print()
        print("  Lines anywhere in the README matching filename-related wording:")
        filename_hits = grep_lines(
            readme_lines, r"file\s*name|filename|uniquefilename|custom\s*name"
        )
        if filename_hits:
            for number, line in filename_hits:
                print(f"  {number:>5} | {line}")
        else:
            print("  (no matches)")

    print()
    print("VERDICT — is there a documented way to discover valid keys?")
    if documents_keys:
        print(
            f"  YES, via documentation only: {readme_path} documents "
            "SetRenderSettings. That README is a text file shipped with the "
            "installation, not something the API reports — the live API "
            "objects expose no key list, as (a) and (b) show."
        )
    else:
        unknown(
            "  Documented key discovery",
            "the live API exposes no key list, and no README on this machine "
            "documents SetRenderSettings",
        )

    print()
    print("VERDICT — is there a key controlling output FILENAME STYLE?")
    if filename_hits:
        print(
            f"  {len(filename_hits)} README line(s) above use filename-related "
            "wording. Those lines are quoted verbatim; read the key names from "
            "them directly. None is asserted here to be the right key for "
            "Individual Clips mode, because that can only be confirmed by "
            "calling SetRenderSettings and reading its True/False return — a "
            "write, which this probe does not perform."
        )
    else:
        unknown(
            "  Filename-style key",
            "nothing in (a), (b) or (c) mentions filename style. Confirming "
            "whether such a key exists would mean calling "
            "project.SetRenderSettings({<candidate>: <value>}) and reading the "
            "True/False return — a write to the project, which this probe does "
            "not perform. No key name is guessed here.",
        )

    # ----------------------------------------------------------------- Q6
    sub("Q6. GetClipProperty() with no argument, for the sample clip")
    if clip is None:
        unknown("Q6", "the media pool contains no clips (see section 11)")
    else:
        properties = api_call(
            "clip.GetClipProperty()", clip, "GetClipProperty", show=False
        )
        if properties is None:
            print(
                "GetClipProperty() with no argument gave nothing back. No "
                "property names are listed, because none were received."
            )
        else:
            print_mapping("clip.GetClipProperty()", properties)

    heading("END OF PROBE — nothing above modified the project")
    return 0


if __name__ == "__main__":
    sys.exit(main())
