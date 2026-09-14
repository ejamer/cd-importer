# cd-importer

Rips audio CDs into `~/Music`, matching the existing library's folder and
ID3 convention (see README.md for the exact convention and usage). This
file is operating notes for Claude, not user-facing docs.

## How to run an import

```
cd ~/Documents/GitHub/cd-importer
python3 rip_cd.py --dump-tracklist /tmp/plan.json   # identifies disc, no rip
```

1. Read the dumped plan: artist, album, genre, disc N of M, full tracklist.
2. Sanity-check it before touching the drive:
   - Track count matches what `cdparanoia -d /dev/sr0 -Q` reports.
   - Artist spelling isn't a MusicBrainz stylization (fancy Unicode,
     unexpected casing) — the script auto-normalizes to `--artist` when
     you pass one, but the disc-ID auto-path has no such reference, so
     eyeball it.
   - Genre populated, no obvious wrong-edition tell (track count off,
     wildly different date/region than expected).
   - Run `find_fuzzy_duplicate()` mentally / check `~/Music/` — the
     script's own fuzzy-duplicate check catches most of this, but a
     second look doesn't hurt.
3. If it looks right: `python3 rip_cd.py --tracklist-json /tmp/plan.json --yes`.
   That one look at the plan *is* the confirmation checkpoint — don't add
   extra prompts or re-confirm mid-rip. If something's off, fix the JSON
   (or rerun with `--artist`/`--album`/`--mbid` to get a better match)
   before the `--yes` run, not after.
4. Rips run a few minutes; use `run_in_background` for the actual
   `--yes` rip (not the `--dump-tracklist` preview, which is instant) and
   poll with TaskOutput rather than a long foreground wait.
5. Report the result path and track count; don't re-list files or re-dump
   tags unless something looks wrong.

If MusicBrainz has no DiscID match, the script falls back to needing
`--artist`/`--album` — ask the user for the CD's identity rather than
guessing from track lengths.

## Fallback when MusicBrainz has nothing (self-released/indie CDs)

Small/indie discs are often in neither the DiscID database nor text
search (`--list-candidates` empty). Expected, not a bug — don't retry
searches, move to this process:

1. WebSearch `<artist> "<album>" tracklist` — Bandcamp/Discogs are the
   best sources for small releases. WebFetch the page for the tracklist.
2. **Cross-check track durations against the physical disc** (`cdparanoia
   -d /dev/sr0 -Q` or `get_disc_toc()`) before trusting the source — should
   match to within a second or two, track-for-track. A mismatch means
   wrong edition/tracklist.
3. Hand-write the tracklist JSON (schema: any `--dump-tracklist` output;
   `mbid: null` is fine) and pass via `--tracklist-json`. Including
   `duration_sec` per track (from the source) makes step 2 an automatic
   check instead of eyeballed.
4. **Get explicit user go-ahead before ripping** — no MusicBrainz
   corroboration here, so this is the one case where the plan-review
   checkpoint should come from the user, not your own `--yes` judgment call.
5. Genre: infer from a quick artist search if not obvious, rather than
   leaving it blank.
6. **Cover art — always, don't skip.** No `mbid` means no Cover Art
   Archive lookup. Either set `cover_url` in the plan JSON to a direct
   image URL (Bandcamp: use the `_0` suffix variant of the `f4.bcbits.com`
   image for full resolution) before ripping, or drop a `cover.jpg` into
   the album folder by hand after. The script prints `*** WARNING: this
   album has no cover.jpg ***` if you skip this — treat that as an
   unfinished import, not optional cleanup.

## Multi-CD box sets (`--boxset`/`--save-to-boxset`)

Large budget box sets (seen: a 50-CD "Classica d'Oro" classical
anthology) are frequently catalogued badly per individual disc — even a
correct DiscID match can point at wrong track counts/durations for that
specific disc (see the gotcha below). Once a disc from a set is
correctly identified by hand:

1. Save it: `--save-to-boxset boxsets/<slug>.json` (plus `--volume-label`
   if you know the printed volume number). This stores the disc's real
   per-track durations as a TOC fingerprint alongside the confirmed
   tracklist — `boxsets/*.json`, plain JSON, safe to inspect/hand-edit.
2. Every later disc from the *same* set: try `--boxset boxsets/<slug>.json`
   first. If the physical disc's TOC matches a cached volume (track
   count + durations, ~3s tolerance), the plan comes straight from the
   cache — no network call, no re-identification risk. Falls through to
   normal identification if it's an unseen disc, so this is always safe
   to try first for a set you've started caching.
3. **Cover art**: budget box sets commonly have zero per-disc art, only
   a photo of the outer box (confirmed on the Classica d'Oro set —
   searched specifically for one disc's exact track combination, found
   nothing disc-specific). Don't assume this from the box's cheapness
   alone — check per volume the way indie CDs get checked — but once
   confirmed, set the box's cover as `default_cover_url` at the JSON's
   top level so every cached volume inherits it without repeating the
   URL (`volume_to_plan()` falls back to it when a volume has no
   `cover_url` of its own).

## Known gotchas (don't rediscover these)

- **Rhythmbox shows "Unknown" quality for new V0 VBR rips** (old 128kbps
  CBR files show fine). Not our bug — verified a valid `Xing` header is
  present and `mutagen` reads it correctly. It's a known GStreamer/
  Rhythmbox VBR-header limitation ([Debian #373154](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=373154),
  [Ubuntu #1654733](https://bugs.launchpad.net/bugs/1654733)), cosmetic
  only. User chose to keep VBR over switching to CBR to fix the display —
  settled, don't re-raise.
- **Multi-disc interleaving in Rhythmbox (`1,1,2,2,3,3...`) means a
  mistagged `TPOS` on one disc, not a Rhythmbox sorting bug.** Verified:
  `BBC Sessions` and the 3-disc *Live at the Gorge* sets use normal
  per-disc `TRCK` with correct `TPOS` (`1/2`/`2/2` etc.) and order fine.
  *Decade*'s Disc 2 had `TPOS=1/1` (original Banshee-rip error, should be
  `2/2`) — both discs claimed to be "disc 1", so it tied and fell back to
  a `TRCK`-only sort. Fix is always: check/correct `TPOS` on the affected
  disc, keep normal per-disc `TRCK`. Do **not** "fix" this with continuous
  cross-disc track numbering — tried that, it's unwanted and unnecessary.
  Decade is retagged correctly now; the other multi-disc albums were
  checked and are fine.
- **Per-track duration vs. MusicBrainz "recording length" routinely
  differs by a constant ~2s on every track** (standard CD inter-track
  pregap) — meaningless. `print_plan()` flags deviation from the disc's
  own median offset, not from zero, so this doesn't trip false positives;
  a genuine wrong-edition problem shows as an outlier relative to the
  rest of the disc.
- **`ffmpeg` here is a snap package** → private `/tmp`, can't see files
  another process wrote to the real `/tmp`. All work happens under
  `./.work` (under `$HOME`). Check `which ffmpeg` before assuming this
  still applies if the environment changes.
- **musicbrainzngs `includes=[...]` are picky per-endpoint**, not what
  the docs suggest: `/release/` wants `artist-credits` (not
  `artist-credit`); `/discid/` 400s on `media`/`tags` (returned by
  default there instead). Check `musicbrainzngs.musicbrainz.VALID_INCLUDES[<entity>]`
  before guessing at a new include.
- **MusicBrainz artist names are sometimes stylized** (e.g. "JAŸ‐Z" for
  "Jay-Z"), or in the artist's **original non-Latin script** (classical/
  international releases — e.g. "Пётр Ильич Чайковский" for Tchaikovsky).
  `normalize_credit()` fixes stylization against `--artist`, and falls
  back to a Latin rendering of `sort-name` when the credit name has no
  Latin letters at all (works even with no `--artist`, e.g. the disc-ID
  auto path) — but still check the dumped plan for anything neither
  handles.
- **Cover Art Archive doesn't have everything, even with a valid `mbid`**
  (404 is normal, not a bug — seen on an obscure classical reissue).
  When it fails: query `get_release_by_id(mbid, includes=["url-rels"])`
  for a linked Discogs release, then Discogs' public API
  (`api.discogs.com/releases/<id>`, no auth needed) for `images[].resource_url`
  — Discogs' own web pages 403 generic fetchers, but the API doesn't.
- **Release-candidate track-count matching** must compare the specific
  medium being ripped, not the sum across all media — a boxset with the
  right total can have the wrong count on disc 1 alone.
- **A DiscID match can point at genuinely bad MusicBrainz data**, not
  just an ambiguous one — seen on a disc in a large (50-CD) budget
  classical box set: matched via DiscID (so the right physical disc),
  but the medium's track-count was wrong (8 vs. 10) *and* the 8 listed
  durations didn't match the disc's real ones either (not the usual ~2s
  pregap offset — genuinely different). Both of `print_plan()`'s
  safeguards (track-count mismatch, duration-outlier flag) caught this
  correctly; trust them over the match. Large/obscure box sets are
  higher-risk for this than mainstream single releases — a third-party
  fallback (Discogs) didn't help either here, since its tracklist grouped
  whole multi-movement works per volume with no clean per-disc/per-track
  structure to map against. When this happens, ask the user for the
  physical liner notes/booklet rather than guessing from online sources.
- **Classical MusicBrainz data can misattribute a work to the wrong
  same-surname composer** — seen on "A Firework of Overtures": the
  recording for "Die Fledermaus" (a Johann Strauss II operetta, common
  knowledge) was linked to the *Richard Strauss* artist entity in raw
  MusicBrainz data — verified via `fetch_release()`'s own
  `artist-credit`, not a bug in this script's handling. Track
  count/duration matched fine (this isn't the wrong-release problem
  above), so `print_plan()`'s safeguards won't catch it — only a
  by-composer sanity read of the tracklist will. Fix confidently when
  the correct composer is well-established, uncontroversial fact (as
  here), not a guess.
- **A plan field can be computed and shown in `print_plan()` without
  reaching the files** unless also threaded into `encode_and_tag()` —
  happened with `genre`. Verify new fields land in a real output file's
  tags, not just the printed plan.
- **Category subfolders (`GENRE_SUBFOLDERS`) only route classical, on
  purpose** — user explicitly said other genres should stay flat. Don't
  add more entries (Jazz, Soundtrack, etc.) without being asked, even
  though the mechanism generalizes trivially. `find_fuzzy_duplicate()`
  already searches inside every configured subfolder, not just
  `MUSIC_ROOT` directly.

## Do NOT

- Commit or push without the user explicitly asking — they review and
  commit this repo themselves.
- Auto-retag or rename folders in the existing (pre-2026) library based
  on fresh MusicBrainz text-search matches — those files were matched by
  exact DiscID at rip time, which a text search can't reproduce reliably.
  If asked to audit the existing library, do it DiscID-based and
  read-only (report a diff, don't write) unless told otherwise.
- Add confirmation prompts beyond the one plan review before `--yes`.
  The point of this tool is one high-level checkpoint per CD, not a
  prompt per track or per pipeline stage.
