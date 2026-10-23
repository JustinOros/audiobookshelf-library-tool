# audiobookshelf-library-tool

Cleans up and renames audiobook and ebook libraries so Audiobookshelf reads them correctly the first time.

It organizes books into this layout:

```
Author/Series/Vol N - Title/Title.ext
Author/Title/Title.ext
```

Multi-file books become `Title - Part 1.m4b`, `Title - Part 2.m4b`. Books split into separate folders such as `Title (Part 1 of 2)` and `Title (Part 2 of 2)` are merged into one folder.

## Usage

```
python3 audiobookshelf-library-tool.py
python3 audiobookshelf-library-tool.py -t
python3 audiobookshelf-library-tool.py -y /Volumes/Audiobooks
python3 audiobookshelf-library-tool.py --fix-tags -t
```

| Option | Description |
| --- | --- |
| `path` | Share or folder to scan. Prompts with a share picker if omitted |
| `-t`, `--test` | Dry run, prints the plan and changes nothing |
| `-y`, `--yes` | Apply without the confirmation prompt |
| `--fix-tags` | Only clean audio file tags, then exit |
| `--keep-duplicates` | Do not look for or remove identical duplicate files |
| `--keep-cue` | Do not remove `.cue` files |
| `--keep-unsupported` | Do not remove files Audiobookshelf cannot use |
| `--strip-text TEXT` | Also remove this text from inside epubs. Can be repeated |
| `--no-strip` | Do not remove watermark text from inside epubs |
| `--no-lookup` | Skip the Audible lookup for missing series info |

With no path, the picker lists mounted shares. If any share name contains "book", only those are shown.

Run the tool on new books before they go into Audiobookshelf. Audiobookshelf reads titles when it first adds a book and does not overwrite them on a re-scan, so clean books go in clean. A staging folder on an external drive works well.

## How series and titles are found

In order:

1. File tags (m4b, m4a, mp4, mp3, flac and others) and epub metadata (Calibre and EPUB3 series tags)
2. Folder and file names
3. Audible catalog lookup

Cover images and other files next to a book move with it, and emptied folders are removed. Books that cannot be resolved are skipped and listed under "Needs attention".

PDF, MOBI, AZW3, CBZ and CBR files are renamed from folder and file names only.

## Cleanup

A normal run also cleans up the library. The dry run lists everything first, and there is a single confirmation before anything changes.

**Broken characters.** Mac SMB mounts and some tools replace characters such as the colon with private-use or look-alike characters, which Audiobookshelf shows as empty boxes. In file and folder names they become ` - `. In audio tags they are changed back to the real character, for example `Dune 2: Dune Messiah`.

**Identical duplicates.** Files with byte-identical contents. One copy is always kept, preferring the one already in the `Author/Series/Vol N - Title` layout. Different files for the same book are never removed. If their names collide, they are skipped and listed under "Needs attention".

**.cue files.** Removed only from folders that contain audio files, and never from folders with disc images such as `.bin` or `.iso`.

**Unsupported files.** Anything Audiobookshelf cannot use is removed, such as `.txt`, `.sfv`, `.url`, `.DS_Store` and `._` files. These are always kept:

- Audio and ebook files
- Cover images (`.jpg`, `.jpeg`, `.png`, `.webp`)
- `.nfo`, `.opf`, `metadata.json`, `desc.txt` and `reader.txt`

Audible `.aax`, `.aaxc`, `.aa` and `.m4p` files, archives (`.zip`, `.rar`, `.7z`) and video are never removed. They are listed so you can convert or extract them. Cleanup only happens in folders that contain books, so a ROMs share is left alone.

**Epub watermark text.** `OceanofPDF.com` is removed from inside every epub (book text, metadata and navigation files, case-insensitive). Only epubs that contain it are rewritten, and each rewrite is verified before it replaces the original. Add more strings with `--strip-text`, or skip this step with `--no-strip`.

Deletions and epub text edits cannot be undone. Use `-t` first.

## Setup

Python 3 only. On first run the tool creates its own environment in `~/.audiobookshelf-library-tool` and installs `mutagen` for reading and writing tags.

Every rename, tag change and deletion is logged to `~/.audiobookshelf-library-tool/renames.log` with the original value.
