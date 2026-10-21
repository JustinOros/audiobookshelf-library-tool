# book-library-tool

Renames audiobook and ebook libraries into a layout Audiobookshelf reads correctly, and cleans up broken characters in names and audio tags.

```
Author/Series/Vol N - Title/Title.ext
Author/Title/Title.ext
```

Multi-file books become `Title - Part 1.m4b`, `Title - Part 2.m4b`.

## Usage

```
python3 book-library-tool.py
python3 book-library-tool.py -t
python3 book-library-tool.py -y /Volumes/Audiobooks
python3 book-library-tool.py --fix-tags -t
```

| Option | Description |
| --- | --- |
| `path` | Share or folder to scan. Prompts with a share picker if omitted |
| `-t`, `--test` | Dry run, prints the plan and changes nothing |
| `-y`, `--yes` | Apply without the confirmation prompt |
| `--fix-tags` | Only clean audio file tags, then exit |
| `--no-lookup` | Skip the Audible lookup for missing series info |

With no path, the picker lists mounted shares. If any share name contains "book", only those are shown.

Run the tool before adding new books to Audiobookshelf. Audiobookshelf reads titles when it first adds a book and does not overwrite them on a re-scan, so clean books go in clean.

## How it works

Series and volume info comes from, in order:

1. File tags (m4b, m4a, mp4, mp3, flac) and epub metadata (Calibre and EPUB3 series tags)
2. Folder and file names
3. Audible catalog lookup

Cover images and other files next to a book move with it, and emptied folders are removed. Books that cannot be resolved are skipped and listed under "Needs attention".

PDF, MOBI and AZW3 files are renamed from folder and file names only.

## Broken characters

Mac SMB mounts and some tools replace characters such as the colon with private-use or look-alike characters. Audiobookshelf shows these as empty boxes.

- In file and folder names they become ` - `.
- In audio tags they are changed back to the real character, for example `Dune 2: Dune Messiah`.

A normal run does both in one pass, with a single confirmation. `--fix-tags` runs only the tag cleanup.

## Setup

Python 3 only. On first run the tool creates its own environment in `~/.book-library-tool` and installs `mutagen` for reading and writing tags.

Every rename and tag change is logged to `~/.book-library-tool/renames.log` with the original value.

Audio-only MP4 files are treated as audiobooks, so a video MP4 in the share would be too. Use `-t` first.
