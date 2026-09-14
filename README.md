# cd-importer

Rips audio CDs into `~/Music` matching the existing library's folder and
ID3 tagging convention (established years ago by Banshee), with metadata
auto-fetched from MusicBrainz. By default it identifies the disc itself
by MusicBrainz DiscID (no typing artist/album needed — the same method
the original library was ripped with); `--artist`/`--album` search by
name instead, and is the automatic fallback when a disc isn't in
MusicBrainz's DiscID database (coverage is partial).

## Library convention this reproduces

- `~/Music/<Album Title>/` — flat, no artist-level folder
- `~/Music/Classical Music/<Album Title>/` for classical (genre-based;
  see `GENRE_SUBFOLDERS` — only classical routes this way for now)
- `~/Music/<Album Title>/Disc N/` for multi-disc releases (cover art stays
  at the album root)
- Track files: `NN. Track Title.mp3` (zero-padded)
- `cover.jpg` at the album root
- ID3v2: `TIT2` title, `TPE1` artist (per-track, so compilations/
  soundtracks get each track's own performer, not a blanket "Various
  Artists"), `TALB` album, `TRCK` "n/total", `TPOS` "disc/total", `TCON`
  genre

## Setup

```
sudo apt install -y cdparanoia python3-musicbrainzngs libdiscid0
pip install --user --break-system-packages discid
```

(`ffmpeg` and `python3-mutagen` are assumed already present. `libdiscid0`
+ the `discid` pip package are only needed for automatic DiscID
identification — the script still works with `--artist`/`--album` search
without them.)

**Note:** if `ffmpeg` is installed via snap, it can't see files another
process writes to `/tmp` (its own private view). Ripping work therefore
happens under `./.work`, under `$HOME`.

## Usage

```
# Typical import — auto-identifies the disc, prompts once with the
# proposed tracklist before ripping
python3 rip_cd.py

# Search by name instead (also the automatic fallback if DiscID lookup
# finds no match)
python3 rip_cd.py --artist "Jay-Z" --album "The Black Album"

# Check what MusicBrainz would match, without ripping
python3 rip_cd.py --artist "X" --album "Y" --list-candidates

# Review/edit metadata before committing to a rip
python3 rip_cd.py --artist "X" --album "Y" --dump-tracklist plan.json
#   ...edit plan.json...
python3 rip_cd.py --artist "X" --album "Y" --tracklist-json plan.json

# Multi-disc set, ripping disc 2
python3 rip_cd.py --artist "X" --album "Y" --disc 2

# Skip the confirmation prompt (e.g. when reviewed already by an agent)
python3 rip_cd.py --artist "X" --album "Y" --yes
```

Other flags: `--mbid <release-id>` (pin an exact MusicBrainz release),
`--genre` (override), `--subfolder "Name"` (override the category
subfolder, or pass `""` to force flat placement), `--boxset PATH` /
`--save-to-boxset PATH` / `--volume-label` (see Box-set cache below),
`--bitrate 320k` / `--quality 0-9` (default is V0 ~245kbps VBR),
`--replace` (overwrite an existing import for that album/disc),
`--no-eject` (leave the disc in the drive when done).

Logs for every run are written to `logs/`.

## Duplicate detection & bitrate upgrades

Before ripping, the script checks whether the album already exists —
both an exact folder-name match and a fuzzy match (differently spelled/
punctuated/cased title, e.g. MusicBrainz's "The Dark Side of the Moon"
vs. an existing "Dark Side Of The Moon" folder). If a match is found and
its files are below ~192kbps (e.g. the original library's 128kbps CBR
rips), it tells you so and offers to replace it as a quality upgrade
rather than just cancel-or-duplicate.

## Box-set cache (for multi-CD compilations)

Large budget box sets (e.g. a 50-CD classical anthology) are often
poorly catalogued on MusicBrainz/Discogs per individual disc — wrong
track counts, wrong durations, or no data at all for a given disc. Once
you've identified a disc by hand, save it so every other disc from the
*same* box set is identified for free afterward, straight from the
physical disc's own table of contents — no network lookup, no risk of
a bad match:

```
# First disc from a set: identify however you can (manual/indie
# process below), confirm, then save it as you rip
python3 rip_cd.py --tracklist-json plan.json --save-to-boxset boxsets/my-set.json --yes

# Every later disc from the same set: instant, offline match by TOC
# fingerprint (track count + per-track durations) if it's been seen before
python3 rip_cd.py --boxset boxsets/my-set.json
```

`--boxset` falls through to normal identification if the disc isn't in
the cache yet — nothing blocks you from importing an unseen disc from
the set. `boxsets/*.json` are plain JSON, safe to hand-edit (add a
`default_cover_url` at the top level if the set only has one cover for
the whole box, common for budget compilations — see `CLAUDE.md`).
