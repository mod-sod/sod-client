# Architecture & the data contract

How `sod-client` turns module data into one client patch, and exactly what a
content module must declare.

## The constraint: whole-file DBCs

A WoW MPQ patch replaces an entire DBC file. There is no row-level merge, and when
two patches contain the same DBC the higher-lettered one wins outright. So you
**cannot** have `mod-sod-mage` ship a `Spell.dbc` and `mod-sod-warrior` ship
another — whichever loads last erases the other's spells.

Every module's custom rows must therefore be merged into **one** copy of each DBC.
That merge is what this repo does.

## The model: stateless consolidation

`build_patch.py` runs in a single pass:

1. Extract the **clean** base DBCs from the client (ignoring our own patches).
2. Aggregate every module's declared data.
3. Append all rows into one set of DBCs.
4. Pack them into one patch (both archive chains).
5. Write each module's server SQL back into that module.

It holds no state between runs — the patch is always rebuilt from the clean client
plus whatever modules are present. Build order is irrelevant, and installing only
some modules just yields a smaller patch. Re-running is always safe.

## What a module declares

Everything a module contributes is **data**, under its `tools/` folder. No module
carries MPQ tooling or build logic.

### Items — `client_items.json`

A list of records, one per custom item, mirroring the module's `item_template`:

```json
[{ "id": 211779, "name": "Comprehension Charm", "class": 15, "subclass": 0,
   "material": 1, "display": 1102, "invtype": 0, "sheath": 0 }]
```

These become `Item.dbc` rows so the item's **bag icon** resolves (a missing row
shows the red "?" in bags; vendor and loot frames are unaffected).

### Custom display icons — `client_displays.json` (optional)

Only when an item's icon has no existing 3.3.5a item display to reuse:

```json
[{ "id": 99001, "name": "Decrepit Phylactery",
   "icon": "spell_shadow_devouringplague" }]
```

These become `ItemDisplayInfo.dbc` rows (custom `DisplayInfoID` → `InventoryIcon`).

### Spells — `sod_spells.py`

A small Python module exporting `build_spells(idx)`. Python (not JSON) so a spell
can carry computed tooltip curves, named constants, and custom visuals. It does
`from sod_dbc import *` for the shared WoW enum constants.

```python
from sod_dbc import *

SKILL_FIRE = 8  # class-specific ids live in the spec

def build_spells(idx):
    return [{
        "id": 401556, "client": True, "template": 2136,   # clone Fire Blast
        "skill_line": SKILL_FIRE, "name": "Living Flame",
        "desc": "...", "overrides": { ... spell_dbc columns ... },
        "bonus": {"direct": 1.0, "dot": 0.0, "ap": 0.0, "ap_dot": 0.0},
    }]

# optional: custom SpellVisual rows (clone an existing visual, zero fields)
SPELL_VISUALS = [{"id": 700556, "clone_from": 143, "zero_fields": [3]}]

# optional: ids this module used to ship and now cleans out of its tables
RETIRED_IDS = []
```

`idx` is the runtime resolver the builder passes in: it maps human values to the
client's own DBC indices/ids — `idx["cast"][0]`, `idx["dur"][3000]`,
`idx["range"][40.0]`, `idx["icon"]["spell_fire_masterofelements"]`.

Per-spell keys:

- `id` / `client` — the spell id; `client: false` means server-only (SQL, no DBC).
- `template` — an existing spell id to clone (so every index column is valid).
- `overrides` — `spell_dbc` column → value (the server row is built from these).
- `client_overrides` / `client_overrides_float` — written to the client DBC only
  (e.g. tooltip level-scaling), never to the server row.
- `name` / `desc` / `aura_desc` — client tooltip strings.
- `skill_line` — the spellbook tab (a `SkillLineAbility` row).
- `script` — binds a `spell_script_names` row.
- `bonus` — `spell_bonus_data` spellpower coefficients.
- `proc` — a `spell_proc` row (required for auras that proc).

## Two outputs, one source

The spec drives **both** sides, which must stay in lockstep:

- **Client DBCs** — consolidated across all modules into the one patch.
- **Server `sod_<class>_spell_dbc.sql`** — written back into each module
  (`spell_dbc` + `spell_script_names` + `spell_bonus_data` + `spell_proc`), with a
  "generated — do not edit" banner. Committed in the module, so the running server
  has no dependency on `sod-client`.

## Patch letters

The unified patch uses letter **`z`**, written to both archive chains:

- **Base chain** — `Data/patch-z.mpq`: the non-localized DBCs (`Item`,
  `ItemDisplayInfo`, `SkillLineAbility`, `SpellVisual`).
- **Locale chain** — `Data/<locale>/patch-<locale>-z.mpq`: the same non-localized
  DBCs **plus** the localized `Spell.dbc`.

Writing the non-localized DBCs to both chains guarantees the override wins
regardless of where the client holds a rival copy. The old per-module split used
`y` (items) and `z` (mage spells); `sod-client` retires `y` and removes it on
build.

## Sources of truth

- [wago.tools](https://wago.tools) — accurate SoD DB2 values as CSV.
- [Wowhead Classic](https://www.wowhead.com/classic) — ids, icons, drop sources.
- [AzerothCore wiki](https://www.azerothcore.org/wiki) — DBC/table schemas.
