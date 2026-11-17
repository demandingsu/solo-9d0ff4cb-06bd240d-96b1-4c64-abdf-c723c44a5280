"""FastAPI application: battery cell sample upload and cycle reports."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from .csv_import import CSVParseError, parse_samples_csv
from .db import ConflictError, Database, ImportError_

DB_PATH = os.environ.get("BATTERY_DB_PATH", "/data/battery.db")

app = FastAPI(
    title="Battery Cell Cycle Capacity API",
    version="1.1.0",
    description=(
        "Upload UTC-second current samples per cell. Zero current marks "
        "segment boundaries; only zero→positive→zero→negative→zero sequences "
        "count as cycles. Gaps over 60 s truncate a sequence. Ah is the "
        "left-value current integral over each interval.\n\n"
        "Control groups pin each control cell's report version and an "
        "absolute Ah tolerance. A target cell is an outlier on a cycle when "
        "its discharge Ah deviates from the control median by more than "
        "max(3×MAD, absolute tolerance); fewer than three control values "
        "make that cycle indeterminate."
    ),
)
db = Database(DB_PATH)


class CycleOut(BaseModel):
    cycle_no: int
    charge_ah: float
    discharge_ah: float
    started_at: int
    ended_at: int


class ReportOut(BaseModel):
    cell_id: str
    version: int
    total_cycles: int
    created_at: str | None
    cycles: list[CycleOut]


class UploadOut(BaseModel):
    cell_id: str
    inserted: int
    duplicate: int
    version: int
    total_cycles: int


class GroupCreateIn(BaseModel):
    cell_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Control cell ids; their latest committed report is pinned",
        examples=[["CTRL-01", "CTRL-02", "CTRL-03"]],
    )
    abs_tolerance_ah: float = Field(
        ...,
        ge=0,
        description="Absolute tolerance floor in Ah",
        examples=[0.005],
    )
    name: str = Field("", description="Optional human-readable group name")


class GroupRebuildIn(BaseModel):
    cell_ids: list[str] = Field(..., min_length=1)
    abs_tolerance_ah: float = Field(..., ge=0)


class GroupMemberOut(BaseModel):
    cell_id: str
    report_version: int
    pinned_version: int
    total_cycles: int
    latest_version: int
    is_stale: bool


class StaleMemberOut(BaseModel):
    cell_id: str
    pinned_version: int
    latest_version: int


class GroupOut(BaseModel):
    group_id: int
    name: str
    abs_tolerance_ah: float
    revision: int
    created_at: str
    updated_at: str
    members: list[GroupMemberOut]
    is_stale: bool
    stale_members: list[StaleMemberOut]


class ControlParticipantOut(BaseModel):
    cell_id: str
    report_version: int
    discharge_ah: float


class CycleJudgementOut(BaseModel):
    cycle_no: int
    status: str
    is_outlier: bool | None
    reason: str | None
    target_discharge_ah: float | None
    control_count: int
    control_median_ah: float | None
    mad_ah: float | None
    threshold_ah: float | None
    deviation_ah: float | None
    controls: list[ControlParticipantOut]


class JudgementOut(BaseModel):
    group_id: int
    name: str
    abs_tolerance_ah: float
    revision: int
    target_cell_id: str
    target_version: int
    cycle_count: int
    status: str
    verdict: str | None
    message: str
    is_stale: bool
    stale_members: list[StaleMemberOut]
    members: list[GroupMemberOut]
    outlier_cycles: list[int]
    indeterminate_cycles: list[int]
    missing_cycles: list[int]
    judged_cycles: list[int]
    cycles: list[CycleJudgementOut]


@app.post(
    "/cells/{cell_id}/samples",
    response_model=UploadOut,
    tags=["upload"],
    summary="Upload a CSV batch of (UTC seconds, current A) samples",
)
async def upload_samples(cell_id: str, file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_samples_csv(content)
        result = db.import_batch(cell_id, rows)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=f"CSV error: {exc}") from exc
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch rejected, no data changed: same timestamp {exc.ts} "
                f"already stored with {exc.existing} A, incoming {exc.incoming} A"
            ),
        ) from exc
    return result


@app.get("/cells", tags=["query"], summary="List all cells with latest report info")
def list_cells():
    return db.list_cells()


@app.get(
    "/cells/{cell_id}/report",
    response_model=ReportOut,
    tags=["query"],
    summary="Latest committed cycle report for one cell",
)
def get_report(cell_id: str):
    report = db.get_report(cell_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return report


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------- #
# Control groups
# ---------------------------------------------------------------------- #
@app.post(
    "/control-groups",
    response_model=GroupOut,
    status_code=201,
    tags=["control-groups"],
    summary="Create a control group and pin each member's report version",
)
def create_control_group(body: GroupCreateIn):
    try:
        return db.create_control_group(
            body.cell_ids, body.abs_tolerance_ah, body.name
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/control-groups",
    response_model=list[GroupOut],
    tags=["control-groups"],
    summary="List control groups with staleness",
)
def list_control_groups():
    return db.list_control_groups()


@app.get(
    "/control-groups/{group_id}",
    response_model=GroupOut,
    tags=["control-groups"],
    summary="Get one control group and whether its snapshot is stale",
)
def get_control_group(group_id: int):
    group = db.get_control_group(group_id)
    if group is None:
        raise HTTPException(
            status_code=404, detail=f"unknown control group {group_id}"
        )
    return group


@app.put(
    "/control-groups/{group_id}",
    response_model=GroupOut,
    tags=["control-groups"],
    summary="Rebuild a control group from fresh committed reports",
)
def rebuild_control_group(group_id: int, body: GroupRebuildIn):
    try:
        return db.rebuild_control_group(
            group_id, body.cell_ids, body.abs_tolerance_ah
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/control-groups/{group_id}/judge/{cell_id}",
    response_model=JudgementOut,
    tags=["control-groups"],
    summary="Judge one target cell's discharge Ah against the control group",
    responses={
        409: {"description": "Control snapshot stale; rebuild required"},
        404: {"description": "Unknown group/cell or report version"},
    },
)
def judge_cell(
    group_id: int,
    cell_id: str,
    report_version: Optional[int] = Query(
        None,
        description="Pinned target report version; default is the latest",
    ),
):
    try:
        result = db.judge_cell(group_id, cell_id, report_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"unknown control group {group_id}"
        )
    if result["status"] == "stale":
        # Do not hand out a judgement against an outdated control snapshot.
        raise HTTPException(
            status_code=409,
            detail={
                "message": result["message"],
                "group_id": group_id,
                "revision": result["revision"],
                "is_stale": True,
                "stale_members": result["stale_members"],
                "members": result["members"],
            },
        )
    return result
