# Solanum Catalog

**The open list of PS5 homebrew that [Solanum](https://github.com/Oliv3rmoon/Solanum) installs.** Solanum is a Nightshade app, named after the genus at the heart of the nightshade family. Anyone can add an app here with a pull request, and there's no gatekeeper: the gate is a pull request.

## Rules
- **Open-source homebrew only.** Every app links its source and has a license.
- **No game dumps, no piracy,** and nothing that downloads either.
- **Releases come from the app's own GitHub repository.** Solanum downloads the app's ZIP from its latest release and checks it against the release's checksum file before installing. Apps whose CI attests their builds are marked `attested`.
- **Authors decide.** If you made an app and want it off the list, open an issue and it comes off.

## Add an app
Add an entry to `catalog.json`'s `apps` and open a pull request. CI runs `tools/check_catalog.py --online`, which checks that:
- the title ID is four letters and five digits, used by no other app;
- the repository's latest release has the ZIP and a checksum file that names it.

| Field | What it is |
|---|---|
| `id` | short lowercase name |
| `name` | the name on the home screen |
| `category` | `emulator`, `media`, `streaming`, `game`, `tool` or `store` |
| `summary` | one line about what it does |
| `title_id` | the app's title ID |
| `repo` | `owner/name` on GitHub |
| `asset` | the release's ZIP; `{version}` stands for the tag without a leading `v` |
| `folder` | the app folder inside the ZIP |
| `checksums` | the release's checksum file, usually `SHA256SUMS` |
| `attested` | whether the release's CI attests its build |
| `license` | SPDX identifier |

## License
The catalog is GPL-3.0-or-later, like Solanum.
