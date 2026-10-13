#!/usr/bin/env python3
"""ORBIT Store registry: checks apps/*.json and builds the catalog the PS2 reads.

  python tools/catalog.py check [apps/x.json ...]   validate + download each asset and match its sha256 (all if none)
  python tools/catalog.py build OUT_DIR             catalog.tsv (+ catalog.sig when ORBIT_STORE_KEY holds a PEM key)
  python tools/catalog.py pin apps/x.json ...        write the sha256 of each entry's current asset into it
  python tools/catalog.py bump                      move GitHub entries to their latest release (new tag + sha256)
  python tools/catalog.py selftest

catalog.tsv: first line "orbit-store-catalog 1", then one app per line, tab-separated, in FIELDS order. The PS2
installs nothing unless catalog.sig (ECDSA P-256 over SHA-256, DER) verifies and the download matches size + sha256.
Standard library only, plus the openssl command for signing.
"""
import glob, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES = ("utilidades", "multimedia", "emuladores", "juegos")
FIELDS = ("id", "name", "author", "category", "version", "license", "kind", "boot", "size", "sha256", "url", "repo",
          "desc_es", "desc_en")
MAX_SIZE = 32 << 20  # a PS2 app bigger than this is not what the store is for
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")  # also the folder: mass0:/APPS/<ID upper-cased>


def validate(e, name="entry"):
    """Errors in one entry (an apps/<id>.json), [] if it is well formed. Nothing is downloaded here."""
    err = []
    def need(k, kind=str):
        if not isinstance(e.get(k), kind) or not e.get(k):
            err.append(f"{name}: '{k}' missing or not a {kind.__name__}")
    for k in ("id", "name", "author", "category", "license"):
        need(k)
    need("description", dict)
    if err:
        return err
    if not ID_RE.match(e["id"]):
        err.append(f"{name}: id must be 2-24 of a-z 0-9 -")
    if name.endswith(".json") and os.path.basename(name) != e["id"] + ".json":
        err.append(f"{name}: file must be named {e['id']}.json")
    if e["category"] not in CATEGORIES:
        err.append(f"{name}: category must be one of {', '.join(CATEGORIES)}")
    for lang in ("es", "en"):
        d = e["description"].get(lang, "")
        if not d or len(d) > 200 or "\t" in d or "\n" in d:
            err.append(f"{name}: description.{lang} needs 1-200 characters on one line")
    for k in ("name", "author", "license"):
        if len(e[k]) > 40 or "\t" in e[k]:
            err.append(f"{name}: '{k}' over 40 characters")
    if not re.fullmatch(r"[0-9a-f]{64}", e.get("sha256", "")):
        err.append(f"{name}: 'sha256' (64 lowercase hex) is required: `check` prints it")
    gh, url = e.get("github"), e.get("url")
    if bool(gh) == bool(url):
        err.append(f"{name}: give either 'github' (owner/repo + tag + asset) or 'url' (external, maintainer only)")
    elif gh:
        if not isinstance(gh, dict) or not re.fullmatch(r"[\w.-]+/[\w.-]+", gh.get("repo", "")) or not gh.get("tag") \
                or not gh.get("asset"):
            err.append(f"{name}: 'github' needs repo (owner/name), tag and asset (file name, * allowed)")
    elif not url.startswith("https://") or not e.get("version"):
        err.append(f"{name}: an external 'url' must be https and come with a 'version'")
    if "boot" in e and not re.fullmatch(r"[\w./ *-]+\.(elf|ELF)", e["boot"]) or ".." in e.get("boot", ""):
        err.append(f"{name}: 'boot' is the ELF's path inside the zip (* allowed)")
    return err


def glob_re(pat):  # '*' = any run of characters, nothing else is special
    return re.compile("^" + re.escape(pat).replace(r"\*", ".*") + "$")


def is_mips_elf(b):
    """A 32-bit little-endian MIPS ELF executable, as the EE runs."""
    return len(b) > 20 and b[:4] == b"\x7fELF" and b[4] == 1 and b[5] == 1 and int.from_bytes(b[18:20], "little") == 8


def inspect(data, boot=None):
    """('elf'|'zip', boot path) for a downloaded asset, or raises ValueError saying why it is not an app."""
    if is_mips_elf(data):
        return "elf", boot or ""
    if data[:4] != b"PK\x03\x04":
        raise ValueError("neither a MIPS ELF nor a zip")
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    if any(n.startswith("/") or ".." in n.split("/") for n in names):
        raise ValueError("zip has absolute or .. paths")
    elfs = [n for n in names if n.lower().endswith(".elf")]
    if boot is None or "*" in boot:  # a pattern, so a new release's folder name does not break it
        hits = [n for n in elfs if boot is None or glob_re(boot).match(n)]
        if len(hits) != 1:
            raise ValueError(f"zip has {len(hits)} ELFs matching '{boot or '*.elf'}': {', '.join(elfs)}")
        boot = hits[0]
    if boot not in names or not is_mips_elf(z.read(boot)[:64]):
        raise ValueError(f"'{boot}' is not a MIPS ELF inside the zip")
    return "zip", boot


def http(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "orbit-store-registry"})
    if accept:
        req.add_header("Accept", accept)
    if os.environ.get("GITHUB_TOKEN") and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(MAX_SIZE + 1)


def release(repo, tag=None):
    api = f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    return json.loads(http(api, "application/vnd.github+json"))


def asset_url(e, rel=None):
    """Download URL of the entry's asset; for GitHub, the one release asset matching 'asset'."""
    if e.get("url"):
        return e["url"]
    gh = e["github"]
    rel = rel or release(gh["repo"], gh["tag"])
    hits = [a for a in rel.get("assets", []) if glob_re(gh["asset"]).match(a["name"])]
    if len(hits) != 1:
        raise ValueError(f"{gh['repo']} {rel.get('tag_name')}: {len(hits)} assets match '{gh['asset']}': "
                         + ", ".join(a["name"] for a in rel.get("assets", [])))
    return hits[0]["browser_download_url"]


def fetch(e, rel=None):
    """(url, data, kind, boot) checked against the entry's sha256; raises ValueError on any mismatch."""
    url = asset_url(e, rel)
    data = http(url)
    if len(data) > MAX_SIZE:
        raise ValueError(f"over {MAX_SIZE >> 20} MB")
    kind, boot = inspect(data, e.get("boot"))
    sha = hashlib.sha256(data).hexdigest()
    if sha != e.get("sha256"):
        raise ValueError(f"sha256 of {url} is {sha} (entry says {e.get('sha256') or 'nothing'})")
    return url, data, kind, boot


def load(paths=None):
    paths = paths or sorted(glob.glob(os.path.join(ROOT, "apps", "*.json")))
    return [(p, json.load(open(p, encoding="utf-8"))) for p in paths]


def check(paths):
    bad = 0
    for p, e in load(paths):
        errs = validate(e, os.path.relpath(p, ROOT).replace("\\", "/"))
        if not errs:
            try:
                url, data, kind, boot = fetch(e)
                print(f"ok   {e['id']}: {kind} {len(data)} bytes{' boot ' + boot if boot else ''} from {url}")
                continue
            except Exception as x:  # network, zip and hash errors alike: report them all, fail at the end
                errs = [f"{e['id']}: {x}"]
        bad += 1
        print("FAIL " + "\nFAIL ".join(errs))
    return bad


def row(e, url, data, kind, boot):
    gh = e.get("github") or {}
    v = {"id": e["id"], "name": e["name"], "author": e["author"], "category": e["category"],
         "version": gh.get("tag") or e.get("version", ""), "license": e["license"], "kind": kind, "boot": boot,
         "size": str(len(data)), "sha256": e["sha256"], "url": url, "repo": gh.get("repo", ""),
         "desc_es": e["description"]["es"], "desc_en": e["description"]["en"]}
    return v


def build(out):
    """OUT_DIR gets the PS2's catalog.tsv (+ .sig), the website's catalog.json and the site itself (GitHub Pages)."""
    rows = []
    for p, e in load():
        errs = validate(e, p)
        if errs:
            sys.exit("\n".join(errs))
        rows.append(row(e, *fetch(e)))
    os.makedirs(out, exist_ok=True)
    lines = ["orbit-store-catalog 1"] + ["\t".join(r[f] for f in FIELDS) for r in rows]
    text = ("\n".join(lines) + "\n").encode()
    open(os.path.join(out, "catalog.tsv"), "wb").write(text)
    json.dump(rows, open(os.path.join(out, "catalog.json"), "w", encoding="utf-8"), ensure_ascii=False)
    shutil.copyfile(os.path.join(ROOT, "site", "index.html"), os.path.join(out, "index.html"))
    key = os.environ.get("ORBIT_STORE_KEY")
    if key:
        with tempfile.TemporaryDirectory() as tmp:  # never inside OUT_DIR: that folder is published
            kp = os.path.join(tmp, "key.pem")
            open(kp, "w").write(key)
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", kp], input=text, capture_output=True,
                                 check=True).stdout
        open(os.path.join(out, "catalog.sig"), "wb").write(sig)
    print(f"{len(lines) - 1} apps{', signed' if key else ', NOT signed (no ORBIT_STORE_KEY)'}")


def save(p, e):
    open(p, "w", encoding="utf-8", newline="\n").write(json.dumps(e, indent=2, ensure_ascii=False) + "\n")


def pin(paths):
    """Writes the sha256 of each entry's current asset into it (after looking at what it downloaded)."""
    for p, e in load(paths):
        data = http(asset_url(e))
        kind, boot = inspect(data, e.get("boot"))
        e["sha256"] = hashlib.sha256(data).hexdigest()
        save(p, e)
        print(f"{e['id']}: {kind} {len(data)} bytes{' boot ' + boot if boot else ''} sha256 {e['sha256']}")


def bump():
    """GitHub entries whose repo has a newer release: tag and sha256 rewritten in place (the workflow opens a PR)."""
    for p, e in load():
        gh = e.get("github")
        if not gh:
            continue
        try:
            rel = release(gh["repo"])
            if rel["tag_name"] == gh["tag"]:
                continue
            data = http(asset_url(e, rel))
            inspect(data, e.get("boot"))
            gh["tag"], e["sha256"] = rel["tag_name"], hashlib.sha256(data).hexdigest()
            save(p, e)
            print(f"{e['id']}: -> {rel['tag_name']}")
        except Exception as x:  # one broken repo must not stop the others
            print(f"{e['id']}: skipped ({x})")


def selftest():
    good = {"id": "fceumm", "name": "FCEUmm", "author": "ps2homebrew", "category": "emuladores", "license": "GPL-2.0",
            "description": {"es": "Emulador de NES.", "en": "NES emulator."}, "sha256": "a" * 64,
            "github": {"repo": "ps2homebrew/Fceumm-PS2", "tag": "v0.3.3", "asset": "*.elf"}}
    assert validate(good) == []
    assert validate(dict(good, url="https://x/y.elf"))  # github and url together
    assert validate(dict(good, id="Bad_ID"))
    assert validate(dict(good, category="cargadores"))
    assert validate(dict(good, sha256=""))
    assert validate(dict(good, boot="../x.elf"))
    ext = dict(good, url="https://example.org/app.zip")
    del ext["github"]
    assert validate(ext)  # an external entry needs its version
    assert validate(dict(ext, version="1.0")) == []
    elf = b"\x7fELF\x01\x01\x01" + bytes(11) + (8).to_bytes(2, "little") + bytes(44)
    assert is_mips_elf(elf) and not is_mips_elf(elf[:18] + (3).to_bytes(2, "little") + elf[20:])
    assert inspect(elf) == ("elf", "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("APP/APP.ELF", elf)
        z.writestr("APP/readme.txt", "hi")
    assert inspect(buf.getvalue()) == ("zip", "APP/APP.ELF")
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("APP/OTHER.ELF", elf)
    try:
        inspect(buf.getvalue())
        raise AssertionError("two ELFs need 'boot'")
    except ValueError:
        pass
    assert inspect(buf.getvalue(), "APP/OTHER.ELF") == ("zip", "APP/OTHER.ELF")
    assert inspect(buf.getvalue(), "*/OTHER.ELF") == ("zip", "APP/OTHER.ELF")
    r = row(good, "https://u", elf, "elf", "")
    assert sorted(r) == sorted(FIELDS) and r["size"] == str(len(elf)) and r["version"] == "v0.3.3"
    for p, e in load():  # the repo's own entries are well formed
        assert validate(e, p) == [], validate(e, p)
    print("catalog selftest ok")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("selftest", [])
    if cmd == "check":
        sys.exit(1 if check(args) else 0)
    elif cmd == "build":
        build(args[0] if args else os.path.join(ROOT, "out"))
    elif cmd == "pin":
        pin(args)
    elif cmd == "bump":
        bump()
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(__doc__)
