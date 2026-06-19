# sod-client

The **client-patch build pipeline** for the SoD project. It turns each content
module's declarative data into the **one** consolidated WoW **3.3.5a** client patch
(and the matching server SQL).

`sod-client` is **not** an AzerothCore module — it doesn't compile into the
worldserver. It's a separate, needed build step. For the project overview and the
build command, see the
[README](https://github.com/mod-sod/sod-client) in the repo root.

## Why it exists

WoW MPQ patches replace **whole DBC files** — no row-level merge, and the highest
patch letter wins. So two content modules can't each ship their own `Spell.dbc` (or
`Item.dbc`): one would silently erase the other. Every module's custom rows must
live in a **single** set of DBCs. `sod-client` owns that consolidation.

## Start here

- **[Architecture & the data contract](architecture.md)** — the whole-DBC
  constraint, the stateless consolidation model, exactly what each module declares
  (`client_items.json`, `client_displays.json`, `sod_spells.py`), the spell-spec
  keys, and the patch-letter scheme.

## Build

From a checkout (Python 3 + [`pympq`](https://pypi.org/project/pympq/), client
**closed**):

```bash
python build_patch.py --server "<azerothcore root>" --client "<WoW 3.3.5a root>"
```

It writes one patch (`patch-z.mpq` + the locale `patch-<locale>-z.mpq`). The
[SoD installer](https://github.com/mod-sod/sod-installer) runs it for you.
