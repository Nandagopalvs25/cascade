import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cascade.models import Artifact, Decision, Job, JobState, Run, RunState


def _new_run(card_id: str, parent_run_id: uuid.UUID | None = None) -> Run:
    return Run(
        parent_run_id=parent_run_id,
        trello_card_id=card_id,
        workload="dock",
        state=RunState.queued,
        spec={"run_id": card_id},
    )


def _persist(db_sessionmaker, *rows):
    async def persist():
        async with db_sessionmaker() as session:
            session.add_all(rows)
            await session.commit()

    asyncio.run(persist())


def test_second_job_row_for_same_attempt_is_rejected(db_sessionmaker):
    run = _new_run("card-attempt")
    _persist(db_sessionmaker, run)
    _persist(
        db_sessionmaker,
        Job(run_id=run.id, attempt=1, executor="cloud_run_job", state=JobState.submitted),
    )

    with pytest.raises(IntegrityError):
        _persist(
            db_sessionmaker,
            Job(run_id=run.id, attempt=1, executor="cloud_run_job", state=JobState.submitted),
        )


def test_retry_of_same_run_is_a_distinct_attempt(db_sessionmaker):
    run = _new_run("card-retry")
    _persist(db_sessionmaker, run)
    _persist(
        db_sessionmaker,
        Job(run_id=run.id, attempt=1, executor="cloud_run_job", state=JobState.failed),
        Job(run_id=run.id, attempt=2, executor="cloud_run_job", state=JobState.succeeded),
    )

    async def read_attempts():
        async with db_sessionmaker() as session:
            result = await session.execute(select(Job.attempt).where(Job.run_id == run.id))
            return sorted(result.scalars().all())

    assert asyncio.run(read_attempts()) == [1, 2]


def test_proposed_followup_run_records_its_parent(db_sessionmaker):
    parent = _new_run("card-dock")
    _persist(db_sessionmaker, parent)
    child = _new_run("card-admet", parent_run_id=parent.id)
    child.workload = "admet"
    _persist(db_sessionmaker, child)

    async def read_parent_of_child():
        async with db_sessionmaker() as session:
            result = await session.execute(
                select(Run.parent_run_id).where(Run.trello_card_id == "card-admet")
            )
            return result.scalar_one()

    assert asyncio.run(read_parent_of_child()) == parent.id


def test_decisions_and_artifacts_hang_off_a_run(db_sessionmaker):
    run = _new_run("card-trace")
    _persist(db_sessionmaker, run)
    _persist(
        db_sessionmaker,
        Decision(
            run_id=run.id,
            agent="planner",
            decision_kind="routing",
            rationale="40 ligands fits a single Cloud Run Job at 8 CPU.",
            inputs={"ligand_count": 40},
            output={"executor": "cloud_run_job"},
        ),
        Artifact(run_id=run.id, kind="scores", gcs_uri="gs://test-bucket/scores.csv"),
    )

    async def read_trace():
        async with db_sessionmaker() as session:
            decisions = await session.execute(select(Decision).where(Decision.run_id == run.id))
            artifacts = await session.execute(select(Artifact).where(Artifact.run_id == run.id))
            return decisions.scalars().all(), artifacts.scalars().all()

    decisions, artifacts = asyncio.run(read_trace())
    assert [d.decision_kind for d in decisions] == ["routing"]
    assert [a.gcs_uri for a in artifacts] == ["gs://test-bucket/scores.csv"]
