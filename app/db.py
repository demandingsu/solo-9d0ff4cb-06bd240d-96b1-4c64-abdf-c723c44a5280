"""SQLite persistence with transactional, versioned cell reports."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, List, Optional, Sequence, Tuple

from .algorithm import Cycle, detect_cycles
from .control import evaluate_cycle

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id      TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS samples (
    cell_id      TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    current_ma   INTEGER NOT NULL,
    PRIMARY KEY (cell_id, ts)
);

CREATE TABLE IF NOT EXISTS report_versions (
    cell_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    total_cycles INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (cell_id, version)
);

CREATE TABLE IF NOT EXISTS cycles (
    cell_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    cycle_no      INTEGER NOT NULL,
    charge_ah     REAL NOT NULL,
    discharge_ah  REAL NOT NULL,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER NOT NULL,
    PRIMARY KEY (cell_id, version, cycle_no)
);

CREATE TABLE IF NOT EXISTS upload_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS control_groups (
    group_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL DEFAULT '',
    abs_tolerance_ah REAL NOT NULL,
    revision         INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS control_group_members (
    group_id       INTEGER NOT NULL,
    revision       INTEGER NOT NULL,
    cell_id        TEXT NOT NULL,
    report_version INTEGER NOT NULL,
    total_cycles   INTEGER NOT NULL,
    PRIMARY KEY (group_id, revision, cell_id)
);
"""


class ConflictError(Exception):
    """Same (cell, ts) already exists with a different current value."""

    def __init__(self, ts: int, existing: float, incoming: float):
        self.ts = ts
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"conflict at ts={ts}: existing={existing}A incoming={incoming}A"
        )


class ImportError_(Exception):
    """Generic import failure (bad CSV payload etc.)."""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        # no transaction was open (e.g. BEGIN itself failed)
        pass


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = _connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #
    def import_batch(
        self, cell_id: str, rows: Iterable[Tuple[int, float]]
    ) -> dict:
        """Atomically import a CSV batch.

        Returns a summary dict. Raises ConflictError / ImportError_ on
        rejection; in that case nothing is committed (old samples and the
        previous report stay untouched).
        """
        # Normalize + validate the payload before opening the transaction.
        incoming: list[tuple[int, float]] = []
        seen: dict[int, float] = {}
        try:
            for ts, current in rows:
                ts = int(ts)
                current = float(current)
                if ts < 0:
                    raise ValueError(f"negative timestamp {ts}")
                if not (current == current):  # NaN
                    raise ValueError("NaN current")
                if ts in seen and seen[ts] != current:
                    raise ImportError_(
                        f"batch itself contains conflicting values at ts={ts}"
                    )
                seen[ts] = current
                incoming.append((ts, current))
        except (TypeError, ValueError) as exc:
            raise ImportError_(f"invalid row: {exc}") from exc

        if not incoming:
            raise ImportError_("CSV contains no data rows")

        conn = _connect(self.db_path)
        try:
            # One writer at a time. BEGIN IMMEDIATE acquires the reserved
            # lock up front so concurrent uploads serialize instead of
            # hitting SQLITE_BUSY mid-transaction.
            conn.execute("BEGIN IMMEDIATE")

            # Reject whole batch on any (cell, ts) value mismatch.
            for ts, current in incoming:
                row = conn.execute(
                    "SELECT current_ma FROM samples WHERE cell_id=? AND ts=?",
                    (cell_id, ts),
                ).fetchone()
                if row is not None and row["current_ma"] != _to_storage(current):
                    raise ConflictError(ts, row["current_ma"] / 1000.0, current)

            inserted = 0
            duplicate = 0
            for ts, current in incoming:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO samples(cell_id, ts, current_ma)"
                    " VALUES(?,?,?)",
                    (cell_id, ts, _to_storage(current)),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicate += 1

            conn.execute(
                "INSERT INTO cells(cell_id) VALUES(?) ON CONFLICT(cell_id)"
                " DO UPDATE SET updated_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (cell_id,),
            )

            # Recompute the report from the full committed sample set.
            all_rows = conn.execute(
                "SELECT ts, current_ma FROM samples WHERE cell_id=?"
                " ORDER BY ts ASC",
                (cell_id,),
            ).fetchall()
            samples = [(r["ts"], r["current_ma"] / 1_000_000.0) for r in all_rows]
            cycles = detect_cycles(samples)

            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM report_versions"
                " WHERE cell_id=?",
                (cell_id,),
            ).fetchone()
            version = version_row["v"] + 1

            conn.execute(
                "INSERT INTO report_versions(cell_id, version, total_cycles)"
                " VALUES(?,?,?)",
                (cell_id, version, len(cycles)),
            )
            conn.executemany(
                "INSERT INTO cycles(cell_id, version, cycle_no, charge_ah,"
                " discharge_ah, started_at, ended_at) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        cell_id,
                        version,
                        c.cycle_no,
                        c.charge_ah,
                        c.discharge_ah,
                        c.started_at,
                        c.ended_at,
                    )
                    for c in cycles
                ],
            )
            conn.execute(
                "INSERT INTO upload_log(cell_id, status, detail) VALUES(?,?,?)",
                (cell_id, "accepted", f"version={version}"),
            )
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            try:
                with self._conn() as log_conn:
                    log_conn.execute(
                        "INSERT INTO upload_log(cell_id, status, detail)"
                        " VALUES(?,?,?)",
                        (cell_id, "rejected", "batch rejected; no data changed"),
                    )
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return {
            "cell_id": cell_id,
            "inserted": inserted,
            "duplicate": duplicate,
            "version": version,
            "total_cycles": len(cycles),
        }

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def get_report(self, cell_id: str) -> Optional[dict]:
        """Return report + cycles of the latest committed version.

        The version row, cycle details and total are all read from the same
        committed version; a single snapshot transaction guarantees they
        belong to one state even if an upload commits concurrently.
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                cell = conn.execute(
                    "SELECT cell_id FROM cells WHERE cell_id=?", (cell_id,)
                ).fetchone()
                if cell is None:
                    return None
                ver = conn.execute(
                    "SELECT version, total_cycles, created_at FROM"
                    " report_versions WHERE cell_id=? ORDER BY version DESC"
                    " LIMIT 1",
                    (cell_id,),
                ).fetchone()
                if ver is None:
                    return {
                        "cell_id": cell_id,
                        "version": 0,
                        "total_cycles": 0,
                        "cycles": [],
                        "created_at": None,
                    }
                cycle_rows = conn.execute(
                    "SELECT cycle_no, charge_ah, discharge_ah, started_at,"
                    " ended_at FROM cycles WHERE cell_id=? AND version=?"
                    " ORDER BY cycle_no ASC",
                    (cell_id, ver["version"]),
                ).fetchall()
                return {
                    "cell_id": cell_id,
                    "version": ver["version"],
                    "total_cycles": ver["total_cycles"],
                    "created_at": ver["created_at"],
                    "cycles": [dict(r) for r in cycle_rows],
                }
            finally:
                conn.execute("COMMIT")

    def list_cells(self) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.cell_id, c.created_at, c.updated_at,"
                " COALESCE(v.total_cycles,0) AS total_cycles,"
                " COALESCE(v.version,0) AS version FROM cells c"
                " LEFT JOIN report_versions v"
                " ON v.cell_id=c.cell_id AND v.version="
                " (SELECT MAX(version) FROM report_versions WHERE cell_id=c.cell_id)"
                " ORDER BY c.cell_id"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Control groups
    # ------------------------------------------------------------------ #
    def create_control_group(
        self, cell_ids: Sequence[str], abs_tolerance_ah: float, name: str = ""
    ) -> dict:
        """Pin each control cell's latest committed report version.

        All members' latest versions are read in one transaction, so the
        group is always a single committed snapshot. Raises KeyError for an
        unknown cell and ValueError for an invalid definition; nothing is
        stored in either case.
        """
        members = _validate_group_input(cell_ids, abs_tolerance_ah)
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            pinned = _pin_members(conn, members)
            cur = conn.execute(
                "INSERT INTO control_groups(name, abs_tolerance_ah)"
                " VALUES(?,?)",
                (name, float(abs_tolerance_ah)),
            )
            group_id = cur.lastrowid
            _insert_members(conn, group_id, 1, pinned)
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()
        return self.get_control_group(group_id)

    def rebuild_control_group(
        self, group_id: int, cell_ids: Sequence[str], abs_tolerance_ah: float
    ) -> dict:
        """Replace a group's member pins with a fresh committed snapshot.

        The group row and all new member pins are written in one
        transaction; on any failure the old revision stays active and
        readable. Raises KeyError for unknown group/cell, ValueError for an
        invalid definition.
        """
        members = _validate_group_input(cell_ids, abs_tolerance_ah)
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            group = conn.execute(
                "SELECT group_id, revision FROM control_groups WHERE group_id=?",
                (group_id,),
            ).fetchone()
            if group is None:
                raise KeyError(f"unknown control group {group_id}")
            pinned = _pin_members(conn, members)
            revision = group["revision"] + 1
            conn.execute(
                "UPDATE control_groups SET abs_tolerance_ah=?, revision=?,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE group_id=?",
                (float(abs_tolerance_ah), revision, group_id),
            )
            _insert_members(conn, group_id, revision, pinned)
            conn.execute("COMMIT")
        except Exception:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()
        return self.get_control_group(group_id)

    def get_control_group(self, group_id: int) -> Optional[dict]:
        """Group definition plus staleness of each pinned member.

        A member is stale when its report version is no longer the cell's
        latest committed version (a new import changed its report).
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                group = conn.execute(
                    "SELECT group_id, name, abs_tolerance_ah, revision,"
                    " created_at, updated_at FROM control_groups"
                    " WHERE group_id=?",
                    (group_id,),
                ).fetchone()
                if group is None:
                    return None
                members = _load_members(conn, group_id, group["revision"])
                return _group_payload(group, members)
            finally:
                conn.execute("COMMIT")

    def list_control_groups(self) -> List[dict]:
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                groups = conn.execute(
                    "SELECT group_id, name, abs_tolerance_ah, revision,"
                    " created_at, updated_at FROM control_groups"
                    " ORDER BY group_id"
                ).fetchall()
                out = []
                for group in groups:
                    members = _load_members(
                        conn, group["group_id"], group["revision"]
                    )
                    out.append(_group_payload(group, members))
                return out
            finally:
                conn.execute("COMMIT")

    def judge_cell(
        self,
        group_id: int,
        cell_id: str,
        report_version: Optional[int] = None,
    ) -> Optional[dict]:
        """Judge the target cell's discharge Ah against the control group.

        Everything (group pins, every control report version, the target
        report version) is read inside one read transaction, so a concurrent
        import can never mix versions between controls or inside a report.

        Returns None if the group does not exist; raises KeyError if the
        target cell is unknown or the requested report version is missing.
        When any pinned control report has been superseded, the returned
        payload has status ``stale``, an empty cycle list and no verdict,
        so callers cannot accidentally present an outdated judgement.
        """
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN")
            try:
                group = conn.execute(
                    "SELECT group_id, name, abs_tolerance_ah, revision,"
                    " created_at, updated_at FROM control_groups"
                    " WHERE group_id=?",
                    (group_id,),
                ).fetchone()
                if group is None:
                    return None

                members = _load_members(conn, group_id, group["revision"])
                target = _select_target_report(conn, cell_id, report_version)

                stale = [
                    {
                        "cell_id": m["cell_id"],
                        "pinned_version": m["pinned_version"],
                        "latest_version": m["latest_version"],
                    }
                    for m in members
                    if m["is_stale"]
                ]
                payload = _group_payload(group, members)
                payload.update(
                    {
                        "target_cell_id": cell_id,
                        "target_version": target["version"],
                        "cycle_count": target["total_cycles"],
                        "status": (
                            "stale"
                            if stale
                            else (
                                "completed"
                                if target["total_cycles"]
                                else "no_cycles"
                            )
                        ),
                        "verdict": None,
                        "cycles": [],
                        "outlier_cycles": [],
                        "indeterminate_cycles": [],
                        "missing_cycles": [],
                        "judged_cycles": [],
                    }
                )

                if stale:
                    # Never mix an outdated control snapshot with current
                    # reports: refuse to judge and ask for a rebuild.
                    payload["message"] = (
                        "control report changed since the group was created;"
                        " rebuild the group before judging"
                    )
                    return payload

                target_ah = _cycle_discharge_map(
                    conn, cell_id, target["version"]
                )
                control_ah = {
                    m["cell_id"]: _cycle_discharge_map(
                        conn, m["cell_id"], m["pinned_version"]
                    )
                    for m in members
                }
                tolerance = float(group["abs_tolerance_ah"])

                cycle_numbers = sorted(
                    set(target_ah)
                    | {no for values in control_ah.values() for no in values}
                )
                for cycle_no in cycle_numbers:
                    participating = [
                        {
                            "cell_id": m["cell_id"],
                            "report_version": m["pinned_version"],
                            "discharge_ah": control_ah[m["cell_id"]][cycle_no],
                        }
                        for m in members
                        if m["cell_id"] != cell_id
                        and cycle_no in control_ah[m["cell_id"]]
                    ]
                    result = evaluate_cycle(
                        cycle_no,
                        target_ah.get(cycle_no),
                        [p["discharge_ah"] for p in participating],
                        tolerance,
                    )
                    result["cycle_no"] = cycle_no
                    result["target_discharge_ah"] = target_ah.get(cycle_no)
                    result["controls"] = participating
                    payload["cycles"].append(result)

                payload["outlier_cycles"] = [
                    c["cycle_no"] for c in payload["cycles"]
                    if c["status"] == "outlier"
                ]
                payload["indeterminate_cycles"] = [
                    c["cycle_no"] for c in payload["cycles"]
                    if c["status"] == "indeterminate"
                ]
                payload["missing_cycles"] = [
                    c["cycle_no"] for c in payload["cycles"]
                    if c["status"] == "missing"
                ]
                judged_cycles = [
                    c["cycle_no"] for c in payload["cycles"]
                    if c["status"] in ("normal", "outlier")
                ]
                payload["judged_cycles"] = judged_cycles
                if payload["outlier_cycles"]:
                    payload["verdict"] = "outlier"
                elif not judged_cycles:
                    # nothing could actually be compared
                    payload["verdict"] = "indeterminate"
                elif payload["indeterminate_cycles"]:
                    payload["verdict"] = "indeterminate"
                else:
                    payload["verdict"] = "normal"
                if payload["outlier_cycles"]:
                    payload["message"] = (
                        "target is an outlier on at least one cycle"
                    )
                elif payload["verdict"] == "indeterminate":
                    payload["message"] = (
                        "no cycle could be judged with enough controls"
                    )
                else:
                    payload["message"] = "no outlier cycle detected"
                return payload
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()


def _validate_group_input(
    cell_ids: Sequence[str], abs_tolerance_ah: float
) -> List[str]:
    try:
        tolerance = float(abs_tolerance_ah)
    except (TypeError, ValueError) as exc:
        raise ValueError("abs_tolerance_ah must be a number") from exc
    if not (tolerance == tolerance) or tolerance in (float("inf"), float("-inf")):
        raise ValueError("abs_tolerance_ah must be finite")
    if tolerance < 0:
        raise ValueError("abs_tolerance_ah must be >= 0")

    members: List[str] = []
    for raw in cell_ids:
        cell_id = str(raw).strip()
        if not cell_id:
            raise ValueError("control cell_id must not be blank")
        if cell_id not in members:
            members.append(cell_id)
    if not members:
        raise ValueError("a control group needs at least one control cell")
    return members


def _pin_members(conn: sqlite3.Connection, cell_ids: Sequence[str]) -> List[dict]:
    """Resolve each cell's latest committed report version inside a txn."""
    pinned: List[dict] = []
    for cell_id in cell_ids:
        version_row = conn.execute(
            "SELECT version, total_cycles FROM report_versions"
            " WHERE cell_id=? ORDER BY version DESC LIMIT 1",
            (cell_id,),
        ).fetchone()
        if version_row is None:
            known = conn.execute(
                "SELECT 1 FROM cells WHERE cell_id=?", (cell_id,)
            ).fetchone()
            if known is None:
                raise KeyError(f"unknown control cell {cell_id}")
            raise ValueError(
                f"control cell {cell_id} has no committed report yet"
            )
        pinned.append(
            {
                "cell_id": cell_id,
                "report_version": version_row["version"],
                "total_cycles": version_row["total_cycles"],
            }
        )
    return pinned


def _insert_members(
    conn: sqlite3.Connection, group_id: int, revision: int, pinned: Sequence[dict]
) -> None:
    conn.executemany(
        "INSERT INTO control_group_members"
        "(group_id, revision, cell_id, report_version, total_cycles)"
        " VALUES(?,?,?,?,?)",
        [
            (
                group_id,
                revision,
                m["cell_id"],
                m["report_version"],
                m["total_cycles"],
            )
            for m in pinned
        ],
    )


def _load_members(
    conn: sqlite3.Connection, group_id: int, revision: int
) -> List[dict]:
    """Members of one revision with staleness against the latest reports.

    Reports are append-only and every successful import commits a new
    version, so a member is stale whenever its cell's latest committed
    report version is no longer the pinned one. That is deliberately
    conservative: any supplement upload (even one that ends up leaving
    the cycle list unchanged) invalidates the pinned snapshot and the
    group must be rebuilt before judging again.
    """
    rows = conn.execute(
        "SELECT m.cell_id, m.report_version, m.total_cycles,"
        " COALESCE((SELECT MAX(v.version) FROM report_versions v"
        "           WHERE v.cell_id=m.cell_id), 0) AS latest_version"
        " FROM control_group_members m"
        " WHERE m.group_id=? AND m.revision=?"
        " ORDER BY m.cell_id",
        (group_id, revision),
    ).fetchall()
    return [
        {
            "cell_id": r["cell_id"],
            "report_version": r["report_version"],
            "pinned_version": r["report_version"],
            "total_cycles": r["total_cycles"],
            "latest_version": r["latest_version"],
            "is_stale": r["latest_version"] != r["report_version"],
        }
        for r in rows
    ]


def _group_payload(group: sqlite3.Row, members: Sequence[dict]) -> dict:
    stale = [m for m in members if m["is_stale"]]
    return {
        "group_id": group["group_id"],
        "name": group["name"],
        "abs_tolerance_ah": group["abs_tolerance_ah"],
        "revision": group["revision"],
        "created_at": group["created_at"],
        "updated_at": group["updated_at"],
        "members": [dict(m) for m in members],
        "is_stale": bool(stale),
        "stale_members": [
            {
                "cell_id": m["cell_id"],
                "pinned_version": m["pinned_version"],
                "latest_version": m["latest_version"],
            }
            for m in stale
        ],
    }


def _select_target_report(
    conn: sqlite3.Connection,
    cell_id: str,
    report_version: Optional[int],
) -> dict:
    if report_version is None:
        row = conn.execute(
            "SELECT version, total_cycles FROM report_versions"
            " WHERE cell_id=? ORDER BY version DESC LIMIT 1",
            (cell_id,),
        ).fetchone()
        if row is None:
            known = conn.execute(
                "SELECT 1 FROM cells WHERE cell_id=?", (cell_id,)
            ).fetchone()
            if known is None:
                raise KeyError(f"unknown target cell {cell_id}")
            return {"version": 0, "total_cycles": 0}
        return {"version": row["version"], "total_cycles": row["total_cycles"]}

    requested = int(report_version)
    row = conn.execute(
        "SELECT version, total_cycles FROM report_versions"
        " WHERE cell_id=? AND version=?",
        (cell_id, requested),
    ).fetchone()
    if row is None:
        raise KeyError(
            f"cell {cell_id} has no committed report version {requested}"
        )
    return {"version": row["version"], "total_cycles": row["total_cycles"]}


def _cycle_discharge_map(
    conn: sqlite3.Connection, cell_id: str, version: int
) -> dict:
    rows = conn.execute(
        "SELECT cycle_no, discharge_ah FROM cycles"
        " WHERE cell_id=? AND version=? ORDER BY cycle_no",
        (cell_id, version),
    ).fetchall()
    return {r["cycle_no"]: r["discharge_ah"] for r in rows}


def _to_storage(current_a: float) -> int:
    """Store amperes as integer microamps.

    CSV currents like 1.234 A are stored as integer microamps so equality
    checks for idempotency are exact (no float text round-trip surprises).
    Precision below 1 µA is rejected by rounding consistency on read.
    """
    return round(float(current_a) * 1_000_000)
