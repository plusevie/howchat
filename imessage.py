"""Read chats straight from the macOS Messages database (~/Library/Messages/chat.db).

Needs Full Disk Access for whatever runs the script (Terminal, iTerm, VS Code...):
System Settings > Privacy & Security > Full Disk Access.
The database is copied to a temp folder first and opened read-only, so Messages is never touched.
"""
import glob
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK = os.path.expanduser("~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb")
APPLE_EPOCH = datetime(2001, 1, 1)

FDA_HELP = """
Can't read the Messages database — macOS needs to give Full Disk Access to the app running this script.
  System Settings > Privacy & Security > Full Disk Access > turn on Terminal (or iTerm / VS Code / Claude)
Then quit and reopen that app and run this again.
"""


def _copy_db(path):
    """Copy a SQLite db (+ WAL files, which hold the newest messages) and open the copy read-only."""
    tmp = tempfile.mkdtemp(prefix="wac-")
    dst = os.path.join(tmp, os.path.basename(path))
    try:
        shutil.copy2(path, dst)
    except PermissionError:
        sys.exit(FDA_HELP)
    except FileNotFoundError:
        sys.exit(f"No database at {path}")
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            shutil.copy2(path + ext, dst + ext)
    con = sqlite3.connect(dst)
    con.row_factory = sqlite3.Row
    return con


def _phone_key(s):
    digits = re.sub(r"\D", "", s or "")
    return digits[-9:] if len(digits) >= 7 else None


def load_contacts(pattern=ADDRESSBOOK):
    """From the local Contacts db, return:
    names  – phone number (last 9 digits) / email -> contact name
    links  – union-find over those keys: every number/email on one contact card is linked,
             so "same person, different address" can be merged (two different Rohans stay apart).
    """
    names, parent = {}, {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for path in glob.glob(pattern, recursive=True):
        try:
            con = _copy_db(path)
        except SystemExit:
            continue
        try:
            people, keys = {}, defaultdict(list)
            for r in con.execute("SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD"):
                name = " ".join(x for x in (r["ZFIRSTNAME"], r["ZLASTNAME"]) if x) or r["ZNICKNAME"] or r["ZORGANIZATION"]
                if name:
                    people[r["Z_PK"]] = name
            for r in con.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                k = _phone_key(r["ZFULLNUMBER"])
                if k and r["ZOWNER"] in people:
                    keys[r["ZOWNER"]].append(k)
            for r in con.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if r["ZADDRESS"] and r["ZOWNER"] in people:
                    keys[r["ZOWNER"]].append(r["ZADDRESS"].strip().lower())
            for owner, ks in keys.items():
                for k in ks:
                    names.setdefault(k, people[owner])
                    parent[find(k)] = find(ks[0])
        except sqlite3.DatabaseError:
            pass
        finally:
            con.close()
    return names, find


def decode_attributed_body(blob):
    """Pull the plain text out of the NSAttributedString 'typedstream' blob newer macOS stores."""
    if not blob:
        return None
    blob = bytes(blob)
    i = blob.find(b"NSString")
    if i < 0:
        return None
    rest = blob[i + len(b"NSString") + 5:]
    if not rest:
        return None
    if rest[0] == 0x81:          # 2-byte little-endian length
        n, start = int.from_bytes(rest[1:3], "little"), 3
    elif rest[0] == 0x82:        # 3-byte length
        n, start = int.from_bytes(rest[1:4], "little"), 4
    else:
        n, start = rest[0], 1
    return rest[start:start + n].decode("utf-8", errors="replace")


def _date(raw):
    if not raw:
        return None
    secs = raw / 1e9 if raw > 1e11 else raw   # nanoseconds since 2001 on newer macOS
    return APPLE_EPOCH + timedelta(seconds=secs)


class Messages:
    def __init__(self, db=CHAT_DB, addressbook=ADDRESSBOOK, me="Me"):
        self.con = _copy_db(db)
        self.contacts, self._find = load_contacts(addressbook)
        self.me = me
        self.handles = {r["ROWID"]: r["id"] for r in self.con.execute("SELECT ROWID, id FROM handle")}
        self.cols = {r[1] for r in self.con.execute("PRAGMA table_info(message)")}

    def name_for(self, handle):
        if not handle:
            return "Unknown"
        return (self.contacts.get(handle.strip().lower()) or self.contacts.get(_phone_key(handle))
                or handle)

    def person_key(self, handle):
        """Same key for every number/email belonging to one contact card."""
        h = (handle or "").strip().lower()
        k = h if "@" in h else (_phone_key(h) or h)
        return self._find(k) if k in self.contacts else k

    def chats(self):
        """One entry per conversation (SMS + iMessage threads with the same person are merged)."""
        rows = self.con.execute("""
            SELECT c.ROWID id, c.chat_identifier ident, c.display_name dname, c.style style,
                   COUNT(cmj.message_id) n, MAX(m.date) last
            FROM chat c
            LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            LEFT JOIN message m ON m.ROWID = cmj.message_id
            GROUP BY c.ROWID""").fetchall()
        members = defaultdict(list)
        for r in self.con.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
            members[r["chat_id"]].append(self.name_for(self.handles.get(r["handle_id"])))

        merged = {}
        for r in rows:
            # group chats: by their own id; 1:1 chats: by person, so phone + email threads merge
            key = ("g", r["ident"]) if r["style"] == 43 else ("p", self.person_key(r["ident"]))
            e = merged.setdefault(key, {"ids": [], "n": 0, "last": 0, "dname": None,
                                               "group": r["style"] == 43, "ident": r["ident"],
                                               "members": set()})
            e["ids"].append(r["id"])
            e["n"] += r["n"] or 0
            e["last"] = max(e["last"], r["last"] or 0)
            e["dname"] = e["dname"] or r["dname"]
            e["members"].update(members[r["id"]])
        out = []
        for e in merged.values():
            if e["dname"]:
                title = e["dname"]
            elif e["group"]:
                ms = sorted(e["members"])
                title = ", ".join(ms[:3]) + (f" +{len(ms) - 3}" if len(ms) > 3 else "")
            else:
                title = self.name_for(e["ident"])
            e["title"] = title or e["ident"]
            out.append(e)
        out = [e for e in out if e["n"]]
        # different people/groups that still share a display name: tell them apart
        dupes = Counter(e["title"] for e in out)
        for e in out:
            if dupes[e["title"]] > 1:
                e["title"] = f'{e["title"]} ({e["ident"]})'

        out.sort(key=lambda e: -e["n"])
        for i, e in enumerate(out, 1):
            e["index"] = i
        return out

    def find(self, query, chats):
        q = query.strip().lstrip("#")
        if q.isdigit() and int(q) <= len(chats) and len(q) < 5:
            return chats[int(q) - 1]
        ql = q.lower()
        exact = [c for c in chats if c["title"].lower() == ql]
        if exact:
            return exact[0]
        part = [c for c in chats if ql in c["title"].lower() or ql in c["ident"].lower()
                or (_phone_key(q) and _phone_key(q) == _phone_key(c["ident"]))]
        if len(part) > 1:
            others = ", ".join(f'#{c["index"]} {c["title"]}' for c in part[1:5])
            print(f'"{query}" matches several chats; using #{part[0]["index"]} {part[0]["title"]} '
                  f"(others: {others})")
        return part[0] if part else None

    def messages(self, chat):
        """Yield (date 'dd/mm/yyyy', sender, text) oldest first, skipping reactions and system events."""
        extra = []
        if "associated_message_type" in self.cols:
            extra.append("COALESCE(m.associated_message_type, 0) = 0")
        if "item_type" in self.cols:
            extra.append("COALESCE(m.item_type, 0) = 0")
        body = "m.attributedBody" if "attributedBody" in self.cols else "NULL"
        where = " AND ".join(extra) or "1"
        ids = ",".join(str(i) for i in chat["ids"])
        sql = f"""
            SELECT m.date, m.is_from_me, m.handle_id, m.text, {body} AS body
            FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE cmj.chat_id IN ({ids}) AND {where}
            ORDER BY m.date"""
        for r in self.con.execute(sql):
            text = r["text"] or decode_attributed_body(r["body"])
            if not text:
                continue
            text = text.replace("￼", " ").strip()   # attachment placeholder
            if not text:
                continue
            if r["is_from_me"]:
                sender = self.me
            else:
                sender = self.name_for(self.handles.get(r["handle_id"]) or
                                       (None if chat["group"] else chat["ident"]))
            d = _date(r["date"])
            yield (d.strftime("%d/%m/%Y") if d else "", sender, text)
