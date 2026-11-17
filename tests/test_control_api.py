"""HTTP tests for control groups: create, rebuild, judge, staleness, races."""

import itertools
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _db  # noqa: E402  (sets BATTERY_DB_PATH before app import)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TOL = 0.001  # Ah

# One shared DB for the module; every test uses distinct cell ids.
_SEQ = itertools.count(1)


def cycle_rows(discharge_a: float, cycles: int = 1, base: int = 0):
    """One or more complete cycles; each discharges |discharge_a| for 10 s."""
    rows = []
    t = base
    for _ in range(cycles):
        rows += [
            (t, 0.0),
            (t + 10, 1.0),
            (t + 20, 0.0),
            (t + 30, -discharge_a),
            (t + 40, 0.0),
        ]
        t += 40
    return rows


def discharge_ah(current_a: float) -> float:
    return abs(current_a) * 10 / 3600.0


def upload(client, cell_id, rows):
    text = "timestamp,current\n" + "".join(f"{t},{v}\n" for t, v in rows)
    r = client.post(
        f"/cells/{cell_id}/samples",
        files={"file": ("samples.csv", text, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def create_group(client, cell_ids, tolerance=TOL):
    return client.post(
        "/control-groups",
        json={"cell_ids": cell_ids, "abs_tolerance_ah": tolerance},
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)

    @staticmethod
    def uid(prefix: str) -> str:
        return f"{prefix}-{next(_SEQ)}"


class CreateGroupTests(Base):
    def test_pins_report_versions_at_creation(self):
        cells = [self.uid("CR"), self.uid("CR"), self.uid("CR")]
        for i, cell in enumerate(cells):
            upload(self.c, cell, cycle_rows(2.0 + i * 0.01))
            upload(self.c, cell, [(1000 + i * 100, 0.0)])  # bumps version
        r = create_group(self.c, cells)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["abs_tolerance_ah"], TOL)
        versions = {m["cell_id"]: m["report_version"] for m in body["members"]}
        self.assertEqual(versions, {c: 2 for c in cells})
        self.assertFalse(body["is_stale"])

    def test_unknown_cell_404_and_nothing_stored(self):
        before = len(self.c.get("/control-groups").json())
        r = create_group(self.c, [self.uid("CR"), "NOPE-NEVER"])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(len(self.c.get("/control-groups").json()), before)

    def test_unknown_cell_404(self):
        # a cell only exists once an import committed a report, so a cell
        # that never uploaded has no report and is unknown -> 404
        r = create_group(self.c, [self.uid("NEVER")])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("unknown control cell", r.json()["detail"])

    def test_duplicate_member_ids_are_deduplicated(self):
        cells = [self.uid("DD"), self.uid("DD"), self.uid("DD")]
        for cell in cells:
            upload(self.c, cell, cycle_rows(2.0))
        r = create_group(self.c, [cells[0], cells[0], cells[1], cells[2]])
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(
            [m["cell_id"] for m in r.json()["members"]], sorted(cells)
        )

    def test_negative_tolerance_rejected(self):
        cell = self.uid("NEG")
        upload(self.c, cell, cycle_rows(2.0))
        r = create_group(self.c, [cell], tolerance=-1)
        self.assertEqual(r.status_code, 422, r.text)

    def test_empty_member_list_rejected(self):
        r = create_group(self.c, [])
        self.assertEqual(r.status_code, 422, r.text)


class JudgeTests(Base):
    def setUp(self):
        super().setUp()
        self.controls = [self.uid("JC"), self.uid("JC"), self.uid("JC")]
        for cell, amps in zip(self.controls, [2.0, 2.02, 2.04]):
            upload(self.c, cell, cycle_rows(amps))
        self.gid = create_group(self.c, self.controls).json()["group_id"]

    def judge(self, cell, **params):
        return self.c.get(f"/control-groups/{self.gid}/judge/{cell}", params=params)

    def test_outlier_detected_with_median_and_mad(self):
        target = self.uid("JT")
        upload(self.c, target, cycle_rows(3.0))
        r = self.judge(target)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["verdict"], "outlier")
        self.assertEqual(body["outlier_cycles"], [1])
        cyc = body["cycles"][0]
        self.assertEqual(cyc["status"], "outlier")
        self.assertAlmostEqual(cyc["control_median_ah"], discharge_ah(2.02))
        self.assertAlmostEqual(cyc["mad_ah"], discharge_ah(0.02))
        self.assertAlmostEqual(
            cyc["threshold_ah"], max(3 * discharge_ah(0.02), TOL)
        )
        # per-cycle participants: control cell ids + their pinned versions
        self.assertEqual(
            [p["cell_id"] for p in cyc["controls"]], self.controls
        )
        self.assertTrue(all(p["report_version"] == 1 for p in cyc["controls"]))
        self.assertEqual(body["target_version"], 1)

    def test_normal_target(self):
        target = self.uid("JT")
        upload(self.c, target, cycle_rows(2.03))
        body = self.judge(target).json()
        self.assertEqual(body["verdict"], "normal")
        self.assertEqual(body["outlier_cycles"], [])
        self.assertEqual(body["cycles"][0]["status"], "normal")

    def test_target_inside_group_excludes_itself(self):
        # The target is also a control. After a rebuild all three pins move
        # together, then a new import adds a deviating 2nd cycle to the
        # target and the group is rebuilt again. Only the *other* pinned
        # controls may count: 2 leftovers < 3 -> indeterminate. Including
        # the target's own value would have produced a judgeable set of 3.
        target = self.controls[0]
        upload(self.c, target, cycle_rows(3.0, base=500))
        gid = self.c.put(
            f"/control-groups/{self.gid}",
            json={"cell_ids": self.controls, "abs_tolerance_ah": TOL},
        ).json()["group_id"]
        r = self.c.get(f"/control-groups/{gid}/judge/{target}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(
            [m["cell_id"] for m in body["members"]], sorted(self.controls)
        )
        by_cycle = {c["cycle_no"]: c for c in body["cycles"]}
        self.assertEqual(sorted(by_cycle), [1, 2])
        # cycle 1 exists for the two other controls -> 2 < 3 -> indeterminate
        first = by_cycle[1]
        self.assertEqual(first["control_count"], 2)
        self.assertEqual(
            [p["cell_id"] for p in first["controls"]], self.controls[1:]
        )
        self.assertEqual(first["status"], "indeterminate")
        # cycle 2 only exists on the target; no control value at all
        self.assertEqual(by_cycle[2]["control_count"], 0)
        self.assertEqual(by_cycle[2]["status"], "indeterminate")
        self.assertEqual(body["verdict"], "indeterminate")
        self.assertEqual(body["outlier_cycles"], [])

    def test_target_inside_larger_group_judged_without_self(self):
        members = [self.uid("JG") for _ in range(4)]
        for cell, amps in zip(members, [2.0, 2.02, 2.04, 2.06]):
            upload(self.c, cell, cycle_rows(amps))
        gid = create_group(self.c, members).json()["group_id"]
        target = members[-1]
        body = self.c.get(f"/control-groups/{gid}/judge/{target}").json()
        cyc = body["cycles"][0]
        self.assertEqual(cyc["control_count"], 3)
        self.assertEqual([p["cell_id"] for p in cyc["controls"]], members[:3])
        self.assertEqual(cyc["status"], "normal")
        self.assertAlmostEqual(cyc["control_median_ah"], discharge_ah(2.02))

    def test_missing_target_cycle_is_missing_not_outlier(self):
        target = self.uid("JT")
        upload(self.c, target, cycle_rows(2.0, cycles=1))
        for cell in self.controls:
            upload(self.c, cell, cycle_rows(2.0, cycles=1, base=400))
        gid = self.c.put(
            f"/control-groups/{self.gid}",
            json={"cell_ids": self.controls, "abs_tolerance_ah": TOL},
        ).json()["group_id"]
        body = self.c.get(f"/control-groups/{gid}/judge/{target}").json()
        statuses = {c["cycle_no"]: c["status"] for c in body["cycles"]}
        self.assertEqual(statuses[1], "normal")
        self.assertEqual(statuses[2], "missing")
        self.assertEqual(body["verdict"], "normal")
        self.assertEqual(body["judged_cycles"], [1])
        self.assertEqual(body["outlier_cycles"], [])
        self.assertEqual(body["indeterminate_cycles"], [])
        self.assertEqual(body["missing_cycles"], [2])

    def test_target_without_any_cycle_is_indeterminate(self):
        target = self.uid("JT")
        # a committed report with zero closed cycles (unclosed tail only)
        upload(self.c, target, [(0, 0.0), (10, 1.0)])
        body = self.judge(target).json()
        self.assertEqual(body["status"], "no_cycles")
        self.assertEqual(body["verdict"], "indeterminate")
        self.assertEqual(body["judged_cycles"], [])
        self.assertTrue(all(c["status"] == "missing" for c in body["cycles"]))
        self.assertEqual(body["outlier_cycles"], [])

    def test_two_controls_makes_indeterminate(self):
        gid = create_group(self.c, self.controls[:2]).json()["group_id"]
        target = self.uid("JT")
        upload(self.c, target, cycle_rows(9.0))
        body = self.c.get(f"/control-groups/{gid}/judge/{target}").json()
        self.assertEqual(body["cycles"][0]["status"], "indeterminate")
        self.assertEqual(body["verdict"], "indeterminate")
        self.assertEqual(body["cycles"][0]["control_count"], 2)

    def test_unknown_target_404(self):
        self.assertEqual(self.judge("NO-SUCH-CELL").status_code, 404)

    def test_unknown_group_404(self):
        r = self.c.get("/control-groups/999999/judge/NOPE")
        self.assertEqual(r.status_code, 404)

    def test_requested_report_version_must_exist(self):
        target = self.uid("JV")
        upload(self.c, target, cycle_rows(2.0))
        self.assertEqual(self.judge(target, report_version=1).status_code, 200)
        self.assertEqual(self.judge(target, report_version=42).status_code, 404)


class StalenessTests(Base):
    def setUp(self):
        super().setUp()
        self.controls = [self.uid("SC"), self.uid("SC"), self.uid("SC")]
        for cell, amps in zip(self.controls, [2.0, 2.02, 2.04]):
            upload(self.c, cell, cycle_rows(amps))
        self.gid = create_group(self.c, self.controls).json()["group_id"]
        self.target = self.uid("ST")
        upload(self.c, self.target, cycle_rows(3.5))

    def test_late_point_on_control_marks_group_stale_and_blocks_judgement(self):
        r = upload(
            self.c, self.controls[1], cycle_rows(2.02, cycles=1, base=900)
        )
        self.assertEqual(r["version"], 2)
        group = self.c.get(f"/control-groups/{self.gid}").json()
        self.assertTrue(group["is_stale"])
        self.assertEqual(
            group["stale_members"],
            [
                {
                    "cell_id": self.controls[1],
                    "pinned_version": 1,
                    "latest_version": 2,
                }
            ],
        )
        judged = self.c.get(f"/control-groups/{self.gid}/judge/{self.target}")
        self.assertEqual(judged.status_code, 409, judged.text)
        detail = judged.json()["detail"]
        self.assertTrue(detail["is_stale"])
        self.assertEqual(detail["stale_members"][0]["cell_id"], self.controls[1])
        # no current judgement is leaked in the stale response
        self.assertNotIn("cycles", detail)
        self.assertNotIn("verdict", detail)

    def test_any_new_control_report_version_stales_group(self):
        # every successful import commits a new report version, so even an
        # idempotent re-upload supersedes the pinned snapshot
        before = self.c.get(f"/control-groups/{self.gid}").json()
        upload(self.c, self.controls[1], cycle_rows(2.02))
        group = self.c.get(f"/control-groups/{self.gid}").json()
        self.assertTrue(group["is_stale"])
        self.assertEqual(
            [m["pinned_version"] for m in group["members"]],
            [m["pinned_version"] for m in before["members"]],
        )
        r = self.c.get(f"/control-groups/{self.gid}/judge/{self.target}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_isolated_zero_still_bumps_version_and_stales(self):
        upload(self.c, self.controls[2], [(5000, 0.0)])
        group = self.c.get(f"/control-groups/{self.gid}").json()
        self.assertEqual(
            {m["cell_id"]: m["latest_version"] for m in group["members"]}[
                self.controls[2]
            ],
            2,
        )
        self.assertTrue(group["is_stale"])
        r = self.c.get(f"/control-groups/{self.gid}/judge/{self.target}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_change_to_target_only_does_not_stale_group(self):
        upload(self.c, self.target, cycle_rows(3.5, cycles=1, base=900))
        group = self.c.get(f"/control-groups/{self.gid}").json()
        self.assertFalse(group["is_stale"])
        r = self.c.get(f"/control-groups/{self.gid}/judge/{self.target}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["target_version"], 2)

    def test_rebuild_refreshes_pins_and_clears_staleness(self):
        upload(
            self.c, self.controls[1], cycle_rows(2.02, cycles=1, base=900)
        )
        rebuilt = self.c.put(
            f"/control-groups/{self.gid}",
            json={"cell_ids": self.controls, "abs_tolerance_ah": 0.002},
        )
        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
        body = rebuilt.json()
        self.assertEqual(body["revision"], 2)
        self.assertEqual(body["abs_tolerance_ah"], 0.002)
        self.assertFalse(body["is_stale"])
        versions = {m["cell_id"]: m["report_version"] for m in body["members"]}
        self.assertEqual(versions[self.controls[1]], 2)
        judged = self.c.get(f"/control-groups/{self.gid}/judge/{self.target}")
        self.assertEqual(judged.status_code, 200, judged.text)
        self.assertEqual(judged.json()["revision"], 2)

    def test_failed_rebuild_keeps_old_group(self):
        upload(
            self.c, self.controls[1], cycle_rows(2.02, cycles=1, base=900)
        )
        before = self.c.get(f"/control-groups/{self.gid}").json()
        r = self.c.put(
            f"/control-groups/{self.gid}",
            json={
                "cell_ids": self.controls + ["MISSING-CELL"],
                "abs_tolerance_ah": 0.5,
            },
        )
        self.assertEqual(r.status_code, 404, r.text)
        after = self.c.get(f"/control-groups/{self.gid}").json()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["abs_tolerance_ah"], before["abs_tolerance_ah"])
        self.assertEqual(after["members"], before["members"])
        # still stale, and judgement still refused
        self.assertTrue(after["is_stale"])
        self.assertEqual(
            self.c.get(f"/control-groups/{self.gid}/judge/{self.target}").status_code,
            409,
        )

    def test_original_reports_still_available(self):
        upload(
            self.c, self.controls[1], cycle_rows(2.02, cycles=1, base=900)
        )
        r = self.c.get(f"/cells/{self.controls[1]}/report")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 2)
        self.assertEqual(r.json()["total_cycles"], 2)


class InterleavingTests(Base):
    """Interleaved creates/imports/queries must never mix committed states."""

    def test_judge_and_uploads_are_consistent_under_concurrency(self):
        controls = [self.uid("IC"), self.uid("IC"), self.uid("IC")]
        target = self.uid("IT")
        for cell in controls + [target]:
            upload(self.c, cell, cycle_rows(2.0))
        gid = create_group(self.c, controls).json()["group_id"]
        upload(self.c, target, cycle_rows(4.0, cycles=1, base=2_000))

        errors = []
        barrier = threading.Barrier(7)

        def uploader(n):
            try:
                barrier.wait()
                c = TestClient(app)
                for k in range(5):
                    # append a fresh closed cycle far after the pinned data
                    base = 100_000 + n * 10_000 + k * 1_000
                    upload(c, controls[n % 3], cycle_rows(2.0, base=base))
            except Exception as exc:  # pragma: no cover
                errors.append(f"uploader {n}: {exc}")

        def judger():
            try:
                barrier.wait()
                c = TestClient(app)
                for _ in range(40):
                    r = c.get(f"/control-groups/{gid}/judge/{target}")
                    if r.status_code == 409:
                        detail = r.json()["detail"]
                        self.assertTrue(detail["stale_members"])
                        self.assertNotIn("cycles", detail)
                        continue
                    self.assertEqual(r.status_code, 200, r.text)
                    body = r.json()
                    # the response must reflect one pinned, non-stale state
                    self.assertFalse(body["is_stale"])
                    pinned = {
                        m["cell_id"]: m["report_version"] for m in body["members"]
                    }
                    for cyc in body["cycles"]:
                        for p in cyc["controls"]:
                            self.assertEqual(
                                p["report_version"], pinned[p["cell_id"]]
                            )
                    c1 = body["cycles"][0]
                    self.assertEqual(c1["cycle_no"], 1)
                    self.assertEqual(c1["control_count"], 3)
            except Exception as exc:  # pragma: no cover
                errors.append(f"judger: {exc}")

        threads = [threading.Thread(target=uploader, args=(n,)) for n in range(3)]
        threads += [threading.Thread(target=judger) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)



if __name__ == "__main__":
    unittest.main(verbosity=2)
