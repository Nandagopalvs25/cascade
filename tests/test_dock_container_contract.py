import json
import sys
from pathlib import Path

import pytest

from cascade.schemas import BindingSite, JobSpec, TargetStructure

DOCK_CONTAINER_DIRECTORY = Path(__file__).resolve().parents[1] / "workloads" / "dock"
sys.path.insert(0, str(DOCK_CONTAINER_DIRECTORY))

from job_spec import DockingParams  # noqa: E402
from job_spec import JobSpec as ContainerJobSpec  # noqa: E402
from structure import (  # noqa: E402
    binding_site_from_atoms,
    extract_protein_receptor_pdb,
    find_cocrystal_ligands,
    receptor_atom_count,
    receptor_chain_ids,
)


def pdb_atom_line(
    record: str,
    serial: int,
    atom_name: str,
    residue_name: str,
    chain_id: str,
    residue_number: int,
    x: float,
    y: float,
    z: float,
    element: str,
    alternate_location: str = " ",
) -> str:
    return (
        f"{record:<6}{serial:>5} {atom_name:<4}{alternate_location}{residue_name:>3} "
        f"{chain_id}{residue_number:>4}    {x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00 20.00"
        f"          {element:>2}"
    )


@pytest.fixture
def structure_pdb_text() -> str:
    lines = [
        pdb_atom_line("ATOM", 1, "N", "PRO", "A", 1, 0.0, 0.0, 0.0, "N"),
        pdb_atom_line("ATOM", 2, "CA", "PRO", "A", 1, 1.0, 0.0, 0.0, "C"),
        pdb_atom_line("ATOM", 3, "C", "PRO", "A", 1, 2.0, 0.0, 0.0, "C"),
        pdb_atom_line("ATOM", 4, "O", "PRO", "A", 1, 3.0, 0.0, 0.0, "O"),
        pdb_atom_line("ATOM", 5, "CB", "SER", "A", 2, 4.0, 0.0, 0.0, "C", alternate_location="B"),
        "TER       5      PRO A   1",
        pdb_atom_line("ATOM", 6, "N", "PRO", "B", 1, 0.0, 5.0, 0.0, "N"),
        pdb_atom_line("ATOM", 7, "CA", "PRO", "B", 1, 1.0, 5.0, 0.0, "C"),
        pdb_atom_line("ATOM", 8, "C", "PRO", "B", 1, 2.0, 5.0, 0.0, "C"),
        pdb_atom_line("ATOM", 9, "O", "PRO", "B", 1, 3.0, 5.0, 0.0, "O"),
        pdb_atom_line("HETATM", 10, "O", "HOH", "A", 300, 9.0, 9.0, 9.0, "O"),
    ]
    lines += [
        pdb_atom_line("HETATM", 11 + index, f"C{index}", "GOL", "A", 400, 20.0, 20.0, 20.0, "C")
        for index in range(6)
    ]
    lines += [
        pdb_atom_line(
            "HETATM",
            17 + index,
            f"C{index}",
            "LIG",
            "B",
            902,
            10.0 + index * 0.5,
            12.0 + index * 0.5,
            14.0,
            "C",
        )
        for index in range(8)
    ]
    lines += [
        "ENDMDL",
        pdb_atom_line("ATOM", 99, "N", "PRO", "Z", 1, 50.0, 50.0, 50.0, "N"),
    ]
    return "\n".join(lines) + "\n"


def service_job_spec() -> JobSpec:
    return JobSpec(
        run_id="run-1",
        workload="dock",
        target=TargetStructure(
            source="rcsb",
            reference="1HSG",
            pdb_id="1hsg",
            structure_uri="gs://cascade-test/runs/run-1/inputs/1HSG.pdb",
            chain=None,
        ),
        ligands_uri="gs://cascade-test/runs/run-1/inputs/ligands.sdf",
        binding_site=BindingSite(center_x=11.634, center_y=22.47, center_z=5.859),
        params={"exhaustiveness": 32, "seed": 7},
        output_uri="gs://cascade-test/runs/run-1/outputs",
        control_compound="indinavir",
    )


def test_service_job_spec_round_trips_through_the_container_parser():
    spec = service_job_spec()

    parsed = ContainerJobSpec.model_validate_json(spec.model_dump_json())

    assert parsed.model_dump() == spec.model_dump()


def test_container_parser_ignores_fields_the_agent_layer_adds_later():
    payload = json.loads(service_job_spec().model_dump_json())
    payload["planner_rationale"] = "docking is the cheapest way to rank these"
    payload["target"]["resolved_by"] = "resolve_target_structure"

    parsed = ContainerJobSpec.model_validate(payload)

    assert parsed.run_id == "run-1"
    assert parsed.target.pdb_id == "1HSG"


def test_container_parser_rejects_a_stage_that_has_no_container():
    payload = json.loads(service_job_spec().model_dump_json())
    payload["workload"] = "synthesis"

    with pytest.raises(ValueError):
        ContainerJobSpec.model_validate(payload)


def test_docking_params_read_the_job_spec_params_and_default_the_rest():
    params = DockingParams.from_job_spec(
        ContainerJobSpec.model_validate_json(service_job_spec().model_dump_json())
    )

    assert params.exhaustiveness == 32
    assert params.seed == 7
    assert params.num_modes == 9
    assert params.receptor_ph == 7.4


def test_binding_site_survives_the_round_trip_as_the_numbers_vina_receives():
    spec = ContainerJobSpec.model_validate_json(service_job_spec().model_dump_json())

    assert spec.binding_site is not None
    assert spec.binding_site.center == [11.634, 22.47, 5.859]
    assert spec.binding_site.box_size == [20.0, 20.0, 20.0]


def test_receptor_extraction_keeps_protein_and_drops_everything_else(structure_pdb_text):
    receptor = extract_protein_receptor_pdb(structure_pdb_text)

    assert receptor_atom_count(receptor) == 8
    assert receptor_chain_ids(receptor) == ["A", "B"]
    assert "HOH" not in receptor
    assert "LIG" not in receptor
    assert "GOL" not in receptor
    assert "SER" not in receptor
    assert " Z " not in receptor


def test_receptor_extraction_honours_the_requested_chain(structure_pdb_text):
    receptor = extract_protein_receptor_pdb(structure_pdb_text, chain="A")

    assert receptor_chain_ids(receptor) == ["A"]
    assert receptor_atom_count(receptor) == 4


def test_receptor_extraction_fails_loudly_on_a_structure_with_no_protein():
    with pytest.raises(ValueError):
        extract_protein_receptor_pdb("REMARK nothing to dock into\nEND\n")


def test_cocrystal_search_ignores_water_and_crystallography_additives(structure_pdb_text):
    ligands = find_cocrystal_ligands(structure_pdb_text)

    assert [ligand.label for ligand in ligands] == ["LIG:B:902"]
    assert ligands[0].heavy_atom_count == 8


def test_binding_site_centres_on_the_cocrystal_ligand_and_clamps_small_boxes(structure_pdb_text):
    ligand = find_cocrystal_ligands(structure_pdb_text)[0]

    site = binding_site_from_atoms(ligand.heavy_atoms, padding=5.0)

    assert site.center == [11.75, 13.75, 14.0]
    assert site.box_size == [16.0, 16.0, 16.0]


def test_binding_site_grows_with_the_ligand_extent(structure_pdb_text):
    ligand = find_cocrystal_ligands(structure_pdb_text)[0]

    site = binding_site_from_atoms(ligand.heavy_atoms, padding=10.0)

    assert site.size_x == 23.5
    assert site.size_z == 20.0
