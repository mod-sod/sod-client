import os, sys, struct
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sod_dbc as dbc

CLIENT = r"E:\Games\World of Warcraft 3.3.5a HD"
work = os.path.join(HERE, "_gc")
os.makedirs(work, exist_ok=True)
locale = dbc.detect_locale(CLIENT)
for name in ("CreatureDisplayInfo.dbc", "CreatureModelData.dbc"):
    dst = os.path.join(work, name)
    src = dbc.extract_client_dbc(CLIENT, name, dst, locale, ("z",))
    print("extracted %s <- %s" % (name, os.path.basename(src)))

def load(path):
    with open(path, "rb") as f:
        raw = f.read()
    _, nrec, nf, rs, _ = struct.unpack("<4siiii", raw[:20])
    body = 20 + nrec * rs
    return [raw[20 + i*rs:20 + (i+1)*rs] for i in range(nrec)], raw[body:]

def gi(r, i):
    return struct.unpack_from("<i", r, i*4)[0]

def rd(s, off):
    e = s.find(b"\x00", off); return s[off:e].decode("latin-1", "replace")

cdi, _ = load(os.path.join(work, "CreatureDisplayInfo.dbc"))
cmd, cstr = load(os.path.join(work, "CreatureModelData.dbc"))
cdi_by = {gi(r, 0): r for r in cdi}
mname = {gi(r, 0): rd(cstr, gi(r, 2)) for r in cmd}

for did in (27214, 7608, 24292, 27712, 23713, 30977):
    r = cdi_by.get(did)
    if not r:
        print(did, "MISSING in client"); continue
    mid = gi(r, 1)
    nm = mname.get(mid, "?")
    g = "FEMALE" if "female" in nm.lower() else ("MALE" if "male" in nm.lower() else "?")
    print("client display %-7d ModelID=%-5d %-7s %s" % (did, mid, g, nm))
