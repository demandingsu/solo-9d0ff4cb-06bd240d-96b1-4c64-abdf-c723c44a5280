"""Live-server smoke test.

Runs against an already-started uvicorn (default http://127.0.0.1:8000).
Covers: late-point rewrite/renumbering, gap truncation, idempotency and
conflict rejection, concurrent uploads/queries with snapshot consistency,
and control groups (create / judge / stale / rebuild / failed rebuild).
"""

import concurrent.futures
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RUN = uuid.uuid4().hex[:8]


def upload(client, cell, rows, header="timestamp,current"):
    text = header + "\n" + "".join(f"{ts},{cur}\n" for ts, cur in rows)
    return client.post(
        f"{BASE}/cells/{cell}/samples",
        files={"file": ("batch.csv", text, "text/csv")},
    )


def cycle_rows(discharge_a, cycles=1, base=0):
    rows = []
    t = base
    for _ in range(cycles):
        rows += [(t, 0), (t + 10, 1), (t + 20, 0), (t + 30, -discharge_a), (t + 40, 0)]
        t += 40
    return rows


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS: {msg}")


def main():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/health")
        check(r.status_code == 200, "health endpoint")

        # --- 1. late point rewrites/renumbers cycles --------------------
        cell = f"LIVE-LATE-{RUN}"
        r = upload(c, cell, [(0, 0), (10, 1), (20, 0), (30, -1), (40, 0), (50, 1)])
        check(r.status_code == 200, "first batch accepted")
        check(r.json()["total_cycles"] == 1, "one closed cycle, tail ignored")
        v1 = r.json()["version"]

        r = upload(c, cell, [(60, 0), (70, -1), (80, 0)])
        check(r.status_code == 200, "late batch accepted")
        check(r.json()["version"] == v1 + 1, "report version bumped")
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 2, "late point closed second cycle")
        check([x["cycle_no"] for x in rep["cycles"]] == [1, 2], "cycles renumbered 1..N")
        check(
            abs(rep["cycles"][0]["charge_ah"] - 1 * 10 / 3600) < 1e-12,
            "charge Ah from left-value integral",
        )

        # --- 2. gap > 60 s truncates ------------------------------------
        cell = f"LIVE-GAP-{RUN}"
        r = upload(c, cell, [(0, 0), (10, 2), (71, 0), (80, -3), (90, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 0, "61 s gap truncates the sequence")

        r = upload(c, cell, [(0, 0), (60, 2), (120, 0), (130, -1), (140, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 1, "exactly-60 s intervals are kept")

        # --- 3. idempotency + conflict whole-batch rejection ------------
        cell = f"LIVE-IDEM-{RUN}"
        rows = [(0, 0), (10, 1.5), (20, 0), (30, -2.5), (40, 0)]
        upload(c, cell, rows)
        r = upload(c, cell, rows)
        check(r.json()["inserted"] == 0 and r.json()["duplicate"] == 5,
              "identical re-upload is idempotent")
        before = c.get(f"{BASE}/cells/{cell}/report").json()
        r = upload(c, cell, [(10, 9.9), (99, 1)])
        check(r.status_code == 409, "different value at same ts -> 409")
        after = c.get(f"{BASE}/cells/{cell}/report").json()
        check(after["version"] == before["version"], "rejected batch keeps old version")
        check(after["total_cycles"] == before["total_cycles"], "old report unchanged")

        # --- 4. concurrency: 8 writers, parallel readers ----------------
        cell = f"LIVE-CONC-{RUN}"

        def writer(n):
            base = n * 1000
            with httpx.Client(timeout=30) as wc:
                return upload(
                    wc, cell,
                    [(base, 0), (base + 10, 1), (base + 20, 0),
                     (base + 30, -1), (base + 40, 0)],
                ).status_code

        def reader(_):
            bad = []
            with httpx.Client(timeout=30) as rc:
                for _ in range(40):
                    rr = rc.get(f"{BASE}/cells/{cell}/report")
                    if rr.status_code == 404:
                        continue
                    rep = rr.json()
                    nos = [x["cycle_no"] for x in rep["cycles"]]
                    if rep["total_cycles"] != len(rep["cycles"]) or nos != list(
                        range(1, rep["total_cycles"] + 1)
                    ):
                        bad.append(rep)
            return bad

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            f_w = [pool.submit(writer, n) for n in range(8)]
            f_r = [pool.submit(reader, n) for n in range(8)]
            codes = [f.result() for f in f_w]
            inconsistencies = [x for f in f_r for x in f.result()]
        check(codes == [200] * 8, f"all 8 concurrent writers committed (got {codes})")
        check(not inconsistencies, "every query saw one internally consistent state")
        final = c.get(f"{BASE}/cells/{cell}/report").json()
        check(final["total_cycles"] == 8, "final report contains all 8 cycles")

        cells = c.get(f"{BASE}/cells").json()
        check(any(x["cell_id"] == cell for x in cells), "cell list endpoint")

        # --- 5. control groups: create / judge / stale / rebuild --------
        controls = [f"LIVE-CTRL-{RUN}-{i}" for i in range(3)]
        for cell, amps in zip(controls, [2.0, 2.02, 2.04]):
            r = upload(c, cell, cycle_rows(amps))
            check(r.status_code == 200, f"control {cell} uploaded")

        r = c.post(
            f"{BASE}/control-groups",
            json={"cell_ids": controls, "abs_tolerance_ah": 0.001, "name": RUN},
        )
        check(r.status_code == 201, "control group created")
        group = r.json()
        gid = group["group_id"]
        check(
            all(m["report_version"] == 1 for m in group["members"]),
            "creation pins each control's report version",
        )
        check(group["is_stale"] is False, "fresh group is not stale")

        target = f"LIVE-TARGET-{RUN}"
        upload(c, target, cycle_rows(3.0))
        r = c.get(f"{BASE}/control-groups/{gid}/judge/{target}")
        check(r.status_code == 200, "target judged against controls")
        judged = r.json()
        check(judged["verdict"] == "outlier", "far-off capacity flagged outlier")
        check(judged["outlier_cycles"] == [1], "outlier cycle number reported")
        check(
            [p["cell_id"] for p in judged["cycles"][0]["controls"]] == controls,
            "judgement lists participating controls",
        )
        check(
            all(p["report_version"] == 1 for p in judged["cycles"][0]["controls"]),
            "judgement lists each control's report version",
        )

        # target inside the group: only two other controls remain -> 1st
        # cycle indeterminate, and the target's own value is never used
        r = c.post(
            f"{BASE}/control-groups",
            json={"cell_ids": controls[:3], "abs_tolerance_ah": 0.001},
        )
        gid2 = r.json()["group_id"]
        r = c.get(f"{BASE}/control-groups/{gid2}/judge/{controls[0]}")
        check(r.status_code == 200, "control cell can be judged as target")
        cyc = r.json()["cycles"][0]
        check(cyc["control_count"] == 2, "self excluded from reference set")
        check(cyc["status"] == "indeterminate", "two controls -> indeterminate")
        check(
            controls[0] not in [p["cell_id"] for p in cyc["controls"]],
            "target's own cell id absent from control list",
        )

        # a late point on one control supersedes its pinned report
        r = upload(c, controls[1], [(900, 0)])
        check(r.status_code == 200 and r.json()["version"] == 2, "late point on control")
        grp = c.get(f"{BASE}/control-groups/{gid}").json()
        check(grp["is_stale"] is True, "group flagged stale after control update")
        check(
            grp["stale_members"][0]["cell_id"] == controls[1],
            "stale member named",
        )
        r = c.get(f"{BASE}/control-groups/{gid}/judge/{target}")
        check(r.status_code == 409, "judgement refused while stale")
        check("cycles" not in r.json()["detail"], "no stale judgement leaked")

        # failed rebuild (unknown cell) keeps the old revision
        before = c.get(f"{BASE}/control-groups/{gid}").json()
        r = c.put(
            f"{BASE}/control-groups/{gid}",
            json={"cell_ids": controls + ["NO-SUCH-CELL"], "abs_tolerance_ah": 0.5},
        )
        check(r.status_code == 404, "rebuild with unknown cell rejected")
        after = c.get(f"{BASE}/control-groups/{gid}").json()
        check(after["revision"] == before["revision"], "failed rebuild keeps revision")
        check(after["members"] == before["members"], "failed rebuild keeps members")
        check(after["is_stale"] is True, "old group stays stale")

        # successful rebuild refreshes pins and allows judgement again
        r = c.put(
            f"{BASE}/control-groups/{gid}",
            json={"cell_ids": controls, "abs_tolerance_ah": 0.001},
        )
        check(r.status_code == 200, "rebuild accepted")
        rebuilt = r.json()
        check(rebuilt["revision"] == before["revision"] + 1, "revision bumped")
        check(rebuilt["is_stale"] is False, "rebuilt group is fresh")
        check(
            {m["cell_id"]: m["report_version"] for m in rebuilt["members"]}[
                controls[1]
            ]
            == 2,
            "rebuild pins the new control report version",
        )
        r = c.get(f"{BASE}/control-groups/{gid}/judge/{target}")
        check(r.status_code == 200, "judgement works after rebuild")
        check(r.json()["revision"] == rebuilt["revision"], "judgement uses new revision")

        # original capacity reports remain available throughout
        r = c.get(f"{BASE}/cells/{controls[1]}/report")
        check(r.status_code == 200 and r.json()["version"] == 2,
              "original capacity report still queryable")

        # --- 6. interleaved uploads, rebuilds and judgements ------------
        mix_controls = [f"LIVE-MIX-{RUN}-{i}" for i in range(3)]
        mix_target = f"LIVE-MIX-T-{RUN}"
        for cell in mix_controls:
            upload(c, cell, cycle_rows(2.0))
        upload(c, mix_target, cycle_rows(2.0))
        gid = c.post(
            f"{BASE}/control-groups",
            json={"cell_ids": mix_controls, "abs_tolerance_ah": 0.001},
        ).json()["group_id"]

        def mix_writer(n):
            codes = []
            with httpx.Client(timeout=30) as wc:
                for k in range(4):
                    base = 10_000 + n * 1_000 + k * 100
                    if n == 0 and k == 2:
                        rr = wc.put(
                            f"{BASE}/control-groups/{gid}",
                            json={"cell_ids": mix_controls, "abs_tolerance_ah": 0.001},
                        )
                    else:
                        rr = upload(
                            wc, mix_controls[n % 3], cycle_rows(2.0, base=base)
                        )
                    codes.append(rr.status_code)
            return codes

        def mix_reader(_):
            problems = []
            with httpx.Client(timeout=30) as rc:
                for _ in range(30):
                    rr = rc.get(f"{BASE}/control-groups/{gid}/judge/{mix_target}")
                    if rr.status_code == 409:
                        if "cycles" in rr.json()["detail"]:
                            problems.append("stale response leaked a judgement")
                        continue
                    if rr.status_code != 200:
                        problems.append(f"unexpected {rr.status_code}")
                        continue
                    body = rr.json()
                    if body["is_stale"]:
                        problems.append("200 response marked stale")
                    pinned = {
                        m["cell_id"]: m["report_version"] for m in body["members"]
                    }
                    for cyc in body["cycles"]:
                        for p in cyc["controls"]:
                            if pinned[p["cell_id"]] != p["report_version"]:
                                problems.append("mixed control report versions")
            return problems

        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
            f_w = [pool.submit(mix_writer, n) for n in range(3)]
            f_r = [pool.submit(mix_reader, n) for n in range(4)]
            write_codes = [code for f in f_w for code in f.result()]
            mix_problems = [p for f in f_r for p in f.result()]
        check(all(code in (200, 409) for code in write_codes),
              "interleaved writes/rebuilds all settled cleanly")
        check(not mix_problems,
              "interleaved judgements never mixed committed states")

    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    for attempt in range(20):
        try:
            httpx.get(f"{BASE}/health", timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    main()
