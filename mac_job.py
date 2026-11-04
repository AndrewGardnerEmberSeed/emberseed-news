#!/usr/bin/env python3
"""The overnight AI News job on Andrew's Mac. Andrew, 4 Nov 2026: "Can we get everything off of the API plan and just
run a scheduled task on my Mac? Including the AI News Edition?" Before that it ran on GitHub with the Claude API.

launchd runs it at 20 past every hour from 1am to 9am (mac/au.com.emberseedautomation.news.plist, installed in
~/Library/LaunchAgents). Each run:
  1. brings this folder up to date with GitHub (a story fixed on GitHub comes down first);
  2. runs build.py, which asks Claude Code on the Max plan. It makes an edition only from 1am to 10am in Adelaide,
     and only once a day, so the later runs find nothing to do;
  3. if there's a new edition, commits data/ and site/ and pushes them. GitHub then publishes the site.
If GitHub published one first (its 6am fallback, while there is one), the Mac's copy is dropped.
  python3 mac_job.py             # what launchd runs
  python3 mac_job.py --dry-run   # a full run with Claude that prints the stories and saves nothing
The log is ~/Library/Logs/emberseed-news.log."""
import datetime as dt
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WHO = ["-c", "user.name=EmberSeed News", "-c", "user.email=news-bot@users.noreply.github.com"]


def git(*args, check=True):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def main():
    today = dt.datetime.now(ZoneInfo("Australia/Adelaide")).date()
    print(f"--- {dt.datetime.now(ZoneInfo('Australia/Adelaide')):%a %d %b %Y %H:%M} Adelaide", flush=True)
    git("pull", "--ff-only", "-q")
    built = subprocess.run([sys.executable, str(ROOT / "build.py"), *sys.argv[1:]], cwd=ROOT)
    if built.returncode:
        sys.exit(f"build.py stopped (exit {built.returncode}); nothing was published. The next hour tries again.")
    if not git("status", "--porcelain", "--", "data", "site").stdout.strip():
        return print("Nothing new to publish.")
    git("add", "data", "site")
    git(*WHO, "commit", "-q", "-m", f"AI news {today} (made on the Mac)")
    if git("push", "-q", check=False).returncode == 0:
        return print(f"Published the {today} edition: GitHub puts it on the site in a minute or two.")
    # GitHub moved on while this ran. If it already has today's edition, keep that one.
    git("fetch", "-q")
    if git("cat-file", "-e", f"origin/main:data/{today}.json", check=False).returncode == 0:
        git("reset", "-q", "--keep", "origin/main")   # (--keep: any other local change is left alone)
        return print(f"GitHub already had the {today} edition, so the Mac's copy was dropped.")
    git("pull", "--rebase", "-q")
    git("push", "-q")
    print(f"Published the {today} edition (after catching up with GitHub).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - the log should say what went wrong, in one line
        sys.exit(f"Stopped: {e}")
