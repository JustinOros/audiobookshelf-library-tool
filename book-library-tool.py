#!/usr/bin/env python3
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

APP_DIR = os.path.join(os.path.expanduser("~"), ".book-library-tool")
VENV_DIR = os.path.join(APP_DIR, "venv")
CACHE_FILE = os.path.join(APP_DIR, "lookup-cache.json")
LOG_FILE = os.path.join(APP_DIR, "renames.log")

AUDIO_EXT = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac"}
BOOK_EXT = {".epub", ".pdf", ".mobi", ".azw3"}
MEDIA_EXT = AUDIO_EXT | BOOK_EXT
SINGLE_FILE_AUDIO = {".m4b", ".m4a"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_DIRS = {"lost+found", "System Volume Information", "$RECYCLE.BIN"}

VOL_RE = re.compile(
    r"^(?:(?:vol(?:ume)?|book|bk)\.?\s*|#\s*)?(\d{1,3}(?:\.\d+)?)\s*[-._:]\s*(.+)$",
    re.I,
)
PART_RE = re.compile(
    r"[\s._-]*[\(\[]?\s*(?:part|pt|disc|disk|cd)\s*\d+\s*[\)\]]?$", re.I
)
JUNK_TITLE_RE = re.compile(
    r"\s*[\(\[]\s*(?:unabridged|abridged|audiobook|dramatized adaptation)\s*[\)\]]\s*$",
    re.I,
)


def bootstrap():
    try:
        import mutagen
        return
    except ImportError:
        pass
    if os.environ.get("BLT_BOOTSTRAPPED"):
        return
    if os.name == "nt":
        py = os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        py = os.path.join(VENV_DIR, "bin", "python3")
    try:
        if not os.path.exists(py):
            print("Setting up environment in " + APP_DIR)
            os.makedirs(APP_DIR, exist_ok=True)
            subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])
            subprocess.check_call([py, "-m", "pip", "install", "-q", "mutagen"])
        env = dict(os.environ, BLT_BOOTSTRAPPED="1")
        if os.name == "nt":
            sys.exit(subprocess.call([py] + sys.argv, env=env))
        os.execve(py, [py] + sys.argv, env)
    except Exception as exc:
        print("Could not set up mutagen (" + str(exc) + "), continuing without tag reading")


bootstrap()

try:
    import mutagen
except ImportError:
    mutagen = None


def nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def first(v):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    v = str(v).strip()
    return v or None


def parse_index(v):
    if v is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    if not m:
        return None
    n = float(m.group(0))
    return str(int(n)) if n == int(n) else str(n)


def strip_part(s):
    return PART_RE.sub("", s).strip()


def clean_title(t):
    if not t:
        return None
    t = JUNK_TITLE_RE.sub("", t)
    t = re.sub(r"\s*-\s*unabridged$", "", t, flags=re.I)
    return t.strip() or None


def clean_author(name):
    if not name:
        return None
    name = re.split(r"\s*(?:;|&|\band\b)\s*", name)[0].strip()
    if name.count(",") == 1:
        a, b = [p.strip() for p in name.split(",")]
        if a and b and " " not in a and " " not in b:
            return b + " " + a
        return a or None
    if "," in name:
        name = name.split(",")[0].strip()
    return name or None


MAPPED_CHARS = dict.fromkeys(range(0xF020, 0xF028), None)
MAPPED_CHARS[0xF022] = " - "
MAPPED_CHARS[0xFF1A] = " - "
MAPPED_CHARS[0xA789] = " - "


def safe(name):
    name = name.translate(MAPPED_CHARS)
    name = re.sub(r"[\u0000-\u001f\u007f]", "", name)
    name = name.replace(":", " - ")
    name = re.sub(r'[\\/*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\s+-$", "", name)
    return name.rstrip(". ")


def read_audio_tags(path):
    info = {"title": None, "author": None, "album": None, "series": None, "index": None}
    if mutagen is None:
        return info
    try:
        f = mutagen.File(path)
        if f is None or f.tags is None:
            return info
        tags = f.tags
        if type(tags).__name__ == "MP4Tags":
            def g(k):
                return first(tags.get(k))
            free = {}
            for k in tags.keys():
                if str(k).startswith("----:"):
                    free[str(k).split(":")[-1].upper()] = first(tags[k])
            info["title"] = g("\xa9nam")
            info["author"] = g("\xa9ART") or g("aART")
            info["album"] = g("\xa9alb")
            info["series"] = (
                g("\xa9mvn") or free.get("SERIES") or free.get("SERIES_NAME")
            )
            info["index"] = (
                g("\xa9mvi")
                or free.get("SERIES-PART")
                or free.get("SERIES_PART")
                or free.get("PART")
            )
        elif hasattr(tags, "getall"):
            def g(*names):
                for want in names:
                    for key in tags.keys():
                        if key.upper() == want.upper():
                            return first(getattr(tags[key], "text", None))
                return None
            info["title"] = g("TIT2")
            info["author"] = g("TPE1", "TPE2")
            info["album"] = g("TALB")
            info["series"] = g("MVNM", "TXXX:SERIES", "TXXX:SERIES_NAME")
            info["index"] = g(
                "MVIN", "TXXX:SERIES-PART", "TXXX:SERIES_PART", "TXXX:PART"
            )
        else:
            low = {str(k).lower(): v for k, v in tags.items()}
            info["title"] = first(low.get("title"))
            info["author"] = first(low.get("artist") or low.get("albumartist"))
            info["album"] = first(low.get("album"))
            info["series"] = first(low.get("series"))
            info["index"] = first(
                low.get("series-part") or low.get("seriespart") or low.get("part")
            )
    except Exception:
        pass
    return info


def read_epub(path):
    info = {"title": None, "author": None, "album": None, "series": None, "index": None}
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            opf = ET.fromstring(z.read(rootfile.get("full-path")))
    except Exception:
        return info

    def text(tag):
        el = opf.find(".//{*}" + tag)
        return first(el.text) if el is not None and el.text else None

    info["title"] = text("title")
    info["author"] = text("creator")
    collections = []
    positions = {}
    for el in opf.iter():
        if not isinstance(el.tag, str) or el.tag.split("}")[-1] != "meta":
            continue
        name = el.get("name")
        prop = el.get("property")
        if name == "calibre:series":
            info["series"] = first(el.get("content"))
        elif name == "calibre:series_index":
            info["index"] = first(el.get("content"))
        elif prop == "belongs-to-collection":
            collections.append((el.get("id"), first(el.text)))
        elif prop == "group-position":
            positions[el.get("refines")] = first(el.text)
    if not info["series"] and collections:
        cid, cname = collections[0]
        info["series"] = cname
        if cid and positions.get("#" + cid):
            info["index"] = positions["#" + cid]
    return info


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_cache(cache):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def audible_lookup(title, author, cache):
    key = norm(title) + "|" + norm(author)
    if key in cache:
        return cache[key]
    params = {
        "title": title,
        "num_results": "10",
        "response_groups": "series,contributors",
    }
    if author:
        params["author"] = author
    url = "https://api.audible.com/1.0/catalog/products?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "book-library-tool"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    time.sleep(0.25)
    result = None
    best = 0.0
    for p in data.get("products", []):
        names = [a.get("name", "") for a in p.get("authors", [])]
        if author and not any(author.split()[-1].lower() in n.lower() for n in names):
            continue
        score = difflib.SequenceMatcher(
            None, norm(clean_title(p.get("title", ""))), norm(title)
        ).ratio()
        if score < 0.85 or score <= best:
            continue
        best = score
        entry = {"series": None, "index": None, "author": names[0] if names else None}
        series = p.get("series") or []
        if series:
            entry["series"] = series[0].get("title")
            entry["index"] = parse_index(series[0].get("sequence"))
        result = entry
    cache[key] = result
    return result


class Group:
    def __init__(self, directory, items, exclusive, allfiles):
        self.directory = directory
        self.items = items
        self.exclusive = exclusive
        self.allfiles = allfiles


class Plan:
    def __init__(self, group):
        self.group = group
        self.author = None
        self.series = None
        self.index = None
        self.title = None
        self.moves = []
        self.status = "pending"
        self.note = ""


def skip_dir(name):
    return name.startswith((".", "@", "#")) or name in SKIP_DIRS


def scan(root):
    media_dirs = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        media = [
            f
            for f in filenames
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in MEDIA_EXT
        ]
        if media:
            media_dirs[dirpath] = (sorted(media, key=nat), sorted(filenames, key=nat))
    ancestors = set()
    for d in media_dirs:
        cur = os.path.dirname(d)
        while True:
            ancestors.add(cur)
            if cur == root or cur == os.path.dirname(cur):
                break
            cur = os.path.dirname(cur)
    groups = []
    for d in sorted(media_dirs, key=nat):
        media, allfiles = media_dirs[d]
        buckets = {}
        for f in media:
            ext = os.path.splitext(f)[1].lower()
            stem = os.path.splitext(f)[0]
            path = os.path.join(d, f)
            if ext in AUDIO_EXT:
                tags = read_audio_tags(path)
                if tags["album"]:
                    key = ("a", norm(tags["album"]))
                elif d == root or ext in SINGLE_FILE_AUDIO:
                    key = ("a", norm(strip_part(stem)))
                else:
                    key = ("a", "")
            else:
                tags = (
                    read_epub(path)
                    if ext == ".epub"
                    else {"title": None, "author": None, "album": None, "series": None, "index": None}
                )
                key = ("b", norm(strip_part(stem)))
            buckets.setdefault(key, []).append((path, ext, tags))
        exclusive = d != root and d not in ancestors and len(buckets) == 1
        for items in buckets.values():
            groups.append(Group(d, items, exclusive, allfiles))
    return groups


def path_guess(root, g):
    rel = os.path.relpath(g.directory, root)
    parts = [] if rel == "." else rel.split(os.sep)
    first_stem = os.path.splitext(os.path.basename(g.items[0][0]))[0]
    if g.exclusive and parts:
        name = parts[-1]
        ctx = parts[:-1]
    else:
        name = first_stem
        ctx = parts
    index = None
    m = VOL_RE.match(name)
    if m:
        index, name = m.group(1), m.group(2)
    name = strip_part(name)
    return {
        "title": name.strip() or None,
        "author": ctx[0] if ctx else None,
        "series": ctx[-1] if len(ctx) >= 2 else None,
        "index": parse_index(index),
    }


def resolve(root, g, use_lookup, cache):
    plan = Plan(g)
    tags = [t for _, _, t in g.items]

    def pick(field):
        for t in tags:
            if t.get(field):
                return t[field]
        return None

    audio_count = sum(1 for _, e, _ in g.items if e in AUDIO_EXT)
    if audio_count > 1:
        title = pick("album") or pick("title")
    else:
        title = pick("title") or pick("album")
    guess = path_guess(root, g)
    title = strip_part(clean_title(title)) if title else None
    title = title or clean_title(guess["title"])
    author = clean_author(pick("author")) or clean_author(guess["author"])
    series = pick("series") or guess["series"]
    index = parse_index(pick("index")) or guess["index"]
    if series and norm(series) == norm(title) and index is None:
        series = None
    if use_lookup and title and (series is None or index is None):
        found = audible_lookup(title, author, cache)
        if found:
            if series is None and found["series"]:
                series = found["series"]
                index = index or found["index"]
            elif (
                series
                and index is None
                and found["series"]
                and norm(found["series"]) == norm(series)
            ):
                index = found["index"]
            if author is None:
                author = clean_author(found["author"])
    if not author:
        plan.status = "skip"
        plan.note = "no author found in tags, path or lookup"
        plan.title = title
        return plan
    if not title:
        plan.status = "skip"
        plan.note = "no title found"
        return plan
    plan.author, plan.series, plan.index, plan.title = author, series, index, title
    if series and index is None:
        plan.note = "series found but no volume number"
    return plan


def build_moves(root, plan):
    g = plan.group
    folder = safe(
        ("Vol " + plan.index + " - " if plan.series and plan.index is not None else "")
        + plan.title
    )
    parts = [safe(plan.author)]
    if plan.series:
        parts.append(safe(plan.series))
    parts.append(folder)
    target_dir = os.path.join(root, *parts)
    audio = sorted([i for i in g.items if i[1] in AUDIO_EXT], key=lambda i: nat(os.path.basename(i[0])))
    others = [i for i in g.items if i[1] not in AUDIO_EXT]
    moves = []
    base = safe(plan.title)
    for n, (path, ext, _) in enumerate(audio, 1):
        name = base + ext if len(audio) == 1 else base + " - Part " + str(n) + ext
        moves.append((path, os.path.join(target_dir, name)))
    for path, ext, _ in others:
        moves.append((path, os.path.join(target_dir, base + ext)))
    moved = {m[0] for m in moves}
    if g.exclusive:
        for f in g.allfiles:
            p = os.path.join(g.directory, f)
            if p in moved or f in JUNK_FILES:
                continue
            if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                continue
            moves.append((p, os.path.join(target_dir, f)))
    else:
        stems = {}
        for src, dst in list(moves):
            stems[os.path.splitext(os.path.basename(src))[0]] = os.path.splitext(dst)[0]
        for f in g.allfiles:
            stem, ext = os.path.splitext(f)
            p = os.path.join(g.directory, f)
            if p in moved or ext.lower() in MEDIA_EXT or stem not in stems:
                continue
            moves.append((p, stems[stem] + ext))
    return moves


def candidate_shares():
    bases = []
    if sys.platform == "darwin":
        bases = ["/Volumes"]
    elif os.name == "nt":
        bases = []
    else:
        bases = ["/mnt/user" if os.path.isdir("/mnt/user") else "/mnt", "/mnt/disks", "/media"]
        user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if user:
            bases.append("/run/media/" + user)
    found = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base), key=nat):
            p = os.path.join(base, name)
            if name.startswith(".") or not os.path.isdir(p):
                continue
            if os.path.realpath(p) == "/":
                continue
            found.append(p)
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = letter + ":\\"
            if os.path.isdir(p):
                found.append(p)
    seen = set()
    unique = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def choose_share():
    shares = candidate_shares()
    book = [s for s in shares if "book" in os.path.basename(s.rstrip("\\/")).lower()]
    shown = book if book else shares
    print("Select a share to scan:")
    for i, s in enumerate(shown, 1):
        print("  " + str(i) + ") " + s)
    print("  0) Enter a path")
    while True:
        try:
            choice = input("> ").strip()
        except EOFError:
            sys.exit(1)
        if choice == "0" or (not shown and choice == ""):
            try:
                return input("Path: ").strip().strip("'\"")
            except EOFError:
                sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(shown):
            return shown[int(choice) - 1]
        print("Pick a number from the list")


def do_move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    same = False
    try:
        same = os.path.exists(dst) and os.path.samefile(src, dst)
    except OSError:
        pass
    if same:
        tmp = src + ".blt-tmp"
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        shutil.move(src, dst)


def prune(d, root):
    while d != root and d.startswith(root) and d != os.path.dirname(d):
        try:
            entries = os.listdir(d)
            if any(e not in JUNK_FILES for e in entries):
                return
            for e in entries:
                os.remove(os.path.join(d, e))
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def rel(p, root):
    return os.path.relpath(p, root).replace(os.sep, "/")


def main():
    ap = argparse.ArgumentParser(
        prog="book-library-tool.py",
        description="Rename audiobook and ebook libraries to Author/Series/Vol N - Title/Title.ext",
    )
    ap.add_argument("path", nargs="?", help="share or folder to scan (prompts if omitted)")
    ap.add_argument("-t", "--test", action="store_true", help="dry run, change nothing")
    ap.add_argument("-y", "--yes", action="store_true", help="apply without confirmation")
    ap.add_argument("--no-lookup", action="store_true", help="do not query Audible for missing series info")
    args = ap.parse_args()

    root = args.path or choose_share()
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        print("Not a folder: " + root)
        sys.exit(1)

    print(("DRY RUN: " if args.test else "") + "Scanning " + root)
    if mutagen is None:
        print("mutagen not available, reading folder names and Audible only")
    groups = scan(root)
    if not groups:
        print("No audio or ebook files found")
        return
    print("Found " + str(len(groups)) + " books, reading metadata")

    cache = load_cache()
    plans = []
    for i, g in enumerate(groups, 1):
        plan = resolve(root, g, not args.no_lookup, cache)
        if plan.status == "pending":
            plan.moves = build_moves(root, plan)
        plans.append(plan)
        if not args.no_lookup and i % 10 == 0:
            save_cache(cache)
    save_cache(cache)

    dst_owner = {}
    for p in plans:
        if p.status != "pending":
            continue
        for _, dst in p.moves:
            dst_owner.setdefault(dst.lower(), []).append(p)
    for owners in dst_owner.values():
        if len(owners) > 1:
            for p in owners:
                if p.status == "pending":
                    p.status = "skip"
                    p.note = "target collides with another book"
    for p in plans:
        if p.status != "pending":
            continue
        for src, dst in p.moves:
            if src == dst:
                continue
            if os.path.exists(dst):
                try:
                    if os.path.samefile(src, dst):
                        continue
                except OSError:
                    pass
                p.status = "skip"
                p.note = "target already exists: " + rel(dst, root)
                break
        if p.status == "pending":
            changed = any(s != d for s, d in p.moves)
            p.status = "change" if changed else "ok"

    changes = [p for p in plans if p.status == "change"]
    unchanged = [p for p in plans if p.status == "ok"]
    skipped = [p for p in plans if p.status == "skip"]
    flagged = [p for p in plans if p.status in ("change", "ok") and p.note]

    for p in changes:
        print("")
        for src, dst in p.moves:
            if src != dst:
                print("  " + rel(src, root))
                print("    -> " + rel(dst, root))
    print("")
    print(
        str(len(changes)) + " to rename, "
        + str(len(unchanged)) + " already correct, "
        + str(len(skipped)) + " skipped"
    )
    if skipped or flagged:
        print("")
        print("Needs attention:")
        for p in skipped + flagged:
            name = rel(p.group.items[0][0], root)
            print("  " + name + ": " + p.note)

    if args.test:
        print("")
        print("Dry run, nothing was changed")
        return
    if not changes:
        return
    if not args.yes:
        try:
            answer = input("\nApply " + str(len(changes)) + " renames in " + root + "? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Cancelled")
            return

    os.makedirs(APP_DIR, exist_ok=True)
    done = 0
    errors = 0
    src_dirs = set()
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("# " + time.strftime("%Y-%m-%d %H:%M:%S") + " " + root + "\n")
        for p in changes:
            try:
                for src, dst in p.moves:
                    if src == dst:
                        continue
                    src_dirs.add(os.path.dirname(src))
                    do_move(src, dst)
                    log.write(src + "\t" + dst + "\n")
                done += 1
            except Exception as exc:
                errors += 1
                print("Failed: " + rel(p.group.items[0][0], root) + ": " + str(exc))
    for d in sorted(src_dirs, key=lambda x: -len(x)):
        prune(d, root)
    print("Renamed " + str(done) + " books" + (", " + str(errors) + " failed" if errors else ""))
    print("Log: " + LOG_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("")
        sys.exit(130)
