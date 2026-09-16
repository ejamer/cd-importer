#!/usr/bin/env python3
"""
rip_cd.py — Rip a CD into ~/Music matching the existing library
convention ("<Artist>/<Album>/" folders — "Classical Music/<Album>/"
instead for classical, no artist level there — "NN. Title.mp3" files,
ID3 TIT2/TPE1/TALB/TRCK/TPOS/TCON, cover.jpg). Metadata from
MusicBrainz, auto-identified by DiscID by default; pass --artist/--album
to search by name instead. See README.md for setup and full usage.

After every successful rip, ~/Music/library_manifest.json (artists ->
albums -> tracks, with bitrate and per-track tag details) is regenerated
from scratch to reflect the current library. Pass --no-manifest to skip.

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
import struct
import subprocess
import sys
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

# Genres (case-insensitive substring match against the plan's genre) that
# get filed under a flat category subfolder instead of the default
# <Artist>/<Album>/ nesting. Only classical is active — add more entries
# here if/when asked, don't infer other categories on your own.
GENRE_SUBFOLDERS = {"classical": "Classical Music"}

def genre_subfolder(genre):
    if not genre:
        return None
    genre_lower = genre.lower()
    for keyword, folder in GENRE_SUBFOLDERS.items():
        if keyword in genre_lower:
            return folder
    return None

_mbz = None
def mb():
    """musicbrainzngs module, configured once."""
    global _mbz
    if _mbz is None:
        import musicbrainzngs
        musicbrainzngs.set_useragent("personal-cd-importer", "1.0")
        _mbz = musicbrainzngs
    return _mbz

def run_logged(cmd, cwd=None, device_gone_limit=25):
    """Run a subprocess, streaming its combined output to console+logfile
    line by line, and raise CalledProcessError on nonzero exit.

    Bails out early (killing the subprocess) if the drive itself
    disappears mid-rip: cdparanoia retries a bad sector forever and
    never notices the block device is gone, so left alone this spins
    for hours and floods the log. "System error: No such device" is
    cdparanoia's specific signature for that (distinct from normal
    scratched-disc retry chatter, which doesn't say this) - seeing it
    repeatedly means the device vanished, not that a sector is slow.

    Counts total occurrences over the whole run, not a consecutive
    streak - cdparanoia's own retry block interleaves the "No such
    device" line with 2-3 other lines (sector/sense/transport-error
    detail) each time, so a strict-consecutive count never advances
    past 1. A healthy rip has zero occurrences of this phrase, so an
    unbroken streak isn't needed to tell the two situations apart."""
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    device_gone_count = 0
    for line in proc.stdout:
        stripped = line.rstrip("\n")
        log(stripped)
        if "no such device" in stripped.lower():
            device_gone_count += 1
            if device_gone_count >= device_gone_limit:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"'{' '.join(cmd)}' reported \"No such device\" "
                    f"{device_gone_count} times - the drive disconnected "
                    f"mid-rip. Reconnect it (check it shows up again, e.g. "
                    f"`ls /dev/sr0`) and rerun; nothing was written for "
                    f"this track yet.")
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

def find_fuzzy_duplicate(album_title, expect_path=None):
    """Scan ~/Music for an existing album folder that's probably the same
    album under a differently-spelled/punctuated/cased title (exact-name
    duplicate check elsewhere won't catch these). Returns a path relative
    to MUSIC_ROOT, or None.

    Genre subfolders (GENRE_SUBFOLDERS, e.g. Classical Music) hold albums
    directly, one level deep. Everything else lives under an artist
    folder (see the 2026-09-16 reorg), so it's searched two levels deep —
    Artist/Album — instead."""
    key = _title_key(album_title)
    if not key:
        return None
    genre_dirs = set(GENRE_SUBFOLDERS.values())
    for base in genre_dirs:
        search_dir = os.path.join(MUSIC_ROOT, base)
        if not os.path.isdir(search_dir):
            continue
        for name in os.listdir(search_dir):
            if name.startswith(".") or not os.path.isdir(os.path.join(search_dir, name)):
                continue
            rel = os.path.join(base, name)
            if _title_key(name) == key and rel != expect_path:
                return rel
    for artist_name in os.listdir(MUSIC_ROOT):
        if artist_name.startswith(".") or artist_name in genre_dirs:
            continue
        artist_dir = os.path.join(MUSIC_ROOT, artist_name)
        if not os.path.isdir(artist_dir):
            continue
        for name in os.listdir(artist_dir):
            if name.startswith(".") or not os.path.isdir(os.path.join(artist_dir, name)):
                continue
            rel = os.path.join(artist_name, name)
            if _title_key(name) == key and rel != expect_path:
                return rel
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

def _has_latin(s):
    return any("a" <= c.lower() <= "z" for c in s or "")

def _credit_name(ac_artist, user_artist, user_key):
    """Best display name for one artist-credit entry: the exact spelling
    the user typed, if this is clearly the same artist after stripping
    stylized Unicode/punctuation (MusicBrainz logo spellings like
    'JAŸ‐Z'); else a Latin rendering derived from sort-name when the
    canonical name is in a non-Latin script (common for classical/
    international releases catalogued in the original language, e.g.
    'Пётр Ильич Чайковский' whose sort-name is 'Tchaikovsky, Pyotr
    Ilyich'); else the name as MusicBrainz has it."""
    name = ac_artist.get("name", "")
    if user_key and _normkey(name) == user_key:
        return user_artist
    if not _has_latin(name):
        sort_name = ac_artist.get("sort-name", "")
        if _has_latin(sort_name):
            last, _, rest = sort_name.partition(",")
            return f"{rest.strip()} {last.strip()}" if rest else sort_name
    return name

def normalize_credit(artist_credit, user_artist):
    """Join an MB artist-credit list into a display string — see
    _credit_name() for the per-entry logic."""
    if not artist_credit:
        return None
    user_key = _normkey(user_artist)
    parts = []
    for ac in artist_credit:
        if isinstance(ac, str):
            parts.append(ac)
            continue
        parts.append(_credit_name(ac.get("artist", {}), user_artist, user_key))
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

def _discid_medium_position(candidate, disc_id):
    """Which medium (1-based) in this candidate's medium-list actually
    carries the given DiscID. get_releases_by_discid can match a
    multi-disc release without saying which of its media matched —
    each medium's own disc-list has to be checked."""
    for medium in candidate.get("medium-list", []):
        for d in medium.get("disc-list", []):
            if d.get("id") == disc_id:
                return int(medium.get("position", 1))
    return None

def rank_candidates(candidates, expected_track_count, disc_id=None):
    """Prefer a candidate where the *specific disc we're ripping* has a
    track count matching the physical CD, then fewer discs overall (a
    plain single-CD release over a boxset/reissue with extra bonus
    discs), then original relevance/list order. When disc_id is given
    (DiscID auto-identify path), the specific disc is whichever medium's
    disc-list actually contains that DiscID; otherwise it's --disc,
    default 1, since there's no other way to know which medium a plain
    artist/album text search corresponds to."""
    def score(item):
        idx, c = item
        counts = _medium_track_counts(c)
        pos = _discid_medium_position(c, disc_id) if disc_id else 1
        disc_pos_count = counts[pos - 1] if pos and pos - 1 < len(counts) else (counts[0] if counts else None)
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
    Returns (release_or_None, disc_id_or_None, matched_disc_no_or_None).
    matched_disc_no is which medium of the release this physical disc
    actually is (1 for a single-disc release) — the DiscID pins this
    exactly, so for multi-disc releases it must be used instead of
    --disc, whose default (1) would otherwise silently mistag disc 2+."""
    disc = compute_disc_id(device)
    if disc is None:
        return None, None, None
    candidates = lookup_by_discid(disc)
    if not candidates:
        log(f"Disc ID {disc.id} — no exact match in MusicBrainz's database "
            f"(common; disc-ID coverage is partial). Falling back to text search.")
        return None, disc.id, None
    best = rank_candidates(candidates, expected_track_count, disc_id=disc.id)[0]
    matched_disc_no = _discid_medium_position(best, disc.id)
    log(f"Disc ID {disc.id} matched: {best['artist-credit-phrase']} — {best['title']} "
        f"[{best['id']}] disc {matched_disc_no or '?'} ({_total_tracks(best)} tracks total, "
        f"{best.get('date','?')}, {best.get('country','?')})")
    return fetch_release(best["id"]), disc.id, matched_disc_no

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
    # Prefer the fuller tag name on a count tie (e.g. "vgm" vs. "video
    # game music" at equal count) — avoids .title() mangling an acronym
    # ("Vgm") when a non-abbreviated tag says the same thing.
    pool = sorted(pool, key=lambda t: (-int(t.get("count", 0)), -len(t["name"])))
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
        # Not auto-derived: MusicBrainz's release-level artist-credit is
        # the performer for classical (which is what "artist" above
        # already holds), not the composer — getting the composer
        # reliably means resolving each recording's linked work and that
        # work's composer relationship, an extra API call per track that
        # classical MB data is too spotty to make worthwhile (see
        # CLAUDE.md gotchas). Fill this in by hand for classical discs,
        # same as genre/cover already get curated; leave it None
        # otherwise. Single string = whole-album composer -> TPE2, or set
        # a track's own "composer" key to override just that track (a
        # multi-composer compilation) -> that track's TCOM.
        "composer": None,
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
        "composer": None,  # classical: fill in by hand — see release_to_plan's note
        "disc_no": disc_no,
        "disc_total": 1,
        # duration_sec pre-filled from the real disc TOC — use it to sanity-check
        # whatever tracklist you find (see CLAUDE.md's manual-import process)
        "tracks": [{"number": i, "title": f"TODO track {i} title", "artist": None,
                     "duration_sec": (disc_durations[i - 1] if disc_durations and i <= len(disc_durations) else None)}
                    for i in range(1, n_tracks + 1)],
    }

# --- Box-set cache: for multi-CD compilations where MusicBrainz/Discogs
# per-disc data is unreliable (see CLAUDE.md). Once a disc's tracklist is
# confirmed by hand, save it (--save-to-boxset); future discs from the
# SAME set are identified for free from the physical TOC alone (track
# count + per-track durations), no network lookup needed.

def load_boxset(path):
    if not os.path.exists(path):
        return {"set_name": os.path.splitext(os.path.basename(path))[0], "volumes": []}
    with open(path) as f:
        return json.load(f)

def find_boxset_volume(boxset, disc_durations, tolerance=3.0):
    """Match this disc's real TOC against cached volumes by track count +
    per-track duration (same physical pressing reads near-identically —
    a few seconds' tolerance absorbs read variance). Returns the matching
    volume dict, or None."""
    if not disc_durations:
        return None
    for vol in boxset.get("volumes", []):
        cached = vol.get("track_durations_sec") or []
        if len(cached) == len(disc_durations) and \
           all(abs(a - b) <= tolerance for a, b in zip(cached, disc_durations)):
            return vol
    return None

def volume_to_plan(vol, boxset=None):
    return {
        "mbid": None,
        # Per-volume cover_url wins; falls back to the box's own cover
        # (many budget multi-disc sets have one box photo, no per-disc art).
        "cover_url": vol.get("cover_url") or (boxset or {}).get("default_cover_url"),
        "artist": vol["artist"],
        "album": vol["album"],
        "genre": vol.get("genre"),
        "composer": vol.get("composer"),
        "disc_no": 1,
        "disc_total": 1,
        "tracks": vol["tracks"],
    }

def save_boxset_volume(path, plan, disc_durations, label=None):
    boxset = load_boxset(path)
    default_cover = boxset.get("default_cover_url")
    boxset.setdefault("volumes", []).append({
        "label": label or f"Volume {len(boxset.get('volumes', [])) + 1}",
        "track_durations_sec": [round(d, 1) for d in disc_durations],
        "artist": plan["artist"],
        "album": plan["album"],
        "genre": plan.get("genre"),
        "composer": plan.get("composer"),
        # Only store per-volume if it differs from the box default, to keep the cache lean.
        "cover_url": plan.get("cover_url") if plan.get("cover_url") != default_cover else None,
        "tracks": [
            {"number": t["number"], "title": t["title"], "artist": t.get("artist"),
             **({"composer": t["composer"]} if t.get("composer") else {})}
            for t in plan["tracks"]
        ],
    })
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(boxset, f, indent=2, ensure_ascii=False)

def fmt_dur(sec):
    if sec is None:
        return None
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"

def print_plan(plan, disc_track_count, disc_durations=None, target_dir=None):
    log(f"\nProposed import:")
    log(f"  Artist : {plan['artist']}")
    log(f"  Album  : {plan['album']}")
    log(f"  Genre  : {plan['genre'] or '(none)'}")
    if plan.get("composer"):
        log(f"  Composer: {plan['composer']}")
    if target_dir:
        log(f"  Path   : {target_dir}")
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

MANIFEST_PATH = os.path.join(MUSIC_ROOT, "library_manifest.json")

def update_manifest():
    """Rebuild ~/Music/library_manifest.json from scratch: every artist
    folder -> album -> track, with per-album average bitrate and
    per-track tag details (title/artist/album/track & disc number/genre/
    duration/bitrate/sample rate). Classical Music is included as its own
    top-level entry mirroring its actual flat "Classical Music/<Album>/"
    layout, since it isn't split into artist folders.

    Always a full rebuild rather than an incremental update — the
    library only grows one CD at a time, so a fresh scan (a few seconds
    of tag reads, no audio decoding) is simpler and can't drift out of
    sync the way incremental bookkeeping could. Never raises: a manifest
    problem shouldn't fail an otherwise-successful rip, just gets logged."""
    from mutagen.mp3 import MP3

    def tag(tags, key):
        if not tags:
            return None
        v = tags.get(key)
        return str(v) if v is not None else None

    def read_track(path, rel_path):
        try:
            audio = MP3(path)
        except Exception as e:
            return {"filename": os.path.basename(path), "relative_path": rel_path, "error": str(e)}
        tags, info = audio.tags, audio.info
        return {
            "filename": os.path.basename(path),
            "relative_path": rel_path,
            "title": tag(tags, "TIT2"),
            "artist": tag(tags, "TPE1"),
            "album": tag(tags, "TALB"),
            "album_artist": tag(tags, "TPE2"),
            "composer": tag(tags, "TCOM"),
            "track_number": tag(tags, "TRCK"),
            "disc_number": tag(tags, "TPOS"),
            "genre": tag(tags, "TCON"),
            "duration_sec": round(info.length, 2),
            "bitrate_kbps": round(info.bitrate / 1000, 1),
            "sample_rate_hz": info.sample_rate,
        }

    def scan_album(album_dir, rel_prefix):
        tracks = []
        has_cover = os.path.isfile(os.path.join(album_dir, "cover.jpg"))
        subdirs = sorted(d for d in os.listdir(album_dir) if os.path.isdir(os.path.join(album_dir, d)))
        multi_disc = bool(subdirs) and all(d.lower().startswith("disc ") for d in subdirs)
        if multi_disc:
            for d in subdirs:
                dpath = os.path.join(album_dir, d)
                for f in sorted(os.listdir(dpath)):
                    if f.lower().endswith(".mp3"):
                        tracks.append(read_track(os.path.join(dpath, f), f"{rel_prefix}/{d}/{f}"))
        else:
            for f in sorted(os.listdir(album_dir)):
                if f.lower().endswith(".mp3"):
                    tracks.append(read_track(os.path.join(album_dir, f), f"{rel_prefix}/{f}"))
        bitrates = [t["bitrate_kbps"] for t in tracks if "bitrate_kbps" in t]
        return {
            "path": rel_prefix,
            "num_tracks": len(tracks),
            "avg_bitrate_kbps": round(sum(bitrates) / len(bitrates), 1) if bitrates else None,
            "has_cover": has_cover,
            "multi_disc": multi_disc,
            "tracks": tracks,
        }

    try:
        manifest = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "music_root": MUSIC_ROOT,
            "artists": {},
        }
        for entry in sorted(os.listdir(MUSIC_ROOT)):
            entry_path = os.path.join(MUSIC_ROOT, entry)
            if entry.startswith(".") or not os.path.isdir(entry_path):
                continue
            albums = {}
            for album_name in sorted(d for d in os.listdir(entry_path) if os.path.isdir(os.path.join(entry_path, d))):
                album_path = os.path.join(entry_path, album_name)
                albums[album_name] = scan_album(album_path, f"{entry}/{album_name}")
            manifest["artists"][entry] = {"albums": albums}
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        log(f"Updated {MANIFEST_PATH}")
    except Exception as e:
        log(f"Manifest update failed ({e}) — library was ripped fine, just the "
            f"catalog file wasn't refreshed. Rerun with --no-manifest omitted to retry.")

def _wav_seconds(path):
    """Actual playable duration of a wav file in seconds, or None if it
    doesn't exist or isn't readable. Deliberately doesn't trust the
    'data' chunk's declared size (what wave.getnframes() reports):
    cdparanoia writes that header upfront with the track's *full*
    expected size before streaming any PCM into it, so a file killed
    mid-write still claims to be complete-length in its own header.
    Real bytes present on disk from the start of the data chunk to EOF,
    divided by the format's bytes/sec, is the only honest measure of
    how much actually got written."""
    try:
        with open(path, "rb") as f:
            if f.read(12)[:4] != b"RIFF":
                return None
            framerate = channels = sampwidth = None
            while True:
                chunk_id, chunk_size = struct.unpack("<4sI", f.read(8))
                if chunk_id == b"fmt ":
                    fmt = f.read(chunk_size)
                    channels, framerate = struct.unpack("<HI", fmt[2:8])
                    sampwidth = struct.unpack("<H", fmt[14:16])[0] // 8
                elif chunk_id == b"data":
                    if framerate is None:
                        return None
                    data_bytes = os.path.getsize(path) - f.tell()
                    bytes_per_sec = framerate * channels * sampwidth
                    return data_bytes / bytes_per_sec if bytes_per_sec > 0 else None
                else:
                    f.seek(chunk_size, 1)
    except (FileNotFoundError, OSError, struct.error):
        return None

def tracks_needing_rip(workdir, n_tracks, disc_durations, tolerance_sec=1.5):
    """Which 1-based track numbers still need a real cdparanoia pass:
    missing entirely, or present but shorter than the disc's own
    reported duration for that track — the signature of a rip that was
    interrupted (killed, drive hiccup) partway through a track. Deletes
    any short file it finds so a retry can't mistake it for done.
    disc_durations may be None (TOC unreadable this call) — falls back
    to "present at all" as the completeness check in that case."""
    missing = []
    for i in range(1, n_tracks + 1):
        wav = os.path.join(workdir, f"track{i:02d}.cdda.wav")
        got = _wav_seconds(wav)
        expected = disc_durations[i - 1] if disc_durations and i - 1 < len(disc_durations) else None
        if got is None:
            missing.append(i)
        elif expected is not None and got < expected - tolerance_sec:
            log(f"track {i}: existing rip is short ({got:.1f}s of {expected:.1f}s expected) "
                f"— discarding, will re-rip.")
            os.remove(wav)
            missing.append(i)
    return missing

def rip_tracks(device, n_tracks, workdir, only_tracks=None):
    if get_disc_track_count(device) is None:
        die(f"Can't read a disc on {device}. Is a CD inserted?")
    if only_tracks is None or len(only_tracks) == n_tracks:
        log(f"\nRipping {n_tracks} track(s) from {device} with cdparanoia "
            f"(this reads the whole disc — a few minutes)...")
        run_logged(["cdparanoia", "-d", device, "-B", f"1-{n_tracks}"], cwd=workdir)
    elif only_tracks:
        log(f"\nPicking up where the last attempt left off — re-ripping "
            f"{len(only_tracks)} track(s): {only_tracks}")
        for t in only_tracks:
            run_logged(["cdparanoia", "-d", device, "-B", f"{t}-{t}"], cwd=workdir)
    log("Rip complete.")

def encode_and_tag(workdir, tracks, disc_no, disc_total, album, genre, target_dir, quality_args, composer=None):
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCOM, TRCK, TPOS, TCON, ID3NoHeaderError

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
        # Composer (TCOM, per-track override or album default) and Album
        # Artist (TPE2, always the album-level composer when known) —
        # classical only, in practice: this is what lets Plex/iTunes/Roon
        # browse by composer instead of fragmenting into one "artist" per
        # soloist/orchestra/conductor combination. See CLAUDE.md.
        track_composer = t.get("composer") or composer
        if track_composer:
            tags["TCOM"] = TCOM(encoding=3, text=track_composer)
        if composer:
            tags["TPE2"] = TPE2(encoding=3, text=composer)
        tags.save(out_path)
        log(f"[{i}/{total}] Tagged  : {out_path}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artist", help="Omit to auto-identify the disc by MusicBrainz DiscID")
    ap.add_argument("--album", help="Omit to auto-identify the disc by MusicBrainz DiscID")
    ap.add_argument("--device", default=DEVICE)
    ap.add_argument("--mbid", help="Specific MusicBrainz release ID (skip search)")
    ap.add_argument("--boxset", metavar="PATH", help="Try to identify the disc from this box-set "
                     "cache file (by TOC fingerprint) before anything else; falls through to the "
                     "normal identification if no cached volume matches")
    ap.add_argument("--save-to-boxset", metavar="PATH", help="After confirming the plan, append it "
                     "to this box-set cache file (creating it if needed) for future TOC lookups")
    ap.add_argument("--volume-label", help='Label for --save-to-boxset (e.g. "Volume 7"); '
                     "default auto-numbers")
    ap.add_argument("--disc", type=int, default=1, help="Which physical disc you're ripping (multi-disc releases)")
    ap.add_argument("--genre", help="Override/force genre tag")
    ap.add_argument("--subfolder", help='Category subfolder under ~/Music (e.g. "Classical Music"). '
                     'Default: auto from genre via GENRE_SUBFOLDERS (currently just classical); '
                     'pass "" to force flat placement even if genre would auto-route.')
    ap.add_argument("--list-candidates", action="store_true", help="Show MusicBrainz matches and exit (no ripping)")
    ap.add_argument("--dump-tracklist", metavar="PATH", help="Write the proposed metadata/tracklist to PATH as JSON and exit, for hand-editing")
    ap.add_argument("--tracklist-json", metavar="PATH", help="Use this (possibly hand-edited) JSON instead of querying MusicBrainz")
    ap.add_argument("--bitrate", help='e.g. "320k" for CBR')
    ap.add_argument("--quality", type=int, help="ffmpeg -q:a VBR quality 0 (best) - 9 (worst); default 0 = V0")
    ap.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt (non-interactive)")
    ap.add_argument("--replace", action="store_true", help="Overwrite an existing import for this album/disc without prompting")
    ap.add_argument("--no-eject", action="store_true", help="Don't eject the disc after a successful import")
    ap.add_argument("--no-manifest", action="store_true",
                     help="Don't regenerate ~/Music/library_manifest.json after a successful import")
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
    boxset_vol, boxset_data = None, None
    if args.boxset:
        boxset_data = load_boxset(args.boxset)
        boxset_vol = find_boxset_volume(boxset_data, disc_durations)
        if not boxset_vol:
            print(f"No cached match in {args.boxset} for this disc; falling back to normal identification.")

    if args.tracklist_json:
        with open(args.tracklist_json) as f:
            plan = json.load(f)
    elif boxset_vol:
        plan = volume_to_plan(boxset_vol, boxset_data)
        print(f"Matched box-set cache: {boxset_vol.get('label', '?')}")
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
        release, disc_id, matched_disc_no = pick_release_by_discid(args.device, expected)
        if release is None:
            reason = (f"DiscID {disc_id} has no match in MusicBrainz's database (common — "
                       f"coverage is partial, especially for promos/regional pressings)"
                       if disc_id else "could not read a usable DiscID from the drive")
            die(f"Could not auto-identify this disc ({reason}). "
                f"Supply --artist and --album to search by name instead.")
        # The DiscID pins the exact medium — trust it over --disc's default
        # of 1, which would otherwise mistag disc 2+ of a multi-disc release.
        plan = release_to_plan(release, matched_disc_no or args.disc, args.genre, args.artist or "")

    if args.dump_tracklist:
        os.makedirs(os.path.dirname(os.path.abspath(args.dump_tracklist)), exist_ok=True)
        with open(args.dump_tracklist, "w") as f:
            json.dump(plan, f, indent=2)
        print(f"Wrote proposed metadata to {args.dump_tracklist}. Edit it, then rerun with "
              f"--tracklist-json {args.dump_tracklist} (add --yes to skip the confirmation prompt).")
        return

    # --- Resolve target directory & duplicate check (before we open a log/start ripping) ---
    subfolder = args.subfolder if args.subfolder is not None else genre_subfolder(plan["genre"])
    if subfolder:
        album_dir = os.path.join(MUSIC_ROOT, subfolder, sanitize(plan["album"]))
    elif args.subfolder == "":
        # Explicit --subfolder "" forces truly flat placement (no genre
        # subfolder, no artist folder either) — the escape hatch the
        # flag has always documented, kept as-is.
        album_dir = os.path.join(MUSIC_ROOT, sanitize(plan["album"]))
    else:
        # Default when no genre subfolder applies: nest under the artist
        # folder (Plex-compatible "<Artist>/<Album>/"), matching the
        # 2026-09-16 library reorg. Classical stays flat under its own
        # genre subfolder, untouched by this.
        album_dir = os.path.join(MUSIC_ROOT, sanitize(plan["artist"]), sanitize(plan["album"]))
    target_dir = target_dir_for(album_dir, plan)

    existing_mp3s = []
    if os.path.isdir(target_dir):
        existing_mp3s = [f for f in os.listdir(target_dir) if f.lower().endswith(".mp3")]
    fuzzy_dup = None if existing_mp3s else find_fuzzy_duplicate(
        plan["album"], expect_path=os.path.relpath(album_dir, MUSIC_ROOT))

    print_plan(plan, expected, disc_durations, target_dir)

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

    if args.save_to_boxset and disc_durations:
        save_boxset_volume(args.save_to_boxset, plan, disc_durations, args.volume_label)
        print(f"Saved to box-set cache: {args.save_to_boxset}")

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

    # A stable (not random-per-run) workdir, so a rip that dies partway
    # through — killed, drive hiccup, stuck on a scratch — can be resumed
    # by rerunning the exact same command: already-good tracks are found
    # here and only the missing/short ones get re-ripped.
    os.makedirs(WORKDIR_BASE, exist_ok=True)
    workdir = os.path.join(WORKDIR_BASE, f"cdrip_{sanitize(plan['album'])}_disc{plan['disc_no']}")
    os.makedirs(workdir, exist_ok=True)
    ok = False
    try:
        missing = tracks_needing_rip(workdir, n_tracks, disc_durations)
        rip_tracks(args.device, n_tracks, workdir, only_tracks=missing)
        for t in plan["tracks"]:
            t["_album_artist"] = plan["artist"]
        encode_and_tag(workdir, plan["tracks"], plan["disc_no"], plan["disc_total"],
                        plan["album"], plan["genre"], target_dir, quality_args,
                        composer=plan.get("composer"))
        ok = True
    except subprocess.CalledProcessError as e:
        log(f"\nFAILED: {e}")

    if ok:
        shutil.rmtree(workdir, ignore_errors=True)
        cover_path = os.path.join(album_dir, "cover.jpg")
        if not os.path.exists(cover_path):
            download_cover(cover_path, mbid=plan.get("mbid"), cover_url=plan.get("cover_url"))
        log(f"\nDone: {target_dir}")
        if not os.path.exists(cover_path):
            log("*** WARNING: this album has no cover.jpg. ***")
        if not args.no_manifest:
            update_manifest()
        if not args.no_eject:
            try:
                subprocess.run(["eject", args.device], check=True)
                log(f"Ejected {args.device}.")
            except Exception as e:
                log(f"Could not eject {args.device} ({e}); eject manually.")
    else:
        log(f"\nImport failed — disc left in drive, files not finalized. Tracks ripped "
            f"so far are kept in {workdir}; rerun the same command (once whatever went "
            f"wrong — a scratch, a stuck drive — is sorted out) and it'll pick up where "
            f"it left off instead of re-ripping everything. See log: {log_path}")
        sys.exit(1)

if __name__ == "__main__":
    main()
