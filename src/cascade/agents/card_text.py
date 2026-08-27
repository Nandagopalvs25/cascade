import re

from cascade.agents.compound_library import smiles_library_lines_from_text
from cascade.agents.policy import (
    allowed_job_params_for_workload,
    executor_for_workload,
)
from cascade.agents.schemas import (
    BlockedStage,
    CompletedStage,
    JobOutcome,
    ProposalDecision,
    ProposalRequest,
    ProposedFollowup,
    TriagedJobResult,
    TriageVerdict,
    WorkloadPlan,
)

STAGE_COST_ESTIMATES = {
    "dock": ("$0.05", "~5 min"),
    "admet": ("$0.02", "~1 min"),
    "cofold": ("$0.40", "~15 min"),
    "md_stability": ("$0.40", "~15 min"),
}

EXECUTOR_LABELS = {"cloud_run_job": "Cloud Run Job", "cloud_batch": "Cloud Batch, spot GPU"}

PROPOSED_FROM_RUN_PATTERN = re.compile(r"from run `([0-9a-fA-F-]{36})`")

SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+")

MODEL_TALKING_TO_ITSELF_PATTERN = re.compile(
    r"^(wait|let's|let us|ok|okay|done|perfect|adjusting|ready to output|no further text)\b",
    re.IGNORECASE,
)

MAXIMUM_AGENT_PROSE_SENTENCES = 4

MAXIMUM_AGENT_PROSE_SENTENCE_CHARACTERS = 320


def agent_prose_without_runaway_text(prose: str, fallback: str) -> str:
    kept: list[str] = []
    for sentence in SENTENCE_BOUNDARY_PATTERN.split(" ".join(prose.split())):
        if not sentence:
            continue
        if len(sentence) > MAXIMUM_AGENT_PROSE_SENTENCE_CHARACTERS:
            break
        if MODEL_TALKING_TO_ITSELF_PATTERN.match(sentence):
            break
        kept.append(sentence)
        if len(kept) == MAXIMUM_AGENT_PROSE_SENTENCES:
            break
    return " ".join(kept) if kept else fallback


def skipped_lines_note(skipped_lines: int) -> str:
    if skipped_lines == 0:
        return ""
    return f", {skipped_lines} line(s) that were not compounds"


BINDING_SITE_METHOD_LABELS = {
    "co_crystal": "derived from the co-crystallized ligand",
    "described_pocket": "the pocket coordinates given on the card",
    "predicted_structure": "taken from the predicted structure",
    "none": "no co-crystal ligand and no pocket on the card, so the whole structure is searched",
}


def binding_site_choice_line(plan: WorkloadPlan) -> str:
    method = BINDING_SITE_METHOD_LABELS[plan.binding_site_method]
    return f"- Binding site: {method} ({plan.binding_site_confidence} confidence)"


def chosen_setting_lines(plan: WorkloadPlan, workload: str) -> list[str]:
    allowed = allowed_job_params_for_workload(workload)
    chosen = {
        key: value
        for key, value in plan.params.model_dump(exclude_none=True).items()
        if key in allowed
    }
    if not chosen:
        return ["- Settings: every setting left at its default"]
    return [f"- `{key}`: {value}" for key, value in sorted(chosen.items())]


def planner_decision_lines(plan: WorkloadPlan, workload: str) -> list[str]:
    lines = [f"**How CASCADE chose to run this.** {plan.rationale}", ""]
    if workload == "dock":
        lines.append(binding_site_choice_line(plan))
    if plan.control_compound:
        lines.append(f"- Control compound: `{plan.control_compound}`")
    lines.extend(chosen_setting_lines(plan, workload))
    return lines


def docking_score_reliability_line(analysis: dict) -> str:
    if not analysis:
        return ""
    notes: list[str] = []
    indistinguishable = analysis.get("compounds_indistinguishable_from_best_count") or 0
    error = analysis.get("scoring_function_error_kcal_per_mol")
    if analysis.get("ranking_separates_best_compound") is False and indistinguishable > 1:
        notes.append(
            f"the top {indistinguishable} compounds are within the {error} kcal/mol scoring "
            "error of each other, so this ranking does not identify a single best compound"
        )
    if analysis.get("ranking_is_size_driven"):
        notes.append(
            "affinity tracks molecule size (Spearman "
            f"{analysis.get('affinity_heavy_atom_correlation')}), so larger compounds are "
            "favoured by the score rather than by fit"
        )
    if analysis.get("metrics_agree_on_best_compound") is False:
        notes.append(
            "raw affinity and ligand efficiency disagree on the best compound "
            f"(ligand efficiency favours {analysis.get('best_by_ligand_efficiency')})"
        )
    if not notes:
        return ""
    return "\n\n**Score reliability:** " + "; ".join(notes) + "."


def docking_headline(summary: dict) -> str:
    return (
        f"{summary.get('ligands_docked', 'unknown')} compounds docked "
        f"({summary.get('ligands_failed', 0)} failed to load); highest score "
        f"{summary.get('best_compound_id', 'unknown')} at "
        f"{summary.get('best_affinity_kcal_per_mol', 'unknown')} kcal/mol"
        f"{docking_score_reliability_line(summary.get('score_analysis') or {})}"
    )


def admet_headline(summary: dict) -> str:
    liabilities = summary.get("liability_counts") or {}
    herg_high = liabilities.get("herg_high", 0)
    pains = liabilities.get("pains", 0)
    return (
        f"{summary.get('compounds_assessed', 'unknown')} compounds screened; "
        f"{summary.get('passed', 0)} passed, {summary.get('flagged', 0)} flagged, "
        f"{summary.get('failed', 0)} failed "
        f"({herg_high} high hERG risk, {pains} PAINS)"
    )


def cofold_headline(summary: dict) -> str:
    return (
        f"{summary.get('complexes_folded', 'unknown')} complexes co-folded with "
        f"{summary.get('protenix_model', 'protenix')}; highest structural confidence was "
        f"{summary.get('best_compound_id', 'unknown')} at ranking score "
        f"{summary.get('best_ranking_score', 'unknown')}"
    )


HEADLINE_BY_WORKLOAD = {
    "dock": docking_headline,
    "admet": admet_headline,
    "cofold": cofold_headline,
}


def job_outcome_headline(outcome: JobOutcome) -> str:
    if outcome.status != "succeeded":
        return f"{outcome.workload} failed with exit code {outcome.exit_code}: {outcome.error}"
    headline = HEADLINE_BY_WORKLOAD.get(outcome.workload)
    if headline is None:
        return f"{outcome.workload} finished"
    return headline(outcome.summary)


def control_compound_detail(workload: str, control: dict) -> str:
    if workload == "dock":
        top_pose_rmsd = control.get("rmsd_to_cocrystal_angstrom")
        lowest_mode_rmsd = control.get("lowest_mode_rmsd_angstrom")
        rank = control.get("lowest_mode_rank")
        if lowest_mode_rmsd is not None and rank not in (None, 1):
            return (
                f"top-ranked pose RMSD {top_pose_rmsd} A, "
                f"closest pose {lowest_mode_rmsd} A at rank {rank}"
            )
        return f"top-ranked pose RMSD {top_pose_rmsd} A"
    if workload == "admet":
        return f"verdict {control.get('verdict')}"
    if workload == "cofold":
        return f"rank {control.get('rank')}, ranking score {control.get('ranking_score')}"
    return ""


def control_compound_line(workload: str, control: dict) -> str:
    if not control or control.get("status") in (None, "not_requested"):
        return ""
    detail = control_compound_detail(workload, control)
    suffix = f" ({detail})" if detail else ""
    return f"\nControl `{control.get('requested_name')}`: {control.get('status')}{suffix}"


CONTROL_VERDICT_LABELS = {
    "passed": "passed",
    "pose_sampled_not_top_ranked": "partial",
    "failed": "FAILED",
}


def compound_disposition_lines(verdict: TriageVerdict) -> list[str]:
    if not verdict.compounds:
        return []
    counts = {"promote": 0, "hold": 0, "reject": 0}
    for judgement in verdict.compounds:
        counts[judgement.disposition] += 1
    lines = [
        "",
        f"{counts['promote']} promoted, {counts['hold']} held, {counts['reject']} rejected.",
    ]
    lines += [
        f"- `{judgement.compound_id}` {judgement.disposition}: {judgement.reason}"
        for judgement in verdict.compounds
    ]
    return lines


def triage_card_comment(triaged: TriagedJobResult) -> str:
    verdict = triaged.verdict
    control = triaged.control
    lines = [f"**Triage.** {verdict.headline}", ""]
    if control.verdict == "not_measured":
        lines.append(f"Control: not measured - {control.detail}")
    else:
        lines.append(
            f"Control `{control.compound_name}`: "
            f"{CONTROL_VERDICT_LABELS[control.verdict]} - {control.detail}"
        )
    lines.append(
        "These results separate the compounds from one another."
        if verdict.results_discriminate
        else "These results do NOT separate the compounds from one another."
    )
    lines += compound_disposition_lines(verdict)
    lines += ["", verdict.rationale]
    return "\n".join(lines)


def estimated_stage_cost_line(workload: str, compound_count: int) -> str:
    estimate = STAGE_COST_ESTIMATES.get(workload)
    if estimate is None:
        return ""
    executor = EXECUTOR_LABELS[executor_for_workload(workload, compound_count)]
    return f"**Estimate.** {estimate[0]} - {estimate[1]} - {executor}. Rough figure, not metered."


def library_lines_for_compounds(library_text: str, compound_names: list[str]) -> list[str]:
    lines, _ = smiles_library_lines_from_text(library_text)
    by_name = {}
    for line in lines:
        smiles, _, name = line.partition("\t")
        by_name[name.strip().lower()] = (smiles, name.strip())
    rendered = []
    for compound_name in compound_names:
        entry = by_name.get(compound_name.strip().lower())
        if entry is not None:
            rendered.append(f"`{entry[0]}` {entry[1]}")
    return rendered


def carried_compound_framing(request: ProposalRequest, compound_count: int) -> str:
    if request.carried_disposition == "promote":
        return (
            f"**Compounds.** {compound_count} compound(s) promoted by the "
            f"{request.completed_stage} stage:"
        )
    return (
        f"**Compounds.** The {request.completed_stage} stage did not separate these "
        f"{compound_count} compound(s), so they all carry forward rather than a ranking that "
        f"cannot support a winner:"
    )


def target_description_line(completed: CompletedStage) -> str:
    if not completed.target_source or not completed.target_reference:
        return ""
    named = completed.target_name or completed.target_reference
    return f"**Target.** {named} ({completed.target_source} {completed.target_reference})"


def proposed_card_description(
    node_input: ProposalDecision, library_lines: list[str], parent_run_id: str
) -> str:
    request = node_input.request
    reason = " ".join(node_input.proposal.reason.split())
    sections = [
        target_description_line(node_input.completed),
        f"**Why.** {reason}",
        estimated_stage_cost_line(node_input.proposal.next_stage, len(library_lines)),
        carried_compound_framing(request, len(library_lines)),
        "\n".join(library_lines),
        f"**Proposed by CASCADE** from run `{parent_run_id}` "
        f"({request.completed_stage}). Drag to To Do to run it.",
    ]
    return "\n\n".join(section for section in sections if section)


def parent_run_id_in_card_description(description: str) -> str | None:
    match = PROPOSED_FROM_RUN_PATTERN.search(description)
    return match.group(1) if match else None


def blocked_stages_note(blocked: list[BlockedStage]) -> str:
    if not blocked:
        return ""
    reasons = " ".join(f"`{stage.workload}` - {stage.reason}" for stage in blocked)
    return f"CASCADE proposed no card for the other stages because {reasons}"


def unproposed_stage_note(request: ProposalRequest) -> str:
    if not request.run_is_trustworthy:
        return (
            f"The {request.completed_stage} run was not judged trustworthy, so CASCADE will not "
            f"build further work on top of it."
        )
    if not request.carried_compounds:
        return (
            f"No compound survived the {request.completed_stage} stage, so there is nothing to "
            f"carry forward."
        )
    return (
        f"{len(request.carried_compounds)} compound(s) survived the {request.completed_stage} "
        f"stage, but CASCADE has no stage it can run on them today."
    )


def followup_card_comment(followup: ProposedFollowup) -> str:
    if followup.created_card_id is None:
        return f"**No follow-up proposed.** {followup.note} {followup.blocked_note}".strip()
    return (
        f"**Proposed next step: {followup.next_stage}.** {followup.note}\n\n"
        f"Created a card in Recommended carrying "
        f"{len(followup.carried_compounds)} compound(s): "
        f"{', '.join(f'`{name}`' for name in followup.carried_compounds)}. "
        f"Drag it to To Do to run it.\n\n{followup.blocked_note}".rstrip()
    )
