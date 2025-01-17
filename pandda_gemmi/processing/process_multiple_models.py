from __future__ import annotations

# Base python
import dataclasses
import time
import pprint
from functools import partial
import os
import json
from typing import Set
import pickle

printer = pprint.PrettyPrinter()

# Scientific python libraries
import ray

## Custom Imports
from pandda_gemmi.logs import (
    summarise_array,
)

from pandda_gemmi import constants
from pandda_gemmi.pandda_functions import (
    process_local_serial,
    truncate,
    save_native_frame_zmap,
    save_reference_frame_zmap,
)
from pandda_gemmi.python_types import *
from pandda_gemmi.common import Dtag, EventID, Partial
from pandda_gemmi.fs import PanDDAFSModel, MeanMapFile, StdMapFile
from pandda_gemmi.dataset import (StructureFactors, Dataset, Datasets,
                                  Resolution, )
from pandda_gemmi.shells import Shell, ShellMultipleModels
from pandda_gemmi.edalignment import Partitioning, Xmap, XmapArray, Grid, from_unaligned_dataset_c
from pandda_gemmi.model import Zmap, Model, Zmaps
from pandda_gemmi.event import Event, Clusterings, Clustering, Events, get_event_mask_indicies, score_clusters


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


# TODO: Remove
def blobfind_event_map_and_report_and_output(
        test_dtag,
        model_number,
        dataset,
        xmaps,
        zmap,
        selected_model_clusterings,
        model,
        dataset_xmaps,
        grid,
        alignments,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        reference,
        contour_level,
        cluster_cutoff_distance_multiplier,
        pandda_fs_model
):
    # Get the events and their BDCs
    events: Events = Events.from_clusters(
        selected_model_clusterings,
        model,
        dataset_xmaps,
        grid,
        alignments[test_dtag],
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        None,
    )

    # Calculate the event maps
    reference_xmap_grid = xmaps[test_dtag].xmap
    reference_xmap_grid_array = np.array(reference_xmap_grid, copy=True)

    for event_id, event in events.events.items():

        event_map_reference_grid = gemmi.FloatGrid(*[reference_xmap_grid.nu,
                                                     reference_xmap_grid.nv,
                                                     reference_xmap_grid.nw,
                                                     ]
                                                   )
        event_map_reference_grid.spacegroup = gemmi.find_spacegroup_by_name("P 1")  # xmap.xmap.spacegroup
        event_map_reference_grid.set_unit_cell(reference_xmap_grid.unit_cell)

        event_map_reference_grid_array = np.array(event_map_reference_grid,
                                                  copy=False,
                                                  )

        mean_array = model.mean
        event_map_reference_grid_array[:, :, :] = (reference_xmap_grid_array - (event.bdc.bdc * mean_array)) / (
                1 - event.bdc.bdc)

        # Mask

        # # Blobfind
        # clustering = Clustering.from_event_map(
        #     event_map_reference_grid_array,
        #     zmap,
        #     reference,
        #     grid,
        #     contour_level,
        #     cluster_cutoff_distance_multiplier,
        # )

        scores = score_clusters(
            {(0, 0): event.cluster},
            Zmap(event_map_reference_grid),
            dataset,

        )

        # Ouptut
        # for cluster_id, cluster in clustering.clustering.items():
        #     string = f"\t\tModel {model_number} Event {event_id.event_idx.event_idx} Cluster {cluster_id} size: {cluster.values.size} reference frame coords {cluster.centroid}"
        #     print(string)
        for score_id, score in scores:
            string = f"\t\tModel {model_number} Event {event_id.event_idx.event_idx} Score {score}"

        # Save event map
        filename = f'event_{model_number}_{event_id.event_idx.event_idx}_ref.ccp4'
        save_reference_frame_zmap(
            pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file.path.parent / filename,
            Zmap(event_map_reference_grid)
        )

        # Save z map
        filename = f'z_{model_number}_{event_id.event_idx.event_idx}_ref.ccp4'
        save_reference_frame_zmap(
            pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file.path.parent / filename,
            zmap
        )

        # Save reference model
        filename = f'ref.pdb'
        reference_structure = reference.dataset.structure.structure
        reference_structure.write_minimal_pdb(
            str(pandda_fs_model.processed_datasets.processed_datasets[test_dtag].z_map_file.path.parent / filename)
        )


def event_score_and_report(
        test_dtag,
        model_number,
        processed_dataset,
        dataset_xmap,
        selected_model_clusterings,
        model,
        grid,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        reference,
        debug=True,
):
    # Get the events and their BDCs
    if debug:
        print("\t\tGetting events...")

    time_event_finding_start = time.time()

    events: Events = Events.from_clusters(
        selected_model_clusterings,
        model,
        {test_dtag: dataset_xmap, },
        grid,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        None,
    )

    time_event_finding_finish = time.time()
    if debug:
        print(f"\t\tTime to find events for model: {time_event_finding_finish - time_event_finding_start}")

    # Calculate the event maps
    reference_xmap_grid = dataset_xmap.xmap
    reference_xmap_grid_array = np.array(reference_xmap_grid, copy=True)

    # Mask protein
    if debug:
        print("\t\tMasking protein...")
    inner_mask = gemmi.Int8Grid(*[grid.grid.nu, grid.grid.nv, grid.grid.nw])
    inner_mask.spacegroup = gemmi.find_spacegroup_by_name("P 1")
    inner_mask.set_unit_cell(grid.grid.unit_cell)
    for atom in reference.dataset.structure.protein_atoms():
        pos = atom.pos
        inner_mask.set_points_around(pos,
                                     radius=2.0,
                                     value=1,
                                     )

    if debug:
        print("\t\tIterating events...")

    event_scores = {}

    time_event_scoring_start = time.time()

    for event_id, event in events.events.items():

        if debug:
            print("\t\t\tCaclulating event maps...")
        event_map_reference_grid = gemmi.FloatGrid(*[reference_xmap_grid.nu,
                                                     reference_xmap_grid.nv,
                                                     reference_xmap_grid.nw,
                                                     ]
                                                   )
        event_map_reference_grid.spacegroup = gemmi.find_spacegroup_by_name("P 1")  # xmap.xmap.spacegroup
        event_map_reference_grid.set_unit_cell(reference_xmap_grid.unit_cell)

        event_map_reference_grid_array = np.array(event_map_reference_grid,
                                                  copy=False,
                                                  )

        mean_array = model.mean
        event_map_reference_grid_array[:, :, :] = (reference_xmap_grid_array - (event.bdc.bdc * mean_array)) / (
                1 - event.bdc.bdc)

        event_map_reference_grid_array[event_map_reference_grid_array < 2.0] = 0.0
        event_map_reference_grid_array[event_map_reference_grid_array >= 2.0] = 1.0

        # Mask the protein except around the event
        # inner_mask = grid.partitioning.inner_mask
        inner_mask_array = np.array(
            inner_mask,
            copy=False,
            dtype=np.int8,
        )

        inner_mask_array[event.cluster.event_mask_indicies] = 0.0
        # event_map_reference_grid_array[np.nonzero(inner_mask_array)] = 0.0
        event_map_reference_grid_array[np.nonzero(inner_mask_array)] = -1.0

        if debug:
            print("\t\t\tScoring...")

        # Score
        time_scoring_start = time.time()
        scores = score_clusters(
            {(0, 0): event.cluster},
            {(0, 0): event_map_reference_grid},
            processed_dataset,
            debug=debug,
        )
        time_scoring_finish = time.time()
        if debug:
            print(f"\t\t\tTime to actually score all events: {time_scoring_finish - time_scoring_start}")

        # Ouptut
        for score_id, score in scores.items():
            string = f"\t\tModel {model_number} Event {event_id.event_idx.event_idx} Score {score} Event Size " \
                     f"{event.cluster.values.size}"
            print(string)

            event_scores[event_id.event_idx.event_idx] = score

    time_event_scoring_finish = time.time()

    if debug:
        print(f"\t\tTime to score all events: {time_event_scoring_finish - time_event_scoring_start}. Num events: "
              f"{len(events.events)}")

    return event_scores


def update_log(shell_log, shell_log_path):
    if shell_log_path.exists():
        os.remove(shell_log_path)

    with open(shell_log_path, "w") as f:
        json.dump(shell_log, f, indent=4)


# def get_event_clusters(clusterings_list: Dict[int, Clusterings]):
#
#     cluster_ids = []
#     event_coords = []
#     for model_id, clusterings in clusterings_list.items():
#         for clustering_id, clustering in clusterings.clusterings.items():
#             for cluster_id, cluster in clustering.clustering.items():
#                 cluster_ids.append((model_id, cluster_id))
#                 event_coords.append(cluster.centroid)
#
#     centroids_array = np.array(event_coords)
#
#
#
#
#     ...

def select_model(model_results: Dict[int, Dict], inner_mask, processed_dataset, debug=False):
    log = {}

    biggest_clusters = {}
    for model_number, model_result in model_results.items():
        cluster_sizes = {}
        for clustering_id, clustering in model_result['clusterings_large'].clusterings.items():
            for cluster_id, cluster in clustering.clustering.items():
                cluster_sizes[(int(model_number), int(cluster_id))] = int(cluster.values.size)

        if len(cluster_sizes) == 0:
            continue
        else:
            biggest_clusters[model_number] = max(cluster_sizes, key=lambda _x: cluster_sizes[_x])

    zmaps = {}
    clusters = {}
    for model_number, model_result in model_results.items():
        for clustering_id, clustering in model_result['clusterings_large'].clusterings.items():
            for cluster_id, cluster in clustering.clustering.items():
                new_cluster_id = (int(model_number), int(cluster_id),)

                if new_cluster_id == biggest_clusters[model_number]:
                    clusters[new_cluster_id] = cluster

                    zmap: Zmap = model_result['zmap']
                    zmap_array = np.array(zmap.zmap, copy=False)

                    zmap_grid: gemmi.FloatGrid = zmap.grid_from_grid_template(zmap.zmap, zmap_array)

                    zmap_grid_array = np.array(zmap_grid, copy=False)

                    inner_mask_array = np.array(inner_mask, copy=True, dtype=np.int8)

                    zmap_grid_array[np.nonzero(inner_mask_array)] = 0.0

                    zmaps[new_cluster_id] = zmap_grid

    # Score the top clusters
    scores = score_clusters(clusters, zmaps, processed_dataset, debug=debug)

    log = {score_key[0]: score for score_key, score in scores.items()}

    if len(scores) == 0:
        return 0, log

    else:
        selected_model_number = max(
            scores,
            key=lambda _score: scores[_score],
        )[0]

    return selected_model_number, log


def EXPERIMENTAL_select_model(
        model_results: Dict[int, Dict],
        inner_mask,
        processed_dataset,
        debug=False,
):
    log = {}

    model_event_scores = {model_id: model['event_scores'] for model_id, model in model_results.items()}

    # Score the top clusters
    model_scores = {
        model_id: max([event_scores[score_id] for score_id in event_scores] + [0.0, ])
        for model_id, event_scores
        in model_event_scores.items()
    }

    if debug:
        print(model_scores)

    log['model_scores'] = {model_id: float(score) for model_id, score in model_scores.items()}

    if len(model_scores) == 0:
        return 0, log

    else:
        selected_model_number = max(
            model_scores,
            key=lambda _score: model_scores[_score],
        )  # [0]

    return selected_model_number, log


#
# def select_model(model_results: Dict[int, Dict], grid):
#     model_scores = {}
#     model_selection_log = {}
#     # for model_number, model_result in model_results.items():
#     #     num_merged_clusters = len(model_result['clusterings_merged'])
#     #     if num_merged_clusters == 0:
#     #         model_score = 0
#     #     elif num_merged_clusters < 6:
#     #         model_score = num_merged_clusters
#     #     elif num_merged_clusters < 12:
#     #         model_score = 0.5
#     #     else:
#     #         model_score = -1
#     #
#     #     #TODO Get number of z map points in large clusters vs outside them as signal to noise estimate
#     #
#     #     model_scores[model_number] = model_score
#     #
#     #
#
#     number_of_events = {
#         model_number: len(model_result['clusterings_large'])
#         for model_number, model_result
#         in model_results.items()
#     }
#
#     model_event_sizes = {}
#     model_event_protein_mask_sizes = {}
#     model_event_contact_mask_sizes = {}
#     for model_number, model_result in model_results.items():
#         model_event_sizes[model_number] = {}
#         model_event_protein_mask_sizes[model_number] = {}
#         model_event_contact_mask_sizes[model_number] = {}
#         for clustering_id, clustering in model_result['clusterings_large'].clusterings.items():
#             for cluster_id, cluster in clustering.clustering.items():
#                 model_event_sizes[model_number][cluster_id] = cluster.values.size #cluster.size(grid)
#                 model_event_protein_mask_sizes[model_number][cluster_id] = np.sum(cluster.cluster_inner_protein_mask)
#                 model_event_contact_mask_sizes[model_number][cluster_id] = np.sum(cluster.cluster_contact_mask)
#
#     event_clusters = get_event_clusters([model_result['clusterings_large'] for model_result in model_results.values()])
#
#
#     signal_to_noise = {}
#     for model_number, model_result in model_results.items():
#         zmap = model_result['zmap']
#         cluster_sizes = [int(event_size) for event_number, event_size in model_event_sizes[model_number].items()]
#         cluster_mask_sizes = [int(event_mask_size) for event_number, event_mask_size in model_event_protein_mask_sizes[
#             model_number].items()]
#         cluster_differences = [cluster_size - cluster_mask_size for cluster_size, cluster_mask_size in zip(
#             cluster_sizes, cluster_mask_sizes)]
#         contact_mask_sizes = [int(contact_mask_size) for event_number, contact_mask_size in
#                               model_event_contact_mask_sizes[
#             model_number].items()]
#         contact_differences = [contact_size - cluster_mask_size for contact_size, cluster_mask_size in zip(
#             contact_mask_sizes, cluster_mask_sizes)]
#         if len(cluster_differences) == 0:
#             max_diff = 0
#         else:
#             max_diff = max(cluster_differences)
#         if len(contact_differences) == 0:
#             max_contact_diff = 0
#         else:
#             max_contact_diff = max(contact_differences)
#
#
#         zmap_array = zmap.to_array()
#         contoured_zmap_array = zmap_array[zmap_array > 2.0]
#         zmap_size = int(zmap_array[zmap_array > 0.0].size)
#         zmap_num_outliers = int(contoured_zmap_array.size)
#         signal = sum(cluster_sizes) / zmap_num_outliers  # Fraction of outliers that are clustered
#         noise = zmap_num_outliers / zmap_size  # Fraction of map that is outliers
#         signal_to_noise[model_number] = signal - noise
#
#
#         # Get cluster signal to noise
#         cluster_stats = {}
#         for clustering_id, clustering in model_result['clusterings_peaked'].clusterings.items():
#             for cluster_id, cluster in clustering.clustering.items():
#                 outer_hull_array = zmap_array[cluster.event_mask_indicies]
#                 outer_hull_contoured_mask_array = outer_hull_array > 2.0
#                 cluster_size = int(cluster.values.size)
#                 outer_hull_num_outliers = int(np.sum(outer_hull_contoured_mask_array))
#                 protein_mask_size = int(np.sum(cluster.cluster_inner_protein_mask))
#                 contact_mask_size = int(np.sum(cluster.cluster_contact_mask))
#                 signal = contact_mask_size - protein_mask_size
#                 noise_with_protein = (outer_hull_num_outliers - cluster_size) + protein_mask_size
#                 noise_without_protein = outer_hull_num_outliers - cluster_size
#                 cluster_stats[int(cluster_id)] = {
#                     'model_id': int(model_number),
#                     'cluster_id': int(cluster_id),
#                     'cluster_size': cluster_size,
#                     'cluster_outer_hull_num_outlier': outer_hull_num_outliers,
#                     'contact_mask_size': contact_mask_size,
#                     'protein_mask_size': protein_mask_size,
#                     'signal': signal,
#                     'noise_with_protein': noise_with_protein,
#                     'noise_without_protein': noise_without_protein,
#                     'signal_to_noise_with_protein': float(signal/(noise_with_protein+1)),
#                     'signal_to_noise_without_protein': float(signal / (noise_without_protein + 1)),
#                     'signal_to_noise_with_protein_scaled': float(signal/(noise_with_protein+1))*(
#                         contact_mask_size-protein_mask_size),
#                                                  'signal_to_noise_without_protein_scaled': float(signal/(
#                                                          noise_without_protein+1))*(
#                         contact_mask_size-protein_mask_size),
#                     'signal_to_noise_with_protein_additive': cluster_size-noise_with_protein,
#                     'signal_to_noise_without_protein_additive': cluster_size - noise_without_protein,
#                     'map_signal': sum(cluster_sizes),
#                     'map_noise': (zmap_num_outliers - sum(cluster_sizes)),
#                     'zmap_signal_to_noise': sum(cluster_sizes) / ((zmap_num_outliers - sum(cluster_sizes))+1),
#                     'zmap_num_outlier': zmap_num_outliers,
#                     'zmap_size': zmap_size,
#                     'score': ((cluster_size-protein_mask_size) + signal)-noise_with_protein
#                 }
#
#
#
#
#         # Print info
#         # model_selection_log[model_number] = f"\t\t{model_number}: signal: {signal}: noise: {noise}: " \
#         #                                     f"{sum(cluster_sizes)}: {zmap_num_outliers}: {zmap_size}: " \
#         #                                     f"{cluster_sizes}: {cluster_mask_sizes}: {cluster_differences}: " \
#         #                                     f"{contact_mask_sizes}: {contact_differences}: " \
#         #                                     f" {max_diff}: {max_contact_diff}"
#         model_selection_log[model_number] = cluster_stats
#
#     flat_stats = {
#         (model_id, cluster_id): model_selection_log[model_id][cluster_id]
#         for model_id
#         in model_selection_log
#         for cluster_id
#         in model_selection_log[model_id]
#     }
#     ranked_stats = [
#         flat_stats[x]
#         for x
#         in sorted(
#             flat_stats,
#             key=lambda _x: flat_stats[_x]['score']
#         )
#     ]
#
#     model_selection_log['ranked'] = ranked_stats
#
#     return max(
#         signal_to_noise,
#                key=lambda _model_number: signal_to_noise[_model_number]
#     ), model_selection_log
#
#     #     model_number: {
#     #         cluster_number: cluster.size()
#     #         for cluster_number, cluster
#     #         in model_result['clusterings_merged'].items()
#     #     }
#     #     for model_number, model_result
#     #     in model_results.items()
#     # }
#     # model_largest_events = {
#     #     model_number: max(
#     #         model_event_sizes[model_number],
#     #         key=lambda _model_number: model_event_sizes[model_number][_model_number]
#     #     )
#     #     for model_number
#     #     in model_event_sizes
#     #     if number_of_events[model_number] != 0
#     # }
#     #
#     # sensible_models = [model_number for model_number in number_of_events
#     #                    if (number_of_events[model_number] > 0) & (number_of_events[model_number] < 7)]
#     #
#     # noisy_models = [model_number for model_number in number_of_events
#     #                 if (number_of_events[model_number] >= 7)]
#     #
#     # no_event_models = [model_number for model_number in number_of_events
#     #                    if number_of_events[model_number] == 0]
#     #
#     # if len(sensible_models) > 0:
#     #     return max(
#     #         model_largest_events,
#     #         key=lambda _number: model_largest_events[_number],
#     #     )
#     # else:
#     #     return no_event_models[0]
#
#     # Want maps with the largest events (highest % of map in large cluster) but the lowest % of map outlying
#
#         # selected_model = max(
#         #     model_scores,
#         #     key=lambda _number: model_scores[_number],
#         # )
#     # return selected_model


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

def analyse_model(
        model,
        model_number,
        test_dtag,
        dataset_xmap,
        reference,
        grid,
        dataset_processed_dataset,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        contour_level,
        cluster_cutoff_distance_multiplier,
        min_blob_volume,
        min_blob_z_peak,
        debug=False
):
    if debug:
        print(f'\tAnalysing model: {model_number}')

    model_log = {}

    # model_log[constants.LOG_DATASET_TRAIN] = [_dtag.dtag for _dtag in shell.train_dtags[model_number]]
    # update_log(dataset_log, dataset_log_path)

    # masked_xmap_array = XmapArray.from_xmaps(
    #     dataset_xmaps,
    #     grid,
    # )

    # masked_train_xmap_array: XmapArray = masked_xmap_array.from_dtags(
    #     [_dtag for _dtag in shell.train_dtags[test_dtag].union({test_dtag, })])

    ###################################################################
    # # Generate the statistical model of the dataset
    ###################################################################
    time_model_analysis_start = time.time()

    # Calculate z maps
    if debug:
        print("\t\tCalculating zmaps")
    time_z_maps_start = time.time()
    zmaps: Dict[Dtag, Zmap] = Zmaps.from_xmaps(
        model=model,
        xmaps={test_dtag: dataset_xmap, },
        model_number=model_number,
        debug=debug,
    )

    if debug:
        print("\t\tCalculated zmaps")

    time_z_maps_finish = time.time()
    model_log[constants.LOG_DATASET_Z_MAPS_TIME] = time_z_maps_finish - time_z_maps_start
    # update_log(dataset_log, dataset_log_path)

    ###################################################################
    # # Cluster the outlying density
    ###################################################################
    time_cluster_start = time.time()

    # Get the clustered electron density outliers

    cluster_paramaterised = partial(
        Clustering.from_zmap,
        reference=reference,
        grid=grid,
        contour_level=contour_level,
        cluster_cutoff_distance_multiplier=cluster_cutoff_distance_multiplier,
    )
    time_cluster_z_start = time.time()

    if debug:
        print("\t\tClustering")

    clusterings: List[Clustering] = process_local_serial(
        [
            partial(cluster_paramaterised, zmaps[dtag], )
            for dtag
            in zmaps
        ]
    )
    time_cluster_z_finish = time.time()

    if debug:
        print("\t\tClustering finished")

    if debug:
        model_log['Time to perform primary clustering of z map'] = time_cluster_z_finish - time_cluster_z_start
        model_log['time_event_mask'] = {}
        for j, clustering in enumerate(clusterings):
            model_log['time_cluster'] = clustering.time_cluster
            model_log['time_np'] = clustering.time_np
            model_log['time_event_masking'] = clustering.time_event_masking
            model_log['time_get_orth'] = clustering.time_get_orth
            model_log['time_fcluster'] = clustering.time_fcluster
            for cluster_num, cluster in clustering.clustering.items():
                model_log['time_event_mask'][int(cluster_num)] = cluster.time_event_mask

    clusterings: Clusterings = Clusterings({dtag: clustering for dtag, clustering in zip(zmaps, clusterings)})

    model_log[constants.LOG_DATASET_INITIAL_CLUSTERS_NUM] = sum(
        [len(clustering) for clustering in clusterings.clusterings.values()])
    # update_log(dataset_log, dataset_log_path)
    cluster_sizes = {}
    for dtag, clustering in clusterings.clusterings.items():
        for cluster_num, cluster in clustering.clustering.items():
            cluster_sizes[int(cluster_num)] = {
                "size": float(cluster.size(grid)),
                "centroid": (float(cluster.centroid[0]), float(cluster.centroid[1]), float(cluster.centroid[2])),
            }
    model_log[constants.LOG_DATASET_CLUSTER_SIZES] = {
        cluster_num: cluster_sizes[cluster_num]
        for j, cluster_num
        in enumerate(sorted(
            cluster_sizes, key=lambda _cluster_num: cluster_sizes[_cluster_num]["size"],
            reverse=True,
        ))
        if j < 10
    }
    # update_log(dataset_log, dataset_log_path)

    # Filter out small clusters
    clusterings_large: Clusterings = clusterings.filter_size(grid,
                                                             min_blob_volume,
                                                             )
    if debug:
        print("\t\tAfter filtering: large: {}".format(
            {dtag: len(cluster) for dtag, cluster in
             zip(clusterings_large.clusterings, clusterings_large.clusterings.values())}))
    model_log[constants.LOG_DATASET_LARGE_CLUSTERS_NUM] = sum(
        [len(clustering) for clustering in clusterings_large.clusterings.values()])
    # update_log(dataset_log, dataset_log_path)

    # Filter out weak clusters (low peak z score)
    clusterings_peaked: Clusterings = clusterings_large.filter_peak(grid,
                                                                    min_blob_z_peak)
    if debug:
        print("\t\tAfter filtering: peak: {}".format(
            {dtag: len(cluster) for dtag, cluster in
             zip(clusterings_peaked.clusterings, clusterings_peaked.clusterings.values())}))
    model_log[constants.LOG_DATASET_PEAKED_CLUSTERS_NUM] = sum(
        [len(clustering) for clustering in clusterings_peaked.clusterings.values()])
    # update_log(dataset_log, dataset_log_path)

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
    model_log[constants.LOG_DATASET_MERGED_CLUSTERS_NUM] = sum(
        [len(clustering) for clustering in clusterings_merged.clusterings.values()])
    # update_log(dataset_log, dataset_log_path)

    # Log the clustering
    time_cluster_finish = time.time()
    model_log[constants.LOG_DATASET_CLUSTER_TIME] = time_cluster_finish - time_cluster_start
    # update_log(dataset_log, dataset_log_path)

    # TODO: REMOVE: event blob analysis
    # blobfind_event_map_and_report_and_output(
    #     test_dtag,
    #     model_number,
    #     dataset_truncated_datasets[test_dtag],
    #     dataset_xmaps,
    #     zmaps[test_dtag],
    #     clusterings_large,
    #     model,
    #     dataset_xmaps,
    #     grid,
    #     alignments,
    #     max_site_distance_cutoff,
    #     min_bdc, max_bdc,
    #     reference,
    #     contour_level,
    #     cluster_cutoff_distance_multiplier,
    #     pandda_fs_model
    # )

    if debug:
        print("\t\tScoring events...")
    event_scores: Dict[int, float] = event_score_and_report(
        test_dtag,
        model_number,
        dataset_processed_dataset,
        dataset_xmap,
        clusterings_large,
        model,
        grid,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        reference,
        debug=debug
    )

    time_model_analysis_finish = time.time()

    model_results = {
        'zmap': zmaps[test_dtag],
        'clusterings': clusterings,
        'clusterings_large': clusterings_large,
        'clusterings_peaked': clusterings_peaked,
        'clusterings_merged': clusterings_merged,
        'event_scores': event_scores,
    }
    model_log["Model analysis time"] = time_model_analysis_finish - time_model_analysis_start
    if debug:
        print(f"\t\tModel analysis time: {time_model_analysis_finish - time_model_analysis_start}")

    return model_results, model_log


@ray.remote
def analyse_model_ray(
        model,
        model_number,
        test_dtag,
        dataset_xmap,
        reference,
        grid,
        dataset_processed_dataset,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        contour_level,
        cluster_cutoff_distance_multiplier,
        min_blob_volume,
        min_blob_z_peak,
        debug=False
):
    return analyse_model(
        model,
        model_number,
        test_dtag,
        dataset_xmap,
        reference,
        grid,
        dataset_processed_dataset,
        dataset_alignment,
        max_site_distance_cutoff,
        min_bdc, max_bdc,
        contour_level,
        cluster_cutoff_distance_multiplier,
        min_blob_volume,
        min_blob_z_peak,
        debug
    )

def dump_and_load(ob, name):
    print(f"Testing: {name}")

    # time_dump_start = time.time()
    # dumps = pickle.dumps(ob)
    # time_dump_finish = time.time()
    # print(f"\tDump time is: {time_dump_finish - time_dump_start}")
    #
    # time_load_start = time.time()
    # loaded = pickle.loads(dumps)
    # time_load_finish = time.time()
    # print(f"\tLoad time is: {time_load_finish - time_load_start}")

    time_dump_start = time.time()
    with open(f"{name}.pickle", 'wb') as f:
        pickle.dump(ob, f)
    time_dump_finish = time.time()
    print(f"\tDump to disk time is: {time_dump_finish - time_dump_start}")

    time_load_start = time.time()
    with open(f"{name}.pickle", 'rb') as f:
        loaded = pickle.load(f)
    time_load_finish = time.time()
    print(f"\tLoad from disk time is: {time_load_finish - time_load_start}")


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
        analyse_model_func,
        process_local=process_local_serial,
        debug=False,
):
    if debug:
        print(f'\tProcessing dtag: {test_dtag}')
    time_dataset_start = time.time()

    dataset_log_path = pandda_fs_model.processed_datasets.processed_datasets[test_dtag].log_path
    dataset_log = {}
    dataset_log["Model analysis time"] = {}

    ###################################################################
    # # Process the models...
    ###################################################################

    time_model_analysis_start = time.time()
    # analyse_model_paramaterised = partial(
    #     analyse_model,
    #     test_dtag=test_dtag,
    #     dataset_xmap=dataset_xmaps[test_dtag],
    #     reference=reference,
    #     grid=grid,
    #     dataset_processed_dataset=pandda_fs_model.processed_datasets[test_dtag],
    #     dataset_alignment=alignments[test_dtag],
    #     max_site_distance_cutoff=max_site_distance_cutoff,
    #     min_bdc=min_bdc, max_bdc=max_bdc,
    #     contour_level=contour_level,
    #     cluster_cutoff_distance_multiplier=cluster_cutoff_distance_multiplier,
    #     min_blob_volume=min_blob_volume,
    #     min_blob_z_peak=min_blob_z_peak,
    #     debug=False
    # )
    #
    # results = process_local(
    #     [
    #         Partial(
    #             analyse_model_paramaterised,
    #             model,
    #             model_number,
    #         )
    #         for model_number, model
    #         in models.items()]
    # )

    results = process_local(
        [
            Partial(
                analyse_model_func,
                model,
                model_number,
                test_dtag=test_dtag,
                dataset_xmap=dataset_xmaps[test_dtag],
                reference=reference,
                grid=grid,
                dataset_processed_dataset=pandda_fs_model.processed_datasets[test_dtag],
                dataset_alignment=alignments[test_dtag],
                max_site_distance_cutoff=max_site_distance_cutoff,
                min_bdc=min_bdc, max_bdc=max_bdc,
                contour_level=contour_level,
                cluster_cutoff_distance_multiplier=cluster_cutoff_distance_multiplier,
                min_blob_volume=min_blob_volume,
                min_blob_z_peak=min_blob_z_peak,
                debug=debug
            )
            for model_number, model
            in models.items()]
    )

    model_results = {model_number: result[0] for model_number, result in zip(models, results)}
    dataset_log["Model logs"] = {model_number: result[1] for model_number, result in zip(models, results)}  #

    time_model_analysis_finish = time.time()

    dataset_log["Time to analyse all models"] = time_model_analysis_finish - time_model_analysis_start

    if debug:
        print(f"\tTime to analyse all models: {time_model_analysis_finish - time_model_analysis_start}")
        for model_number, model_result in model_results.items():
            model_time = dataset_log["Model logs"][model_number]["Model analysis time"]
            print(f"\t\tModel {model_number} processed in {model_time}")

    # dump_and_load(dataset_xmaps[test_dtag], "xmap")
    # dump_and_load(reference, "reference")
    # dump_and_load(grid, "grid")
    # dump_and_load(pandda_fs_model.processed_datasets[test_dtag], "processed_dataset")
    # dump_and_load(alignments, "alignments")
    # # dump_and_load(alignments, "func")
    # dump_and_load([model for model in models.values()][0], "model")
    # dump_and_load(
    #     partial(
    #         analyse_model_paramaterised,
    #         [model for model in models.values()][0],
    #         [model_number for model_number in models.keys()][0],
    #     ), "func")
    # dump_and_load(
    #     results,
    #     "results")

    ###################################################################
    # # Decide which model to use...
    ###################################################################
    if debug:
        print(f"\tSelecting model...")
    selected_model_number, model_selection_log = EXPERIMENTAL_select_model(
        model_results,
        grid.partitioning.inner_mask,
        pandda_fs_model.processed_datasets[test_dtag],
        debug=debug,
    )
    selected_model = models[selected_model_number]
    selected_model_clusterings = model_results[selected_model_number]['clusterings_merged']
    zmap = model_results[selected_model_number]['zmap']
    dataset_log['Selected model'] = int(selected_model_number)
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
        sample_rate=dataset_truncated_datasets[test_dtag].reflections.resolution().resolution / 0.5
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

    # TODO: Remove altogether
    # if debug:
    #     for model_number, model_result in model_results.items():
    #         save_reference_frame_zmap(
    #             pandda_fs_model.processed_datasets.processed_datasets[
    #                 test_dtag].z_map_file.path.parent / f'{model_number}_ref.ccp4',
    #                 model_result['zmap']
    #         )
    #         save_native_frame_zmap(
    #             pandda_fs_model.processed_datasets.processed_datasets[
    #                 test_dtag].z_map_file.path.parent / f'{model_number}_native.ccp4',
    #             model_result['zmap'],
    #             dataset_truncated_datasets[test_dtag],
    #             alignments[test_dtag],
    #             grid,
    #             structure_factors,
    #             outer_mask,
    #             inner_mask_symmetry,
    #             partitioning,
    #             sample_rate,
    #         )

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
        load_xmap_func,
        analyse_model_func,
        debug=False,
):
    if debug:
        print(f"Processing shell at resolution: {shell.res}")

    if memory_availability == "very_low":
        process_local_in_shell = process_local_serial
        process_local_in_dataset = process_local_serial
        process_local_over_datasets = process_local_serial
    elif memory_availability == "low":
        process_local_in_shell = process_local
        process_local_in_dataset = process_local
        process_local_over_datasets = process_local_serial
    elif memory_availability == "high":
        process_local_in_shell = process_local
        process_local_in_dataset = process_local_serial
        process_local_over_datasets = process_local

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
    if debug:
        print(f"\tTruncating shell datasets")
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
    if debug:
        print(f"\tLoading xmaps")

    time_xmaps_start = time.time()

    # load_xmap_paramaterised = partial(
    #     Xmap.from_unaligned_dataset_c,
    #     grid=grid,
    #     structure_factors=structure_factors,
    #     # sample_rate=sample_rate,
    #     sample_rate=shell.res / 0.5
    # )
    #
    # results = process_local_in_shell(
    #     [partial(
    #         load_xmap_paramaterised,
    #         shell_truncated_datasets[key],
    #         alignments[key],
    #     )
    #         for key
    #         in shell_truncated_datasets
    #     ]
    # )

    results = process_local_in_shell(
        [
            Partial(
            load_xmap_func,
            shell_truncated_datasets[key],
            alignments[key],
            grid = grid,
            structure_factors = structure_factors,
            sample_rate = shell.res / 0.5,
        )
            for key
            in shell_truncated_datasets
        ]
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
    if debug:
        print(f"\tGetting models")
    models = get_models(
        shell.test_dtags,
        shell.train_dtags,
        xmaps,
        grid,
        process_local_in_shell,
    )

    ###################################################################
    # # Process each test dataset
    ###################################################################
    # Now that all the data is loaded, get the comparison set and process each test dtag

    process_dataset_paramaterized = Partial(
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
        sample_rate=shell.res / 0.5,
        statmaps=statmaps,
        analyse_model_func=analyse_model_func,
        process_local=process_local_in_dataset,
        debug=debug,
    )

    # Process each dataset in the shell
    all_train_dtags_unmerged = [_dtag for l in shell.train_dtags.values() for _dtag in l]
    all_train_dtags = []
    for _dtag in all_train_dtags_unmerged:
        if _dtag not in all_train_dtags:
            all_train_dtags.append(_dtag)

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
