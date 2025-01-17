# Base python
import os
import traceback
from typing import Dict, List, Set
import time
from pathlib import Path
import pprint
from functools import partial
import multiprocessing as mp
import json

# Scientific python libraries
from dask.distributed import Client
import joblib

joblib.externals.loky.set_loky_pickler('pickle')
import ray

## Custom Imports
from pandda_gemmi import constants
from pandda_gemmi.common import Dtag, EventID, Partial
from pandda_gemmi.args import PanDDAArgs
from pandda_gemmi.pandda_logging import STDOUTManager, log_arguments, PanDDAConsole
from pandda_gemmi.dependencies import check_dependencies
from pandda_gemmi.dataset import Datasets, Reference, StructureFactors, smooth, smooth_ray, DatasetStatistics
from pandda_gemmi.edalignment import (Grid, Alignments, from_unaligned_dataset_c,
                                      from_unaligned_dataset_c_flat, from_unaligned_dataset_c_ray,
                                      from_unaligned_dataset_c_flat_ray,
                                      )
from pandda_gemmi.filters import remove_models_with_large_gaps
from pandda_gemmi.comparators import get_multiple_comparator_sets, ComparatorCluster
from pandda_gemmi.shells import get_shells_multiple_models
from pandda_gemmi.logs import (
    summarise_grid, save_json_log, summarise_datasets, dump_datasets, pandda_note, pandda_warning, report_removed_datasets
)

from pandda_gemmi.pandda_functions import (
    process_local_serial,
    process_local_joblib,
    process_local_multiprocessing,
    process_local_dask,
    process_local_ray,
    get_dask_client,
    process_global_serial,
    process_global_dask,
    get_shells,
    get_shells_clustered,
    get_comparators_high_res,
    get_comparators_high_res_random,
    get_comparators_closest_cutoff,
    get_comparators_closest_apo_cutoff,
    get_clusters_nn,
    get_comparators_closest_cluster,
    validate_strategy_num_datasets,
    validate,
    get_common_structure_factors,
)
from pandda_gemmi.event import Event, Events
from pandda_gemmi.ranking import (
    rank_events_size,
    rank_events_autobuild,
)
from pandda_gemmi.autobuild import (
    AutobuildResult,
    autobuild_rhofit,
    autobuild_rhofit_ray,
    merge_ligand_into_structure_from_paths,
    save_pdb_file,
)
from pandda_gemmi.tables import (
    EventTable,
    SiteTable,
)
from pandda_gemmi.fs import PanDDAFSModel, ShellDirs
from pandda_gemmi.processing import (
    process_shell,
    process_shell_multiple_models,
    ShellResult,
    analyse_model,
    analyse_model_ray
)

printer = pprint.PrettyPrinter()
console = PanDDAConsole()

def update_log(shell_log, shell_log_path):
    if shell_log_path.exists():
        os.remove(shell_log_path)

    with open(shell_log_path, "w") as f:
        json.dump(shell_log, f, indent=2)

pp = pprint.PrettyPrinter(indent=4, compact=False, sort_dicts=True)

def get_comparator_func(pandda_args, load_xmap_flat_func, process_local):
    if pandda_args.comparison_strategy == "closest":
        # Closest datasets after clustering
        raise NotImplementedError()

    elif pandda_args.comparison_strategy == "high_res":
        # Almost Old PanDDA strategy: highest res datasets

        comparators_func = Partial(
            get_comparators_high_res,
            comparison_min_comparators=pandda_args.comparison_min_comparators,
            comparison_max_comparators=pandda_args.comparison_max_comparators,
        )

    elif pandda_args.comparison_strategy == "high_res_random":
        # Old pandda strategy: random datasets that are higher resolution

        comparators_func = Partial(
            get_comparators_high_res_random,
            comparison_min_comparators=pandda_args.comparison_min_comparators,
            comparison_max_comparators=pandda_args.comparison_max_comparators,
        )

    elif pandda_args.comparison_strategy == "cluster":
        comparators_func = Partial(
            get_multiple_comparator_sets,
            comparison_min_comparators=pandda_args.comparison_min_comparators,
            sample_rate=pandda_args.sample_rate,
            # TODO: add option: pandda_args.resolution_cutoff,
            resolution_cutoff=3.0,
            load_xmap_flat_func=load_xmap_flat_func,
            process_local=process_local,
            debug=pandda_args.debug,
        )

    else:
        raise Exception("Unrecognised comparison strategy")

    return comparators_func


def get_process_local(pandda_args):
    if pandda_args.local_processing == "serial":
        process_local = process_local_serial
        pandda_note("using process_local_serial")

    elif pandda_args.local_processing == "joblib":
        process_local = partial(process_local_joblib, n_jobs=pandda_args.local_cpus, verbose=50, max_nbytes=None)
        pandda_note("using process_local_joblib")

    elif pandda_args.local_processing == "multiprocessing_forkserver":
        mp.set_start_method("forkserver")
        process_local = partial(process_local_multiprocessing, n_jobs=pandda_args.local_cpus, method="forkserver")
        # process_local_load = partial(process_local_joblib, int(joblib.cpu_count() * 3), "threads")
        pandda_note("using process_local_multiprocessing [forkserver]")

    elif pandda_args.local_processing == "multiprocessing_spawn":
        mp.set_start_method("spawn")
        process_local = partial(process_local_multiprocessing, n_jobs=pandda_args.local_cpus, method="spawn")
        # process_local_load = partial(process_local_joblib, int(joblib.cpu_count() * 3), "threads")
        pandda_note("using process_local_multiprocessing [spawn]")

    elif pandda_args.local_processing == "dask":
        client = Client(n_workers=pandda_args.local_cpus)
        process_local = partial(
            process_local_dask,
            client=client
        )
        pandda_note("using process_local_dask")

    elif pandda_args.local_processing == "ray":
        ray.init(num_cpus=pandda_args.local_cpus)
        process_local = partial(process_local_ray, )
        pandda_note("using process_local_ray")

    else:
        raise Exception()

    return process_local


def get_smooth_func(pandda_args):
    if pandda_args.local_processing == "ray":
        smooth_func = smooth_ray
        pandda_note("using smooth_ray")
    else:
        smooth_func = smooth
        pandda_note("using smooth")

    return smooth_func


def get_load_xmap_func(pandda_args):
    if pandda_args.local_processing == "ray":
        load_xmap_func = from_unaligned_dataset_c_ray
        pandda_note("using from_unaligned_dataset_c_ray")
    else:
        load_xmap_func = from_unaligned_dataset_c
        pandda_note("using from_unaligned_dataset_c")
    return load_xmap_func


def get_load_xmap_flat_func(pandda_args):
    if pandda_args.local_processing == "ray":
        load_xmap_flat_func = from_unaligned_dataset_c_flat_ray
        pandda_note("using from_unaligned_dataset_c_flat_ray")
    else:
        load_xmap_flat_func = from_unaligned_dataset_c_flat
        pandda_note("using from_unaligned_dataset_c_flat")
    return load_xmap_flat_func


def get_analyse_model_func(pandda_args):
    if pandda_args.local_processing == "ray":
        analyse_model_func = analyse_model_ray
    else:
        analyse_model_func = analyse_model
    return analyse_model_func


def process_pandda(pandda_args: PanDDAArgs, ):
    ###################################################################
    # # Configuration
    ###################################################################
    time_start = time.time()

    # Process args
    distributed_tmp = Path(pandda_args.distributed_tmp)

    # Check dependencies
    # with STDOUTManager('Checking dependencies ...', 'All dependencies validated!'):
    console.start_dependancy_check()
    check_dependencies(pandda_args)

    # Initialise log
    # with STDOUTManager('Initialising log ...', 'PanDDA log initialised!'):
    console.start_log()
    pandda_log: Dict = {}
    pandda_log[constants.LOG_START] = time.time()
    initial_args = log_arguments(pandda_args, )

    pandda_log[constants.LOG_ARGUMENTS] = initial_args

    # Get global processor
    # with STDOUTManager('Getting global processor ...', 'Got global processor!'):
    console.start_initialise_shell_processor()
    if pandda_args.global_processing == "serial":
        process_global = process_global_serial
    elif pandda_args.global_processing == "distributed":
        client = get_dask_client(
            scheduler=pandda_args.distributed_scheduler,
            num_workers=pandda_args.distributed_num_workers,
            queue=pandda_args.distributed_queue,
            project=pandda_args.distributed_project,
            cores_per_worker=pandda_args.local_cpus,
            distributed_mem_per_core=pandda_args.distributed_mem_per_core,
            resource_spec=pandda_args.distributed_resource_spec,
            job_extra=pandda_args.distributed_job_extra,
            walltime=pandda_args.distributed_walltime,
            watcher=pandda_args.distributed_watcher,
        )
        process_global = partial(
            process_global_dask,
            client=client,
            tmp_dir=distributed_tmp
        )
    else:
        raise Exception()

    # Get local processor
    # with STDOUTManager('Getting local processor ...', 'Got local processor!'):
    console.start_initialise_multiprocessor()
    process_local = get_process_local(pandda_args)

    smooth_func = get_smooth_func(pandda_args)
    load_xmap_func = get_load_xmap_func(pandda_args)
    load_xmap_flat_func = get_load_xmap_flat_func(pandda_args)
    analyse_model_func = get_analyse_model_func(pandda_args)

    comparators_func = get_comparator_func(
        pandda_args,
        load_xmap_flat_func,
        process_local
    )

    # Set up autobuilding
    if pandda_args.autobuild:

        # with STDOUTManager('Setting up autobuilding ...', 'Set up autobuilding!'):
        if pandda_args.autobuild_strategy == "rhofit":

            if pandda_args.local_processing == "ray":
                autobuild_func = autobuild_rhofit_ray
            else:
                autobuild_func = autobuild_rhofit,

        elif pandda_args.autobuild_strategy == "inbuilt":
            raise NotImplementedError("Autobuilding with inbuilt method is not yet implemented")


        else:
            raise Exception(f"Autobuild strategy: {pandda_args.autobuild_strategy} is not valid!")

    else:
        pandda_note("no auto-building requested")

    try:

        ###################################################################
        # # Get datasets
        ###################################################################

        # with STDOUTManager(f'Building model of file system in {pandda_args.data_dirs} ...',
        #                    'Built file system model!'):
        console.start_fs_model()
        time_fs_model_building_start = time.time()
        pandda_fs_model: PanDDAFSModel = PanDDAFSModel.from_dir(
            pandda_args.data_dirs,
            pandda_args.out_dir,
            pandda_args.pdb_regex,
            pandda_args.mtz_regex,
            pandda_args.ligand_dir_regex,
            pandda_args.ligand_cif_regex,
            pandda_args.ligand_pdb_regex,
            pandda_args.ligand_smiles_regex,
            process_local=None
        )
        pandda_fs_model.build(process_local=None)
        time_fs_model_building_finish = time.time()
        pandda_log["FS model building time"] = time_fs_model_building_finish - time_fs_model_building_start

        console.summarise_fs_model(pandda_fs_model)
        update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        ###################################################################
        # # Pre-pandda
        ###################################################################

        # Get datasets
        # with STDOUTManager('Loading datasets ...', 'Loaded datasets!'):
        console.start_load_datasets()
        datasets_initial: Datasets = Datasets.from_dir(pandda_fs_model, )
        dataset_statistics = DatasetStatistics(datasets_initial.datasets)
        console.summarise_datasets(datasets_initial.datasets, dataset_statistics)

        dump_datasets(datasets_initial)

        # If structure factors not given, check if any common ones are available
        with STDOUTManager('Looking for common structure factors in datasets ...','Found structure factors!'):
            if not pandda_args.structure_factors:
                structure_factors = get_common_structure_factors(datasets_initial)
                # If still no structure factors
                if not structure_factors:
                    raise Exception(
                        "No common structure factors found in mtzs. Please manually provide the labels with the --structure_factors option.")
            else:
                structure_factors = StructureFactors(pandda_args.structure_factors[0], pandda_args.structure_factors[1])

            print('\tf, phi =',structure_factors.f,structure_factors.phi)

        # Make dataset validator
        validation_strategy = partial(
            validate_strategy_num_datasets,
            min_characterisation_datasets=pandda_args.min_characterisation_datasets,
        )
        validate_parameterized = partial(
            validate,
            strategy=validation_strategy,
        )

        # this filter code is using the dataset_xyz from the previous
        # step, which makes it a bit triclky to switch on/off specific
        # filters. Ideally, each filter should just Initial
        # filtersreturn the latest lif of datasets for the next step
        # (which itself can be active or deactivated)

        with STDOUTManager('Filtering datasets with invalid structure factors ...','Done!'):
            datasets_invalid: Datasets = datasets_initial.remove_invalid_structure_factor_datasets(
                structure_factors)
            pandda_log[constants.LOG_INVALID] = [dtag.dtag for dtag in datasets_initial if dtag not in datasets_invalid]
            validate_parameterized(datasets_invalid, exception=Exception("Too few datasets after filter: invalid"))
            report_removed_datasets(datasets_initial,datasets_invalid)

        with STDOUTManager('Truncating MTZ columns to only those needed for PanDDA ...','Done!'):
            datasets_truncated_columns = datasets_invalid.drop_columns(structure_factors)
            report_removed_datasets(datasets_invalid,datasets_truncated_columns)

        with STDOUTManager('Removing datasets with poor low resolution completeness ...','Done!'):
            datasets_low_res: Datasets = datasets_truncated_columns.remove_low_resolution_datasets(
                pandda_args.low_resolution_completeness)
            pandda_log[constants.LOG_LOW_RES] = [dtag.dtag for dtag in datasets_truncated_columns if
                                                 dtag not in datasets_low_res]
            validate_parameterized(datasets_low_res, exception=Exception("Too few datasets after filter: low res"))
            report_removed_datasets(datasets_truncated_columns,datasets_low_res)

        if pandda_args.max_rfree < 1:
            with STDOUTManager('Removing datasets with poor rfree ...','Done!'):
                datasets_rfree: Datasets = datasets_low_res.remove_bad_rfree(pandda_args.max_rfree)
                pandda_log[constants.LOG_RFREE] = [dtag.dtag for dtag in datasets_low_res if
                                                   dtag not in datasets_rfree]
                validate_parameterized(datasets_rfree, exception=Exception("Too few datasets after filter: rfree"))
                report_removed_datasets(datasets_low_res,datasets_rfree)
        else:
            datasets_rfree = datasets_low_res

        with STDOUTManager('Removing datasets with poor wilson rmsd ...','Done!'):
            datasets_wilson: Datasets = datasets_rfree.remove_bad_wilson(
                pandda_args.max_wilson_plot_z_score)  # TODO
            validate_parameterized(datasets_wilson, exception=Exception("Too few datasets after filter: wilson"))
            report_removed_datasets(datasets_rfree,datasets_wilson)

        # Select reference
        with STDOUTManager('Deciding on reference dataset ...','Done!'):
            reference: Reference = Reference.from_datasets(datasets_wilson)
            pandda_log["Reference Dtag"] = reference.dtag.dtag
            print('\treference dataset =',reference.dtag.dtag)

        # Post-reference filters
        with STDOUTManager('Performing b-factor smoothing ...','Done!'):
            start = time.time()
            datasets_smoother: Datasets = datasets_wilson.smooth_datasets(
                reference,
                structure_factors=structure_factors,
                smooth_func=smooth_func,
                mapper=process_local,
            )
            finish = time.time()
            pandda_log["Time to perform b factor smoothing"] = finish - start

        with STDOUTManager('Removing datasets with dissimilar models ...','Done!'):
            datasets_diss_struc: Datasets = datasets_smoother.remove_dissimilar_models(
                reference,
                pandda_args.max_rmsd_to_reference,
            )
            pandda_log[constants.LOG_DISSIMILAR_STRUCTURE] = [dtag.dtag for dtag in datasets_smoother if
                                                              dtag not in datasets_diss_struc]
            report_removed_datasets(datasets_smoother,datasets_diss_struc)
            validate_parameterized(datasets_diss_struc, exception=Exception("Too few datasets after filter: structure"))

        with STDOUTManager('Removing datasets whose models have large gaps ...','Done!'):
            datasets_gaps: Datasets = remove_models_with_large_gaps(datasets_diss_struc, reference )
            for dtag in datasets_gaps:
                if dtag not in datasets_diss_struc.datasets:
                    print(f"WARNING: Removed dataset {dtag} due to a large gap")
            pandda_log[constants.LOG_GAPS] = [dtag.dtag for dtag in datasets_diss_struc if
                                              dtag not in datasets_gaps]
            report_removed_datasets(datasets_diss_struc,datasets_gaps)
            validate_parameterized(datasets_gaps, exception=Exception("Too few datasets after filter: structure gaps"))

        with STDOUTManager('Removing datasets with dissimilar spacegroups to the reference ...','Done!'):
            datasets_diss_space: Datasets = datasets_gaps.remove_dissimilar_space_groups(reference)
            report_removed_datasets(datasets_gaps,datasets_diss_space)
            pandda_log[constants.LOG_SG] = [dtag.dtag for dtag in datasets_gaps if
                                            dtag not in datasets_diss_space]
            validate_parameterized(datasets_diss_space,
                                  exception=Exception("Too few datasets after filter: space group"))

            datasets = {dtag: datasets_diss_space[dtag] for dtag in datasets_diss_space}
            pandda_log[constants.LOG_DATASETS] = summarise_datasets(datasets, pandda_fs_model)

        if pandda_args.debug:
            print(pandda_log[constants.LOG_DATASETS])

        # Grid
        with STDOUTManager('Getting the analysis grid ...','Done!'):
            grid: Grid = Grid.from_reference(reference,
                                             pandda_args.outer_mask,
                                             pandda_args.inner_mask_symmetry,
                                             # sample_rate=pandda_args.sample_rate,
                                             sample_rate=reference.dataset.reflections.resolution().resolution / 0.5
                                             )
        #pp.pprint(grid.grid)

        with STDOUTManager('Getting local alignments of the electron density to the reference ...','Done!'):
            alignments: Alignments = Alignments.from_datasets(
                reference,
                datasets,
            )
        #pp.pprint(alignments)

        update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        ###################################################################
        # # Assign comparison datasets
        ###################################################################

        with STDOUTManager('Deciding on the datasets to characterise the groundstate for each dataset to analyse ...','Done!'):

            comparators, cluster_assignments = comparators_func(
                datasets,
                alignments,
                grid,
                structure_factors,
                pandda_fs_model,
            )
        pandda_note("comparators below")
        pp.pprint(comparators)

        pandda_log["Cluster Assignments"] = {dtag.dtag: int(cluster) for dtag, cluster in cluster_assignments.items()}
        pandda_log["Neighbourhood core dtags"] = {int(neighbourhood_number): [dtag.dtag for dtag in
                                                                          neighbourhood.core_dtags]
                                                 for neighbourhood_number, neighbourhood
                                                 in comparators.items()
                                                 }

        if pandda_args.debug:
            print("Comparators are:")
            printer.pprint(pandda_log["Cluster Assignments"])
            printer.pprint(pandda_log["Neighbourhood core dtags"])
            printer.pprint(comparators)

        update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        ###################################################################
        # # Process shells
        ###################################################################

        # Partition the Analysis into shells in which all datasets are being processed at a similar resolution for the
        # sake of computational efficiency
        with STDOUTManager('Deciding on how to partition the datasets into resolution shells for processing ...',
                           'Done!'):
            if pandda_args.comparison_strategy == "cluster":
                pandda_note("using comparison strategy = \"cluster\"")
                shells = get_shells_multiple_models(
                    datasets,
                    comparators,
                    pandda_args.min_characterisation_datasets,
                    pandda_args.max_shell_datasets,
                    pandda_args.high_res_increment,
                    pandda_args.only_datasets,
                    debug=pandda_args.debug,
                )
                # TODO
                if pandda_args.debug:
                    print('Got shells that support multiple models')
                    for shell_res, shell in shells.items():
                        print(f'\tShell res: {shell.res}: {shell.test_dtags[:3]}')
                        for cluster_num, dtags in shell.train_dtags.items():
                            print(f'\t\t{cluster_num}: {dtags[:5]}')

            else:
                shells = get_shells(
                    datasets,
                    comparators,
                    pandda_args.min_characterisation_datasets,
                    pandda_args.max_shell_datasets,
                    pandda_args.high_res_increment,
                )
            pandda_fs_model.shell_dirs = ShellDirs.from_pandda_dir(pandda_fs_model.pandda_dir, shells)
            pandda_fs_model.shell_dirs.build()

        pandda_note("results (in shells) follow below")
        pp.pprint(shells)

        if pandda_args.debug:
            printer.pprint(shells)

        # Parameterise
        if pandda_args.comparison_strategy == "cluster":
            process_shell_paramaterised = partial(
                process_shell_multiple_models,
                process_local=process_local,
                structure_factors=structure_factors,
                sample_rate=pandda_args.sample_rate,
                contour_level=pandda_args.contour_level,
                cluster_cutoff_distance_multiplier=pandda_args.cluster_cutoff_distance_multiplier,
                min_blob_volume=pandda_args.min_blob_volume,
                min_blob_z_peak=pandda_args.min_blob_z_peak,
                outer_mask=pandda_args.outer_mask,
                inner_mask_symmetry=pandda_args.inner_mask_symmetry,
                max_site_distance_cutoff=pandda_args.max_site_distance_cutoff,
                min_bdc=pandda_args.min_bdc,
                max_bdc=pandda_args.max_bdc,
                memory_availability=pandda_args.memory_availability,
                statmaps=pandda_args.statmaps,
                load_xmap_func=load_xmap_func,
                analyse_model_func=analyse_model_func,
                debug=pandda_args.debug,
            )
        else:
            process_shell_paramaterised = partial(
                process_shell,
                process_local=process_local,
                structure_factors=structure_factors,
                sample_rate=pandda_args.sample_rate,
                contour_level=pandda_args.contour_level,
                cluster_cutoff_distance_multiplier=pandda_args.cluster_cutoff_distance_multiplier,
                min_blob_volume=pandda_args.min_blob_volume,
                min_blob_z_peak=pandda_args.min_blob_z_peak,
                outer_mask=pandda_args.outer_mask,
                inner_mask_symmetry=pandda_args.inner_mask_symmetry,
                max_site_distance_cutoff=pandda_args.max_site_distance_cutoff,
                min_bdc=pandda_args.min_bdc,
                max_bdc=pandda_args.max_bdc,
                memory_availability=pandda_args.memory_availability,
                statmaps=pandda_args.statmaps,
                load_xmap_func=load_xmap_func,
            )
        pandda_note("process_shell_paramaterised below")
        pp.pprint(process_shell_paramaterised)

        # Process the shells
        with STDOUTManager('Processing the shells ...','Done!'):
            time_shells_start = time.time()
            shell_results: List[ShellResult] = process_global(
                [
                    partial(
                        process_shell_paramaterised,
                        shell,
                        datasets,
                        alignments,
                        grid,
                        pandda_fs_model,
                        reference,
                    )
                    for res, shell
                    in shells.items()
                ],
            )
            time_shells_finish = time.time()
            pandda_log[constants.LOG_SHELLS] = {
                res: shell_result.log
                for res, shell_result
                in zip(shells, shell_results)
                if shell_result
            }
            pandda_log["Time to process all shells"] = time_shells_finish - time_shells_start
            if pandda_args.debug:
                print(f"Time to process all shells: {time_shells_finish - time_shells_start}")

        all_events: Dict[EventID, Event] = {}
        for shell_result in shell_results:
            if shell_result:
                for dtag, dataset_result in shell_result.dataset_results.items():
                    all_events.update(dataset_result.events.events)

        # Add the event maps to the fs
        for event_id, event in all_events.items():
            pandda_fs_model.processed_datasets[event_id.dtag].event_map_files.add_event(event)

        update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        ###################################################################
        # # Autobuilding
        ###################################################################

        # Autobuild the results if set to
        if pandda_args.autobuild:
            with STDOUTManager('Attempting to autobuild events ...','Done!'):

                if pandda_args.global_processing == 'serial':
                    process_autobuilds = process_local
                else:
                    process_autobuilds = process_global

                time_autobuild_start = time.time()
                autobuild_results_list: Dict[EventID, AutobuildResult] = process_autobuilds(
                    [
                        Partial(
                            autobuild_func,
                            datasets[event_id.dtag],
                            all_events[event_id],
                            pandda_fs_model,
                            cif_strategy=pandda_args.cif_strategy,
                            rhofit_coord=pandda_args.rhofit_coord,
                        )
                        for event_id
                        in all_events
                    ]
                )

                time_autobuild_finish = time.time()
                pandda_log[constants.LOG_AUTOBUILD_TIME] = time_autobuild_finish - time_autobuild_start

                autobuild_results: Dict[EventID, AutobuildResult] = {
                    event_id: autobuild_result
                    for event_id, autobuild_result
                    in zip(all_events, autobuild_results_list)
                }

                # Save results
                pandda_log[constants.LOG_AUTOBUILD_COMMANDS] = {}
                for event_id, autobuild_result in autobuild_results.items():
                    dtag = str(event_id.dtag.dtag)
                    if dtag not in pandda_log[constants.LOG_AUTOBUILD_COMMANDS]:
                        pandda_log[constants.LOG_AUTOBUILD_COMMANDS][dtag] = {}

                    event_idx = int(event_id.event_idx.event_idx)

                    pandda_log[constants.LOG_AUTOBUILD_COMMANDS][dtag][event_idx] = autobuild_result.command

            with STDOUTManager('Updating the PanDDA models with best scoring fragment build ...','Done!'):
                # Add the best fragment by scoring method to default model
                pandda_log[constants.LOG_AUTOBUILD_SELECTED_BUILDS] = {}
                pandda_log[constants.LOG_AUTOBUILD_SELECTED_BUILD_SCORES] = {}
                for dtag in datasets:
                    dataset_autobuild_results = {
                        event_id: autobuild_result
                        for event_id, autobuild_result
                        in autobuild_results.items()
                        if dtag == event_id.dtag
                    }

                    if len(dataset_autobuild_results) == 0:
                        # print("\tNo autobuilds for this dataset!")
                        continue

                    all_scores = {}
                    for event_id, autobuild_result in dataset_autobuild_results.items():
                        for path, score in autobuild_result.scores.items():
                            all_scores[path] = score

                    if len(all_scores) == 0:
                        # print(f"\tNo autobuilds for this dataset!")
                        continue

                    # Select fragment build
                    selected_fragement_path = max(
                        all_scores,
                        key=lambda _path: all_scores[_path],
                    )

                    pandda_log[constants.LOG_AUTOBUILD_SELECTED_BUILDS][dtag.dtag] = str(selected_fragement_path)
                    pandda_log[constants.LOG_AUTOBUILD_SELECTED_BUILD_SCORES][dtag.dtag] = float(
                        all_scores[selected_fragement_path])

                    # Copy to pandda models
                    model_path = str(pandda_fs_model.processed_datasets[dtag].input_pdb)
                    pandda_model_path = pandda_fs_model.processed_datasets[
                                            dtag].dataset_models.path / constants.PANDDA_EVENT_MODEL.format(dtag.dtag)
                    merged_structure = merge_ligand_into_structure_from_paths(model_path, selected_fragement_path)
                    save_pdb_file(merged_structure, pandda_model_path)

            update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        ###################################################################
        # # Rank Events
        ###################################################################
        with STDOUTManager('Ranking events ...','Done!'):
            if pandda_args.rank_method == "size":
                pandda_note("ranking by size")
                all_events_ranked = rank_events_size(all_events, grid)
            elif pandda_args.rank_method == "size_delta":
                pandda_note("ranking by size_delta")
                raise NotImplementedError()
                # all_events_ranked = rank_events_size_delta()
            elif pandda_args.rank_method == "cnn":
                pandda_note("ranking by cnn")
                raise NotImplementedError()
                # all_events_ranked = rank_events_cnn()

            elif pandda_args.rank_method == "autobuild":
                pandda_note("ranking by autobuild")
                if not pandda_args.autobuild:
                    raise Exception("Cannot rank on autobuilds if autobuild is not set!")
                else:
                    all_events_ranked = rank_events_autobuild(
                        all_events,
                        autobuild_results,
                        datasets,
                        pandda_fs_model,
                    )
            else:
                raise Exception(f"Ranking method: {pandda_args.rank_method} is unknown!")

            update_log(pandda_log, pandda_args.out_dir / constants.PANDDA_LOG_FILE)

        pp.pprint(all_events_ranked)

        ###################################################################
        # # Assign Sites
        ###################################################################

        # Get the events and assign sites to them
        with STDOUTManager('Assigning sites to each event ...','Done!'):
            all_events_events = Events.from_all_events(all_events_ranked, grid, pandda_args.max_site_distance_cutoff)

        pp.pprint(all_events_events)

        ###################################################################
        # # Output pandda summary information
        ###################################################################

        # Output a csv of the events
        with STDOUTManager('Building and outputting event table ...','Done!'):
            event_table: EventTable = EventTable.from_events(all_events_events)
            event_table.save(pandda_fs_model.analyses.pandda_analyse_events_file)

        # Output site table
        with STDOUTManager('Building and outputting site table ...','Done!'):
            site_table: SiteTable = SiteTable.from_events(all_events_events, pandda_args.max_site_distance_cutoff)
            site_table.save(pandda_fs_model.analyses.pandda_analyse_sites_file)

        time_finish = time.time()
        pandda_log[constants.LOG_TIME] = time_finish - time_start

        # Output json log
        with STDOUTManager('Saving json log with detailed information on run ...','Done!'):
            if pandda_args.debug:
                printer.pprint(pandda_log)
            save_json_log(
                pandda_log,
                pandda_args.out_dir / constants.PANDDA_LOG_FILE,
            )

        print(f"PanDDA ran in: {time_finish - time_start}")

    ###################################################################
    # # Handle Exceptions
    ###################################################################

    except Exception as e:
        # traceback.print_exc()
        console.print_exception(e, pandda_args.debug)
        console.save(pandda_fs_model.console_log_file)

        pandda_log[constants.LOG_TRACE] = traceback.format_exc()
        pandda_log[constants.LOG_EXCEPTION] = str(e)

        print(f"Saving PanDDA log to: {pandda_args.out_dir / constants.PANDDA_LOG_FILE}")

        # printer.pprint(
        #     pandda_log
        # )

        save_json_log(
            pandda_log,
            pandda_args.out_dir / constants.PANDDA_LOG_FILE,
        )


if __name__ == '__main__':
    print('\n ======================= PanDDA2 version 2022-02-28 ===============================\n')
    with STDOUTManager('Parsing command line args ...', 'Parsed command line arguments!'):
        args = PanDDAArgs.from_command_line()
        print('\n'.join("          %s=\t%s" % item for item in vars(args).items()))
        #print(args)
        if args.only_datasets:
            print(args.only_datasets)
        console.summarise_arguments(args)

    process_pandda(args)
