from __future__ import annotations

# Base python
import dataclasses
import time
import pprint
from functools import partial
import os
import json
from typing import Set

printer = pprint.PrettyPrinter()

# Scientific python libraries


## Custom Imports
from pandda_gemmi.logs import (
    summarise_array,
)

from pandda_gemmi import constants
from pandda_gemmi.pandda_functions import (
    process_local_serial,
    truncate,
    save_native_frame_zmap
)
from pandda_gemmi.python_types import *
from pandda_gemmi.common import Dtag, EventID
from pandda_gemmi.fs import PanDDAFSModel, MeanMapFile, StdMapFile
from pandda_gemmi.dataset import (StructureFactors, Dataset, Datasets,
                                  Resolution, )
from pandda_gemmi.shells import Shell, ShellMultipleModels
from pandda_gemmi.edalignment import Partitioning, Xmap, XmapArray, Grid
from pandda_gemmi.model import Zmap, Model, Zmaps
from pandda_gemmi.event import Event, Clusterings, Clustering, Events, get_event_mask_indicies


@dataclasses.dataclass()
class DatasetResult:
    dtag: Dtag
    events: Dict[EventID, Event]
    log: Dict


@dataclasses.dataclass()
class ShellResult:
    shell: Shell
    dataset_results: Dict[Dtag, DatasetResult]
    log: Dict


def update_log(shell_log, shell_log_path):
    if shell_log_path.exists():
        os.remove(shell_log_path)

    with open(shell_log_path, "w") as f:
        json.dump(shell_log, f, indent=4)


def select_model(model_results: Dict[int, Dict], grid):
    model_scores = {}
    model_selection_log = {}
    # for model_number, model_result in model_results.items():
    #     num_merged_clusters = len(model_result['clusterings_merged'])
    #     if num_merged_clusters == 0:
    #         model_score = 0
    #     elif num_merged_clusters < 6:
    #         model_score = num_merged_clusters
    #     elif num_merged_clusters < 12:
    #         model_score = 0.5
    #     else:
    #         model_score = -1
    #
    #     #TODO Get number of z map points in large clusters vs outside them as signal to noise estimate
    #
    #     model_scores[model_number] = model_score
    #
    #

    number_of_events = {
        model_number: len(model_result['clusterings_large'])
        for model_number, model_result
        in model_results.items()
    }

    model_event_sizes = {}
    model_event_protein_mask_sizes = {}
    model_event_contact_mask_sizes = {}
    for model_number, model_result in model_results.items():
        model_event_sizes[model_number] = {}
        model_event_protein_mask_sizes[model_number] = {}
        model_event_contact_mask_sizes[model_number] = {}
        for clustering_id, clustering in model_result['clusterings_large'].clusterings.items():
            for cluster_id, cluster in clustering.clustering.items():
                model_event_sizes[model_number][cluster_id] = cluster.values.size #cluster.size(grid)
                model_event_protein_mask_sizes[model_number][cluster_id] = np.sum(cluster.cluster_inner_protein_mask)
                model_event_contact_mask_sizes[model_number][cluster_id] = np.sum(cluster.cluster_contact_mask)


    signal_to_noise = {}
    for model_number, model_result in model_results.items():
        zmap = model_result['zmap']
        cluster_sizes = [int(event_size) for event_number, event_size in model_event_sizes[model_number].items()]
        cluster_mask_sizes = [int(event_mask_size) for event_number, event_mask_size in model_event_protein_mask_sizes[
            model_number].items()]
        cluster_differences = [cluster_size - cluster_mask_size for cluster_size, cluster_mask_size in zip(
            cluster_sizes, cluster_mask_sizes)]
        contact_mask_sizes = [int(contact_mask_size) for event_number, contact_mask_size in
                              model_event_contact_mask_sizes[
            model_number].items()]
        contact_differences = [contact_size - cluster_mask_size for contact_size, cluster_mask_size in zip(
            contact_mask_sizes, cluster_mask_sizes)]
        if len(cluster_differences) == 0:
            max_diff = 0
        else:
            max_diff = max(cluster_differences)
        if len(contact_differences) == 0:
            max_contact_diff = 0
        else:
            max_contact_diff = max(contact_differences)


        zmap_array = zmap.to_array()
        contoured_zmap_array = zmap_array[zmap_array > 2.0]
        zmap_size = int(zmap_array[zmap_array > 0.0].size)
        zmap_num_outliers = int(contoured_zmap_array.size)
        signal = sum(cluster_sizes) / zmap_num_outliers  # Fraction of outliers that are clustered
        noise = zmap_num_outliers / zmap_size  # Fraction of map that is outliers
        signal_to_noise[model_number] = signal - noise


        # Get cluster signal to noise
        cluster_stats = {}
        for clustering_id, clustering in model_result['clusterings_peaked'].clusterings.items():
            for cluster_id, cluster in clustering.clustering.items():
                outer_hull_array = zmap_array[cluster.event_mask_indicies]
                outer_hull_contoured_mask_array = outer_hull_array > 2.0
                cluster_size = int(cluster.values.size)
                outer_hull_num_outliers = int(np.sum(outer_hull_contoured_mask_array))
                protein_mask_size = int(np.sum(cluster.cluster_inner_protein_mask))
                contact_mask_size = int(np.sum(cluster.cluster_contact_mask))
                signal = contact_mask_size - protein_mask_size
                noise_with_protein = (outer_hull_num_outliers - cluster_size) + protein_mask_size
                noise_without_protein = outer_hull_num_outliers - cluster_size
                cluster_stats[int(cluster_id)] = {
                    'cluster_size': cluster_size,
                    'cluster_outer_hull_num_outlier': outer_hull_num_outliers,
                    'contact_mask_size': contact_mask_size,
                    'protein_mask_size': protein_mask_size,
                    'signal': signal,
                    'noise_with_protein': noise_with_protein,
                    'noise_without_protein': noise_without_protein,
                    'signal_to_noise_with_protein': float(signal/(noise_with_protein+1)),
                    'signal_to_noise_without_protein': float(signal / (noise_without_protein + 1)),
                    'map_signal': sum(cluster_sizes),
                    'map_noise': (zmap_num_outliers - sum(cluster_sizes)),
                    'zmap_signal_to_noise': sum(cluster_sizes) / ((zmap_num_outliers - sum(cluster_sizes))+1),
                    'zmap_num_outlier': zmap_num_outliers,
                    'zmap_size': zmap_size,
                }


        # Print info
        # model_selection_log[model_number] = f"\t\t{model_number}: signal: {signal}: noise: {noise}: " \
        #                                     f"{sum(cluster_sizes)}: {zmap_num_outliers}: {zmap_size}: " \
        #                                     f"{cluster_sizes}: {cluster_mask_sizes}: {cluster_differences}: " \
        #                                     f"{contact_mask_sizes}: {contact_differences}: " \
        #                                     f" {max_diff}: {max_contact_diff}"
        model_selection_log[model_number] = cluster_stats

    return max(
        signal_to_noise,
               key=lambda _model_number: signal_to_noise[_model_number]
    ), model_selection_log

    #     model_number: {
    #         cluster_number: cluster.size()
    #         for cluster_number, cluster
    #         in model_result['clusterings_merged'].items()
    #     }
    #     for model_number, model_result
    #     in model_results.items()
    # }
    # model_largest_events = {
    #     model_number: max(
    #         model_event_sizes[model_number],
    #         key=lambda _model_number: model_event_sizes[model_number][_model_number]
    #     )
    #     for model_number
    #     in model_event_sizes
    #     if number_of_events[model_number] != 0
    # }
    #
    # sensible_models = [model_number for model_number in number_of_events
    #                    if (number_of_events[model_number] > 0) & (number_of_events[model_number] < 7)]
    #
    # noisy_models = [model_number for model_number in number_of_events
    #                 if (number_of_events[model_number] >= 7)]
    #
    # no_event_models = [model_number for model_number in number_of_events
    #                    if number_of_events[model_number] == 0]
    #
    # if len(sensible_models) > 0:
    #     return max(
    #         model_largest_events,
    #         key=lambda _number: model_largest_events[_number],
    #     )
    # else:
    #     return no_event_models[0]

    # Want maps with the largest events (highest % of map in large cluster) but the lowest % of map outlying

        # selected_model = max(
        #     model_scores,
        #     key=lambda _number: model_scores[_number],
        # )
    # return selected_model


def get_models(
        test_dtags,
        comparison_sets: Dict[int, List[Dtag]],
        shell_xmaps,
        grid: Grid,
        process_local,
):
    masked_xmap_array = XmapArray.from_xmaps(
        shell_xmaps,
        grid,
    )

    models = {}
    for comparison_set_id, comparison_set_dtags in comparison_sets.items():
        # comparison_set_dtags =

        # Get the relevant dtags' xmaps
        masked_train_characterisation_xmap_array: XmapArray = masked_xmap_array.from_dtags(
            comparison_set_dtags)
        masked_train_all_xmap_array: XmapArray = masked_xmap_array.from_dtags(
            comparison_set_dtags + [test_dtag for test_dtag in test_dtags])

        mean_array: np.ndarray = Model.mean_from_xmap_array(masked_train_characterisation_xmap_array,
                                                            )  # Size of grid.partitioning.total_mask > 0
        # dataset_log[constants.LOG_DATASET_MEAN] = summarise_array(mean_array)
        # update_log(dataset_log, dataset_log_path)

        sigma_is: Dict[Dtag, float] = Model.sigma_is_from_xmap_array(masked_train_all_xmap_array,
                                                                     mean_array,
                                                                     1.5,
                                                                     )  # size of n
        # dataset_log[constants.LOG_DATASET_SIGMA_I] = {_dtag.dtag: float(sigma_i) for _dtag, sigma_i in sigma_is.items()}
        # update_log(dataset_log, dataset_log_path)

        sigma_s_m: np.ndarray = Model.sigma_sms_from_xmaps(masked_train_characterisation_xmap_array,
                                                           mean_array,
                                                           sigma_is,
                                                           process_local,
                                                           )  # size of total_mask > 0
        # dataset_log[constants.LOG_DATASET_SIGMA_S] = summarise_array(sigma_s_m)
        # update_log(dataset_log, dataset_log_path)

        model: Model = Model.from_mean_is_sms(
            mean_array,
            sigma_is,
            sigma_s_m,
            grid,
        )
        models[comparison_set_id] = model

    return models


def process_dataset_multiple_models(
        test_dtag,
        models,
        shell: ShellMultipleModels,
        dataset_truncated_datasets,
        alignments,
        dataset_xmaps,
        pandda_fs_model: PanDDAFSModel,
        reference,
        grid,
        contour_level,
        cluster_cutoff_distance_multiplier,
        min_blob_volume,
        min_blob_z_peak,
        structure_factors,
        outer_mask,
        inner_mask_symmetry,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        sample_rate,
        statmaps,
        process_local=process_local_serial,
        debug=False,
):
    if debug:
        print(f'\tProcessing dtag: {test_dtag}')
    time_dataset_start = time.time()

    dataset_log_path = pandda_fs_model.processed_datasets.processed_datasets[test_dtag].log_path
    dataset_log = {}

    model_results = {}
    for model_number, model in models.items():
        if debug:
            print(f'\tAnalysing model: {model_number}')

        dataset_log[constants.LOG_DATASET_TRAIN] = [_dtag.dtag for _dtag in shell.train_dtags[model_number]]
        update_log(dataset_log, dataset_log_path)

        # masked_xmap_array = XmapArray.from_xmaps(
        #     dataset_xmaps,
        #     grid,
        # )

        # masked_train_xmap_array: XmapArray = masked_xmap_array.from_dtags(
        #     [_dtag for _dtag in shell.train_dtags[test_dtag].union({test_dtag, })])

        ###################################################################
        # # Generate the statistical model of the dataset
        ###################################################################
        time_model_start = time.time()

        # Calculate z maps
        time_z_maps_start = time.time()
        zmaps: Dict[Dtag, Zmap] = Zmaps.from_xmaps(
            model=model,
            xmaps={test_dtag: dataset_xmaps[test_dtag], },
        )
        time_z_maps_finish = time.time()
        dataset_log[constants.LOG_DATASET_Z_MAPS_TIME] = time_z_maps_finish - time_z_maps_start
        update_log(dataset_log, dataset_log_path)

        ###################################################################
        # # Cluster the outlying density
        ###################################################################
        time_cluster_start = time.time()

        # Get the clustered electron desnity outliers

        cluster_paramaterised = partial(
            Clustering.from_zmap,
            reference=reference,
            grid=grid,
            contour_level=contour_level,
            cluster_cutoff_distance_multiplier=cluster_cutoff_distance_multiplier,
        )
        time_cluster_z_start = time.time()
        clusterings: List[Clustering] = process_local(
            [
                partial(cluster_paramaterised, zmaps[dtag], )
                for dtag
                in zmaps
            ]
        )
        time_cluster_z_finish = time.time()
        if debug:
            dataset_log['Time to perform primary clustering of z map'] = time_cluster_z_finish - time_cluster_z_start
            dataset_log['time_event_mask'] = {}
            for j, clustering in enumerate(clusterings):
                dataset_log['time_cluster'] = clustering.time_cluster
                dataset_log['time_np'] = clustering.time_np
                dataset_log['time_event_masking'] = clustering.time_event_masking
                dataset_log['time_get_orth'] = clustering.time_get_orth
                dataset_log['time_fcluster'] = clustering.time_fcluster
                for cluster_num, cluster in clustering.clustering.items():
                    dataset_log['time_event_mask'][int(cluster_num)] = cluster.time_event_mask

        clusterings: Clusterings = Clusterings({dtag: clustering for dtag, clustering in zip(zmaps, clusterings)})

        dataset_log[constants.LOG_DATASET_INITIAL_CLUSTERS_NUM] = sum(
            [len(clustering) for clustering in clusterings.clusterings.values()])
        update_log(dataset_log, dataset_log_path)
        cluster_sizes = {}
        for dtag, clustering in clusterings.clusterings.items():
            for cluster_num, cluster in clustering.clustering.items():
                cluster_sizes[int(cluster_num)] = {
                    "size": float(cluster.size(grid)),
                    "centroid": (float(cluster.centroid[0]), float(cluster.centroid[1]), float(cluster.centroid[2])),
                }
        dataset_log[constants.LOG_DATASET_CLUSTER_SIZES] = {
            cluster_num: cluster_sizes[cluster_num]
            for j, cluster_num
            in enumerate(sorted(
                cluster_sizes, key=lambda _cluster_num: cluster_sizes[_cluster_num]["size"],
                reverse=True,
            ))
            if j < 10
        }
        update_log(dataset_log, dataset_log_path)

        # Filter out small clusters
        clusterings_large: Clusterings = clusterings.filter_size(grid,
                                                                 min_blob_volume,
                                                                 )
        if debug:
            print("\t\tAfter filtering: large: {}".format(
                {dtag: len(cluster) for dtag, cluster in
                 zip(clusterings_large.clusterings, clusterings_large.clusterings.values())}))
        dataset_log[constants.LOG_DATASET_LARGE_CLUSTERS_NUM] = sum(
            [len(clustering) for clustering in clusterings_large.clusterings.values()])
        update_log(dataset_log, dataset_log_path)

        # Filter out weak clusters (low peak z score)
        clusterings_peaked: Clusterings = clusterings_large.filter_peak(grid,
                                                                        min_blob_z_peak)
        if debug:
            print("\t\tAfter filtering: peak: {}".format(
                {dtag: len(cluster) for dtag, cluster in
                 zip(clusterings_peaked.clusterings, clusterings_peaked.clusterings.values())}))
        dataset_log[constants.LOG_DATASET_PEAKED_CLUSTERS_NUM] = sum(
            [len(clustering) for clustering in clusterings_peaked.clusterings.values()])
        update_log(dataset_log, dataset_log_path)

        # Add the event mask
        for clustering_id, clustering in clusterings_peaked.clusterings.items():
            for cluster_id, cluster in clustering.clustering.items():
                cluster.event_mask_indicies = get_event_mask_indicies(zmaps[test_dtag], cluster.cluster_positions_array)

        # Merge the clusters
        clusterings_merged = clusterings_peaked.merge_clusters()
        if debug:
            print("\t\tAfter filtering: merged: {}".format(
                {dtag: len(_cluster) for dtag, _cluster in
                 zip(clusterings_merged.clusterings, clusterings_merged.clusterings.values())}))
        dataset_log[constants.LOG_DATASET_MERGED_CLUSTERS_NUM] = sum(
            [len(clustering) for clustering in clusterings_merged.clusterings.values()])
        update_log(dataset_log, dataset_log_path)

        # Log the clustering
        time_cluster_finish = time.time()
        dataset_log[constants.LOG_DATASET_CLUSTER_TIME] = time_cluster_finish - time_cluster_start
        update_log(dataset_log, dataset_log_path)

        model_results[model_number] = {
            'zmap': zmaps[test_dtag],
            'clusterings': clusterings,
            'clusterings_large': clusterings_large,
            'clusterings_peaked': clusterings_peaked,
            'clusterings_merged': clusterings_merged,

        }

    ###################################################################
    # # Decide which model to use...
    ###################################################################
    selected_model_number, model_selection_log = select_model(model_results, grid)
    selected_model = models[selected_model_number]
    selected_model_clusterings = model_results[selected_model_number]['clusterings_merged']
    zmap = model_results[selected_model_number]['zmap']
    dataset_log['Selected model'] = selected_model_number
    dataset_log['Model selection log'] = model_selection_log

    if debug:
        print(f'\tSelected model is: {selected_model_number}')

    ###################################################################
    # # Output the z map
    ###################################################################
    time_output_zmap_start = time.time()

    native_grid = dataset_truncated_datasets[test_dtag].reflections.reflections.transform_f_phi_to_map(
            structure_factors.f,
            structure_factors.phi,
            # sample_rate=sample_rate,  # TODO: make this d_min/0.5?
        sample_rate=dataset_truncated_datasets[test_dtag].reflections.resolution().resolution/0.5
        )

    partitioning = Partitioning.from_structure_multiprocess(
        dataset_truncated_datasets[test_dtag].structure,
        native_grid,
        outer_mask,
        inner_mask_symmetry,
    )
    # pandda_fs_model.processed_datasets.processed_datasets[dtag].z_map_file.save_reference_frame_zmap(zmap)

    save_native_frame_zmap(
        pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file.path,
        zmap,
        dataset_truncated_datasets[test_dtag],
        alignments[test_dtag],
        grid,
        structure_factors,
        outer_mask,
        inner_mask_symmetry,
        partitioning,
        sample_rate,
    )

    if debug:
        for model_number, model_result in model_results.items():
            save_native_frame_zmap(
                pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file.path.parent / f'{model_number}.ccp4',
                model_result['zmap'],
                dataset_truncated_datasets[test_dtag],
                alignments[test_dtag],
                grid,
                structure_factors,
                outer_mask,
                inner_mask_symmetry,
                partitioning,
                sample_rate,
            )

    if statmaps:
        mean_map_file = MeanMapFile.from_zmap_file(
            pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file)
        mean_map_file.save_native_frame_mean_map(
            selected_model,
            zmap,
            dataset_truncated_datasets[test_dtag],
            alignments[test_dtag],
            grid,
            structure_factors,
            outer_mask,
            inner_mask_symmetry,
            partitioning,
            sample_rate,
        )

        std_map_file = StdMapFile.from_zmap_file(pandda_fs_model.processed_datasets.processed_datasets[
                                                     test_dtag].z_map_file)
        std_map_file.save_native_frame_std_map(
            test_dtag,
            selected_model,
            zmap,
            dataset_truncated_datasets[test_dtag],
            alignments[test_dtag],
            grid,
            structure_factors,
            outer_mask,
            inner_mask_symmetry,
            partitioning,
            sample_rate,
        )
    time_output_zmap_finish = time.time()
    dataset_log['Time to output z map'] = time_output_zmap_finish - time_output_zmap_start

    ###################################################################
    # # Find the events
    ###################################################################
    time_event_start = time.time()
    # Calculate the shell events
    events: Events = Events.from_clusters(
        selected_model_clusterings,
        selected_model,
        dataset_xmaps,
        grid,
        alignments[test_dtag],
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        None,
    )

    time_event_finish = time.time()
    dataset_log[constants.LOG_DATASET_EVENT_TIME] = time_event_finish - time_event_start
    update_log(dataset_log, dataset_log_path)

    ###################################################################
    # # Generate event maps
    ###################################################################
    time_event_map_start = time.time()

    # Save the event maps!
    # printer.pprint(events)
    events.save_event_maps(
        dataset_truncated_datasets,
        alignments,
        dataset_xmaps,
        selected_model,
        pandda_fs_model,
        grid,
        structure_factors,
        outer_mask,
        inner_mask_symmetry,
        sample_rate,
        native_grid,
        mapper=process_local_serial,
    )

    time_event_map_finish = time.time()
    dataset_log[constants.LOG_DATASET_EVENT_MAP_TIME] = time_event_map_finish - time_event_map_start
    update_log(dataset_log, dataset_log_path)

    time_dataset_finish = time.time()
    dataset_log[constants.LOG_DATASET_TIME] = time_dataset_finish - time_dataset_start
    update_log(dataset_log, dataset_log_path)

    return DatasetResult(
        dtag=test_dtag.dtag,
        events=events,
        log=dataset_log,
    )


def process_shell_multiple_models(
        shell: ShellMultipleModels,
        datasets: Dict[Dtag, Dataset],
        alignments,
        grid,
        pandda_fs_model: PanDDAFSModel,
        reference,
        process_local,
        structure_factors: StructureFactors,
        sample_rate: float,
        contour_level,
        cluster_cutoff_distance_multiplier,
        min_blob_volume,
        min_blob_z_peak,
        outer_mask,
        inner_mask_symmetry,
        max_site_distance_cutoff,
        min_bdc,
        max_bdc,
        memory_availability,
        statmaps,
        debug=False,
):
    time_shell_start = time.time()
    shell_log_path = pandda_fs_model.shell_dirs.shell_dirs[shell.res].log_path
    shell_log = {}

    # Seperate out test and train datasets
    shell_datasets: Dict[Dtag, Dataset] = {
        dtag: dataset
        for dtag, dataset
        in datasets.items()
        if dtag in shell.all_dtags
    }
    shell_log[constants.LOG_SHELL_DATASETS] = [dtag.dtag for dtag in shell_datasets]
    update_log(shell_log, shell_log_path)

    ###################################################################
    # # Homogonise shell datasets by truncation of resolution
    ###################################################################
    shell_working_resolution = Resolution(
        min([datasets[dtag].reflections.resolution().resolution for dtag in shell.all_dtags]))
    shell_truncated_datasets: Datasets = truncate(
        shell_datasets,
        resolution=shell_working_resolution,
        structure_factors=structure_factors,
    )
    shell_log["Shell Working Resolution"] = shell_working_resolution.resolution

    ###################################################################
    # # Generate aligned Xmaps
    ###################################################################
    time_xmaps_start = time.time()

    load_xmap_paramaterised = partial(
        Xmap.from_unaligned_dataset_c,
        grid=grid,
        structure_factors=structure_factors,
        # sample_rate=sample_rate,
        sample_rate=shell.res/0.5
    )

    results = process_local(
        partial(
            load_xmap_paramaterised,
            shell_truncated_datasets[key],
            alignments[key],
        )
        for key
        in shell_truncated_datasets
    )

    xmaps = {
        dtag: xmap
        for dtag, xmap
        in zip(shell_truncated_datasets, results)
    }

    time_xmaps_finish = time.time()
    shell_log[constants.LOG_SHELL_XMAP_TIME] = time_xmaps_finish - time_xmaps_start
    update_log(shell_log, shell_log_path)

    ###################################################################
    # # Get the models to test
    ###################################################################
    models = get_models(
        shell.test_dtags,
        shell.train_dtags,
        xmaps,
        grid,
        process_local,
    )

    ###################################################################
    # # Process each test dataset
    ###################################################################
    # Now that all the data is loaded, get the comparison set and process each test dtag
    if memory_availability == "very_low":
        process_local_in_dataset = process_local_serial
        process_local_over_datasets = process_local_serial
    elif memory_availability == "low":
        process_local_in_dataset = process_local
        process_local_over_datasets = process_local_serial
    elif memory_availability == "high":
        process_local_in_dataset = process_local_serial
        process_local_over_datasets = process_local

    process_dataset_paramaterized = partial(
        process_dataset_multiple_models,
        models=models,
        shell=shell,
        alignments=alignments,
        pandda_fs_model=pandda_fs_model,
        reference=reference,
        grid=grid,
        contour_level=contour_level,
        cluster_cutoff_distance_multiplier=cluster_cutoff_distance_multiplier,
        min_blob_volume=min_blob_volume,
        min_blob_z_peak=min_blob_z_peak,
        structure_factors=structure_factors,
        outer_mask=outer_mask,
        inner_mask_symmetry=inner_mask_symmetry,
        max_site_distance_cutoff=max_site_distance_cutoff,
        min_bdc=min_bdc,
        max_bdc=max_bdc,
        # sample_rate=sample_rate,
        sample_rate=shell.res/0.5,
        statmaps=statmaps,
        process_local=process_local_in_dataset,
        debug=debug,
    )

    # Process each dataset in the shell
    all_train_dtags = [_dtag for l in shell.train_dtags.values() for _dtag in l]
    if debug:
        print(f"\tAll train datasets are: {all_train_dtags}")
    # dataset_dtags = {_dtag:  for _dtag in shell.test_dtags for n in shell.train_dtags}
    dataset_dtags = {_dtag: [_dtag] + all_train_dtags for _dtag in shell.test_dtags}
    if debug:
        print(f"\tDataset dtags are: {dataset_dtags}")
    results = process_local_over_datasets(
        [
            partial(
                process_dataset_paramaterized,
                test_dtag,
                dataset_truncated_datasets={_dtag: shell_truncated_datasets[_dtag] for _dtag in
                                            dataset_dtags[test_dtag]},
                dataset_xmaps={_dtag: xmaps[_dtag] for _dtag in dataset_dtags[test_dtag]},
            )
            for test_dtag
            in shell.test_dtags
        ],
    )

    # Update shell log with dataset results
    shell_log[constants.LOG_SHELL_DATASET_LOGS] = {}
    for result in results:
        if result:
            shell_log[constants.LOG_SHELL_DATASET_LOGS][result.dtag] = result.log

    time_shell_finish = time.time()
    shell_log[constants.LOG_SHELL_TIME] = time_shell_finish - time_shell_start
    update_log(shell_log, shell_log_path)

    return ShellResult(
        shell=shell,
        dataset_results={dtag: result for dtag, result in zip(shell.test_dtags, results) if result},
        log=shell_log,
    )
