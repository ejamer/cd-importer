#!/usr/bin/env python3
"""
rip_cd.py — Rip a CD into ~/Music matching the existing library
convention (flat "<Album>/" folder, "NN. Title.mp3" files, ID3
TIT2/TPE1/TALB/TRCK/TPOS/TCON, cover.jpg). Metadata from MusicBrainz,
auto-identified by DiscID by default; pass --artist/--album to search
by name instead. See README.md for setup and full usage.

    python3 rip_cd.py                                    # auto-identify
    python3 rip_cd.py --artist X --album Y --dump-tracklist plan.json  # preview, no rip
    python3 rip_cd.py --tracklist-json plan.json --yes    # rip a reviewed/edited plan

ffmpeg here is a snap package with its own private /tmp — it can't see
files another process wrote to the real /tmp. All working files are
therefore staged under $HOME (WORKDIR_BASE), never /tmp.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request

sys.stdout.reconfigure(line_buffering=True)

MUSIC_ROOT = os.path.expanduser("~/Music")
DEVICE = "/dev/sr0"
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR_BASE = os.path.join(TOOL_DIR, ".work")   # under $HOME — see note above
LOG_DIR = os.path.join(TOOL_DIR, "logs")

_log_fh = None

def log(msg=""):
    print(msg)
    if _log_fh:
        _log_fh.write(msg + "\n")
        _log_fh.flush()

def die(msg):
    log(f"error: {msg}")
    sys.exit(1)

def confirm(prompt, default_no=True):
    suffix = "[y/N]" if default_no else "[Y/n]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        ans = ""
    if not ans:
        return not default_no
    return ans in ("y", "yes")

def choice(prompt, options, default):
    """options: {key: description}. Returns the chosen key (default on
    blank/EOF/unrecognized input)."""
    opts_str = "/".join(k.upper() if k == default else k for k in options)
    try:
        ans = input(f"{prompt} [{opts_str}] ").strip().lower()
    except EOFError:
        ans = ""
    return ans if ans in options else default

def check_tools():
    missing = [t for t in ("cdparanoia", "ffmpeg") if not shutil.which(t)]
    if missing:
        die(f"missing required tool(s): {', '.join(missing)}. "
            f"Run: sudo apt install -y cdparanoia python3-musicbrainzngs")
    try:
        import musicbrainzngs  # noqa: F401
    except ImportError:
        die("python3-musicbrainzngs not installed. "
            "Run: sudo apt install -y python3-musicbrainzngs")
    try:
        import mutagen  # noqa: F401
    except ImportError:
        die("python3-mutagen not installed. Run: sudo apt install -y python3-mutagen")

def sanitize(name):
    name = name.replace("/", "-")
    return re.sub(r'[\\:*?"<>|]', "_", name).strip()

def target_dir_for(album_dir, plan):
    return album_dir if plan["disc_total"] == 1 else os.path.join(album_dir, f"Disc {plan['disc_no']}")

_mbz = None
def mb():
    """musicbrainzngs module, configured once."""
    global _mbz
    if _mbz is None:
        import musicbrainzngs
        musicbrainzngs.set_useragent("personal-cd-importer", "1.0")
        _mbz = musicbrainzngs
    return _mbz

def run_logged(cmd, cwd=None):
    """Run a subprocess, streaming its combined output to console+logfile
    line by line, and raise CalledProcessError on nonzero exit."""
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        log(line.rstrip("\n"))
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

def get_disc_toc(device):
    """Best-effort: returns a list of per-track durations in seconds
    (index 0 = track 1), or None if no disc is readable. Metadata-only
    flows (--dump-tracklist, --tracklist-json review) tolerate None —
    the actual rip step checks readability for real."""
    out = subprocess.run(["cdparanoia", "-d", device, "-Q"],
                          capture_output=True, text=True)
    text = out.stderr + out.stdout
    if "unable to open" in text.lower() or "no such" in text.lower():
        print(f"warning: can't read a disc on {device} yet ({text.strip()[:200]})")
        return None
    # e.g. "  1.    11114 [02:28.14]        0 [00:00.32]    OK   no  2"
    #        track#  length-in-frames                                 (75 frames/sec)
    rows = re.findall(r"^\s*(\d+)\.\s+(\d+)\s+\[", text, re.MULTILINE)
    durations = {int(n): int(frames) / 75.0 for n, frames in rows if int(n) > 0}
    if not durations:
        return None
    return [durations.get(i) for i in range(1, max(durations) + 1)]

def get_disc_track_count(device):
    toc = get_disc_toc(device)
    return len(toc) if toc else None

def search_releases(artist, album, limit=8):
    return mb().search_releases(artist=artist, release=album, limit=limit).get("release-list", [])

def fetch_release(mbid):
    return mb().get_release_by_id(
        mbid, includes=["recordings", "media", "artist-credits", "tags", "release-groups"])["release"]

def compute_disc_id(device):
    """Read the disc's MusicBrainz DiscID (exact TOC fingerprint) via
    libdiscid — the same identification method the original library was
    ripped with (Banshee left TXXX:MusicBrainz DiscID tags behind)."""
    try:
        import discid
        return discid.read(device)
    except ImportError:
        log("python 'discid' module not installed — can't auto-identify by DiscID. "
            "Run: sudo apt install -y libdiscid0 && pip install --user --break-system-packages discid")
        return None
    except Exception as e:
        log(f"Could not compute a disc ID ({e}).")
        return None

def lookup_by_discid(disc):
    # "media"/"tags" aren't valid includes on this endpoint (400) — it
    # already returns track-count data by default. Only used to *select*
    # a release; fetch_release() does the full fetch afterward.
    try:
        res = mb().get_releases_by_discid(disc.id, includes=["artist-credits", "release-groups"])
    except mb().musicbrainz.ResponseError:
        return []
    return res.get("disc", {}).get("release-list", [])

def _normkey(s):
    """Casefold + strip diacritics/punctuation, so 'JAŸ‐Z' and 'Jay-Z' compare equal."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())

def _title_key(s):
    """Like _normkey, but also drops a leading/embedded 'the' — so
    MusicBrainz's 'The Dark Side of the Moon' matches an existing folder
    named 'Dark Side Of The Moon'."""
    if not s:
        return ""
    s = re.sub(r"\bthe\b", "", s, flags=re.IGNORECASE)
    return _normkey(s)

def find_fuzzy_duplicate(album_title):
    """Scan ~/Music for an existing album folder that's probably the same
    album under a differently-spelled/punctuated/cased title (exact-name
    duplicate check elsewhere won't catch these)."""
    key = _title_key(album_title)
    if not key or not os.path.isdir(MUSIC_ROOT):
        return None
    for name in os.listdir(MUSIC_ROOT):
        if name.startswith(".") or not os.path.isdir(os.path.join(MUSIC_ROOT, name)):
            continue
        if _title_key(name) == key and sanitize(album_title) != name:
            return name
    return None

# Anything ripped below this is presumed old/low-quality (matches the
# ~128kbps CBR the original Banshee-ripped library used) and worth
# flagging as upgradable — independent of whatever --bitrate/--quality
# this particular run happens to be using.
LOW_BITRATE_FLOOR = 192_000

def existing_avg_bitrate(dir_path):
    """Average bitrate (bps) of the mp3s directly in dir_path, or None if
    there are none / it can't be read."""
    from mutagen.mp3 import MP3
    rates = []
    try:
        for f in os.listdir(dir_path):
            if f.lower().endswith(".mp3"):
                try:
                    rates.append(MP3(os.path.join(dir_path, f)).info.bitrate)
                except Exception:
                    pass
    except OSError:
        pass
    return sum(rates) // len(rates) if rates else None

def bitrate_upgrade_note(dir_path):
    """A note to tack onto a duplicate-found message when the existing
    files are below LOW_BITRATE_FLOOR — empty string otherwise."""
    br = existing_avg_bitrate(dir_path)
    if br and br < LOW_BITRATE_FLOOR:
        return (f" Existing files are ~{br // 1000}kbps, below the current "
                f"default (~245kbps V0) — replacing would upgrade audio quality.")
    return ""

def normalize_credit(artist_credit, user_artist):
    """Join an MB artist-credit list into a string, but swap in the exact
    spelling the user typed (--artist) wherever an entry is 'the same
    artist' after stripping stylized Unicode/punctuation — MusicBrainz
    releases often use a stylized logo spelling (e.g. 'JAŸ‐Z') instead of
    the plain name."""
    if not artist_credit:
        return None
    user_key = _normkey(user_artist)
    parts = []
    for ac in artist_credit:
        if isinstance(ac, str):
            parts.append(ac)
            continue
        name = ac.get("artist", {}).get("name", "")
        if user_key and _normkey(name) == user_key:
            name = user_artist
        parts.append(name)
        parts.append(ac.get("joinphrase", ""))
    return "".join(parts)

def release_group_genre(release):
    rg = release.get("release-group")
    if not rg or not rg.get("id"):
        return None
    try:
        return best_genre(mb().get_release_group_by_id(rg["id"], includes=["tags"])["release-group"])
    except Exception:
        return None

def _medium_track_counts(c):
    return [int(m.get("track-count", 0)) for m in c.get("medium-list", [])]

def _total_tracks(c):
    return sum(_medium_track_counts(c))

def rank_candidates(candidates, expected_track_count):
    """Prefer a candidate where the *specific disc we're ripping* (--disc,
    default 1) has a track count matching the physical CD, then fewer
    discs overall (a plain single-CD release over a boxset/reissue with
    extra bonus discs), then original relevance/list order."""
    def score(item):
        idx, c = item
        counts = _medium_track_counts(c)
        disc_pos_count = counts[0] if counts else None
        exact_match = (expected_track_count is not None and
                       disc_pos_count == expected_track_count)
        return (not exact_match, len(counts), idx)
    return [c for _, c in sorted(enumerate(candidates), key=score)]

def pick_release(artist, album, expected_track_count, mbid=None):
    if mbid:
        return fetch_release(mbid)

    candidates = search_releases(artist, album)
    if not candidates:
        return None  # caller decides how to fall back

    best = rank_candidates(candidates, expected_track_count)[0]
    log(f"Selected release: {best['artist-credit-phrase']} — {best['title']} "
        f"[{best['id']}] ({_total_tracks(best)} tracks, {best.get('date','?')}, "
        f"{best.get('country','?')})")
    return fetch_release(best["id"])

def pick_release_by_discid(device, expected_track_count):
    """Try exact-match lookup via the disc's MusicBrainz DiscID first.
    Returns (release_or_None, disc_id_or_None)."""
    disc = compute_disc_id(device)
    if disc is None:
        return None, None
    candidates = lookup_by_discid(disc)
    if not candidates:
        log(f"Disc ID {disc.id} — no exact match in MusicBrainz's database "
            f"(common; disc-ID coverage is partial). Falling back to text search.")
        return None, disc.id
    best = rank_candidates(candidates, expected_track_count)[0]
    log(f"Disc ID {disc.id} matched: {best['artist-credit-phrase']} — {best['title']} "
        f"[{best['id']}] ({_total_tracks(best)} tracks, {best.get('date','?')}, "
        f"{best.get('country','?')})")
    return fetch_release(best["id"]), disc.id

def list_candidates(artist, album):
    candidates = search_releases(artist, album)
    if not candidates:
        print("No results.")
        return
    for c in candidates:
        tt = sum(int(m.get("track-count", 0)) for m in c.get("medium-list", []))
        print(f"  {c['id']}  {c['artist-credit-phrase']} — {c['title']} "
              f"({tt} tracks, {c.get('date','?')}, {c.get('country','?')}, "
              f"{c.get('status','?')})")

def best_genre(release):
    tags = release.get("tag-list") or []
    genres = release.get("genre-list") or []
    pool = genres if genres else tags
    if not pool:
        return None
    pool = sorted(pool, key=lambda t: -int(t.get("count", 0)))
    return pool[0]["name"].title()

def release_to_plan(release, disc_no, genre_override, user_artist):
    media = release.get("medium-list", [])
    medium = next((m for m in media if int(m.get("position", 1)) == disc_no), media[0])
    tracks = []
    for t in medium["track-list"]:
        rec = t.get("recording", {})
        title = t.get("title") or rec.get("title") or f"Track {t.get('number')}"
        track_artist = normalize_credit(rec.get("artist-credit"), user_artist)
        length_ms = t.get("length") or rec.get("length")
        duration_sec = round(int(length_ms) / 1000, 1) if length_ms else None
        tracks.append({"number": int(t.get("number") or len(tracks) + 1),
                        "title": title, "artist": track_artist, "duration_sec": duration_sec})
    album_artist = (normalize_credit(release.get("artist-credit"), user_artist)
                     or release["artist-credit-phrase"])
    genre = genre_override or best_genre(release) or release_group_genre(release)
    return {
        "mbid": release["id"],
        "cover_url": None,  # only used as a fallback if the mbid has no Cover Art Archive entry
        "artist": album_artist,
        "album": release["title"],
        "genre": genre,
        "disc_no": disc_no,
        "disc_total": len(media),
        "tracks": tracks,
    }

def blank_plan(artist, album, disc_no, n_tracks, genre_override, disc_durations=None):
    n_tracks = n_tracks or (len(disc_durations) if disc_durations else 1)
    return {
        "mbid": None,
        "cover_url": None,  # no mbid = no Cover Art Archive; set a direct image URL here instead
        "artist": artist,
        "album": album,
        "genre": genre_override,
        "disc_no": disc_no,
        "disc_total": 1,
        # duration_sec pre-filled from the real disc TOC — use it to sanity-check
        # whatever tracklist you find (see CLAUDE.md's manual-import process)
        "tracks": [{"number": i, "title": f"TODO track {i} title", "artist": None,
                     "duration_sec": (disc_durations[i - 1] if disc_durations and i <= len(disc_durations) else None)}
                    for i in range(1, n_tracks + 1)],
    }

def fmt_dur(sec):
    if sec is None:
        return None
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"

def print_plan(plan, disc_track_count, disc_durations=None):
    log(f"\nProposed import:")
    log(f"  Artist : {plan['artist']}")
    log(f"  Album  : {plan['album']}")
    log(f"  Genre  : {plan['genre'] or '(none)'}")
    log(f"  Disc   : {plan['disc_no']} of {plan['disc_total']}")
    log(f"  Tracks : {len(plan['tracks'])}"
        + (f"  (disc reports {disc_track_count})" if disc_track_count else ""))
    if disc_track_count and len(plan['tracks']) != disc_track_count:
        log(f"  *** MISMATCH: physical disc has {disc_track_count} tracks, "
            f"plan has {len(plan['tracks'])}. Fix before proceeding. ***")

    # CD TOC length vs. MusicBrainz "recording length" routinely differ by a
    # constant ~2s (the standard inter-track pregap) on every track — meaningless.
    # Flag deviation from THIS DISC'S median offset, not from zero, so only real
    # outliers (wrong track order/edition) trip the warning.
    offsets = [t["duration_sec"] - disc_durations[i - 1]
               for i, t in enumerate(plan["tracks"], start=1)
               if disc_durations and i <= len(disc_durations) and t.get("duration_sec") is not None]
    baseline = sorted(offsets)[len(offsets) // 2] if offsets else 0.0
    if offsets and abs(baseline) > 1:
        log(f"  (note: this disc's tracks run ~{baseline:+.1f}s vs. MusicBrainz-listed "
            f"durations throughout — normal pregap-convention offset, not a problem)")

    for i, t in enumerate(plan["tracks"], start=1):
        artist_note = f"  [{t['artist']}]" if t.get("artist") and t["artist"] != plan["artist"] else ""
        disc_dur = disc_durations[i - 1] if disc_durations and i <= len(disc_durations) else None
        plan_dur = t.get("duration_sec")
        dur_note = ""
        if disc_dur is not None:
            if plan_dur is not None:
                deviation = (plan_dur - disc_dur) - baseline
                flag = "" if abs(deviation) <= 3 else "  *** DURATION MISMATCH, check track order/edition ***"
                dur_note = f"  ({fmt_dur(plan_dur)} vs disc {fmt_dur(disc_dur)}){flag}"
            else:
                dur_note = f"  (disc: {fmt_dur(disc_dur)})"
        log(f"    {t['number']:>2}. {t['title']}{artist_note}{dur_note}")
    log("")

def download_cover(dest_path, mbid=None, cover_url=None):
    """Cover Art Archive (via mbid), then cover_url as fallback. Always
    logs the outcome, including failure — a missing cover must never
    pass silently, since nothing else checks for it afterward."""
    def _fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "personal-cd-importer/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp, open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)

    if mbid:
        try:
            _fetch(f"https://coverartarchive.org/release/{mbid}/front")
            log(f"Cover art saved (Cover Art Archive): {dest_path}")
            return True
        except Exception as e:
            log(f"Cover Art Archive fetch failed ({e}).")
    if cover_url:
        try:
            _fetch(cover_url)
            log(f"Cover art saved ({cover_url}): {dest_path}")
            return True
        except Exception as e:
            log(f"cover_url fetch failed ({e}).")
    log("*** No cover art fetched. Add cover.jpg manually, or rerun with a "
        "'cover_url' field in the tracklist JSON pointing at an image. ***")
    return False

def rip_tracks(device, n_tracks, workdir):
    if get_disc_track_count(device) is None:
        die(f"Can't read a disc on {device}. Is a CD inserted?")
    log(f"\nRipping {n_tracks} track(s) from {device} with cdparanoia "
        f"(this reads the whole disc — a few minutes)...")
    run_logged(["cdparanoia", "-d", device, "-B", f"1-{n_tracks}"], cwd=workdir)
    log("Rip complete.")

def encode_and_tag(workdir, tracks, disc_no, disc_total, album, genre, target_dir, quality_args):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TPOS, TCON, ID3NoHeaderError

    os.makedirs(target_dir, exist_ok=True)
    total = len(tracks)
    for i, t in enumerate(tracks, start=1):
        title, track_artist = t["title"], t.get("artist")
        wav = os.path.join(workdir, f"track{i:02d}.cdda.wav")
        if not os.path.exists(wav):
            log(f"warning: expected {wav} not found, skipping track {i}. "
                f"workdir contains: {os.listdir(workdir)}")
            continue
        fname = f"{i:02d}. {sanitize(title)}.mp3"
        out_path = os.path.join(target_dir, fname)
        log(f"[{i}/{total}] Encoding: {title}")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
               "-codec:a", "libmp3lame", *quality_args, out_path]
        run_logged(cmd)

        try:
            tags = ID3(out_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=track_artist or t.get("_album_artist"))
        tags["TALB"] = TALB(encoding=3, text=album)
        tags["TRCK"] = TRCK(encoding=3, text=f"{i}/{total}")
        tags["TPOS"] = TPOS(encoding=3, text=f"{disc_no}/{disc_total}")
        if genre:
            tags["TCON"] = TCON(encoding=3, text=genre)
        tags.save(out_path)
        log(f"[{i}/{total}] Tagged  : {out_path}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artist", help="Omit to auto-identify the disc by MusicBrainz DiscID")
    ap.add_argument("--album", help="Omit to auto-identify the disc by MusicBrainz DiscID")
    ap.add_argument("--device", default=DEVICE)
    ap.add_argument("--mbid", help="Specific MusicBrainz release ID (skip search)")
    ap.add_argument("--disc", type=int, default=1, help="Which physical disc you're ripping (multi-disc releases)")
    ap.add_argument("--genre", help="Override/force genre tag")
    ap.add_argument("--list-candidates", action="store_true", help="Show MusicBrainz matches and exit (no ripping)")
    ap.add_argument("--dump-tracklist", metavar="PATH", help="Write the proposed metadata/tracklist to PATH as JSON and exit, for hand-editing")
    ap.add_argument("--tracklist-json", metavar="PATH", help="Use this (possibly hand-edited) JSON instead of querying MusicBrainz")
    ap.add_argument("--bitrate", help='e.g. "320k" for CBR')
    ap.add_argument("--quality", type=int, help="ffmpeg -q:a VBR quality 0 (best) - 9 (worst); default 0 = V0")
    ap.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt (non-interactive)")
    ap.add_argument("--replace", action="store_true", help="Overwrite an existing import for this album/disc without prompting")
    ap.add_argument("--no-eject", action="store_true", help="Don't eject the disc after a successful import")
    args = ap.parse_args()

    check_tools()

    if args.list_candidates:
        if not (args.artist and args.album):
            die("--list-candidates needs --artist and --album.")
        list_candidates(args.artist, args.album)
        return

    disc_durations = get_disc_toc(args.device)
    expected = len(disc_durations) if disc_durations else None
    if expected:
        print(f"Disc reports {expected} track(s).")

    # --- Build the plan (metadata + tracklist) ---
    if args.tracklist_json:
        with open(args.tracklist_json) as f:
            plan = json.load(f)
    elif args.mbid:
        plan = release_to_plan(fetch_release(args.mbid), args.disc, args.genre, args.artist)
    elif args.artist and args.album:
        release = pick_release(args.artist, args.album, expected, mbid=None)
        if release is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            template = os.path.join(LOG_DIR, f"{sanitize(args.album)}.template.json")
            with open(template, "w") as f:
                json.dump(blank_plan(args.artist, args.album, args.disc, expected, args.genre, disc_durations), f, indent=2)
            die(f"No MusicBrainz results for artist={args.artist!r} album={args.album!r}. "
                f"A blank template was written to {template} — fill in track titles by hand, "
                f"then rerun with --tracklist-json {template}. Or try --mbid with an ID you "
                f"find manually on musicbrainz.org.")
        plan = release_to_plan(release, args.disc, args.genre, args.artist)
    else:
        print("No --artist/--album given — identifying disc by MusicBrainz DiscID...")
        release, disc_id = pick_release_by_discid(args.device, expected)
        if release is None:
            reason = (f"DiscID {disc_id} has no match in MusicBrainz's database (common — "
                       f"coverage is partial, especially for promos/regional pressings)"
                       if disc_id else "could not read a usable DiscID from the drive")
            die(f"Could not auto-identify this disc ({reason}). "
                f"Supply --artist and --album to search by name instead.")
        plan = release_to_plan(release, args.disc, args.genre, args.artist or "")

    if args.dump_tracklist:
        with open(args.dump_tracklist, "w") as f:
            json.dump(plan, f, indent=2)
        print(f"Wrote proposed metadata to {args.dump_tracklist}. Edit it, then rerun with "
              f"--tracklist-json {args.dump_tracklist} (add --yes to skip the confirmation prompt).")
        return

    # --- Resolve target directory & duplicate check (before we open a log/start ripping) ---
    album_dir = os.path.join(MUSIC_ROOT, sanitize(plan["album"]))
    target_dir = target_dir_for(album_dir, plan)

    existing_mp3s = []
    if os.path.isdir(target_dir):
        existing_mp3s = [f for f in os.listdir(target_dir) if f.lower().endswith(".mp3")]
    fuzzy_dup = None if existing_mp3s else find_fuzzy_duplicate(plan["album"])

    print_plan(plan, expected, disc_durations)

    if existing_mp3s:
        note = bitrate_upgrade_note(target_dir)
        print(f"'{target_dir}' already has {len(existing_mp3s)} mp3 file(s) — this looks like a duplicate import.{note}")
        if args.replace:
            pass
        elif args.yes:
            die("Refusing to overwrite an existing import without --replace (and --yes was given, so I can't prompt).")
        elif not confirm("Replace the existing files?" + (" (upgrades quality)" if note else "")):
            print("Cancelled.")
            return
        shutil.rmtree(target_dir)
    elif fuzzy_dup:
        fuzzy_path = os.path.join(MUSIC_ROOT, fuzzy_dup)
        note = bitrate_upgrade_note(fuzzy_path)
        print(f"'~/Music/{fuzzy_dup}' already exists and looks like the same album under a "
              f"different spelling/title — probably already in your library.{note}")
        if args.yes:
            if not args.replace:
                die("Looks like a likely duplicate of an existing folder; refusing to proceed "
                    "under --yes without --replace. Rerun interactively, or pass --replace to "
                    "replace that folder in place.")
            shutil.rmtree(fuzzy_path)
            album_dir = fuzzy_path
            target_dir = target_dir_for(album_dir, plan)
        elif note:
            # Low-bitrate existing copy — worth explicitly offering the upgrade path.
            pick = choice("Replace that existing (lower-quality) folder, import as a "
                           "separate new folder, or cancel?",
                           {"r": "replace", "s": "separate", "c": "cancel"}, default="c")
            if pick == "c":
                print("Cancelled.")
                return
            elif pick == "r":
                shutil.rmtree(fuzzy_path)
                album_dir = fuzzy_path
                target_dir = target_dir_for(album_dir, plan)
        elif not confirm(f"Import anyway as a separate '{sanitize(plan['album'])}' folder?"):
            print("Cancelled.")
            return

    if not args.yes:
        if not confirm("Proceed with rip?"):
            print("Cancelled.")
            return

    # --- Logging setup ---
    global _log_fh
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(LOG_DIR, f"{ts}_{sanitize(plan['album'])}.log")
    _log_fh = open(log_path, "w")
    log(f"Log: {log_path}")

    n_tracks = len(plan["tracks"])
    if args.bitrate:
        quality_args = ["-b:a", args.bitrate]
    else:
        q = args.quality if args.quality is not None else 0
        quality_args = ["-q:a", str(q)]

    os.makedirs(WORKDIR_BASE, exist_ok=True)
    ok = False
    with tempfile.TemporaryDirectory(prefix="cdrip_", dir=WORKDIR_BASE) as workdir:
        try:
            rip_tracks(args.device, n_tracks, workdir)
            for t in plan["tracks"]:
                t["_album_artist"] = plan["artist"]
            encode_and_tag(workdir, plan["tracks"], plan["disc_no"], plan["disc_total"],
                            plan["album"], plan["genre"], target_dir, quality_args)
            ok = True
        except subprocess.CalledProcessError as e:
            log(f"\nFAILED: {e}")

    if ok:
        cover_path = os.path.join(album_dir, "cover.jpg")
        if not os.path.exists(cover_path):
            download_cover(cover_path, mbid=plan.get("mbid"), cover_url=plan.get("cover_url"))
        log(f"\nDone: {target_dir}")
        if not os.path.exists(cover_path):
            log("*** WARNING: this album has no cover.jpg. ***")
        if not args.no_eject:
            try:
                subprocess.run(["eject", args.device], check=True)
                log(f"Ejected {args.device}.")
            except Exception as e:
                log(f"Could not eject {args.device} ({e}); eject manually.")
    else:
        log(f"\nImport failed — disc left in drive, files not finalized. See log: {log_path}")
        sys.exit(1)

if __name__ == "__main__":
    main()
