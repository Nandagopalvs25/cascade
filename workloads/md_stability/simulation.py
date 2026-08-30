import logging
import time
from dataclasses import dataclass

from openff.toolkit import Molecule
from openmm import CustomExternalForce, LangevinMiddleIntegrator, Platform, unit
from openmm.app import ForceField as OpenMMForceField
from openmm.app import HBonds, Modeller, Simulation
from openmmforcefields.generators import GAFFTemplateGenerator
from pdbfixer import PDBFixer
from rdkit import Chem

from job_spec import StabilityParams
from poses import DockedPose
from stability import (
    PoseStabilityTrajectory,
    heavy_atom_rmsd_in_receptor_frame,
    receptor_ligand_contacts,
    retained_contact_fraction,
)

LOGGER = logging.getLogger("cascade.md_stability.simulation")

PROTEIN_FORCE_FIELD = "amber14-all.xml"
IMPLICIT_SOLVENT_FORCE_FIELD = "implicit/gbn2.xml"
RESTRAINT_STRENGTH_KCAL_PER_MOLE_PER_ANGSTROM_SQUARED = 5.0
FRICTION_PER_PICOSECOND = 1.0
PREFERRED_PLATFORMS = ("CUDA", "OpenCL", "CPU")
PLATFORM_PROPERTIES = {"CUDA": {"Precision": "mixed"}, "OpenCL": {"Precision": "mixed"}}


class SimulationSetupError(Exception):
    pass


class PoseSimulationTimeout(Exception):
    pass


def raise_when_pose_budget_is_spent(deadline: float, timeout_seconds: int, stage: str) -> None:
    if time.monotonic() > deadline:
        raise PoseSimulationTimeout(
            f"pose exceeded its {timeout_seconds}s simulation budget during {stage}"
        )


def advance_simulation_within_budget(
    simulation: Simulation,
    total_steps: int,
    chunk_steps: int,
    deadline: float,
    timeout_seconds: int,
    stage: str,
) -> None:
    remaining = total_steps
    while remaining > 0:
        taken = min(chunk_steps, remaining)
        simulation.step(taken)
        remaining -= taken
        raise_when_pose_budget_is_spent(deadline, timeout_seconds, stage)


@dataclass
class PreparedComplex:
    simulation: Simulation
    ligand_atom_indices: list[int]
    receptor_atom_indices: list[int]
    platform_name: str


def platforms_in_preference_order() -> list[Platform]:
    available = {
        Platform.getPlatform(index).getName() for index in range(Platform.getNumPlatforms())
    }
    ordered = [
        Platform.getPlatformByName(name) for name in PREFERRED_PLATFORMS if name in available
    ]
    if not ordered:
        raise SimulationSetupError("no OpenMM platform is available in this container")
    return ordered


def simulation_on_fastest_usable_platform(
    topology: object, system: object, params: StabilityParams
) -> tuple[Simulation, str]:
    rejections = []
    for platform in platforms_in_preference_order():
        platform_name = platform.getName()
        integrator = LangevinMiddleIntegrator(
            params.temperature_kelvin * unit.kelvin,
            FRICTION_PER_PICOSECOND / unit.picosecond,
            params.timestep_femtoseconds * unit.femtoseconds,
        )
        try:
            simulation = Simulation(
                topology, system, integrator, platform, PLATFORM_PROPERTIES.get(platform_name)
            )
        except Exception as error:
            LOGGER.warning(
                "openmm platform %s is present but unusable here: %s", platform_name, error
            )
            rejections.append(f"{platform_name}: {error}")
            continue
        LOGGER.info("simulating on the %s platform", platform_name)
        return simulation, platform_name
    raise SimulationSetupError(
        f"no OpenMM platform could hold the complex - {'; '.join(rejections)}"
    )


def prepared_receptor_from_pdb(pdb_path: str) -> tuple[object, object]:
    fixer = PDBFixer(filename=pdb_path)
    fixer.removeHeterogens(keepWater=False)
    fixer.findMissingResidues()
    fixer.missingResidues = {}
    fixer.findNonstandardResidues()
    fixer.replaceNonstandardResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.4)
    return fixer.topology, fixer.positions


def coordinate_tuples_in_unit(positions: object, length_unit: object) -> list[tuple]:
    return [
        (float(point[0]), float(point[1]), float(point[2]))
        for point in positions.value_in_unit(length_unit)
    ]


def ligand_molecule_from_pose(pose: DockedPose) -> Molecule:
    try:
        molecule = Molecule.from_rdkit(
            Chem.AddHs(pose.mol, addCoords=True), allow_undefined_stereo=True
        )
    except Exception as error:
        raise SimulationSetupError(f"ligand could not be typed for simulation: {error}") from error
    if not molecule.conformers:
        raise SimulationSetupError("ligand carried no conformer to simulate")
    return molecule


def harmonic_position_restraint(
    simulation_system: object, positions: object, atom_indices: list[int]
) -> None:
    restraint = CustomExternalForce("k*((x-x0)^2+(y-y0)^2+(z-z0)^2)")
    restraint.addGlobalParameter(
        "k",
        RESTRAINT_STRENGTH_KCAL_PER_MOLE_PER_ANGSTROM_SQUARED
        * unit.kilocalories_per_mole
        / unit.angstrom**2,
    )
    for name in ("x0", "y0", "z0"):
        restraint.addPerParticleParameter(name)
    coordinates = coordinate_tuples_in_unit(positions, unit.nanometer)
    for index in atom_indices:
        restraint.addParticle(index, list(coordinates[index]))
    simulation_system.addForce(restraint)


def heavy_atom_indices(topology: object, atom_indices: list[int]) -> list[int]:
    by_index = {atom.index: atom for atom in topology.atoms()}
    return [index for index in atom_indices if by_index[index].element.symbol != "H"]


def build_complex_simulation(
    receptor_pdb_path: str, pose: DockedPose, params: StabilityParams
) -> PreparedComplex:
    molecule = ligand_molecule_from_pose(pose)
    receptor_topology, receptor_positions = prepared_receptor_from_pdb(receptor_pdb_path)

    modeller = Modeller(receptor_topology, receptor_positions)
    ligand_positions = molecule.conformers[0].to_openmm()
    receptor_atom_indices = [atom.index for atom in modeller.topology.atoms()]

    modeller.add(molecule.to_topology().to_openmm(), ligand_positions)
    ligand_atom_indices = [
        atom.index
        for atom in modeller.topology.atoms()
        if atom.index not in set(receptor_atom_indices)
    ]

    force_field = OpenMMForceField(PROTEIN_FORCE_FIELD, IMPLICIT_SOLVENT_FORCE_FIELD)
    force_field.registerTemplateGenerator(GAFFTemplateGenerator(molecules=molecule).generator)
    system = force_field.createSystem(modeller.topology, constraints=HBonds, rigidWater=False)
    harmonic_position_restraint(
        system, modeller.positions, heavy_atom_indices(modeller.topology, receptor_atom_indices)
    )

    simulation, platform_name = simulation_on_fastest_usable_platform(
        modeller.topology, system, params
    )
    simulation.context.setPositions(modeller.positions)
    return PreparedComplex(
        simulation=simulation,
        ligand_atom_indices=ligand_atom_indices,
        receptor_atom_indices=receptor_atom_indices,
        platform_name=platform_name,
    )


def coordinates_for_indices(state_positions: object, indices: list[int]) -> list[tuple]:
    coordinates = coordinate_tuples_in_unit(state_positions, unit.angstrom)
    return [coordinates[index] for index in indices]


def simulate_pose_stability(
    receptor_pdb_path: str, pose: DockedPose, params: StabilityParams
) -> tuple[PoseStabilityTrajectory, str]:
    deadline = time.monotonic() + params.simulation_timeout_seconds
    prepared = build_complex_simulation(receptor_pdb_path, pose, params)
    raise_when_pose_budget_is_spent(
        deadline, params.simulation_timeout_seconds, "force field setup"
    )
    simulation = prepared.simulation
    ligand_heavy = heavy_atom_indices(simulation.topology, prepared.ligand_atom_indices)
    receptor_heavy = heavy_atom_indices(simulation.topology, prepared.receptor_atom_indices)

    simulation.minimizeEnergy(maxIterations=params.minimization_max_iterations)
    raise_when_pose_budget_is_spent(
        deadline, params.simulation_timeout_seconds, "energy minimization"
    )
    reference_state = simulation.context.getState(getPositions=True)
    reference_ligand = coordinates_for_indices(reference_state.getPositions(), ligand_heavy)
    reference_receptor = coordinates_for_indices(reference_state.getPositions(), receptor_heavy)
    initial_contacts = receptor_ligand_contacts(
        reference_receptor, reference_ligand, params.contact_cutoff_angstrom
    )

    simulation.context.setVelocitiesToTemperature(params.temperature_kelvin * unit.kelvin)
    steps_per_frame = max(params.production_steps // params.frames_recorded, 1)
    if params.equilibration_steps:
        advance_simulation_within_budget(
            simulation,
            params.equilibration_steps,
            steps_per_frame,
            deadline,
            params.simulation_timeout_seconds,
            "equilibration",
        )

    trajectory = PoseStabilityTrajectory(
        compound_id=pose.name,
        affinity_rank=pose.affinity_rank,
        best_affinity_kcal_per_mol=pose.best_affinity_kcal_per_mol,
        heavy_atoms=len(ligand_heavy),
    )
    for _ in range(params.frames_recorded):
        simulation.step(steps_per_frame)
        raise_when_pose_budget_is_spent(deadline, params.simulation_timeout_seconds, "production")
        positions = simulation.context.getState(getPositions=True).getPositions()
        ligand_frame = coordinates_for_indices(positions, ligand_heavy)
        receptor_frame = coordinates_for_indices(positions, receptor_heavy)
        trajectory.record_frame(
            heavy_atom_rmsd_in_receptor_frame(reference_ligand, ligand_frame),
            retained_contact_fraction(
                initial_contacts,
                receptor_frame,
                ligand_frame,
                params.contact_break_cutoff_angstrom,
            ),
        )
    return trajectory, prepared.platform_name
