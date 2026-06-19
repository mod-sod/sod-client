# sod-client

The **client-patch build pipeline** for the SoD project. It turns each content
module's declarative data into the **one** consolidated WoW **3.3.5a** client
patch (and the matching server SQL), then writes it into your client.

> **Just want to play?** The [**SoD installer**](https://github.com/mod-sod/sod-installer)
> runs this for you as part of setup. The steps below are for building by hand or
> contributing.

## Why this exists

WoW MPQ patches replace **whole DBC files** — there is no row-level merge, and the
highest-lettered patch wins. So two content modules can't each ship their own
`Spell.dbc` (or `Item.dbc`): one would silently erase the other. Every module's
custom client rows must live in a **single** set of DBCs.

`sod-client` owns that consolidation. It is **not** an AzerothCore module — it
doesn't compile into the worldserver. It's a separate, needed build step that
reads every module's data and produces one patch.

## What each module declares (the data contract)

A content module under `modules/mod-sod-*/` contributes **data only** — no build
tooling, no MPQ libraries:

| File | Produces |
|---|---|
| `tools/client_items.json` | `Item.dbc` rows (bag icons for custom items) |
| `tools/client_displays.json` | `ItemDisplayInfo.dbc` rows (custom icons) |
| `tools/sod_spells.py` | `Spell.dbc` / `SkillLineAbility.dbc` / `SpellVisual.dbc` rows **and** the module's server `sod_<class>_spell_dbc.sql` |

`sod-client` globs all of them, builds one patch, and writes each module's server
SQL back into that module (committed, regenerated — never hand-edited).

See [docs/architecture.md](docs/architecture.md) for the full contract, the spell
spec format, and the patch-letter scheme.

## Build

Requires Python 3 with [`pympq`](https://pypi.org/project/pympq/) (StormLib).
**Close the WoW client first** — it locks the MPQ files.

```bash
pip install -r requirements.txt
python build_patch.py --server /path/to/azerothcore --client "/path/to/WoW 3.3.5a"
```

- `--server` — your AzerothCore root (has `modules/` and the `spell_dbc` table def).
- `--client` — your WoW 3.3.5a client root (contains `Data/`).
- `--dry-run` — build the patched DBCs and SQL but don't write the MPQ.

The build is **stateless**: it always rebuilds from the clean client plus whatever
modules are present, in one pass. Order doesn't matter and partial installs work.
It writes one patch (`patch-z.mpq` + the locale `patch-<locale>-z.mpq`) and removes
the retired `patch-y` from the old per-module split.

## Conventions

No worldserver edits — and no game logic lives here. This repo only transforms
module-declared data into client/server artifacts.

## License

GPL v2 (inherited from AzerothCore). See [LICENSE](LICENSE).
