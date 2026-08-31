"""Clear away Playwright browsers left behind by earlier runs.

modem_analyzer closes its browser in a ``finally`` block, so a normal exit —
including Ctrl+C — cleans up after itself. A force-kill or a hard crash never
reaches that block. On this machine that had built up to 32 leftover profile
directories in %TEMP%, the oldest six weeks old.

What that leak is NOT, despite first appearances: a pile of stray browsers.
Playwright drives Chrome over ``--remote-debugging-pipe``, so when the driver
dies the pipe closes and Chrome exits with it — verified by deliberately
leaking a browser and watching it disappear on its own. Every Chrome process
found during this investigation had a living owner. The directories are the
real residue, and clearing them is what this module mostly does. The orphan
kill below is a belt-and-braces measure for the case where Chrome does outlive
its driver; expect it to report zero most of the time.

The rule this module follows, and the reason it is safe to run at startup:

    A browser whose driver process is still alive belongs to something that is
    still running — possibly another copy of this very script — and is never
    touched. Only a browser whose parent is gone is an orphan.

That distinction matters. "Kill every Playwright Chrome" would have killed the
browser behind a live dashboard the user was watching at the time.

Every failure here is swallowed. Tidying up is a convenience; it must never be
the reason the analyzer fails to start.
"""

from __future__ import annotations

import os
import shutil
import tempfile

MARKER = "playwright_chromiumdev_profile-"


def _profile_id(cmdline) -> str | None:
    """The profile suffix a Chrome command line points at, if any."""
    for token in cmdline or []:
        if MARKER in token:
            return token.split(MARKER, 1)[1].strip('"').strip("'")
    return None


def sweep(verbose: bool = True) -> dict:
    """Kill orphaned Playwright browsers and delete unused profile directories.

    Returns counts so a caller can report or test them. Never raises.
    """
    result = {"killed": 0, "dirs_removed": 0, "kept_profiles": [], "error": None}

    try:
        import psutil  # noqa: PLC0415 - optional, and absence must not be fatal
    except Exception as exc:  # pragma: no cover - depends on the environment
        result["error"] = f"psutil unavailable ({exc}); skipping sweep"
        if verbose:
            print(f"[janitor] {result['error']}")
        return result

    live_profiles: set[str] = set()
    orphans = []

    for proc in psutil.process_iter(["name", "cmdline", "ppid"]):
        try:
            if (proc.info["name"] or "").lower() != "chrome.exe":
                continue
            profile = _profile_id(proc.info["cmdline"])
            if profile is None:
                continue

            # Chrome's own renderer/GPU children are also chrome.exe and carry
            # the same command line. Only the top-level browser has a non-Chrome
            # parent, and only it is worth killing — the children go with it.
            parent = None
            try:
                parent = proc.parent()
            except psutil.Error:
                parent = None

            # A directory is in use if ANY process references it, top-level or
            # not. Deriving that only from top-level browsers leaves a race:
            # during the seconds between a driver exiting and its browser
            # noticing, a renderer still has the profile open, and deleting it
            # would corrupt a session that is still winding down.
            live_profiles.add(profile)

            if parent is not None and (parent.name() or "").lower() == "chrome.exe":
                continue  # a child of a browser we are already judging

            if parent is None or not parent.is_running():
                orphans.append(proc)
        except psutil.Error:
            continue

    for proc in orphans:
        try:
            profile = _profile_id(proc.info["cmdline"])
            proc.kill()
            result["killed"] += 1
            # Killed browsers no longer hold their profile, so the directory
            # becomes collectable in this same pass.
            live_profiles.discard(profile)
            if verbose:
                print(f"[janitor] killed orphaned browser pid={proc.pid} profile={profile}")
        except psutil.Error:
            continue

    # Directories go second: a profile only counts as in use once the process
    # scan above has confirmed a living owner.
    temp = tempfile.gettempdir()
    try:
        entries = os.listdir(temp)
    except OSError as exc:
        result["error"] = f"cannot read {temp}: {exc}"
        return result

    for name in entries:
        if not name.startswith(MARKER):
            continue
        if name[len(MARKER):] in live_profiles:
            result["kept_profiles"].append(name[len(MARKER):])
            continue
        path = os.path.join(temp, name)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                result["dirs_removed"] += 1
        except OSError:
            continue

    if verbose and (result["killed"] or result["dirs_removed"]):
        print(
            f"[janitor] cleaned {result['killed']} orphaned browser(s) "
            f"and {result['dirs_removed']} leftover profile folder(s)"
        )

    return result


if __name__ == "__main__":
    print(sweep())
