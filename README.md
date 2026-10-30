# Solanum Catalog

**The open list of PS5 homebrew that [Solanum](https://github.com/Oliv3rmoon/Solanum) installs.** Solanum is a Nightshade app, named after the genus at the heart of the nightshade family. Anyone can add an app here with a pull request, and there's no gatekeeper: the gate is a pull request.

## Rules
- **Open-source homebrew only.** Every app links its source and has an OSI license in its repository.
- **No game dumps, no piracy,** and nothing that downloads either: no ROMs, BIOS, keys or game files in a release, no backup loaders, no ports of commercial games' code. Engine ports that need your own game files (OpenMW, OpenRCT2...) and emulators are fine, as long as they ship none.
- **Releases come from the app's own GitHub repository.** Solanum downloads the app's ZIP from its latest release.
- **Every release is pinned.** A workflow downloads each app's newest release, checks it, and writes its SHA-256 and size here (see *Pinning*). Solanum checks every download against that pin, and against the release's checksum file or GitHub's digest when there is one, before anything is unpacked. Apps whose CI attests their builds are marked `attested`.
- **Authors decide.** If you made an app and want it off the list, open an issue and it comes off.

## Add an app
Add an entry to `catalog.json`'s `apps` and open a pull request; leave out `release`, `pins` and `stars`, which the pin workflow writes. CI runs `tools/check_catalog.py --online`, which checks that:
- the title ID is four letters and five digits, used by no other app;
- the repository's latest release has the file the entry's `asset` names, and Solanum can check it: the release's checksum file names it, GitHub keeps a digest of it, or it is pinned;
- every pinned release is the file the pattern names for its tag, and the file GitHub lists for a pinned tag is still the pinned one.

| Field | What it is |
|---|---|
| `id` | short lowercase name |
| `name` | the name on the home screen |
| `category` | `emulator`, `media`, `streaming`, `game`, `tool` or `store` |
| `summary` | one line about what it does |
| `title_id` | the app's title ID (its `sce_sys/param.json` says it) |
| `repo` | `owner/name` on GitHub |
| `asset` | the release's ZIP; `{version}` stands for the tag without a leading `v`, `{content}` for the tag as a content version (`v0.7.0` is `00.007.000`) |
| `folder` | the app folder inside the ZIP (Solanum finds it up to three folders deep whatever its name, and installs it named after the title ID) |
| `checksums` | the release's checksum file, usually `SHA256SUMS` (one name; leave `SHA256SUMS` when it has none) |
| `attested` | whether the release's CI attests its build (`gh attestation verify` must accept it) |
| `license` | SPDX identifier |
| `author` | optional: who makes it |
| `tags` | optional: up to eight lowercase words, for Solanum's search (`#emulator`, `#ps1`) |
| `data` | optional: the folder the app keeps its own files in, `/data/<name>`, which Solanum's Uninstall offers to remove too. Only a folder of the app's own, never one it shares with another app |
| `release`, `pins`, `stars` | written by the pin workflow: the newest release's tag, file, size, SHA-256, date and notes; the ones pinned before it; the repository's stars |

## Pinning
Most homebrew releases publish no checksum file, so this repository pins them. `.github/workflows/pin.yml` runs every six hours and on every push that changes the catalog or its tools. For each app, `tools/pin_catalog.py`:

1. reads the repository's newest release (not a draft or a pre-release);
2. downloads the file `asset` names, hashing it as it arrives;
3. pins it only when everything agrees: the size GitHub lists, GitHub's digest of the file when it keeps one, the release's checksum file when it has one; the ZIP holds an app folder (`eboot.bin` beside `sce_sys/param.json`) whose `titleId` is the entry's; it holds no game images, ROMs, keys or packages; and, for an `attested` app, `gh attestation verify` accepts it;
4. writes `release` (and moves the one before into `pins`, five kept) and `stars`.

A release that is pinned is never pinned again: when the file GitHub lists for a pinned tag changes, the file was replaced after it was pinned, and the run fails instead. On `main` the workflow commits new pins; on other branches it shows what would change. Every pin is in this repository's history.

Solanum shows each app's pinned release without asking GitHub's API, so a catalog of any size costs nothing of the 60 requests an hour GitHub allows an address; it asks GitHub only for the apps installed on the console, to see a release newer than its pin.

Run it yourself with `GITHUB_TOKEN=... python3 tools/pin_catalog.py` (or `--only id,id`); `python3 -m unittest discover -s tests` checks the tool without a network.

## Your own catalog
Solanum can also read other catalogs in this format (Settings > Catalogs): a GitHub repository with a `catalog.json` at its root, or any HTTPS address of one. Their apps are shown as third-party, never as attested; their pins are not used (their downloads are checked against what GitHub says alone); and an app this catalog lists comes from this catalog.

## License
The catalog is GPL-3.0-or-later, like Solanum.
