# ORBIT Store registry

The catalog of **ORBIT Store**, the app store for PlayStation 2 homebrew: emulators, media players, utilities and
games. The PS2 app and the website read what this repo publishes; the binaries stay in each author's GitHub releases.

- Website: https://danielnuld.github.io/orbit-store-registry/ (browse and download without the PS2 app)
- PS2 catalog: `catalog.tsv` + `catalog.sig` at the same address

## Add your app

1. Publish the app as a **GitHub release** of its repo: an `.elf`, or a `.zip` with the ELF inside. The repo needs a
   licence. No BIOS files, commercial games or ROMs.
2. Either open an issue with the **Enviar una app / Submit an app** form, or send a PR adding one file,
   `apps/<id>.json`:

```json
{
  "id": "fceumm",
  "name": "FCEUmm",
  "author": "ps2homebrew",
  "category": "emuladores",
  "license": "GPL-2.0",
  "description": {
    "es": "Emulador de NES y Famicom. Carga las ROMs desde la USB, el disco duro o un CD/DVD.",
    "en": "NES and Famicom emulator. Loads ROMs from USB, the internal HDD or a CD/DVD."
  },
  "github": { "repo": "ps2homebrew/Fceumm-PS2", "tag": "v0.3.3", "asset": "fceu-packed.*.cdvdsupport.elf" },
  "sha256": "84274cfcfe3a865a2f4480042184ec0885e446a7a97d842519e789cb3de052ab"
}
```

| Field | |
|---|---|
| `id` | 2-24 of `a-z 0-9 -`; the file name, and the install folder `APPS/<ID>` |
| `category` | `utilidades`, `multimedia`, `emuladores` or `juegos` |
| `description` | the repo's own GitHub description in `en` (or its README's first line if it has none) and a literal Spanish translation in `es`; one line, up to 200 characters each |
| `github.asset` | the release asset's file name; `*` matches anything, so it keeps working on the next release |
| `boot` | only for a zip with several ELFs: the one to run, path inside the zip (`*` allowed) |
| `sha256` | of the asset: `python tools/catalog.py pin apps/<id>.json` writes it |

`python tools/catalog.py check apps/<id>.json` runs the same check as the PR: the entry is well formed, the asset
downloads, it is a MIPS ELF (or a zip holding one), and its sha256 matches.

**Updates:** every night a bot opens a PR when your repo has a newer release (new tag and sha256). It reaches the
consoles when the maintainer merges it, so a hijacked release never installs by itself.

**External entries:** apps that live outside GitHub (forum downloads) use `"url"` and `"version"` instead of
`"github"`. Only the maintainer adds those.

## How it works

`publish.yml` builds the catalog on every merge to `main` and puts it on GitHub Pages:

- `catalog.tsv`: one app per line, tab-separated (`tools/catalog.py` FIELDS), with the resolved download URL, size,
  sha256 and the ELF to boot;
- `catalog.sig`: ECDSA P-256 signature (DER, over SHA-256) made with the store's own key, the `ORBIT_STORE_KEY`
  secret; its public half is `store-key.pub.pem`. The PS2 app installs nothing unless it verifies and the download
  matches size and sha256.
- `catalog.json` and `index.html`: the website.

`tools/catalog.py` uses only Python's standard library (plus `openssl` to sign). `python tools/catalog.py selftest`
checks it.
