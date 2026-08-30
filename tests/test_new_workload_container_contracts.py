import importlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from cascade.schemas import JobSpec, TargetStructure

WORKLOADS_DIRECTORY = Path(__file__).resolve().parents[1] / "workloads"
SHARED_CONTAINER_MODULE_NAMES = (
    "job_spec",
    "artifacts",
    "compounds",
    "properties",
    "alerts",
    "assessment",
    "results",
    "run",
    "structure",
    "ligands",
    "docking",
    "confidence",
    "protenix_job",
    "sequences",
    "stability",
    "poses",
    "simulation",
)


@contextmanager
def container_modules(workload: str, names: tuple[str, ...]):
    directory = str(WORKLOADS_DIRECTORY / workload)
    saved_path = list(sys.path)
    saved_modules = {
        name: sys.modules.pop(name) for name in SHARED_CONTAINER_MODULE_NAMES if name in sys.modules
    }
    sys.path.insert(0, directory)
    try:
        yield tuple(importlib.import_module(name) for name in names)
    finally:
        for name in SHARED_CONTAINER_MODULE_NAMES:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def service_job_spec(workload: str, params: dict | None = None) -> JobSpec:
    return JobSpec(
        run_id="run-1",
        workload=workload,
        target=TargetStructure(
            source="rcsb",
            reference="1M17",
            pdb_id="1m17",
            structure_uri="gs://cascade-test/runs/run-1/inputs/structure.pdb",
            chain="A",
        ),
        ligands_uri="gs://cascade-test/runs/run-1/inputs/ligands.smi",
        params=params or {},
        output_uri="gs://cascade-test/runs/run-1/outputs",
        control_compound="erlotinib",
    )


@pytest.mark.parametrize("workload", ["admet", "cofold", "md_stability"])
def test_service_job_spec_round_trips_through_each_container_parser(workload):
    spec = service_job_spec(workload)

    with container_modules(workload, ("job_spec",)) as (job_spec,):
        parsed = job_spec.JobSpec.model_validate_json(spec.model_dump_json())

    assert parsed.model_dump() == spec.model_dump()


@pytest.mark.parametrize("workload", ["admet", "cofold", "md_stability"])
def test_each_container_parser_ignores_fields_the_agent_layer_adds_later(workload):
    payload = json.loads(service_job_spec(workload).model_dump_json())
    payload["planner_rationale"] = "cheapest way to triage these"
    payload["target"]["resolved_by"] = "resolve_target_structure"

    with container_modules(workload, ("job_spec",)) as (job_spec,):
        parsed = job_spec.JobSpec.model_validate(payload)

    assert parsed.run_id == "run-1"
    assert parsed.target.pdb_id == "1M17"


@pytest.mark.parametrize("workload", ["admet", "cofold", "md_stability"])
def test_each_container_parser_rejects_a_stage_that_has_no_container(workload):
    payload = json.loads(service_job_spec(workload).model_dump_json())
    payload["workload"] = "synthesis"

    with container_modules(workload, ("job_spec",)) as (job_spec,):
        with pytest.raises(ValueError):
            job_spec.JobSpec.model_validate(payload)


def test_admet_params_read_the_job_spec_params_and_default_the_rest():
    spec = service_job_spec("admet", {"herg_logp_threshold": 4.5, "max_compounds": 50})

    with container_modules("admet", ("job_spec",)) as (job_spec,):
        params = job_spec.AdmetParams.from_job_spec(
            job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        )

    assert params.herg_logp_threshold == 4.5
    assert params.max_compounds == 50
    assert params.herg_minimum_aromatic_rings == 2
    assert params.lipinski_violations_that_fail == 2


def test_fold_params_read_the_job_spec_params_and_default_the_rest():
    spec = service_job_spec("cofold", {"seeds": [7, 8], "samples_per_seed": 2})

    with container_modules("cofold", ("job_spec",)) as (job_spec,):
        params = job_spec.FoldParams.from_job_spec(
            job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        )

    assert params.seeds == [7, 8]
    assert params.seeds_argument == "7,8"
    assert params.samples_per_seed == 2
    assert params.model_name == "protenix_base_default_v1.0.0"
    assert params.use_msa is False


def pdb_with_seqres_and_atoms() -> str:
    seqres = [
        "SEQRES   1 A   24  MET LYS THR ALA TYR ILE ALA LYS GLN ARG GLN ILE SER",
        "SEQRES   2 A   24  PHE VAL LYS SER HIS PHE SER ARG GLN LEU GLU",
        "SEQRES   1 B    3  GLY GLY GLY",
    ]
    atoms = [
        f"ATOM  {index:>5}  CA  {residue} A{index:>4}    "
        f"{float(index):>8.3f}{0.0:>8.3f}{0.0:>8.3f}  1.00 20.00           C"
        for index, residue in enumerate(["MET", "LYS", "THR", "ALA", "TYR"], start=1)
    ]
    return "\n".join([*seqres, *atoms, "END", ""])


def test_fold_container_reads_the_target_sequence_from_seqres_records():
    with container_modules("cofold", ("sequences",)) as (sequences,):
        sequence = sequences.protein_sequence_from_structure_text(
            pdb_with_seqres_and_atoms(), chain="A"
        )

    assert sequence == "MKTAYIAKQRQISFVKSHFSRQLE"


def test_fold_container_falls_back_to_alpha_carbons_when_seqres_is_absent():
    atoms_only = "\n".join(
        line for line in pdb_with_seqres_and_atoms().splitlines() if not line.startswith("SEQRES")
    )

    with container_modules("cofold", ("sequences",)) as (sequences,):
        with pytest.raises(sequences.SequenceExtractionError):
            sequences.protein_sequence_from_structure_text(atoms_only, chain="A")


def test_fold_container_reads_a_fasta_target():
    with container_modules("cofold", ("sequences",)) as (sequences,):
        sequence = sequences.protein_sequence_from_structure_text(
            ">egfr kinase domain\nMKTAYIAKQRQISFVKSHFSRQLE\n"
        )

    assert sequence == "MKTAYIAKQRQISFVKSHFSRQLE"


def test_protenix_input_matches_the_documented_sequence_entity_schema():
    with container_modules("cofold", ("protenix_job",)) as (protenix_job,):
        payload = protenix_job.build_protenix_input(
            "MKTAYIAKQRQISFVKSHFSRQLE",
            [
                protenix_job.ComplexRequest(
                    name="001_erlotinib", compound_name="erlotinib", smiles="C#Cc1ccccc1"
                )
            ],
        )

    assert payload == [
        {
            "name": "001_erlotinib",
            "sequences": [
                {"proteinChain": {"sequence": "MKTAYIAKQRQISFVKSHFSRQLE", "count": 1}},
                {"ligand": {"ligand": "C#Cc1ccccc1", "count": 1}},
            ],
        }
    ]


def test_protenix_command_carries_the_model_seeds_and_msa_switch(tmp_path):
    spec = service_job_spec("cofold", {"seeds": [11, 12], "use_msa": True})

    with container_modules("cofold", ("job_spec", "protenix_job")) as (
        job_spec,
        protenix_job,
    ):
        params = job_spec.FoldParams.from_job_spec(
            job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        )
        command = protenix_job.protenix_prediction_command(
            tmp_path / "in.json", tmp_path / "out", params
        )

    assert command[:2] == ["protenix", "pred"]
    assert "--model_name" in command
    assert command[command.index("--seeds") + 1] == "11,12"
    assert command[command.index("--use_msa") + 1] == "true"


def write_summary_confidence(directory: Path, complex_name: str, rank: int, score: float) -> None:
    predictions = directory / "dataset" / complex_name / "seed_101" / "predictions"
    predictions.mkdir(parents=True, exist_ok=True)
    (predictions / f"{complex_name}_summary_confidence_sample_{rank}.json").write_text(
        json.dumps({"ranking_score": score, "plddt": 0.8, "iptm": 0.7})
    )
    (predictions / f"{complex_name}_sample_{rank}.cif").write_text("data_stub\n")


def test_fold_container_keeps_the_highest_confidence_sample_per_compound(tmp_path):
    write_summary_confidence(tmp_path, "001_erlotinib", 0, 0.91)
    write_summary_confidence(tmp_path, "001_erlotinib", 1, 0.55)
    write_summary_confidence(tmp_path, "002_gefitinib", 0, 0.77)

    with container_modules("cofold", ("protenix_job", "confidence")) as (
        protenix_job,
        confidence,
    ):
        summaries = protenix_job.summary_confidence_files(tmp_path)
        predictions = confidence.load_complex_predictions(
            summaries, {"001_erlotinib": "erlotinib", "002_gefitinib": "gefitinib"}
        )
        ranked = confidence.best_prediction_per_compound(predictions)

    assert [prediction.compound_name for prediction in ranked] == ["erlotinib", "gefitinib"]
    assert [prediction.ranking_score for prediction in ranked] == [0.91, 0.77]
    assert ranked[0].seed == 101


def test_fold_results_refuse_to_present_confidence_as_an_affinity(tmp_path):
    write_summary_confidence(tmp_path, "001_erlotinib", 0, 0.91)
    spec = service_job_spec("cofold")

    with container_modules("cofold", ("job_spec", "protenix_job", "confidence", "results")) as (
        job_spec,
        protenix_job,
        confidence,
        results,
    ):
        params = job_spec.FoldParams.from_job_spec(
            job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        )
        ranked = confidence.best_prediction_per_compound(
            confidence.load_complex_predictions(
                protenix_job.summary_confidence_files(tmp_path), {"001_erlotinib": "erlotinib"}
            )
        )
        container_spec = job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        control = results.measure_control_compound(container_spec, ranked)
        summary = results.build_run_summary(
            container_spec, params, "MKTAYIAKQ", ranked, [], control
        )

    assert summary["method"]["engine"] == "protenix"
    assert "not binding free" in summary["method"]["caveat"]
    assert summary["predictions"][0]["ranking_score"] == 0.91
    assert "affinity" not in summary["predictions"][0]
    assert control.status == "measured"
    assert control.rank == 1


def md_summary_for(
    stability, name: str, rmsds: list[float], contacts: list[float], rank: int | None = None
):
    trajectory = stability.PoseStabilityTrajectory(compound_id=name, affinity_rank=rank)
    for rmsd, retained in zip(rmsds, contacts, strict=True):
        trajectory.record_frame(rmsd, retained)
    return stability.summarize_pose_stability(trajectory, 2.5, 0.6)


def test_stability_params_read_the_job_spec_params_and_default_the_rest():
    spec = service_job_spec("md_stability", {"production_steps": 20000, "max_complexes": 3})

    with container_modules("md_stability", ("job_spec",)) as (job_spec,):
        parsed = job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        params = job_spec.StabilityParams.from_job_spec(parsed)

        assert params.production_steps == 20000
        assert params.max_complexes == 3
        assert params.temperature_kelvin == 300.0
        assert params.production_picoseconds == 40.0


def test_every_planner_settable_md_param_is_honoured_by_the_container():
    from cascade.agents.policy import allowed_job_params_for_workload

    with container_modules("md_stability", ("job_spec",)) as (job_spec,):
        assert allowed_job_params_for_workload("md_stability") <= set(
            job_spec.StabilityParams.model_fields
        )


def test_md_container_calls_a_pose_that_holds_position_and_contacts_stable():
    with container_modules("md_stability", ("stability",)) as (stability,):
        summary = md_summary_for(stability, "indinavir", [0.4, 0.6, 0.8], [1.0, 0.95, 0.9])

        assert summary.verdict == "stable"
        assert summary.final_rmsd_angstrom == 0.8


def test_md_container_calls_a_pose_that_moves_but_keeps_contacts_drifted():
    with container_modules("md_stability", ("stability",)) as (stability,):
        summary = md_summary_for(stability, "ritonavir", [0.5, 2.0, 3.4], [0.9, 0.8, 0.7])

        assert summary.verdict == "drifted"
        assert "beyond the 2.5 A drift threshold" in " ".join(summary.reasons)


def test_md_container_calls_a_pose_that_loses_its_contacts_unstable():
    with container_modules("md_stability", ("stability",)) as (stability,):
        summary = md_summary_for(stability, "caffeine", [0.6, 5.0, 11.2], [0.9, 0.3, 0.05])

        assert summary.verdict == "unstable"
        assert "left the pocket" in " ".join(summary.reasons)


def test_md_container_does_not_call_a_returning_excursion_drifted():
    with container_modules("md_stability", ("stability",)) as (stability,):
        summary = md_summary_for(stability, "saquinavir", [0.5, 3.2, 0.9], [0.95, 0.7, 0.92])

        assert summary.verdict == "stable"
        assert "mobile but not displaced" in " ".join(summary.reasons)


def test_md_rmsd_is_measured_in_the_receptor_frame_without_alignment():
    with container_modules("md_stability", ("stability",)) as (stability,):
        reference = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]

        assert stability.heavy_atom_rmsd_in_receptor_frame(reference, reference) == 0.0
        assert (
            stability.heavy_atom_rmsd_in_receptor_frame(
                reference, [(1.0, 0.0, 0.0), (2.0, 1.0, 1.0)]
            )
            == 1.0
        )


def test_md_contacts_are_counted_within_the_cutoff_and_retention_is_a_fraction():
    with container_modules("md_stability", ("stability",)) as (stability,):
        contacts = stability.receptor_ligand_contacts(
            [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], 4.0
        )

        assert contacts == {(0, 0)}
        assert (
            stability.retained_contact_fraction(
                {(0, 0), (1, 0)}, [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], 5.5
            )
            == 0.5
        )


def test_md_thermal_jitter_past_the_contact_cutoff_does_not_break_a_contact():
    with container_modules("md_stability", ("stability",)) as (stability,):
        initial = stability.receptor_ligand_contacts([(0.0, 0.0, 0.0)], [(3.9, 0.0, 0.0)], 4.0)
        assert initial == {(0, 0)}

        jittered = stability.retained_contact_fraction(
            initial, [(0.0, 0.0, 0.0)], [(4.6, 0.0, 0.0)], 5.5
        )
        departed = stability.retained_contact_fraction(
            initial, [(0.0, 0.0, 0.0)], [(7.0, 0.0, 0.0)], 5.5
        )

        assert jittered == 1.0
        assert departed == 0.0


def test_md_verdict_uses_sustained_contact_retention_not_one_noisy_final_frame():
    with container_modules("md_stability", ("stability",)) as (stability,):
        trajectory = stability.PoseStabilityTrajectory(compound_id="indinavir")
        for _ in range(9):
            trajectory.record_frame(1.3, 0.95)
        trajectory.record_frame(1.36, 0.54)

        summary = stability.summarize_pose_stability(trajectory, 2.5, 0.6)

        assert summary.final_contact_retention == 0.54
        assert summary.sustained_contact_retention > 0.6
        assert summary.verdict == "stable"


def test_md_results_refuse_to_present_pose_stability_as_an_affinity():
    spec = service_job_spec("md_stability")

    with container_modules("md_stability", ("job_spec", "stability", "results")) as (
        job_spec,
        stability,
        results,
    ):
        parsed = job_spec.JobSpec.model_validate_json(spec.model_dump_json())
        summaries = [
            md_summary_for(stability, "erlotinib", [0.4, 0.7], [1.0, 0.92], rank=1),
            md_summary_for(stability, "caffeine", [0.6, 11.2], [0.9, 0.05], rank=2),
        ]
        summary = results.build_run_summary(
            parsed,
            job_spec.StabilityParams.from_job_spec(parsed),
            summaries,
            [],
            results.measure_control_compound(parsed, summaries),
            "CPU",
        )

        assert summary["verdict_counts"] == {"stable": 1, "drifted": 0, "unstable": 1}
        assert summary["control_compound"]["verdict"] == "stable"
        assert summary["stability_analysis"]["most_stable_compound"] == "erlotinib"
        assert "not a binding free energy" in summary["method"]["caveat"]
        assert (
            "must not be read as a predicted affinity" in (summary["stability_analysis"]["caveat"])
        )


def test_triage_reads_the_per_compound_key_the_md_container_writes():
    from cascade.agents.policy import compound_records_from_manifest

    records = compound_records_from_manifest(
        "md_stability", {"trajectories": [{"compound_id": "erlotinib", "verdict": "stable"}]}
    )

    assert records == [{"compound_id": "erlotinib", "verdict": "stable"}]
    assert compound_records_from_manifest("md_stability", {"scores": [{"compound_id": "x"}]}) == []
