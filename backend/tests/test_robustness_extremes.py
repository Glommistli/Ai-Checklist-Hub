import random

import pytest
from pydantic import ValidationError

from app.models import ImportBatch
from app.schemas import LearningUnit, LearningUnitSet, PlanningOptions
from app.services.planning import _partition_ordered, _split_unit, build_schedule


def make_unit(index: int, minutes: int, *, splittable: bool = True) -> LearningUnit:
    return LearningUnit(
        id=f"unit-{index}", title=f"任务单元 {index + 1}", document_id=1,
        source_path="extreme-course.md", topic=f"主题 {index // 4 + 1}", sequence=index,
        estimated_minutes=minutes, splittable=splittable,
    )


def test_partition_property_grid_never_drops_or_reorders_units():
    rng = random.Random(20260923)
    for unit_count in (1, 2, 3, 7, 25, 80):
        for day_count in (1, 2, 5, 12, 100):
            weights = [rng.randint(1, 600) for _ in range(unit_count)]
            source = [make_unit(index, minutes, splittable=False) for index, minutes in enumerate(weights)]
            buckets = _partition_ordered(source, day_count)
            flattened = [unit for bucket in buckets for unit in bucket]
            assert len(buckets) == day_count
            assert [unit.id for unit in flattened] == [unit.id for unit in source]
            assert sum(unit.estimated_minutes for unit in flattened) == sum(weights)
            assert sum(bool(bucket) for bucket in buckets) == min(unit_count, day_count)


def test_large_media_split_preserves_duration_and_contiguous_ranges():
    source = LearningUnit(
        id="long-video", title="九小时课程视频", document_id=1,
        source_path="course.mp4", unit_type="video", estimated_minutes=540,
        media_start=0, media_end=9 * 3600, splittable=True,
    )
    parts = _split_unit(source, 60)
    assert len(parts) == 9
    assert sum(part.estimated_minutes for part in parts) == 540
    assert parts[0].media_start == 0
    assert parts[-1].media_end == 9 * 3600
    assert all(left.media_end == right.media_start for left, right in zip(parts, parts[1:]))
    assert len({part.id for part in parts}) == len(parts)


def test_single_day_end_date_keeps_unsplittable_over_capacity_task():
    source = make_unit(0, 10080, splittable=False)
    preview = build_schedule(LearningUnitSet(title="单日极限计划", units=[source]), PlanningOptions(
        batch_id=1, start_date="2026-10-01", schedule_mode="end_date", end_date="2026-10-01",
        daily_available_minutes=15, review_ratio=0,
    ), "Provider", "Model")
    assert preview.day_count == 1
    assert [unit.id for unit in preview.days[0].units] == [source.id]
    assert preview.days[0].estimated_minutes == 10080
    assert preview.days[0].load_ratio == 672
    assert any("所有资料仍会保留" in warning for warning in preview.warnings)


def test_end_date_mode_rejects_impossible_ranges_and_no_available_days():
    with pytest.raises(ValidationError, match="结束日期不能早于开始日期"):
        PlanningOptions(
            batch_id=1, start_date="2026-10-02", schedule_mode="end_date", end_date="2026-10-01",
        )
    with pytest.raises(ValidationError, match="最多允许 366 个自然日"):
        PlanningOptions(
            batch_id=1, start_date="2026-01-01", schedule_mode="end_date", end_date="2027-01-03",
        )

    unit_set = LearningUnitSet(title="无可用日期", units=[make_unit(0, 30)])
    with pytest.raises(ValueError, match="没有可安排任务的日期"):
        build_schedule(unit_set, PlanningOptions(
            batch_id=1, start_date="2026-10-03", schedule_mode="end_date", end_date="2026-10-04",
            include_weekends=False,
        ), "Provider", "Model")


def test_empty_material_set_still_produces_stable_buffer_days():
    preview = build_schedule(LearningUnitSet(title="空资料", units=[]), PlanningOptions(
        batch_id=1, start_date="2026-10-01", day_count=5, daily_available_minutes=60,
    ), "Provider", "Model")
    assert len(preview.days) == 5
    assert all(day.units == [] for day in preview.days)
    assert all(day.title == "缓冲与调整" for day in preview.days)
    assert preview.total_estimated_minutes == 0
    assert preview.average_daily_minutes == 1


def test_confirming_520_generated_children_remains_fully_pageable(client, db):
    batch = ImportBatch(source_kind="directory", root_name="超大型课程", status="planned")
    db.add(batch)
    db.commit()
    units = [{
        "id": f"huge-{index}", "title": f"自动任务 {index + 1}", "document_id": 1,
        "source_path": "huge-course.md", "unit_type": "reading", "topic": "大型计划",
        "sequence": index, "estimated_minutes": 10, "splittable": False,
        "prerequisites": [], "summary": "自动生成后仍需可查看、可编辑", "metadata": {},
    } for index in range(520)]
    schedule = {
        "title": "超大型自动任务清单", "goal": "验证大量任务不会消失",
        "start_date": "2026-10-01", "end_date": "2026-10-01", "day_count": 1,
        "total_estimated_minutes": 5200, "average_daily_minutes": 5200,
        "days": [{
            "date": "2026-10-01", "title": "大型计划", "estimated_minutes": 5200,
            "capacity_minutes": 60, "load_ratio": 86.67, "units": units,
            "warnings": ["预计超出当日容量 5140 分钟"], "locked": False,
        }],
        "unscheduled_units": [], "warnings": ["所有资料仍会保留并创建为可编辑任务。"],
        "provider": "Extreme Planner", "model": "extreme-test-model",
    }
    confirmed = client.post("/api/planning/confirm", json={"batch_id": batch.id, "schedule": schedule})
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["created"] == 521
    assert len(result["list_ids"]) == 1

    list_id = result["list_ids"][0]
    first = client.get("/api/items", params={"list_id": list_id, "limit": 500, "offset": 0}).json()
    second = client.get("/api/items", params={"list_id": list_id, "limit": 500, "offset": 500}).json()
    combined = first + second
    assert len(first) == 500
    assert len(second) == 21
    assert len({item["id"] for item in combined}) == 521
    assert sum(item["parent_id"] is None for item in combined) == 1
    assert sum(item["parent_id"] is not None for item in combined) == 520
    assert all(item["list_id"] == list_id for item in combined)
