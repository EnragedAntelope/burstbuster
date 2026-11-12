"""The only module that moves files. Rules:
- Moves are same-volume renames into <root>/_BurstBuster_Holding/<run>/<relative path>. Never copy+delete.
- Every move is written to a journal (and committed) BEFORE it happens, so a crash can always be undone.
- Never overwrite anything. A file that changed since the scan is left alone.
- A shot's files (RAW pair, Live Photo video, sidecars) move together or not at all.
- Nothing is ever permanently deleted. The final step sends files to the Windows Recycle Bin, and
  only on drives that have one; Windows is told to warn before any permanent delete.
"""
import json
import os
import sqlite3
import time

from . import APP_DIR, HOLDING_DIR

JOURNAL = APP_DIR / "journal.sqlite"


def _db():
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(JOURNAL)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS moves(
        id INTEGER PRIMARY KEY, run TEXT, root TEXT, shot TEXT, src TEXT, dst TEXT,
        size INT, mtime INT, state TEXT, note TEXT, updated TEXT)""")
    return db


def _set(db, row_id, state, note=None):
    db.execute("UPDATE moves SET state=?, note=?, updated=? WHERE id=?",
               (state, note, time.strftime("%Y-%m-%d %H:%M:%S"), row_id))
    db.commit()


def new_run_id():
    return time.strftime("%Y-%m-%d_%H%M%S")


def holding_dir(root, run):
    return os.path.join(os.path.abspath(root), HOLDING_DIR, run)


def _move(src, dst):
    """Same-volume move that never replaces an existing file."""
    if os.name == "nt":
        os.rename(src, dst)  # Windows rename refuses to overwrite
    else:
        os.link(src, dst)  # POSIX rename would silently replace dst; link fails if dst exists
        os.unlink(src)


def recycle_supported(path):
    """(True, "") if files on this drive can go to the Recycle Bin, else (False, why)."""
    if os.name != "nt":
        return True, ""  # send2trash finds the right Trash per volume, and fails rather than deletes
    import ctypes
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive.startswith("\\\\"):
        return False, "Network folder: Windows has no Recycle Bin here."
    kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    if kind == 3:  # DRIVE_FIXED
        return True, ""
    if kind == 4:
        return False, "Network drive: Windows has no Recycle Bin here."
    if kind == 2:
        return False, "Removable drive: Windows may not have a Recycle Bin here."
    return False, "This drive type has no reliable Recycle Bin."


def set_aside(root, run, shots):
    """Move shots into the holding folder. shots: [{"id", "stat": [[path, size, mtime_ns], ...]}].
    Returns {"moved": [ids], "skipped": [[id, reason]]}."""
    root = os.path.abspath(root)
    hold = holding_dir(root, run)
    root_dev = os.stat(root).st_dev
    db = _db()
    moved, skipped = [], []
    for shot in shots:
        plan, problem = [], None
        for src, size, mtime in shot["stat"]:
            src = os.path.abspath(src)
            dst = os.path.join(hold, os.path.relpath(src, root))
            try:
                st = os.stat(src)
            except OSError:
                problem = f"{os.path.basename(src)} is no longer there"
                break
            if os.path.commonpath([root, src]) != root:
                problem = "file is outside the scanned folder"
            elif st.st_size != size or st.st_mtime_ns != mtime:
                problem = f"{os.path.basename(src)} changed since the scan"
            elif st.st_dev != root_dev:
                problem = "file is on a different drive than the scanned folder"
            elif os.path.lexists(dst):
                problem = "a file with that name is already in the holding folder"
            if problem:
                break
            plan.append((src, dst, size, mtime))
        if problem:
            skipped.append([shot["id"], problem])
            continue
        ids = []
        for src, dst, size, mtime in plan:  # journal first
            cur = db.execute("INSERT INTO moves(run, root, shot, src, dst, size, mtime, state, updated) "
                             "VALUES (?,?,?,?,?,?,?, 'planned', ?)",
                             (run, root, shot["id"], src, dst, size, mtime, time.strftime("%Y-%m-%d %H:%M:%S")))
            ids.append(cur.lastrowid)
        db.commit()
        done = []
        try:
            for row_id, (src, dst, _, _) in zip(ids, plan):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _move(src, dst)  # same volume; fails rather than overwrite
                _set(db, row_id, "moved")
                done.append((row_id, src, dst))
        except OSError as e:
            for row_id, src, dst in reversed(done):  # all or nothing
                _move(dst, src)
                _set(db, row_id, "rolledback", str(e))
            for row_id in ids[len(done):]:
                _set(db, row_id, "failed", str(e))
            skipped.append([shot["id"], f"could not move: {e.strerror or e}"])
            continue
        moved.append(shot["id"])
    _write_manifest(db, run, hold)
    db.close()
    return {"moved": moved, "skipped": skipped}


def _write_manifest(db, run, hold):
    """Human-readable copy of the journal inside the holding folder: where each file came from."""
    rows = db.execute("SELECT src, dst, state FROM moves WHERE run=? AND state='moved'", (run,)).fetchall()
    if rows and os.path.isdir(hold):
        tmp = os.path.join(hold, "where-these-came-from.json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([{"original": s, "now": d} for s, d, _ in rows], f, indent=1)
        os.replace(tmp, os.path.join(hold, "where-these-came-from.json"))


def restore(run, shot_ids=None):
    """Move files back to where they came from. Never overwrites. Returns {"restored", "skipped"}."""
    db = _db()
    q = "SELECT id, shot, src, dst, root FROM moves WHERE run=? AND state='moved'"
    rows = db.execute(q, (run,)).fetchall()
    restored, skipped = set(), []
    hold = None
    for row_id, shot, src, dst, root in rows:
        hold = holding_dir(root, run)
        if shot_ids is not None and shot not in shot_ids:
            continue
        if os.path.lexists(src):
            skipped.append([shot, f"{os.path.basename(src)} already exists in the original folder"])
            continue
        if not os.path.exists(dst):
            skipped.append([shot, f"{os.path.basename(dst)} is missing from the holding folder"])
            continue
        os.makedirs(os.path.dirname(src), exist_ok=True)
        _move(dst, src)
        _set(db, row_id, "restored")
        restored.add(shot)
    if hold:
        _write_manifest(db, run, hold)
        _prune_empty(hold)
    db.close()
    return {"restored": sorted(restored), "skipped": skipped}


def _recycle(paths):
    """Send files to the Recycle Bin via the Windows shell. Windows asks before any permanent delete
    (e.g. a file too big for the bin) instead of doing it silently. Elsewhere: the Trash, via send2trash."""
    if os.name != "nt":
        from send2trash import send2trash
        send2trash([os.path.abspath(p) for p in paths])  # raises instead of deleting when there is no Trash
        return True
    import pythoncom
    from win32com.shell import shell
    FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_WANTNUKEWARNING = 0x10, 0x40, 0x4000
    FOFX_RECYCLEONDELETE, FOFX_ADDUNDORECORD = 0x00080000, 0x20000000
    pythoncom.CoInitialize()
    try:
        op = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL,
                                        shell.IID_IFileOperation)
        op.SetOperationFlags(FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_WANTNUKEWARNING |
                             FOFX_RECYCLEONDELETE | FOFX_ADDUNDORECORD)
        for p in paths:
            op.DeleteItem(shell.SHCreateItemFromParsingName(os.path.abspath(p), None, shell.IID_IShellItem))
        op.PerformOperations()
        return not op.GetAnyOperationsAborted()
    finally:
        pythoncom.CoUninitialize()


def recycle(run, recycler=None):
    """Send everything still in this run's holding folder to the Recycle Bin.
    Returns {"recycled": n, "left": [paths], "error": str|None}."""
    recycler = recycler or _recycle
    db = _db()
    rows = db.execute("SELECT id, dst, root FROM moves WHERE run=? AND state='moved'", (run,)).fetchall()
    if not rows:
        return {"recycled": 0, "left": [], "error": None}
    ok, why = recycle_supported(rows[0][2])
    if not ok:
        return {"recycled": 0, "left": [r[1] for r in rows], "error": why + " Photos stay in the holding folder."}
    present = [r for r in rows if os.path.exists(r[1])]
    error = None
    try:
        if present and not recycler([r[1] for r in present]):
            error = "Some files were not sent to the Recycle Bin (cancelled or in use)."
    except Exception as e:
        error = f"Recycle Bin step failed: {e}"
    n, left = 0, []
    for row_id, dst, _ in present:
        if os.path.exists(dst):
            left.append(dst)
        else:
            _set(db, row_id, "recycled")
            n += 1
    hold = holding_dir(rows[0][2], run)
    _write_manifest(db, run, hold)
    _prune_empty(hold)
    db.close()
    return {"recycled": n, "left": left, "error": error}


def _prune_empty(hold):
    """Remove empty folders (and the manifest once nothing is left) under the holding run folder."""
    if not os.path.isdir(hold):
        return
    files = [os.path.join(d, f) for d, _, fs in os.walk(hold) for f in fs]
    if files == [os.path.join(hold, "where-these-came-from.json")]:
        os.remove(files[0])
    for d, _, _ in sorted(os.walk(hold), key=lambda x: -len(x[0])):
        try:
            os.rmdir(d)  # only succeeds when empty
        except OSError:
            pass
    parent = os.path.dirname(hold)
    try:
        os.rmdir(parent)
    except OSError:
        pass


def reconcile():
    """After a crash: settle any move that was journaled but not confirmed. Returns problems to show."""
    db = _db()
    problems = []
    for row_id, src, dst in db.execute("SELECT id, src, dst FROM moves WHERE state='planned'").fetchall():
        s, d = os.path.lexists(src), os.path.lexists(dst)
        if d and s and os.path.samefile(src, dst):  # POSIX move stopped between link and unlink
            os.unlink(src)
            _set(db, row_id, "moved", "finished after restart")
        elif d and not s:
            _set(db, row_id, "moved", "confirmed after restart")
        elif s and not d:
            _set(db, row_id, "skipped", "never moved (interrupted)")
        else:
            _set(db, row_id, "conflict", "both or neither exist")
            problems.append({"src": src, "dst": dst, "both": s and d})
    db.close()
    return problems


def open_runs():
    """Runs that still have photos in a holding folder: [{"run", "root", "count", "bytes"}]."""
    db = _db()
    rows = db.execute("SELECT run, root, COUNT(DISTINCT shot), SUM(size) FROM moves WHERE state='moved' "
                      "GROUP BY run, root ORDER BY run DESC").fetchall()
    db.close()
    return [{"run": r, "root": root, "count": c, "bytes": b or 0} for r, root, c, b in rows]


def run_shots(run):
    """Shot ids currently in the holding folder for a run."""
    db = _db()
    rows = db.execute("SELECT DISTINCT shot FROM moves WHERE run=? AND state='moved'", (run,)).fetchall()
    db.close()
    return [r[0] for r in rows]


def path_for(run, original):
    """Current location of a file: the holding folder if set aside in this run, else the original path."""
    db = _db()
    r = db.execute("SELECT dst FROM moves WHERE run=? AND src=? AND state='moved'",
                   (run, os.path.abspath(original))).fetchone()
    db.close()
    return r[0] if r else original
