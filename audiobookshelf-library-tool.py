#!/usr/bin/env python3
import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

APP_DIR = os.path.join(os.path.expanduser("~"), ".audiobookshelf-library-tool")
OLD_APP_DIR = os.path.join(os.path.expanduser("~"), ".book-library-tool")
VENV_DIR = os.path.join(APP_DIR, "venv")
CACHE_FILE = os.path.join(APP_DIR, "lookup-cache.json")
LOG_FILE = os.path.join(APP_DIR, "renames.log")

AUDIO_EXT = {".m4b", ".m4a", ".mp4", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wav", ".wma", ".aiff", ".mka", ".webm", ".webma", ".awb", ".caf"}
BOOK_EXT = {".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr"}
MEDIA_EXT = AUDIO_EXT | BOOK_EXT
KEEP_EXT = {".jpg", ".jpeg", ".png", ".webp", ".opf", ".nfo", ".cue"}
KEEP_NAMES = {"metadata.json", "metadata.abs", "desc.txt", "reader.txt"}
DRM_EXT = {".aax", ".aaxc", ".aa", ".m4p"}
PROTECT_EXT = {".aax", ".aaxc", ".aa", ".m4p", ".zip", ".rar", ".7z", ".tar", ".gz", ".mkv", ".avi", ".mov", ".m4v", ".wmv"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
DISC_EXT = {".bin", ".iso", ".img", ".mdf", ".chd", ".ccd", ".sub"}
SINGLE_FILE_AUDIO = {".m4b", ".m4a", ".mp4"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
STRIP_TEXT = ["OceanofPDF.com"]
TEXT_MEMBER_EXT = {".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx", ".svg", ".css"}
SKIP_DIRS = {"lost+found", "System Volume Information", "$RECYCLE.BIN"}

VOL_RE = re.compile(
    r"^(?:(?:vol(?:ume)?|book|bk)\.?\s*|#\s*)?(\d{1,3}(?:\.\d+)?)\s*[-._:]\s*(.+)$",
    re.I,
)
PART_RE = re.compile(
    r"[\s._-]*[\(\[]?\s*(?:part|pt|disc|disk|cd)\s*\d+(?:\s*of\s*\d+)?\s*[\)\]]?$", re.I
)
SERIES_SUFFIX_RE = re.compile(
    r"\s*[\[\(]\s*([^\]\)\d][^\]\)]*?)[,\s]+(?:book|vol\.?|volume|#)?\s*(\d+(?:\.\d+)?)\s*[\]\)]\s*$", re.I
)
ID_TAIL_RE = re.compile(r"\s*\[(?:B0[0-9A-Z]{8}|[0-9]{9}[0-9Xx])\]\s*$")
ASIN_RE = re.compile(r"\[(B0[0-9A-Z]{8}|[0-9]{9}[0-9Xx])\]")
NOISE_DIRS = {"audiobooks", "audiobook", "books", "ebooks", "library", "audible", "libation", "audio"}
PART_NUM_RE = re.compile(r"(?:part|pt|disc|disk|cd)\s*(\d+)", re.I)
JUNK_TITLE_RE = re.compile(
    r"\s*[\(\[]\s*(?:unabridged|abridged|audiobook|dramatized adaptation)\s*[\)\]]\s*$",
    re.I,
)


def migrate_app_dir():
    if os.path.isdir(OLD_APP_DIR) and not os.path.exists(APP_DIR):
        try:
            os.makedirs(APP_DIR)
            for name in ("lookup-cache.json", "renames.log"):
                src = os.path.join(OLD_APP_DIR, name)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(APP_DIR, name))
        except OSError:
            pass


migrate_app_dir()


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
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def same_path(a, b):
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


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


def split_series_suffix(t):
    m = SERIES_SUFFIX_RE.search(t or "")
    if not m:
        return t, None, None
    name = m.group(1).strip()
    if len(name) < 3 or re.match(r"(?i)^(part|pt|disc|disk|cd|book|vol|volume|unabridged|abridged)\b", name):
        return t, None, None
    return t[: m.start()].rstrip(), name, parse_index(m.group(2))


def clean_title(t, keep_suffix=False):
    if not t:
        return None
    if not keep_suffix:
        t = split_series_suffix(t)[0]
    t = ID_TAIL_RE.sub("", t)
    t = re.sub(r"(?<=\S)_\s+", ": ", t)
    t = JUNK_TITLE_RE.sub("", t)
    t = re.sub(r"\s*-\s*unabridged$", "", t, flags=re.I)
    return t.strip() or None


def clean_author(name):
    if not name or ID_TAIL_RE.search(name):
        return None
    name = re.sub(r"[,\s]+(?:ph\.?\s?d|m\.?d)\.?$", "", name.strip(), flags=re.I)
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


TAG_CHARS = {
    0xF020: '"',
    0xF021: "*",
    0xF022: ":",
    0xF023: "<",
    0xF024: ">",
    0xF025: "?",
    0xF026: "",
    0xF027: "|",
    0xA789: ":",
    0xFF1A: ":",
}


def clean_tag_text(value):
    return value.translate(TAG_CHARS)


def clean_values(values):
    out = []
    found = []
    for v in values:
        if isinstance(v, str):
            c = clean_tag_text(v)
            if c != v:
                found.append((v, c))
            out.append(c)
        else:
            out.append(v)
    return out, found


def tag_fixes(path):
    if mutagen is None:
        return None
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None or f.tags is None:
        return None
    tags = f.tags
    changes = []
    if hasattr(tags, "getall"):
        for key in list(tags.keys()):
            frame = tags[key]
            text = getattr(frame, "text", None)
            if not isinstance(text, list):
                continue
            new, found = clean_values(text)
            if found:
                frame.text = new
                changes.extend((key, o, n) for o, n in found)
    else:
        for key in list(tags.keys()):
            vals = tags[key]
            if not isinstance(vals, list):
                continue
            new, found = clean_values(vals)
            if found:
                tags[key] = new
                changes.extend((str(key), o, n) for o, n in found)
    return f, changes


def collect_tag_fixes(root, exclude=()):
    pending = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        for name in sorted(filenames, key=nat):
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                continue
            path = os.path.join(dirpath, name)
            if path in exclude:
                continue
            scanned += 1
            result = tag_fixes(path)
            if result and result[1]:
                pending.append((path, result[0], result[1]))
    return pending, scanned


def print_tag_fixes(pending, root):
    for path, _, changes in pending:
        print("")
        print("  " + rel(path, root))
        for key, old, new in changes:
            print("    " + key + ": " + repr(old) + " -> " + repr(new))


def apply_tag_fixes(pending, root):
    os.makedirs(APP_DIR, exist_ok=True)
    done = 0
    errors = 0
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("# " + time.strftime("%Y-%m-%d %H:%M:%S") + " tags " + root + "\n")
        for path, f, changes in pending:
            try:
                f.save()
                for key, old, new in changes:
                    log.write("TAG\t" + path + "\t" + key + "\t" + repr(old) + "\t" + repr(new) + "\n")
                done += 1
            except Exception as exc:
                errors += 1
                print("Failed: " + rel(path, root) + ": " + str(exc))
    return done, errors


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def keep_rank(root, path):
    parts = os.path.relpath(os.path.dirname(path), root).split(os.sep)
    has_vol = any(VOL_RE.match(p) for p in parts)
    return (0 if has_vol else 1, -len(parts), nat(os.path.relpath(path, root)))


def find_duplicates(root):
    by_size = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if name.startswith(".") or ext not in MEDIA_EXT:
                continue
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > 0:
                by_size.setdefault((ext, size), []).append(path)
    groups = []
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_hash = {}
        for path in paths:
            try:
                by_hash.setdefault(file_digest(path), []).append(path)
            except OSError:
                continue
        for same in by_hash.values():
            if len(same) > 1:
                same = sorted(same, key=lambda x: keep_rank(root, x))
                groups.append((same[0], same[1:]))
    groups.sort(key=lambda g: nat(g[0]))
    return groups


def find_unsupported(root):
    media_dirs = set()
    listing = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        listing.append((dirpath, filenames))
        if any(
            not f.startswith(".") and os.path.splitext(f)[1].lower() in MEDIA_EXT
            for f in filenames
        ):
            media_dirs.add(dirpath)
    eligible = set()
    for d in media_dirs:
        cur = d
        while True:
            eligible.add(cur)
            if cur == root or cur == os.path.dirname(cur):
                break
            cur = os.path.dirname(cur)
    remove = []
    protected = []
    for dirpath, filenames in listing:
        if dirpath not in eligible:
            protected.extend(
                os.path.join(dirpath, f)
                for f in sorted(filenames, key=nat)
                if os.path.splitext(f)[1].lower() in DRM_EXT
            )
            continue
        exts = {os.path.splitext(f)[1].lower() for f in filenames}
        if exts & DISC_EXT:
            continue
        for f in sorted(filenames, key=nat):
            low = f.lower()
            ext = os.path.splitext(f)[1].lower()
            path = os.path.join(dirpath, f)
            if low in JUNK_NAMES or f.startswith("._"):
                remove.append(path)
            elif ext in MEDIA_EXT or ext in KEEP_EXT or low in KEEP_NAMES:
                continue
            elif ext in PROTECT_EXT:
                protected.append(path)
            elif f.startswith("."):
                continue
            else:
                remove.append(path)
    return remove, protected


def find_cues(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        cues = [f for f in filenames if f.lower().endswith(".cue") and not f.startswith(".")]
        if not cues:
            continue
        exts = {os.path.splitext(f)[1].lower() for f in filenames}
        if exts & DISC_EXT or not exts & AUDIO_EXT:
            continue
        found.extend(os.path.join(dirpath, f) for f in sorted(cues, key=nat))
    return found


def run_fix_tags(root, test, yes):
    if mutagen is None:
        print("mutagen is required for --fix-tags")
        sys.exit(1)
    pending, scanned = collect_tag_fixes(root)
    print_tag_fixes(pending, root)
    print("")
    print(str(len(pending)) + " of " + str(scanned) + " audio files need tag fixes")
    if test:
        print("")
        print("Dry run, nothing was changed")
        return
    if not pending:
        return
    if not yes:
        try:
            answer = input("\nWrite tag fixes to " + str(len(pending)) + " files in " + root + "? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Cancelled")
            return
    done, errors = apply_tag_fixes(pending, root)
    print("Fixed " + str(done) + " files" + (", " + str(errors) + " failed" if errors else ""))
    print("Log: " + LOG_FILE)


def epub_strip_counts(path, patterns):
    counts = {}
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if os.path.splitext(info.filename)[1].lower() not in TEXT_MEMBER_EXT:
                    continue
                data = z.read(info.filename)
                for pat in patterns:
                    n = len(pat.findall(data))
                    if n:
                        key = pat.pattern.decode("utf-8").replace("\\", "")
                        counts[key] = counts.get(key, 0) + n
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return {}
    return counts


def collect_epub_strips(root, texts, exclude=()):
    patterns = [re.compile(re.escape(t.encode("utf-8")), re.I) for t in texts]
    pending = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        for name in sorted(filenames, key=nat):
            if name.startswith(".") or os.path.splitext(name)[1].lower() != ".epub":
                continue
            path = os.path.join(dirpath, name)
            if path in exclude:
                continue
            scanned += 1
            counts = epub_strip_counts(path, patterns)
            if counts:
                pending.append((path, counts))
    return pending, scanned, patterns


def rewrite_epub(path, patterns):
    tmp = path + ".strip-tmp"
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if os.path.splitext(info.filename)[1].lower() in TEXT_MEMBER_EXT:
                    for pat in patterns:
                        data = pat.sub(b"", data)
                zout.writestr(info, data, compress_type=info.compress_type)
        with zipfile.ZipFile(tmp) as check:
            if check.testzip() is not None:
                raise OSError("rewritten epub failed verification")
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def print_epub_strips(pending, root):
    for path, counts in pending:
        print("  " + rel(path, root) + ": " + ", ".join(str(n) + "x " + k for k, n in counts.items()))


def apply_epub_strips(pending, patterns, root):
    done = 0
    errors = 0
    os.makedirs(APP_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("# " + time.strftime("%Y-%m-%d %H:%M:%S") + " strip text " + root + "\n")
        for path, counts in pending:
            try:
                rewrite_epub(path, patterns)
                log.write("STRIP\t" + path + "\t" + json.dumps(counts) + "\n")
                done += 1
            except Exception as exc:
                errors += 1
                print("Failed: " + rel(path, root) + ": " + str(exc))
    return done, errors


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


def title_core(t):
    return norm(re.split(r"\s+-\s+|:|\(|\[", t or "")[0])


def title_score(a, b, loose=False):
    score = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    if loose:
        ca, cb = title_core(a), title_core(b)
        if ca and cb:
            score = max(score, difflib.SequenceMatcher(None, ca, cb).ratio())
            short, long_ = sorted((ca, cb), key=len)
            if len(short.split()) >= 2 and len(short) >= 8 and long_.startswith(short + " "):
                score = max(score, 0.9)
    return score


def audible_search(params, title, author):
    base = {"num_results": "10", "response_groups": "series,contributors,product_desc"}
    base.update(params)
    if author:
        base["author"] = author
    url = "https://api.audible.com/1.0/catalog/products?" + urllib.parse.urlencode(base)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "audiobookshelf-library-tool"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None, False
    time.sleep(0.25)
    result = None
    best = 0.0
    for p in data.get("products", []):
        names = [a.get("name", "") for a in p.get("authors", [])]
        if author and not any(author.split()[-1].lower() in n.lower() for n in names):
            continue
        main_title = clean_title(p.get("title", "")) or ""
        sub = (p.get("subtitle") or "").strip()
        score = title_score(main_title, title, loose=not author)
        if sub:
            score = max(score, title_score(main_title + ": " + sub, title, loose=not author))
        if score < 0.85 or score <= best:
            continue
        best = score
        entry = {"series": None, "index": None, "author": names[0] if names else None}
        series = p.get("series") or []
        if series:
            entry["series"] = series[0].get("title")
            entry["index"] = parse_index(series[0].get("sequence"))
        result = entry
    return result, True


def audible_lookup(title, author, cache):
    key = "v4|" + norm(title) + "|" + norm(author)
    if key in cache:
        return cache[key]
    result, ok = audible_search({"title": title}, title, author)
    if not ok:
        return None
    if result is None and not author:
        core = re.split(r"\s+-\s+|:|\(|\[", title)[0].strip()
        if core:
            result, ok = audible_search({"keywords": core}, title, author)
            if not ok:
                return None
    cache[key] = result
    return result


def audible_by_asin(asin, cache):
    key = "asin2:" + asin
    if cache.get(key) is not None:
        return cache[key]
    url = "https://api.audible.com/1.0/catalog/products/" + asin + "?" + urllib.parse.urlencode(
        {"response_groups": "series,contributors,product_desc"}
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "audiobookshelf-library-tool"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    time.sleep(0.25)
    p = data.get("product") or {}
    result = None
    if p:
        names = [a.get("name", "") for a in p.get("authors", []) if a.get("name")]
        series = p.get("series") or []
        full = p.get("title") or ""
        sub = (p.get("subtitle") or "").strip()
        series_name = series[0].get("title") if series else None
        series_sub = bool(sub) and (
            bool(re.search(r"\bbook\s+\w+$", sub, re.I))
            or bool(series_name and norm(series_name) in norm(sub))
        )
        if full and sub and not series_sub:
            full = full + ": " + sub
        result = {"series": None, "index": None, "author": names[0] if names else None, "title": full or None}
        if series:
            result["series"] = series_name
            result["index"] = parse_index(series[0].get("sequence"))
    if result is not None:
        cache[key] = result
    return result


def find_asin(g):
    names = [os.path.basename(g.directory)]
    names.extend(os.path.basename(path) for path, _, _ in g.items)
    for name in names:
        m = ASIN_RE.search(name)
        if m:
            return m.group(1)
    return None


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
        self.part = None
        self.raw_title = None
        self.dupes = []
        self.status = "pending"
        self.note = ""


def skip_dir(name):
    return name.startswith((".", "@", "#")) or name in SKIP_DIRS


def scan(root, exclude=()):
    media_dirs = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not skip_dir(d)], key=nat)
        filenames = [f for f in filenames if os.path.join(dirpath, f) not in exclude]
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
        companions = []
        for f in media:
            ext = os.path.splitext(f)[1].lower()
            stem = os.path.splitext(f)[0]
            path = os.path.join(d, f)
            if ext == ".pdf" and any(os.path.splitext(m)[1].lower() in AUDIO_EXT for m in media):
                companions.append((path, ext))
                continue
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
        if companions:
            audio_keys = [k for k in buckets if k[0] == "a"]
            empty = {"title": None, "author": None, "album": None, "series": None, "index": None}
            for path, ext in companions:
                if len(audio_keys) == 1:
                    buckets[audio_keys[0]].append((path, ext, empty))
                else:
                    buckets.setdefault(("b", norm(strip_part(os.path.splitext(os.path.basename(path))[0]))), []).append((path, ext, empty))
        exclusive = d != root and d not in ancestors and len(buckets) == 1
        for items in buckets.values():
            groups.append(Group(d, items, exclusive, allfiles))
    return groups


def path_guess(root, g):
    rel = os.path.relpath(g.directory, root)
    raw = [] if rel == "." else rel.split(os.sep)
    parts = [x for x in raw if norm(x) not in NOISE_DIRS]
    first_stem = os.path.splitext(os.path.basename(g.items[0][0]))[0]
    if g.exclusive and raw and norm(raw[-1]) not in NOISE_DIRS:
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
    _, hint_series, hint_index = split_series_suffix(ID_TAIL_RE.sub("", title or guess["title"] or ""))
    raw_source = title or guess["title"]
    raw_title = strip_part(clean_title(raw_source, keep_suffix=True) or "") or None
    title = strip_part(clean_title(title)) if title else None
    tag_title = bool(title)
    title = title or clean_title(guess["title"])
    author = clean_author(pick("author"))
    if not author:
        author = clean_author(guess["author"])
        if author and title and len(norm(author)) >= 3 and norm(title).startswith(norm(author)):
            author = None
    series = pick("series") or guess["series"] or hint_series
    index = parse_index(pick("index")) or guess["index"] or (hint_index if hint_series else None)
    if series and norm(series) == norm(title) and index is None:
        series = None
    if series and author and norm(series) == norm(author):
        series = None
    asin = find_asin(g) if use_lookup else None
    if asin:
        known = audible_by_asin(asin, cache)
        if known:
            if not author:
                author = clean_author(known["author"])
            if series is None and known["series"]:
                series = known["series"]
                index = index or known["index"]
            if not tag_title and known["title"]:
                title = strip_part(clean_title(known["title"])) or title
    if use_lookup and title and (series is None or index is None or author is None):
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
    if series and author and norm(series) == norm(author):
        series = None
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
    plan.raw_title = raw_title if (tag_title or not asin) else None
    if series and index is None:
        plan.note = "series found but no volume number"
    return plan


def plan_part_number(plan):
    path = plan.group.items[0][0]
    m = PART_NUM_RE.search(os.path.splitext(os.path.basename(path))[0])
    if m:
        return int(m.group(1))
    return None


def merge_parts(plans):
    buckets = {}
    for p in plans:
        if p.status != "pending" or len(p.group.items) != 1:
            continue
        if p.group.items[0][1] not in AUDIO_EXT:
            continue
        n = plan_part_number(p)
        if n is None:
            continue
        key = (norm(p.author), norm(p.series or ""), p.index, norm(p.title))
        buckets.setdefault(key, []).append((n, p))
    for entries in buckets.values():
        nums = [n for n, _ in entries]
        if len(entries) < 2 or len(set(nums)) != len(nums):
            continue
        for n, p in entries:
            p.part = n


def drop_shared_sidecars(plans):
    claimed = {}
    for p in plans:
        if p.part is None or p.status != "pending":
            continue
        kept = []
        for src, dst in p.moves:
            owner = claimed.get(dst.lower())
            if os.path.splitext(src)[1].lower() in AUDIO_EXT or owner is None:
                kept.append((src, dst))
            else:
                p.dupes.append((src, owner))
        p.moves = kept
        for _, dst in p.moves:
            claimed.setdefault(dst.lower(), dst)


def keep_existing_names(root, plan):
    raw = plan.raw_title
    if not raw or raw == plan.title:
        return
    saved = plan.title
    plan.title = raw
    moves = build_moves(root, plan)
    if all(same_path(s, d) for s, d in moves):
        plan.moves = moves
    else:
        plan.title = saved


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
        if plan.part is not None:
            name = base + " - Part " + str(plan.part) + ext
        else:
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
        prog="audiobookshelf-library-tool.py",
        description="Clean up and rename an Audiobookshelf library to Author/Series/Vol N - Title/Title.ext",
    )
    ap.add_argument("path", nargs="?", help="share or folder to scan (prompts if omitted)")
    ap.add_argument("-t", "--test", action="store_true", help="dry run, change nothing")
    ap.add_argument("-y", "--yes", action="store_true", help="apply without confirmation")
    ap.add_argument("--fix-tags", action="store_true", help="only clean mapped characters out of audio file tags, then exit")
    ap.add_argument("--keep-duplicates", action="store_true", help="do not look for or remove identical duplicate files")
    ap.add_argument("--keep-unsupported", action="store_true", help="do not remove files Audiobookshelf cannot use")
    ap.add_argument("--keep-cue", action="store_true", help="do not remove .cue files")
    ap.add_argument("--strip-text", action="append", default=[], metavar="TEXT", help="also remove this text from inside epub files (repeatable)")
    ap.add_argument("--no-strip", action="store_true", help="do not remove watermark text from inside epub files")
    ap.add_argument("--no-lookup", action="store_true", help="do not query Audible for missing series info")
    args = ap.parse_args()

    root = args.path or choose_share()
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        print("Not a folder: " + root)
        sys.exit(1)

    if args.fix_tags:
        print(("DRY RUN: " if args.test else "") + "Checking tags in " + root)
        run_fix_tags(root, args.test, args.yes)
        return

    print(("DRY RUN: " if args.test else "") + "Scanning " + root)
    if mutagen is None:
        print("mutagen not available, reading folder names and Audible only")
    exclude = set()
    dup_groups = []
    cue_files = []
    if not args.keep_duplicates:
        print("Checking for duplicates")
        dup_groups = find_duplicates(root)
        for _, drops in dup_groups:
            exclude.update(drops)
    if not args.keep_cue:
        cue_files = find_cues(root)
        exclude.update(cue_files)
    unsupported = []
    protected = []
    if not args.keep_unsupported:
        unsupported, protected = find_unsupported(root)
        exclude.update(unsupported)
    deletions = sum(len(drops) for _, drops in dup_groups) + len(cue_files) + len(unsupported)
    groups = scan(root, exclude)
    if not groups and not deletions:
        print("No audio or ebook files found")
        return
    print("Found " + str(len(groups)) + " books, reading metadata")

    cache = load_cache()
    plans = []
    for i, g in enumerate(groups, 1):
        plan = resolve(root, g, not args.no_lookup, cache)
        plans.append(plan)
        if not args.no_lookup and i % 10 == 0:
            save_cache(cache)
    save_cache(cache)
    merge_parts(plans)
    for plan in plans:
        if plan.status == "pending":
            plan.moves = build_moves(root, plan)
            keep_existing_names(root, plan)
    drop_shared_sidecars(plans)

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
            if same_path(src, dst):
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
            changed = any(not same_path(s, d) for s, d in p.moves)
            p.status = "change" if changed else "ok"

    changes = [p for p in plans if p.status == "change"]
    unchanged = [p for p in plans if p.status == "ok"]
    skipped = [p for p in plans if p.status == "skip"]
    flagged = [p for p in plans if p.status in ("change", "ok") and p.note]

    for p in changes:
        print("")
        for src, dst in p.moves:
            if not same_path(src, dst):
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

    if dup_groups:
        print("")
        print("Duplicates (identical files):")
        for keep, drops in dup_groups:
            print("  keep    " + rel(keep, root))
            for d in drops:
                print("  remove  " + rel(d, root))
    if cue_files:
        print("")
        print(".cue files to remove:")
        for c in cue_files:
            print("  remove  " + rel(c, root))

    if unsupported:
        print("")
        print("Unsupported files to remove:")
        for u in unsupported:
            print("  remove  " + rel(u, root))
    if protected:
        print("")
        print("Not removed, but Audiobookshelf cannot use these:")
        for u in protected:
            print("  " + rel(u, root))

    tag_pending = []
    if mutagen is not None:
        tag_pending, _ = collect_tag_fixes(root, exclude)
    if tag_pending:
        print("")
        print("Tag fixes:")
        print_tag_fixes(tag_pending, root)
        print("")
        print(str(len(tag_pending)) + " audio files need tag fixes")

    strip_pending = []
    strip_patterns = []
    has_epub = any(ext == ".epub" for g in groups for _, ext, _ in g.items)
    if not args.no_strip and has_epub:
        strip_texts = STRIP_TEXT + args.strip_text
        print("Checking epubs for " + ", ".join(strip_texts))
        strip_pending, epub_count, strip_patterns = collect_epub_strips(root, strip_texts, exclude)
        if strip_pending:
            print("")
            print("Epub text to remove:")
            print_epub_strips(strip_pending, root)
            print("")
            print(str(len(strip_pending)) + " of " + str(epub_count) + " epubs contain text to remove")

    if args.test:
        print("")
        print("Dry run, nothing was changed")
        return
    if not changes and not tag_pending and not deletions and not strip_pending:
        return
    if not args.yes:
        try:
            answer = input("\nApply " + str(len(changes)) + " renames, " + str(len(tag_pending)) + " tag fixes, " + str(len(strip_pending)) + " epub text edits and " + str(deletions) + " deletions in " + root + "? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Cancelled")
            return

    deleted_dirs = set()
    removed = 0
    if deletions:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write("# " + time.strftime("%Y-%m-%d %H:%M:%S") + " deletions " + root + "\n")
            for keep, drops in dup_groups:
                if not os.path.exists(keep):
                    continue
                for d in drops:
                    try:
                        if os.path.getsize(d) != os.path.getsize(keep):
                            continue
                        os.remove(d)
                        log.write("DELETE\t" + d + "\tduplicate of\t" + keep + "\n")
                        deleted_dirs.add(os.path.dirname(d))
                        removed += 1
                    except OSError as exc:
                        print("Failed: " + rel(d, root) + ": " + str(exc))
            for c in cue_files + unsupported:
                try:
                    os.remove(c)
                    log.write("DELETE\t" + c + "\tunsupported\n")
                    deleted_dirs.add(os.path.dirname(c))
                    removed += 1
                except OSError as exc:
                    print("Failed: " + rel(c, root) + ": " + str(exc))
        print("Removed " + str(removed) + " files")
    if strip_pending:
        stripped, strip_errors = apply_epub_strips(strip_pending, strip_patterns, root)
        print("Cleaned text in " + str(stripped) + " epubs" + (", " + str(strip_errors) + " failed" if strip_errors else ""))
    if tag_pending:
        fixed, tag_errors = apply_tag_fixes(tag_pending, root)
        print("Fixed tags in " + str(fixed) + " files" + (", " + str(tag_errors) + " failed" if tag_errors else ""))
    os.makedirs(APP_DIR, exist_ok=True)
    done = 0
    errors = 0
    src_dirs = set(deleted_dirs)
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write("# " + time.strftime("%Y-%m-%d %H:%M:%S") + " " + root + "\n")
        for p in changes:
            try:
                for src, dst in p.moves:
                    if same_path(src, dst):
                        continue
                    src_dirs.add(os.path.dirname(src))
                    do_move(src, dst)
                    log.write(src + "\t" + dst + "\n")
                for src, keep in p.dupes:
                    try:
                        if os.path.exists(src) and os.path.exists(keep) and os.path.getsize(src) == os.path.getsize(keep):
                            os.remove(src)
                            src_dirs.add(os.path.dirname(src))
                            log.write("DELETE\t" + src + "\tduplicate of\t" + keep + "\n")
                    except OSError:
                        pass
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
