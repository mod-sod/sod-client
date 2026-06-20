#!/usr/bin/env python3
"""Shared client-DBC/MPQ library for the SoD client patch pipeline.

This module is the single home for the machinery that turns each content
module's *declarative data* into the one consolidated client patch:

  * WDBC          — a minimal 3.3.5a DBC reader/writer (fixed-width records +
                    trailing string block).
  * MPQ helpers   — locale detection, archive priority, clean-base extraction,
                    and packing (pympq / StormLib).
  * Item builders — Item.dbc / ItemDisplayInfo.dbc rows from item/display
                    manifests (moved here from mod-sod-world).
  * Spell builders— Spell.dbc / SkillLineAbility.dbc / SpellVisual.dbc rows and
                    the server spell_dbc SQL from per-module spell specs (moved
                    here from mod-sod-mage).

`build_patch.py` is the entry point that drives all of this. Content modules
never import this directly except their spell spec's `from sod_dbc import *`
(for the shared WoW enum constants below).

Requires `pympq` (StormLib binding) for the MPQ read/write paths only; the pure
DBC/SQL paths work without it.
"""

import glob
import json
import os
import re
import struct

# ---------------------------------------------------------------------------
# Shared WoW enum values (from core SharedDefines.h / SpellAuraDefines.h).
# Spell specs do `from sod_dbc import *` to use these without re-declaring them.
# Class-specific ids (skill lines, custom SpellVisual ids) live in each spec.
# ---------------------------------------------------------------------------
SPELL_ATTR0_PASSIVE = 0x00000040
SPELL_ATTR0_DO_NOT_DISPLAY = 0x00000080
SPELL_ATTR1_IS_CHANNELED = 0x00000004
SPELL_ATTR1_NO_THREAT = 0x00000400  # no threat; also does not cause target to engage
EFFECT_SCHOOL_DAMAGE = 2
EFFECT_DUMMY = 3
EFFECT_APPLY_AURA = 6
EFFECT_HEAL = 10
AURA_DUMMY = 4
AURA_PERIODIC_HEAL = 8
AURA_MOD_DAMAGE_PERCENT_DONE = 79   # +% damage; miscvalue = affected school mask
AURA_MOD_MANA_REGEN_INTERRUPT = 134  # % of spirit mana regen kept under the FSR
AURA_PERIODIC_DUMMY = 226
TARGET_UNIT_CASTER = 1
TARGET_UNIT_TARGET_ENEMY = 6
TARGET_UNIT_DEST_AREA_ENEMY = 16  # enemies around a cast destination
TARGET_UNIT_TARGET_ALLY = 21
TARGET_UNIT_LASTTARGET_AREA_PARTY = 37  # the target's party within radius
SCHOOL_MASK_FIRE = 1 << 2    # 4
SCHOOL_MASK_ARCANE = 1 << 6  # 64
SCHOOL_MASK_SPELLFIRE = SCHOOL_MASK_FIRE | SCHOOL_MASK_ARCANE  # 68 (SoD "Spellfire")
SCHOOL_MASK_MAGIC = 126  # all six magic schools (Holy..Arcane), excludes Physical
DISPEL_MAGIC = 1
NAME_MASK = 16712190  # standard "enUS available" locale mask
# Proc-on-caster masks (harmful magic / none-class spell damage / ranged auto).
PROC_DONE_MAGIC_NEG = 0x00010000
PROC_DONE_NONE_NEG = 0x00001000
PROC_DONE_RANGED_AUTO = 0x00000040

# spell_proc table column order (named-column INSERT).
SPELL_PROC_COLUMNS = [
    "SpellId", "SchoolMask", "SpellFamilyName", "SpellFamilyMask0",
    "SpellFamilyMask1", "SpellFamilyMask2", "ProcFlags", "SpellTypeMask",
    "SpellPhaseMask", "HitMask", "AttributesMask", "DisableEffectsMask",
    "ProcsPerMinute", "Chance", "Cooldown", "Charges",
]

# Inner MPQ paths.
ITEM_INNER = "DBFilesClient\\Item.dbc"
IDI_INNER = "DBFilesClient\\ItemDisplayInfo.dbc"
SPELL_INNER = "DBFilesClient\\Spell.dbc"
SKILL_INNER = "DBFilesClient\\SkillLineAbility.dbc"
SPELLVIS_INNER = "DBFilesClient\\SpellVisual.dbc"
CDI_INNER = "DBFilesClient\\CreatureDisplayInfo.dbc"
CDIE_INNER = "DBFilesClient\\CreatureDisplayInfoExtra.dbc"
FACTION_INNER = "DBFilesClient\\Faction.dbc"
FACTIONTPL_INNER = "DBFilesClient\\FactionTemplate.dbc"

# Faction.dbc (3.3.5a): Name_Lang_enUS is field 23, Name_Lang_Mask is field 39.
FACTION_NAME_FIELD = 23
FACTION_NAME_MASK_FIELD = 39
# Reputation fields (each *_FIELD is the first of 4 slots, except the parent
# ones): ReputationIndex(1), RaceMask(2-5), ClassMask(6-9), Base(10-13),
# Flags(14-17), ParentFactionID(18), ParentFactionMod(19-20 floats),
# ParentFactionCap(21-22).
FACTION_REPIDX_FIELD = 1
FACTION_RACEMASK_FIELD = 2
FACTION_CLASSMASK_FIELD = 6
FACTION_BASE_FIELD = 10
FACTION_FLAGS_FIELD = 14
FACTION_PARENT_FIELD = 18
FACTION_PARENTMOD_FIELD = 19
FACTION_PARENTCAP_FIELD = 21

# ItemDisplayInfo.dbc (3.3.5a): 25 int fields, field 5 = InventoryIcon[0].
IDI_ICON_FIELD = 5


# ---------------------------------------------------------------------------
# WDBC reader/writer (3.3.5a fixed-width records + trailing string block).
# ---------------------------------------------------------------------------
class WDBC:
    def __init__(self, raw):
        magic, self.nrec, self.nfield, self.recsize, self.strsize = \
            struct.unpack("<4siiii", raw[:20])
        if magic != b"WDBC":
            raise ValueError("not a WDBC file")
        body = 20 + self.nrec * self.recsize
        self.records = [bytearray(raw[20 + i * self.recsize:
                                      20 + (i + 1) * self.recsize])
                        for i in range(self.nrec)]
        self.strings = bytearray(raw[body:body + self.strsize])

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            return cls(fh.read())

    def get_int(self, rec, field):
        return struct.unpack_from("<i", rec, field * 4)[0]

    def set_int(self, rec, field, value):
        struct.pack_into("<i", rec, field * 4, int(value))

    def set_float(self, rec, field, value):
        struct.pack_into("<f", rec, field * 4, float(value))

    def add_string(self, text):
        """Append a string, return its offset within the string block."""
        offset = len(self.strings)
        self.strings += text.encode("utf-8") + b"\x00"
        return offset

    def find(self, row_id):
        for rec in self.records:
            if self.get_int(rec, 0) == row_id:
                return rec
        raise KeyError(row_id)

    def serialize(self):
        out = bytearray()
        out += struct.pack("<4siiii", b"WDBC", len(self.records),
                           self.nfield, self.recsize, len(self.strings))
        for rec in self.records:
            out += rec
        out += self.strings
        return bytes(out)


def load_aux(path):
    """Parse a simple int-only aux DBC into a list of field tuples."""
    with open(path, "rb") as fh:
        raw = fh.read()
    _, nrec, nfield, recsize, _ = struct.unpack("<4siiii", raw[:20])
    rows = []
    for i in range(nrec):
        off = 20 + i * recsize
        rows.append(struct.unpack_from("<%di" % nfield, raw, off))
    return rows, raw


def load_columns(table_sql_path):
    """Column-name -> field-index map parsed from the core spell_dbc table def
    (index in the returned list == DBC field index)."""
    cols = []
    with open(table_sql_path, encoding="utf-8") as fh:
        in_table = False
        for line in fh:
            if "CREATE TABLE" in line and "spell_dbc" in line:
                in_table = True
                continue
            if in_table:
                if line.lstrip().startswith("PRIMARY KEY") or \
                        line.lstrip().startswith(")"):
                    break
                m = re.match(r"\s*`([A-Za-z0-9_]+)`", line)
                if m:
                    cols.append(m.group(1))
    if len(cols) < 200:
        raise RuntimeError("failed to parse spell_dbc columns (%d)" % len(cols))
    return cols


# ---------------------------------------------------------------------------
# Manifest aggregation (data-only contract with the content modules).
# ---------------------------------------------------------------------------
def _load_manifests(modules_dir, filename):
    rows = {}
    pattern = os.path.join(modules_dir, "mod-sod-*", "tools", filename)
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print("[!] skipping %s: %s" % (path, exc))
            continue
        module = os.path.basename(os.path.dirname(os.path.dirname(path)))
        for row in data:
            rid = int(row["id"])
            if rid in rows and rows[rid][1] != row:
                print("[!] conflict on id %d: %s overrides %s"
                      % (rid, module, rows[rid][0]))
            rows[rid] = (module, row)
    return [r for _, r in sorted(rows.values(), key=lambda kv: int(kv[1]["id"]))]


def load_items(modules_dir):
    return _load_manifests(modules_dir, "client_items.json")


def load_displays(modules_dir):
    return _load_manifests(modules_dir, "client_displays.json")


def load_creature_displays(modules_dir):
    return _load_manifests(modules_dir, "client_creature_displays.json")


def load_factions(modules_dir):
    return _load_manifests(modules_dir, "client_factions.json")


# ---------------------------------------------------------------------------
# MPQ extraction / packing (pympq / StormLib).
# ---------------------------------------------------------------------------
def mpq_priority(path):
    """Approximate WoW's archive load priority (higher wins).

    Patch suffixes load after the plain archive in the order 2..9 then a..z, so
    `patch-enus-s.mpq` overrides `patch-enus.mpq` even though it sorts earlier
    alphabetically. Non-patch base archives rank lowest.
    """
    name = os.path.basename(path).lower()
    m = re.match(r"patch(?:-[a-z]{2,4})?(?:-([0-9a-z]+))?\.mpq$", name)
    if not m:
        return (0, 0)
    suffix = m.group(1) or ""
    if suffix == "":
        rank = 0
    elif suffix.isdigit():
        rank = int(suffix)
    else:
        rank = 10 + (ord(suffix[0]) - ord("a"))
    return (1, rank)


def detect_locale(client_dir):
    """Return the client's locale folder token under Data/ (the one holding the
    `locale-*.mpq` archives), e.g. 'enus' or 'dede'. Preserves the on-disk case
    so derived patch names match the client. Defaults to 'enus' if none found."""
    data = os.path.join(client_dir, "data")
    try:
        for d in sorted(os.listdir(data)):
            p = os.path.join(data, d)
            if os.path.isdir(p) and any(
                    f.lower().startswith("locale-") and f.lower().endswith(".mpq")
                    for f in os.listdir(p)):
                return d
    except OSError:
        pass
    return "enus"


def our_patch_names(locale, custom_letters):
    """Our output patch filenames in both chains for every custom letter — used
    to exclude them when reading clean client data (never build on our own
    output; legitimate HD base patches are kept)."""
    names = set()
    for letter in custom_letters:
        names.add(("patch-%s.mpq" % letter).lower())               # base chain
        names.add(("patch-%s-%s.mpq" % (locale, letter)).lower())  # locale chain
    return names


def extract_client_dbc(client_dir, name, dest, locale, custom_letters):
    """Extract `name` from the highest-priority archive that is not one of our
    own patches (so we build on the clean client base)."""
    import pympq
    inner = "DBFilesClient\\" + name
    ignore = our_patch_names(locale, custom_letters)
    base = os.path.join(client_dir, "data")
    locale_dir = os.path.join(base, locale)
    search = []
    for d in (base, locale_dir):
        if os.path.isdir(d):
            search += [os.path.join(d, f) for f in os.listdir(d)
                       if f.lower().endswith(".mpq")
                       and f.lower() not in ignore]
    found = None
    best = (-1, -1)
    for p in search:
        try:
            m = pympq.open_archive(p, None)
        except Exception:
            continue
        try:
            if m.has_file(inner) and mpq_priority(p) > best:
                best = mpq_priority(p)
                found = p
        finally:
            m.close()
    if not found:
        raise RuntimeError("not found in any archive: " + name)
    m = pympq.open_archive(found, None)
    try:
        m.extract_file(inner, dest)
    finally:
        m.close()
    return found


def pack_mpq(files, out_mpq):
    """files: list of (src_path, inner_mpq_path) to add to a fresh patch MPQ."""
    import pympq
    if os.path.exists(out_mpq):
        os.remove(out_mpq)
    m = pympq.create_archive(
        out_mpq,
        [pympq.MPQ_CREATE_ARCHIVE_V1, pympq.MPQ_CREATE_LISTFILE,
         pympq.MPQ_CREATE_ATTRIBUTES],
        8)
    try:
        for src, inner in files:
            m.add_file(src, inner,
                       [pympq.MPQ_FILE_COMPRESS, pympq.MPQ_FILE_REPLACEEXISTING],
                       [pympq.MPQ_COMPRESSION_ZLIB])
    finally:
        m.close()


# ---------------------------------------------------------------------------
# Index/icon resolvers (read the client's own aux DBCs from the workdir).
# ---------------------------------------------------------------------------
def resolve_indexes(workdir):
    idx = {"cast": {}, "dur": {}, "range": {}, "icon": {}}

    rows, _ = load_aux(os.path.join(workdir, "SpellCastTimes.dbc"))
    for r in rows:  # id, base, perlevel, min
        idx["cast"].setdefault(r[1], r[0])

    rows, _ = load_aux(os.path.join(workdir, "SpellDuration.dbc"))
    for r in rows:  # id, duration, perlevel, max
        idx["dur"].setdefault(r[1], r[0])

    with open(os.path.join(workdir, "SpellRange.dbc"), "rb") as fh:
        raw = fh.read()
    _, nrec, nfield, recsize, _ = struct.unpack("<4siiii", raw[:20])
    for i in range(nrec):
        rid = struct.unpack_from("<i", raw, 20 + i * recsize)[0]
        maxr = struct.unpack_from("<f", raw, 20 + i * recsize + 12)[0]  # RangeMax[0]
        idx["range"].setdefault(round(maxr, 3), rid)

    with open(os.path.join(workdir, "SpellIcon.dbc"), "rb") as fh:
        raw = fh.read()
    _, nrec, nfield, recsize, _ = struct.unpack("<4siiii", raw[:20])
    body = 20 + nrec * recsize
    sb = raw[body:]
    for i in range(nrec):
        iid, off = struct.unpack_from("<ii", raw, 20 + i * recsize)
        end = sb.find(b"\x00", off)
        name = sb[off:end].decode("latin-1").replace("/", "\\").split("\\")[-1].lower()
        idx["icon"].setdefault(name, iid)
    return idx


# ---------------------------------------------------------------------------
# Item.dbc / ItemDisplayInfo.dbc builders (item/display manifests).
# ---------------------------------------------------------------------------
def build_item_dbc(workdir, items):
    """Item.dbc rows: ID, ClassID, SubclassID, SoundOverrideSubclassID(-1),
    Material, DisplayInfoID, InventoryType, SheatheType."""
    item = WDBC.load(os.path.join(workdir, "Item.dbc"))
    existing = {item.get_int(r, 0) for r in item.records}
    for it in items:
        if it["id"] in existing:
            rec = item.find(it["id"])      # re-runnable: update in place
        else:
            rec = bytearray(item.recsize)
            item.records.append(rec)
        item.set_int(rec, 0, it["id"])
        item.set_int(rec, 1, it["class"])
        item.set_int(rec, 2, it["subclass"])
        item.set_int(rec, 3, -1)
        item.set_int(rec, 4, it["material"])
        item.set_int(rec, 5, it["display"])
        item.set_int(rec, 6, it["invtype"])
        item.set_int(rec, 7, it["sheath"])
        print("[*] Item.dbc: %d (%s) -> display %d"
              % (it["id"], it["name"], it["display"]))
    out = os.path.join(workdir, "Item.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(item.serialize())
    return out


def build_item_display_info(workdir, displays):
    """ItemDisplayInfo.dbc rows for custom displayids: set only the id and
    InventoryIcon[0]; all model/texture fields stay empty (these displays are
    only ever resolved for a bag icon, never equipped)."""
    if not displays:
        return None
    idi = WDBC.load(os.path.join(workdir, "ItemDisplayInfo.dbc"))
    existing = {idi.get_int(r, 0) for r in idi.records}
    for disp in displays:
        if disp["id"] in existing:
            rec = idi.find(disp["id"])
        else:
            rec = bytearray(idi.recsize)
            idi.records.append(rec)
        idi.set_int(rec, 0, disp["id"])
        idi.set_int(rec, IDI_ICON_FIELD, idi.add_string(disp["icon"]))
        print("[*] ItemDisplayInfo.dbc: %d -> icon %s (%s)"
              % (disp["id"], disp["icon"], disp["name"]))
    out = os.path.join(workdir, "ItemDisplayInfo.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(idi.serialize())
    return out


# ---------------------------------------------------------------------------
# CreatureDisplayInfo / CreatureDisplayInfoExtra / Faction / FactionTemplate
# builders. Custom NpcCharacter displays and custom factions (3.3.5a layouts,
# matching the core's `<name>_dbc` override tables). These rows are appended (or
# updated in place on re-run) so the client renders the custom model and shows the
# faction name; the matching SERVER rows are hand-written in the module SQL.
# ---------------------------------------------------------------------------
def _find_or_append(store, row_id):
    """Return the record with id `row_id`, or a fresh zeroed record appended."""
    for rec in store.records:
        if store.get_int(rec, 0) == row_id:
            return rec
    rec = bytearray(store.recsize)
    store.records.append(rec)
    return rec


def build_creature_display_info(workdir, displays):
    """CreatureDisplayInfo.dbc rows: ID, ModelID, SoundID, ExtendedDisplayInfoID,
    CreatureModelScale(float), CreatureModelAlpha. Model/texture strings stay empty.
    Alpha must be 255 or the model is invisible."""
    if not displays:
        return None
    cdi = WDBC.load(os.path.join(workdir, "CreatureDisplayInfo.dbc"))
    for d in displays:
        rec = _find_or_append(cdi, d["id"])
        cdi.set_int(rec, 0, d["id"])
        cdi.set_int(rec, 1, d["model"])
        cdi.set_int(rec, 2, d.get("sound", 0))
        cdi.set_int(rec, 3, d.get("extended", 0))
        cdi.set_float(rec, 4, d.get("scale", 1.0))
        cdi.set_int(rec, 5, d.get("alpha", 255))
        print("[*] CreatureDisplayInfo.dbc: %d (%s) -> model %d, extra %d"
              % (d["id"], d.get("name", ""), d["model"], d.get("extended", 0)))
    out = os.path.join(workdir, "CreatureDisplayInfo.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(cdi.serialize())
    return out


def build_creature_display_info_extra(workdir, displays):
    """CreatureDisplayInfoExtra.dbc rows: race/sex/skin/face/hair + NPCItemDisplay
    geosets (11) + Flags + BakeName. Reads each display's embedded `extra`."""
    extras = [d["extra"] for d in displays if d.get("extra")]
    if not extras:
        return None
    cdie = WDBC.load(os.path.join(workdir, "CreatureDisplayInfoExtra.dbc"))
    for e in extras:
        rec = _find_or_append(cdie, e["id"])
        cdie.set_int(rec, 0, e["id"])
        cdie.set_int(rec, 1, e.get("race", 1))
        cdie.set_int(rec, 2, e.get("sex", 1))
        cdie.set_int(rec, 3, e.get("skin", 0))
        cdie.set_int(rec, 4, e.get("face", 0))
        cdie.set_int(rec, 5, e.get("hair_style", 0))
        cdie.set_int(rec, 6, e.get("hair_color", 0))
        cdie.set_int(rec, 7, e.get("facial_hair", 0))
        items = (e.get("items", []) + [0] * 11)[:11]
        for k, it in enumerate(items):
            cdie.set_int(rec, 8 + k, it)
        cdie.set_int(rec, 19, e.get("flags", 0))
        cdie.set_int(rec, 20, cdie.add_string(e.get("bake_name", "")))
        print("[*] CreatureDisplayInfoExtra.dbc: %d -> race %d sex %d items %s"
              % (e["id"], e.get("race", 1), e.get("sex", 1), items))
    out = os.path.join(workdir, "CreatureDisplayInfoExtra.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(cdie.serialize())
    return out


def build_faction(workdir, factions):
    """Faction.dbc rows: ID, ReputationIndex, Name_Lang_enUS, Name_Lang_Mask. A
    non-reputation faction (rep_index -1) just provides the unit-tooltip name; a
    real reputation faction additionally carries a `reputation` block (race/class
    masks, base, flags, parent category) so it renders in the client's rep pane."""
    if not factions:
        return None
    fac = WDBC.load(os.path.join(workdir, "Faction.dbc"))
    for f in factions:
        rec = _find_or_append(fac, f["id"])
        fac.set_int(rec, 0, f["id"])
        fac.set_int(rec, FACTION_REPIDX_FIELD, f.get("rep_index", -1))
        fac.set_int(rec, FACTION_NAME_FIELD, fac.add_string(f["name"]))
        fac.set_int(rec, FACTION_NAME_MASK_FIELD, NAME_MASK)
        rep = f.get("reputation")
        if rep:
            for i in range(4):
                fac.set_int(rec, FACTION_RACEMASK_FIELD + i, rep["race_mask"][i])
                fac.set_int(rec, FACTION_CLASSMASK_FIELD + i, rep["class_mask"][i])
                fac.set_int(rec, FACTION_BASE_FIELD + i, rep["base"][i])
                fac.set_int(rec, FACTION_FLAGS_FIELD + i, rep["flags"][i])
            fac.set_int(rec, FACTION_PARENT_FIELD, rep.get("parent", 0))
            for i in range(2):
                fac.set_float(rec, FACTION_PARENTMOD_FIELD + i,
                              rep.get("parent_mod", [0, 0])[i])
                fac.set_int(rec, FACTION_PARENTCAP_FIELD + i,
                            rep.get("parent_cap", [0, 0])[i])
        print("[*] Faction.dbc: %d -> '%s' (rep_index %d)"
              % (f["id"], f["name"], f.get("rep_index", -1)))
    out = os.path.join(workdir, "Faction.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(fac.serialize())
    return out


def build_faction_template(workdir, factions):
    """FactionTemplate.dbc rows: ID, Faction, Flags, FactionGroup, FriendGroup,
    EnemyGroup, Enemies[4], Friend[4]. Reads each faction's embedded `template`."""
    tpls = [f["template"] for f in factions if f.get("template")]
    if not tpls:
        return None
    ft = WDBC.load(os.path.join(workdir, "FactionTemplate.dbc"))
    for t in tpls:
        rec = _find_or_append(ft, t["id"])
        ft.set_int(rec, 0, t["id"])
        ft.set_int(rec, 1, t.get("faction", 0))
        ft.set_int(rec, 2, t.get("flags", 0))
        ft.set_int(rec, 3, t.get("faction_group", 0))
        ft.set_int(rec, 4, t.get("friend_group", 0))
        ft.set_int(rec, 5, t.get("enemy_group", 0))
        enemies = (t.get("enemies", []) + [0] * 4)[:4]
        friends = (t.get("friends", []) + [0] * 4)[:4]
        for k, e in enumerate(enemies):
            ft.set_int(rec, 6 + k, e)
        for k, fr in enumerate(friends):
            ft.set_int(rec, 10 + k, fr)
        print("[*] FactionTemplate.dbc: %d -> faction %d (friend %d/enemy %d)"
              % (t["id"], t.get("faction", 0), t.get("friend_group", 0),
                 t.get("enemy_group", 0)))
    out = os.path.join(workdir, "FactionTemplate.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(ft.serialize())
    return out


# ---------------------------------------------------------------------------
# Spell.dbc / SkillLineAbility.dbc / SpellVisual.dbc builders (spell specs).
# ---------------------------------------------------------------------------
def build_spell_dbc(workdir, cols, spells):
    """Clone each `client` spell's `template` row, apply `overrides` (+ client-
    only tooltip overrides), set ID/Name/Description, and append it. Returns the
    patched file path."""
    spell = WDBC.load(os.path.join(workdir, "Spell.dbc"))
    field_of = {c: i for i, c in enumerate(cols)}
    existing = {spell.get_int(r, 0) for r in spell.records}
    for s in spells:
        if not s["client"]:
            continue
        if s["id"] in existing:
            raise RuntimeError("ID already exists in Spell.dbc: %d" % s["id"])
        base = bytearray(spell.find(s["template"]))
        for col, val in s["overrides"].items():
            spell.set_int(base, field_of[col], val)
        # Client-only tooltip level-scaling fields: written to the client DBC so
        # the client can render a dynamic tooltip, but kept out of the server SQL
        # row (emit_spell_sql ignores these) so server behavior stays scripted.
        for col, val in s.get("client_overrides", {}).items():
            spell.set_int(base, field_of[col], val)
        for col, val in s.get("client_overrides_float", {}).items():
            spell.set_float(base, field_of[col], val)
        spell.set_int(base, field_of["ID"], s["id"])
        spell.set_int(base, field_of["Name_Lang_enUS"], spell.add_string(s["name"]))
        spell.set_int(base, field_of["Name_Lang_Mask"], NAME_MASK)
        if s.get("desc"):
            spell.set_int(base, field_of["Description_Lang_enUS"],
                          spell.add_string(s["desc"]))
            spell.set_int(base, field_of["Description_Lang_Mask"], NAME_MASK)
        if s.get("aura_desc"):
            spell.set_int(base, field_of["AuraDescription_Lang_enUS"],
                          spell.add_string(s["aura_desc"]))
            spell.set_int(base, field_of["AuraDescription_Lang_Mask"], NAME_MASK)
        spell.records.append(base)
        print("[*] client row added: %d (%s) from template %d"
              % (s["id"], s["name"], s["template"]))
    out = os.path.join(workdir, "Spell.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(spell.serialize())
    print("[*] wrote patched Spell.dbc (%d records)" % len(spell.records))
    return out


def build_skill_line_ability(workdir, spells):
    """Append SkillLineAbility rows so categorized spells land in the right
    spellbook tab (the client groups known spells into tabs by skill line).
    Returns the patched path, or None if no spell opts in.

    Rows are zeroed except ID / SkillLine / Spell: no race or class restriction,
    no skill-rank requirement, AcquireMethod 0 — purely display categorization.
    """
    targets = [s for s in spells if s.get("client") and s.get("skill_line")]
    if not targets:
        return None
    sla = WDBC.load(os.path.join(workdir, "SkillLineAbility.dbc"))
    next_id = max(sla.get_int(r, 0) for r in sla.records) + 1
    for s in targets:
        rec = bytearray(sla.recsize)
        sla.set_int(rec, 0, next_id)          # ID
        sla.set_int(rec, 1, s["skill_line"])  # SkillLine
        sla.set_int(rec, 2, s["id"])          # Spell
        sla.records.append(rec)
        print("[*] SkillLineAbility row added: spell %d -> skill line %d (id %d)"
              % (s["id"], s["skill_line"], next_id))
        next_id += 1
    out = os.path.join(workdir, "SkillLineAbility.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(sla.serialize())
    return out


def build_spell_visual(workdir, visuals):
    """Add custom SpellVisual rows. Each `visuals` entry is a dict:
        {"id": <new id>, "clone_from": <existing id>, "zero_fields": [<field>...]}
    The row is cloned from `clone_from`, its id set to `id`, and each listed
    field index zeroed (e.g. drop an on-target impact kit). Returns the patched
    path, or None when no module declares a custom visual."""
    if not visuals:
        return None
    sv = WDBC.load(os.path.join(workdir, "SpellVisual.dbc"))
    for v in visuals:
        rec = bytearray(sv.find(v["clone_from"]))
        sv.set_int(rec, 0, v["id"])
        for field in v.get("zero_fields", []):
            sv.set_int(rec, field, 0)
        sv.records.append(rec)
        print("[*] SpellVisual row added: %d (clone of %d, zeroed %s)"
              % (v["id"], v["clone_from"], v.get("zero_fields", [])))
    out = os.path.join(workdir, "SpellVisual.dbc.patched")
    with open(out, "wb") as fh:
        fh.write(sv.serialize())
    return out


# ---------------------------------------------------------------------------
# Server spell_dbc SQL emission (one file per content module).
# ---------------------------------------------------------------------------
def emit_spell_sql(spells, cols, module, retired_ids=None):
    """Emit the server-side SQL for one module's spells: spell_dbc +
    spell_script_names + spell_bonus_data + spell_proc, each as an idempotent
    upsert (REPLACE, keyed on the natural PK). `module` is the folder name (e.g.
    mod-sod-mage) used in comments.

    Committed SQL must never DELETE -- it runs on every end-user DB and could wipe
    rows that aren't ours (see the module CLAUDE.md SQL rules). `retired_ids` is
    accepted for call compatibility but intentionally NOT emitted as deletes; clean
    retired ids by hand on the test DB."""
    out = [
        "-- %s: server-side spell_dbc rows. GENERATED by sod-client" % module,
        "-- (build_patch.py) — do not edit by hand. The 4xxxxx IDs are mirrored",
        "-- by the consolidated client MPQ patch.",
        "--",
        "-- Idempotent via REPLACE / ON DUPLICATE KEY UPDATE -- never DELETEs (it",
        "-- runs on every end-user DB). Retired ids are cleaned by hand, not here.",
        "",
    ]
    for s in spells:
        row = dict(s["overrides"])
        row["ID"] = s["id"]
        row["Name_Lang_enUS"] = s["name"]
        row["Name_Lang_Mask"] = NAME_MASK
        colnames = []
        values = []
        for col in cols:  # stable column order
            if col in row:
                colnames.append("`%s`" % col)
                v = row[col]
                values.append("'%s'" % v.replace("'", "''") if isinstance(v, str)
                              else str(v))
        out.append("REPLACE INTO `spell_dbc` (%s) VALUES (%s);"
                   % (", ".join(colnames), ", ".join(values)))

    scripted = [s for s in spells if s.get("script")]
    if scripted:
        out.append("")
        vals = ",\n".join("(%d,'%s')" % (s["id"], s["script"]) for s in scripted)
        out.append("REPLACE INTO `spell_script_names` "
                   "(`spell_id`, `ScriptName`) VALUES\n%s;" % vals)

    bonuses = [s for s in spells if s.get("bonus")]
    if bonuses:
        out.append("")
        for s in bonuses:
            b = s["bonus"]
            out.append("REPLACE INTO `spell_bonus_data` "
                       "(`entry`, `direct_bonus`, `dot_bonus`, `ap_bonus`, "
                       "`ap_dot_bonus`, `comments`) "
                       "VALUES (%d, %s, %s, %s, %s, '%s %s');"
                       % (s["id"], b["direct"], b["dot"], b["ap"], b["ap_dot"],
                          module, s["name"]))

    procs = [s for s in spells if s.get("proc")]
    if procs:
        out.append("")
        for s in procs:
            row = dict(s["proc"])
            row["SpellId"] = s["id"]
            vals = ", ".join(str(row[c]) for c in SPELL_PROC_COLUMNS)
            cn = ", ".join("`%s`" % c for c in SPELL_PROC_COLUMNS)
            out.append("REPLACE INTO `spell_proc` (%s) VALUES (%s);" % (cn, vals))
    return "\n".join(out) + "\n"
