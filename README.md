# book-library-tool

Renames audiobook and ebook libraries into a layout Audiobookshelf reads correctly.

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
```

| Option | Description |
| --- | --- |
| `path` | Share or folder to scan. Prompts with a share picker if omitted |
| `-t`, `--test` | Dry run, prints the plan and changes nothing |
| `-y`, `--yes` | Apply without the confirmation prompt |
| `--no-lookup` | Skip the Audible lookup for missing series info |

With no path, the picker lists mounted shares. If any share name contains "book", only those are shown.

## How it works

Series and volume info comes from, in order:

1. File tags (m4b, mp3, flac) and epub metadata (Calibre and EPUB3 series tags)
2. Folder and file names
3. Audible catalog lookup

Cover images and other files next to a book move with it, and emptied folders are removed. Books that cannot be resolved are skipped and listed under "Needs attention".

PDF, MOBI and AZW3 files are renamed from folder and file names only.

## Setup

Python 3 only. On first run the tool creates its own environment in `~/.book-library-tool` and installs `mutagen` for reading tags.

Every rename is logged to `~/.book-library-tool/renames.log` with the original path.
