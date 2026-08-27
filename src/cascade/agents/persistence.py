import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cascade.db import async_session
from cascade.models import Artifact, Decision, Job, JobState, Run, RunState
from cascade.schemas import JobSpec


def job_interrupt_id(run_id: str, attempt: int) -> str:
    return f"job:{run_id}:{attempt}"


def parsed_run_uuid(run_id: str | None) -> uuid.UUID | None:
    if not run_id:
        return None
    try:
        return uuid.UUID(run_id)
    except ValueError:
        return None


async def record_run_and_reserve_job_attempt(
    run_id: str,
    card_id: str,
    spec: JobSpec,
    executor: str,
    attempt: int,
    parent_run_id: str | None = None,
) -> str | None:
    run_uuid = uuid.UUID(run_id)
    async with async_session() as session:
        run = await session.get(Run, run_uuid)
        if run is None:
            parent_uuid = parsed_run_uuid(parent_run_id)
            if parent_uuid is not None and await session.get(Run, parent_uuid) is None:
                parent_uuid = None
            session.add(
                Run(
                    id=run_uuid,
                    parent_run_id=parent_uuid,
                    trello_card_id=card_id,
                    workload=spec.workload,
                    state=RunState.queued,
                    spec=spec.model_dump(exclude_none=True),
                )
            )
        else:
            run.workload = spec.workload
            run.state = RunState.queued
            run.spec = spec.model_dump(exclude_none=True)

        existing = await load_job_for_attempt(session, run_id, attempt)
        if existing is None:
            session.add(
                Job(
                    run_id=run_uuid,
                    attempt=attempt,
                    executor=executor,
                    state=JobState.submitted,
                )
            )
        await session.commit()
        return existing.external_id if existing is not None else None


async def attach_execution_name_to_job(run_id: str, attempt: int, execution_name: str) -> None:
    async with async_session() as session:
        job = await load_job_for_attempt(session, run_id, attempt)
        if job is None:
            return
        job.external_id = execution_name
        job.state = JobState.running
        await session.commit()


async def load_succeeded_run_for_card(card_id: str) -> tuple[str, str] | None:
    async with async_session() as session:
        result = await session.execute(
            select(Run.id, Run.workload).where(
                Run.trello_card_id == card_id, Run.state == RunState.succeeded
            )
        )
        row = result.first()
        return (str(row[0]), row[1]) if row is not None else None


async def load_job_for_attempt(session: AsyncSession, run_id: str, attempt: int) -> Job | None:
    result = await session.execute(
        select(Job).where(Job.run_id == uuid.UUID(run_id), Job.attempt == attempt)
    )
    return result.scalar_one_or_none()


async def load_pending_job_attempt_and_card(
    session: AsyncSession, run_id: str
) -> tuple[int, str] | None:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return None

    result = await session.execute(
        select(Job.attempt, Run.trello_card_id)
        .join(Run, Job.run_id == Run.id)
        .where(Job.run_id == run_uuid, Job.finished_at.is_(None))
        .order_by(Job.attempt.desc())
    )
    row = result.first()
    return (row[0], row[1]) if row is not None else None


async def record_job_completion(
    run_id: str, attempt: int, succeeded: bool, exit_code: int, artifact_uris: dict[str, str]
) -> int:
    finished_at = datetime.now(UTC)
    run_uuid = uuid.UUID(run_id)
    async with async_session() as session:
        job = await load_job_for_attempt(session, run_id, attempt)
        if job is not None:
            job.state = JobState.succeeded if succeeded else JobState.failed
            job.exit_code = exit_code
            job.finished_at = finished_at

        run = await session.get(Run, run_uuid)
        if run is not None:
            run.state = RunState.succeeded if succeeded else RunState.failed
            run.finished_at = finished_at

        recorded = 0
        for kind, uri in artifact_uris.items():
            if not uri:
                continue
            session.add(Artifact(run_id=run_uuid, kind=kind, gcs_uri=uri))
            recorded += 1

        await session.commit()
        return recorded


async def record_decision(
    run_id: str, agent: str, decision_kind: str, rationale: str, inputs: dict, output: dict
) -> None:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        return
    async with async_session() as session:
        if await session.get(Run, run_uuid) is None:
            return
        session.add(
            Decision(
                run_id=run_uuid,
                agent=agent,
                decision_kind=decision_kind,
                rationale=rationale,
                inputs=inputs,
                output=output,
            )
        )
        await session.commit()


async def workloads_already_run_in_lineage(run_id: str) -> dict[str, str]:
    run_uuid = parsed_run_uuid(run_id)
    if run_uuid is None:
        return {}
    already_run: dict[str, str] = {}
    visited: set[uuid.UUID] = set()
    async with async_session() as session:
        while run_uuid is not None and run_uuid not in visited:
            visited.add(run_uuid)
            run = await session.get(Run, run_uuid)
            if run is None:
                break
            already_run.setdefault(run.workload, str(run.id))
            run_uuid = run.parent_run_id
    return already_run
