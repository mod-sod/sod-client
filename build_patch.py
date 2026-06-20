#!/usr/bin/env python3
"""Build the ONE consolidated SoD client patch from every content module's data.

WoW MPQ patches replace whole DBC files (no row-level merge), so all SoD modules'
custom client rows must live in a single set of DBCs. This tool is the single
owner of that consolidation. It reads each content module's *declarative data*:

  * tools/client_items.json    — [{id,name,class,subclass,material,display,
                                   invtype,sheath}, ...]   -> Item.dbc
  * tools/client_displays.json — [{id,name,icon}, ...]     -> ItemDisplayInfo.dbc
  * tools/sod_spells.py        — build_spells(idx) [+ SPELL_VISUALS, RETIRED_IDS]
                                 -> Spell.dbc / SkillLineAbility.dbc /
                                    SpellVisual.dbc  AND each module's server
                                    data/sql/.../sod_<class>_spell_dbc.sql

It globs <server>/modules/mod-sod-*/ for the above and builds one patch (letter
`z` — the highest letter, so it loads last and outranks every other archive),
written to BOTH the locale chain (patch-<locale>-z.mpq) and the base chain
(patch-z.mpq). Stateless: always rebuilt from the clean client + all present
modules in one pass, so order doesn't matter and partial installs work.

Requires `pympq` (StormLib binding) for the MPQ paths.

Usage:
    python build_patch.py [--server DIR] [--client DIR] [--dry-run]
      --server  AzerothCore root (has modules/ and data/sql/base/db_world/)
      --client  WoW 3.3.5a client root (contains Data/)
"""

import argparse
import importlib.util
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # so each module's `from sod_dbc import *` resolves

import sod_dbc as dbc  # noqa: E402  (after sys.path setup)

# Defaults match this machine; override with --server / --client.
DEFAULT_SERVER = r"/home/ben/wow-server-playerbots"
DEFAULT_CLIENT = r"E:\Games\World of Warcraft 3.3.5a HD"

# The patch letter is `z` — the highest letter, so it loads last and wins every
# conflict. Excluded when reading clean client data so a rebuild never sources
# from our own previous output.
PATCH_LETTER = "z"
CUSTOM_PATCH_LETTERS = ("z",)


def load_spell_specs(modules_dir):
    """Import each module's tools/sod_spells.py. Returns a list of
    (module_name, spec_module) for every spec found."""
    specs = []
    pattern = os.path.join(modules_dir, "mod-sod-*", "tools", "sod_spells.py")
    for path in sorted(glob.glob(pattern)):
        module_name = os.path.basename(os.path.dirname(os.path.dirname(path)))
        uniq = "sod_spells_" + module_name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(uniq, path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # a broken spec shouldn't sink the whole build
            print("[!] skipping %s: %s" % (path, exc))
            continue
        specs.append((module_name, mod))
    return specs


def module_class(module_name):
    """mod-sod-mage -> mage (the data/sql filename + SQL comment scope)."""
    return module_name[len("mod-sod-"):]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help="AzerothCore root (has modules/ and data/sql/base/)")
    ap.add_argument("--client", default=DEFAULT_CLIENT,
                    help="WoW client root (contains Data/ or data/)")
    ap.add_argument("--workdir", default=os.path.join(HERE, "_work"),
                    help="scratch dir for extracted DBCs / patched output")
    ap.add_argument("--dry-run", action="store_true",
                    help="emit SQL + patched DBCs but do not write the MPQ")
    args = ap.parse_args()

    modules_dir = os.path.join(args.server, "modules")
    table_def = os.path.join(args.server, "data", "sql", "base", "db_world",
                             "spell_dbc.sql")

    # --- gather declarative data from every module ---
    items = dbc.load_items(modules_dir)
    displays = dbc.load_displays(modules_dir)
    creature_displays = dbc.load_creature_displays(modules_dir)
    factions = dbc.load_factions(modules_dir)
    specs = load_spell_specs(modules_dir)

    # Import-then-resolve: importing a spec only defines build_spells (no idx
    # needed); we extract + resolve indexes, then call build_spells(idx).
    has_spells = bool(specs)
    if not items and not displays and not creature_displays and not factions \
            and not has_spells:
        print("[*] nothing to patch")
        return

    os.makedirs(args.workdir, exist_ok=True)
    locale = dbc.detect_locale(args.client)
    print("[*] aggregated %d item(s), %d display(s), %d creature-display(s), "
          "%d faction(s), %d spell spec(s) (locale: %s)"
          % (len(items), len(displays), len(creature_displays), len(factions),
             len(specs), locale))

    def extract(name):
        dest = os.path.join(args.workdir, name)
        src = dbc.extract_client_dbc(args.client, name, dest, locale,
                                     CUSTOM_PATCH_LETTERS)
        print("    %-22s <- %s" % (name, os.path.basename(src)))
        return src

    print("[*] extracting clean client DBCs from", args.client)
    if items:
        extract("Item.dbc")
    if displays:
        extract("ItemDisplayInfo.dbc")
    if creature_displays:
        extract("CreatureDisplayInfo.dbc")
        extract("CreatureDisplayInfoExtra.dbc")
    if factions:
        extract("Faction.dbc")
        extract("FactionTemplate.dbc")

    all_spells, all_visuals = [], []
    per_module = []  # (module_name, spells, retired_ids)
    cols = None
    if has_spells:
        for name in ("Spell.dbc", "SpellCastTimes.dbc", "SpellDuration.dbc",
                     "SpellRange.dbc", "SpellIcon.dbc", "SkillLineAbility.dbc",
                     "SpellVisual.dbc"):
            extract(name)
        idx = dbc.resolve_indexes(args.workdir)
        cols = dbc.load_columns(table_def)
        for module_name, mod in specs:
            spells = mod.build_spells(idx) if hasattr(mod, "build_spells") else []
            if not spells:
                continue
            visuals = list(getattr(mod, "SPELL_VISUALS", []))
            retired = list(getattr(mod, "RETIRED_IDS", []))
            per_module.append((module_name, spells, retired))
            all_spells += spells
            all_visuals += visuals
            print("[*] %s: %d spell(s), %d custom visual(s)"
                  % (module_name, len(spells), len(visuals)))

    # --- build patched client DBCs ---
    item_patched = dbc.build_item_dbc(args.workdir, items) if items else None
    idi_patched = dbc.build_item_display_info(args.workdir, displays)
    cdi_patched = dbc.build_creature_display_info(args.workdir, creature_displays)
    cdie_patched = dbc.build_creature_display_info_extra(args.workdir,
                                                         creature_displays)
    faction_patched = dbc.build_faction(args.workdir, factions)
    factiontpl_patched = dbc.build_faction_template(args.workdir, factions)
    spell_patched = sla_patched = sv_patched = None
    if all_spells:
        spell_patched = dbc.build_spell_dbc(args.workdir, cols, all_spells)
        sla_patched = dbc.build_skill_line_ability(args.workdir, all_spells)
        sv_patched = dbc.build_spell_visual(args.workdir, all_visuals)

    # --- emit each module's server SQL ---
    for module_name, spells, retired in per_module:
        cls = module_class(module_name)
        out_dir = os.path.join(modules_dir, module_name, "data", "sql",
                               "db-world", "base")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "sod_%s_spell_dbc.sql" % cls)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dbc.emit_spell_sql(spells, cols, module_name, retired))
        print("[*] wrote SQL -> %s" % out_path)

    if args.dry_run:
        print("[*] dry-run: skipping MPQ pack")
        return

    # --- pack the one patch (both chains) ---
    # Spell.dbc is localized (locale chain only); Item/ItemDisplayInfo/
    # SkillLineAbility/SpellVisual are non-localized -> both chains, since which
    # chain holds a rival copy varies per client.
    nonloc = []
    if item_patched:
        nonloc.append((item_patched, dbc.ITEM_INNER))
    if idi_patched:
        nonloc.append((idi_patched, dbc.IDI_INNER))
    if cdi_patched:
        nonloc.append((cdi_patched, dbc.CDI_INNER))
    if cdie_patched:
        nonloc.append((cdie_patched, dbc.CDIE_INNER))
    if factiontpl_patched:
        nonloc.append((factiontpl_patched, dbc.FACTIONTPL_INNER))
    if sla_patched:
        nonloc.append((sla_patched, dbc.SKILL_INNER))
    if sv_patched:
        nonloc.append((sv_patched, dbc.SPELLVIS_INNER))
    locale_files = list(nonloc)
    if spell_patched:
        locale_files.append((spell_patched, dbc.SPELL_INNER))
    if faction_patched:
        # Faction.dbc is localized (Name_Lang) -> locale chain only.
        locale_files.append((faction_patched, dbc.FACTION_INNER))

    data_dir = os.path.join(args.client, "data")
    locale_mpq = os.path.join(data_dir, locale,
                              "patch-%s-%s.mpq" % (locale, PATCH_LETTER))
    dbc.pack_mpq(locale_files, locale_mpq)
    print("[*] wrote locale patch -> %s (%d DBC file(s))"
          % (locale_mpq, len(locale_files)))

    if nonloc:
        base_mpq = os.path.join(data_dir, "patch-%s.mpq" % PATCH_LETTER)
        dbc.pack_mpq(nonloc, base_mpq)
        print("[*] wrote base patch   -> %s (%d DBC file(s))"
              % (base_mpq, len(nonloc)))


if __name__ == "__main__":
    sys.exit(main())
