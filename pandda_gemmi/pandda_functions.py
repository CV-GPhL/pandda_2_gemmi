from __future__ import annotations

from typing import *
from time import sleep
from functools import partial
import json
import pickle
import secrets

import numpy as np
import multiprocessing as mp
import joblib
from scipy import spatial as spsp, cluster as spc

from sklearn import decomposition
import umap
from bokeh.plotting import ColumnDataSource, figure, output_file, show, save
import hdbscan
from matplotlib import pyplot as plt

from pandda_gemmi.pandda_types import *
from pandda_gemmi import constants


def run(func):
    return func()


def process_local_serial(funcs):
    results = []
    for func in funcs:
        results.append(func())

    return results


def process_local_joblib(n_jobs, verbosity, funcs):
    mapper = joblib.Parallel(n_jobs=n_jobs,
                             verbose=verbosity,
                             backend="loky",
                             )

    results = mapper(joblib.delayed(func)() for func in funcs)

    return results


def process_local_multiprocessing(funcs, n_jobs=12, method="forkserver"):
    if method == "forkserver":
        try:
            mp.set_start_method("forkserver")
        except Exception as e:
            print(e)

    elif method == "spawn":
        try:
            mp.set_start_method("spawn")
        except Exception as e:
            print(e)

    else:
        raise Exception(
            f"Method {method} is not a valid multiprocessing start method: try spawn (stable) or forkserver (fast)")

    with mp.Pool(n_jobs) as pool:
        results = pool.map(run, funcs)

    return results


def process_local_dask(funcs, client=None):
    processes = [client.submit(func) for func in funcs]
    results = client.gather(processes)
    return results


def process_shell_dask(funcs):
    from dask.distributed import worker_client

    with worker_client() as client:
        # Multiprocess
        processes = [client.submit(func) for func in funcs]
        results = client.gather(processes)
    return results


def process_global_serial(funcs):
    results = []
    for func in funcs:
        results.append(func())

    return results


def get_dask_client(scheduler="SGE",
                    num_workers=10,
                    queue=None,
                    project=None,
                    cores_per_worker=12,
                    distributed_mem_per_core=10,
                    resource_spec="",
                    job_extra=("",),
                    walltime="30:00:00",
                    watcher=True,
                    ):
    import dask
    from dask.distributed import Client
    from dask_jobqueue import HTCondorCluster, PBSCluster, SGECluster, SLURMCluster

    dask.config.set({'distributed.worker.daemon': False})

    schedulers = ["HTCONDOR", "PBS", "SGE", "SLURM"]
    if scheduler not in schedulers:
        raise Exception(f"Supported schedulers are: {schedulers}")

    if scheduler == "HTCONDOR":
        job_extra = [(f"GetEnv", "True"), ]
        cluster = HTCondorCluster(
            # queue=queue,
            # project=project,
            cores=cores_per_worker,
            memory=f"{distributed_mem_per_core * cores_per_worker}G",
            # resource_spec=resource_spec,
            # walltime=walltime,
            disk="10G",
            processes=1,
            nanny=watcher,
            job_extra=job_extra,
        )

    elif scheduler == "PBS":
        cluster = PBSCluster(
            queue=queue,
            project=project,
            cores=cores_per_worker,
            memory=f"{distributed_mem_per_core * cores_per_worker}G",
            resource_spec=resource_spec,
            walltime=walltime,
            processes=1,
            nanny=watcher,
        )

    elif scheduler == "SGE":
        extra = [f"-pe smp {cores_per_worker}", "-V"]
        cluster = SGECluster(
            queue=queue,
            project=project,
            cores=cores_per_worker,
            memory=f"{distributed_mem_per_core * cores_per_worker}G",
            resource_spec=resource_spec,
            walltime=walltime,
            processes=1,
            nanny=watcher,
            job_extra=extra,
        )

    elif scheduler == "SLURM":
        cluster = SLURMCluster(
            queue=queue,
            project=project,
            cores=cores_per_worker,
            memory=f"{distributed_mem_per_core * cores_per_worker}GB",
            walltime=walltime,
            processes=1,
            nanny=watcher,
            job_extra=job_extra
        )

    else:
        raise Exception("Something has gone wrong process_global_dask")

    # Scale the cluster up to the number of workers
    cluster.scale(jobs=num_workers)

    # Launch the client
    client = Client(cluster)
    return client


#
# def process_global_dask(
#         funcs,
#         client=None,
# ):
#
#     # Pickle all the funcs to some directory
#
#     # construct the run functions
#
#     #
#
#
#     # Multiprocess
#     processes = [client.submit(func) for func in funcs]
#     while any(f.status == 'pending' for f in processes):
#         sleep(0.1)
#
#     if any(f.status == 'error' for f in processes):
#         errored_processes = [f for f in processes if f.status == 'error']
#
#         print(f'{len(errored_processes)} out of {len(processes)} processes errored! Attempting to recreate clocally')
#
#         for f in errored_processes:
#             client.recreate_error_locally(f)
#         raise Exception(f'Failed to recreate errors in dask distribution locally!')
#
#     results = client.gather(processes)
#
#     return results


class Run:
    def __init__(self,
                 func,
                 input_file,
                 output_file,
                 # target_file,
                 ):
        self.input_file = input_file
        self.output_file = output_file

        with open(input_file, 'wb') as f:
            pickle.dump(func, f)

    def __call__(self):
        with open(self.input_file, 'rb') as f:
            f = pickle.load(f)

        result = f()

        with open(self.output_file, "wb") as f:
            pickle.dump(result, f)

        return self.output_file

    def clean(self):
        try:
            os.remove(self.input_file)
            os.remove(self.output_file)
        except Exception as e:
            print(e)


def process_global_dask(
        funcs,
        client=None,
        tmp_dir=None,
):
    # Key
    keys = [str(secrets.token_hex(16)) for func in funcs]

    # construct the run functions
    run_funcs = [
        Run(func, tmp_dir / f"{key}.in.pickle", tmp_dir / f"{key}.out.pickle")
        for key, func
        in zip(keys, funcs, )
    ]

    # Multiprocess
    processes = [client.submit(func) for func in run_funcs]
    while any(f.status == 'pending' for f in processes):
        sleep(1)

    if any(f.status == 'error' for f in processes):
        errored_processes = [f for f in processes if f.status == 'error']

        print(f'{len(errored_processes)} out of {len(processes)} processes errored! Attempting to recreate clocally')

        for f in errored_processes:
            client.recreate_error_locally(f)
        raise Exception(f'Failed to recreate errors in dask distribution locally!')

    results = client.gather(processes)

    # Load all the pickled results
    results_loaded = []
    for result in results:
        with open(result, 'rb') as f:
            results_loaded.append(pickle.load(f))

    for run_func in run_funcs:
        run_func.clean()

    return results_loaded


def get_comparators_high_res_random(
        datasets: Dict[Dtag, Dataset],
        comparison_min_comparators,
        comparison_max_comparators,
):
    dtag_list = [dtag for dtag in datasets]

    dtags_by_res = list(
        sorted(
            dtag_list,
            key=lambda dtag: datasets[dtag].reflections.resolution().resolution,
        )
    )

    highest_res_datasets = dtags_by_res[:comparison_min_comparators + 1]
    highest_res_datasets_max = max(
        [datasets[dtag].reflections.resolution().resolution for dtag in highest_res_datasets])

    comparators = {}
    for dtag in dtag_list:
        current_res = datasets[dtag].reflections.resolution().resolution

        truncation_res = max(current_res, highest_res_datasets_max)

        truncated_datasets = [dtag for dtag in dtag_list if
                              datasets[dtag].reflections.resolution().resolution < truncation_res]

        comparators[dtag] = list(
            np.random.choice(
                truncated_datasets,
                size=comparison_min_comparators,
                replace=False,
            )
        )

    return comparators


def get_distance_matrix(samples: MutableMapping[str, np.ndarray]) -> np.ndarray:
    # Make a pairwise matrix
    correlation_matrix = np.zeros((len(samples), len(samples)))

    for x, reference_sample in enumerate(samples.values()):

        reference_sample_mean = np.mean(reference_sample)
        reference_sample_demeaned = reference_sample - reference_sample_mean
        reference_sample_denominator = np.sqrt(np.sum(np.square(reference_sample_demeaned)))

        for y, sample in enumerate(samples.values()):
            sample_mean = np.mean(sample)
            sample_demeaned = sample - sample_mean
            sample_denominator = np.sqrt(np.sum(np.square(sample_demeaned)))

            nominator = np.sum(reference_sample_demeaned * sample_demeaned)
            denominator = sample_denominator * reference_sample_denominator

            correlation = nominator / denominator

            correlation_matrix[x, y] = correlation

    correlation_matrix = np.nan_to_num(correlation_matrix)

    # distance_matrix = np.ones(correlation_matrix.shape) - correlation_matrix

    for j in range(correlation_matrix.shape[0]):
        correlation_matrix[j, j] = 1.0

    return correlation_matrix


def embed_umap(distance_matrix):
    pca = decomposition.PCA(n_components=min(distance_matrix.shape[0], 50))
    reducer = umap.UMAP()
    transform = pca.fit_transform(distance_matrix)
    transform = reducer.fit_transform(transform)
    return transform


def bokeh_scatter_plot(embedding, labels, known_apos, plot_file):
    output_file(str(plot_file))

    apos = []
    for label in labels:
        if label in known_apos:
            apos.append("green")
        else:
            apos.append("pink")

    source = ColumnDataSource(
        data=dict(
            x=embedding[:, 0].tolist(),
            y=embedding[:, 1].tolist(),
            dtag=labels,
            apo=apos
        ))

    TOOLTIPS = [
        ("index", "$index"),
        ("(x,y)", "($x, $y)"),
        ("dtag", "@dtag"),
        ("apo", "@apo"),
    ]

    p = figure(plot_width=1200, plot_height=1200, tooltips=TOOLTIPS,
               title="Mouse over the dots",
               )

    p.circle('x', 'y', size=15, source=source, color="apo")

    save(p)


def save_plot_pca_umap_bokeh(dataset_connectivity_matrix, labels, known_apos, plot_file):
    embedding = embed_umap(dataset_connectivity_matrix)
    bokeh_scatter_plot(embedding, labels, known_apos, plot_file)


def from_unaligned_dataset_c_flat(dataset: Dataset,
                                  alignment: Alignment,
                                  grid: Grid,
                                  structure_factors: StructureFactors,
                                  sample_rate: float = 3.0, ):
    xmap = Xmap.from_unaligned_dataset_c(dataset,
                                         alignment,
                                         grid,
                                         structure_factors,
                                         sample_rate,
                                         )

    xmap_array = xmap.to_array()

    masked_array = xmap_array[grid.partitioning.total_mask == 1]

    return masked_array


def get_comparators_closest_cutoff(
        datasets: Dict[Dtag, Dataset],
        alignments,
        grid,
        comparison_min_comparators,
        comparison_max_comparators,
        structure_factors,
        sample_rate,
        resolution_cutoff,
        pandda_fs_model: PanDDAFSModel,
        process_local,
        exclude_local=5
):
    dtag_list = [dtag for dtag in datasets]
    dtag_array = np.array(dtag_list)

    dtags_by_res = list(
        sorted(
            dtag_list,
            key=lambda dtag: datasets[dtag].reflections.resolution().resolution,
        )
    )

    highest_res_datasets = dtags_by_res[:comparison_min_comparators + 1]
    highest_res_datasets_max = max(
        [datasets[dtag].reflections.resolution().resolution for dtag in highest_res_datasets])

    # Load the xmaps
    print("Truncating datasets...")
    shell_truncated_datasets: Datasets = truncate(
        datasets,
        resolution=Resolution(highest_res_datasets_max),
        structure_factors=structure_factors,
    )

    # Generate aligned xmaps
    print("Loading xmaps")
    start = time.time()
    load_xmap_paramaterised = partial(
        from_unaligned_dataset_c_flat,
        grid=grid,
        structure_factors=structure_factors,
        sample_rate=sample_rate,
    )

    results = process_local(
        [
            partial(
                load_xmap_paramaterised,
                shell_truncated_datasets[key],
                alignments[key],
            )
            for key
            in shell_truncated_datasets
        ]
    )
    print("Got xmaps!")

    # Get the maps as arrays
    print("Getting xmaps as arrays")
    xmaps = {dtag: xmap
             for dtag, xmap
             in zip(datasets, results)
             }

    finish = time.time()
    print(f"Mapped in {finish - start}")

    # Get the correlation distance between maps
    correlation_matrix = get_distance_matrix(xmaps)

    # Save a bokeh plot
    labels = [dtag.dtag for dtag in xmaps]
    known_apos = [dtag.dtag for dtag, dataset in datasets.items()]
    save_plot_pca_umap_bokeh(correlation_matrix,
                             labels,
                             known_apos,
                             pandda_fs_model.pandda_dir / f"pca_umap.html")

    # Get the comparators: for each dataset rank all comparators, then go along accepting or rejecting them
    # Based on whether they are within the res cutoff
    comparators = {}
    for j, dtag in enumerate(dtag_list):
        print(f"Finding closest for dtag: {dtag}")
        current_res = datasets[dtag].reflections.resolution().resolution

        # Get dtags ordered by distance
        row = correlation_matrix[j, :].flatten()
        print(f"\tRow is: {row}")
        closest_dtags_indexes = np.flip(np.argsort(row))
        closest_dtags = np.take_along_axis(dtag_array, closest_dtags_indexes, axis=0)
        print(f"\tClosest dtags are: {closest_dtags}")
        print(f"\tdistances are: {np.take_along_axis(row, closest_dtags_indexes, axis=0)}")

        # Decide the res upper bound
        truncation_res = max(current_res + resolution_cutoff, highest_res_datasets_max)
        print(f"\tTrucation res is: {truncation_res}")

        # Go down the list of closes datasets seeing if they fall within truncation res and adding them to comparators
        # if so

        potential_comparator_dtags = []
        for j, potential_comparator_dtag in enumerate(closest_dtags):

            if j < exclude_local:
                if j > 0:
                    continue

            if datasets[dtag].reflections.resolution().resolution < truncation_res:
                potential_comparator_dtags.append(potential_comparator_dtag)
            else:
                continue

            # of enough accuulated, continue
            if len(potential_comparator_dtags) > comparison_min_comparators:
                comparators[dtag] = potential_comparator_dtags
                break

    return comparators


def get_comparators_closest_apo_cutoff(
        datasets: Dict[Dtag, Dataset],
        alignments,
        grid,
        comparison_min_comparators,
        comparison_max_comparators,
        structure_factors,
        sample_rate,
        resolution_cutoff,
        pandda_fs_model: PanDDAFSModel,
        process_local,
        known_apos: List[Dtag],
):
    dtag_list = [dtag for dtag in datasets]
    dtag_array = np.array(dtag_list)
    known_apo_array = np.array(known_apos)
    index_to_known_apo = {j: dtag for j, dtag in enumerate(known_apo_array)}
    dtag_to_index = {dtag: j for j, dtag in enumerate(dtag_list)}

    dtags_by_res = list(
        sorted(
            dtag_list,
            key=lambda dtag: datasets[dtag].reflections.resolution().resolution,
        )
    )

    highest_res_datasets = dtags_by_res[:comparison_min_comparators + 1]
    highest_res_datasets_max = max(
        [datasets[dtag].reflections.resolution().resolution for dtag in highest_res_datasets])

    # Load the xmaps
    print("Truncating datasets...")
    shell_truncated_datasets: Datasets = truncate(
        datasets,
        resolution=Resolution(highest_res_datasets_max),
        structure_factors=structure_factors,
    )

    # Generate aligned xmaps
    print("Loading xmaps")
    start = time.time()
    load_xmap_paramaterised = partial(
        from_unaligned_dataset_c_flat,
        grid=grid,
        structure_factors=structure_factors,
        sample_rate=sample_rate,
    )

    results = process_local(
        [
            partial(
                load_xmap_paramaterised,
                shell_truncated_datasets[key],
                alignments[key],
            )
            for key
            in shell_truncated_datasets
        ]
    )
    print("Got xmaps!")

    # Get the maps as arrays
    print("Getting xmaps as arrays")
    xmaps = {dtag: xmap
             for dtag, xmap
             in zip(datasets, results)
             }

    finish = time.time()
    print(f"Mapped in {finish - start}")

    # Get known apo mask
    def is_known_apo(dtag: Dtag, known_apos: List[Dtag]):
        if dtag in known_apos:
            return True
        else:
            return False

    known_apo_mask = np.array([is_known_apo(dtag, known_apos) for dtag in xmaps])

    # Get the correlation distance between maps
    correlation_matrix = get_distance_matrix(xmaps)

    # Save a bokeh plot
    labels = [dtag.dtag for dtag in xmaps]
    known_apos_strings = [dtag.dtag for dtag in known_apos]
    save_plot_pca_umap_bokeh(correlation_matrix,
                             labels,
                             known_apos_strings,
                             pandda_fs_model.pandda_dir / f"pca_umap.html")

    # Get known apo distances
    known_apo_closest_dtags = {}
    known_apo_rows = correlation_matrix[known_apo_mask, :]
    for j, known_apo in enumerate(known_apos):
        distances = known_apo_rows[j, :].flatten()
        print(f"Known apo {known_apo.dtag} has distances: {distances}")

        closest_dtags_indexes = np.flip(np.argsort(distances))
        known_apo_closest_dtags = np.take_along_axis(known_apo_array, closest_dtags_indexes, axis=0)
        print(f"Known apo {known_apo.dtag} has closest dtags: {known_apo_closest_dtags}")

    # Get the comparators: for each dataset rank all comparators, then go along accepting or rejecting them
    # Based on whether they are within the res cutoff
    comparators = {}
    for j, dtag in enumerate(dtag_list):
        print(f"Finding closest for dtag: {dtag}")
        current_res = datasets[dtag].reflections.resolution().resolution

        # Get dtags ordered by distance
        row = correlation_matrix[j, :].flatten()
        print(f"\tRow is: {row}")

        # Get distances to known apos
        row_known_apos = row[known_apo_mask]

        # Get closest known apo
        closest_dtags_indexes = np.flip(np.argsort(row_known_apos))
        closest_known_apo_distances = np.take_along_axis(closest_dtags_indexes, closest_dtags_indexes, axis=0)[0]
        closest_known_apo_index = closest_dtags_indexes[0]
        closest_known_apo_dtag = index_to_known_apo[closest_known_apo_index]
        closest_known_apo_all_index = dtag_to_index[closest_known_apo_dtag]

        print(f"\tDtag {dtag.dtag} has closest known apo: {closest_known_apo_dtag}")
        print(f"\tOther known apo distances are: {closest_known_apo_distances}")

        # Get closest dtags to known apo
        closest_dtags = known_apo_closest_dtags[closest_known_apo_dtag]
        print(f"\tClosest dtags are: {closest_dtags}")
        print(f"\tdistances are: {np.take_along_axis(row, closest_dtags_indexes, axis=0)}")

        # Decide the res upper bound
        truncation_res = max(current_res + resolution_cutoff, highest_res_datasets_max)
        print(f"\tTrucation res is: {truncation_res}")

        # Go down the list of closes datasets seeing if they fall within truncation res and adding them to comparators
        # if so

        potential_comparator_dtags = []
        for potential_comparator_dtag in closest_dtags:

            if datasets[dtag].reflections.resolution().resolution < truncation_res:
                potential_comparator_dtags.append(potential_comparator_dtag)
            else:
                continue

            # of enough accuulated, continue
            if len(potential_comparator_dtags) > comparison_min_comparators:
                comparators[dtag] = potential_comparator_dtags
                break

    return comparators


#
# def get_distance_matrix(samples: MutableMapping[str, np.ndarray]) -> np.ndarray:
#     # Make a pairwise matrix
#     correlation_matrix = np.zeros((len(samples), len(samples)))
#
#     for x, reference_sample in enumerate(samples.values()):
#
#         reference_sample_mean = np.mean(reference_sample)
#         reference_sample_demeaned = reference_sample - reference_sample_mean
#         reference_sample_denominator = np.sqrt(np.sum(np.square(reference_sample_demeaned)))
#
#         for y, sample in enumerate(samples.values()):
#             sample_mean = np.mean(sample)
#             sample_demeaned = sample - sample_mean
#             sample_denominator = np.sqrt(np.sum(np.square(sample_demeaned)))
#
#             nominator = np.sum(reference_sample_demeaned * sample_demeaned)
#             denominator = sample_denominator * reference_sample_denominator
#
#             correlation = nominator / denominator
#
#             correlation_matrix[x, y] = correlation
#
#     correlation_matrix = np.nan_to_num(correlation_matrix)
#
#     # distance_matrix = np.ones(correlation_matrix.shape) - correlation_matrix
#
#     for j in range(correlation_matrix.shape[0]):
#         correlation_matrix[j, j] = 1.0
#
#     return correlation_matrix


def get_linkage_from_correlation_matrix(correlation_matrix):
    condensed = spsp.distance.squareform(1.0 - correlation_matrix)
    linkage = spc.hierarchy.linkage(condensed, method='complete')
    # linkage = spc.linkage(condensed, method='ward')

    return linkage


def get_linkage_from_observations(observations):
    linkage = spc.hierarchy.linkage(observations, method='complete')

    return linkage


def cluster_linkage(linkage, cutoff):
    idx = spc.hierarchy.fcluster(linkage, cutoff, 'distance')

    return idx


def cluster_density(linkage: np.ndarray, cutoff: float) -> np.ndarray:
    # Get the linkage matrix
    # Cluster the datasets
    clusters: np.ndarray = cluster_linkage(linkage, cutoff)
    # Determine which clusters have known apos in them

    return clusters


def save_dendrogram_plot(linkage,
                         labels,
                         dendrogram_plot_file,
                         threshold=0.3
                         ):
    fig, ax = plt.subplots(figsize=(0.2 * len(labels), 40))
    dn = spc.hierarchy.dendrogram(linkage, ax=ax, labels=labels, leaf_font_size=10, color_threshold=threshold)
    fig.savefig(str(dendrogram_plot_file))
    fig.clear()
    plt.close(fig)


def get_clusters_linkage(
        reduced_array,
        dtag_list,
        dtag_array,
        dtag_to_index,
        pandda_fs_model,
):
    linkage = get_linkage_from_observations(reduced_array)

    save_dendrogram_plot(linkage,
                         [_dtag.dtag for _dtag in dtag_list],
                         str(pandda_fs_model.pandda_dir / f"dendrogram.png"),
                         # threshold=0.3,
                         )

    rootnode, nodelist = spc.hierarchy.to_tree(linkage, rd=True)

    def recurse_node(node, min_samples):

        _clusters = []
        # if node.count > min_samples:
        if node.left.count >= min_samples:
            left_clusters = recurse_node(node.left, min_samples)
            for left_cluster in left_clusters:
                _clusters.append(left_cluster)

        if node.right.count >= min_samples:
            right_clusters = recurse_node(node.right, min_samples)
            for right_cluster in right_clusters:
                _clusters.append(right_cluster)

        if node.count >= min_samples:
            if (node.right.count < min_samples) and (node.left.count < min_samples):
                _clusters.append(node.pre_order(lambda x: x.id))

        return _clusters

    clusters = recurse_node(rootnode, 30)
    print(clusters)

    clusters_dict = {}
    dtag_to_cluster = {}
    for j, cluster in enumerate(clusters):
        clusters_dict[j] = dtag_array[np.array(cluster)]
        for dtag in clusters_dict[j]:
            dtag_to_cluster[dtag] = j
    print(clusters_dict)

    save_dendrogram_plot(linkage,
                         [
                             f"{_dtag.dtag}_{dtag_to_cluster[_dtag]}" if _dtag in dtag_to_cluster else _dtag.dtag
                             for _dtag
                             in dtag_list
                         ],
                         str(pandda_fs_model.pandda_dir / f"dendrogram_with_clusters.png"),
                         # threshold=0.3,
                         )

    #
    #
    # # Get the cores of each cluster
    # cluster_cores = {}
    # for n in np.unique(labels):
    #     if n != -1:
    #         indexes = np.arange(len(labels))
    #         cluster_member_mask = labels == n
    #         cluster_member_indexes = np.nonzero(cluster_member_mask)
    #         cluster_member_values = probabilities[cluster_member_mask]
    #         cluster_members_sorted_indexes = np.argsort(cluster_member_values)
    #
    #         if np.sum(cluster_member_indexes) >= 30:
    #             cluster_cores[n] = cluster_member_indexes[cluster_member_mask][cluster_members_sorted_indexes][:30]
    #
    #         else:
    #             print(f"There were less than 30 members of the cluster!")
    #
    # print(f"Cluster cores are:")
    # print(cluster_cores)

    # Save a bokeh plot
    labels = [dtag.dtag for dtag in dtag_list]
    # known_apos = [dtag.dtag for dtag, dataset in datasets.items() if any(dtag in x for x in cluster_cores.values())]
    known_apos = []
    for cluster_num, cluster_dtags in clusters_dict.items():
        print(f"\tCluster {cluster_num} dtags are {cluster_dtags}")
        for cluster_core_dtag in cluster_dtags:
            known_apos.append(cluster_core_dtag.dtag)

    print(f"Labels are: {labels}")
    print(f"Known apos are: {known_apos}")

    save_plot_pca_umap_bokeh(
        reduced_array,
        labels,
        known_apos,
        pandda_fs_model.pandda_dir / f"pca_umap.html",
    )

    #
    cophenetic_matrix = spsp.distance.squareform(spc.hierarchy.cophenet(linkage))
    dtag_distance_to_cluster = {}
    for _dtag in dtag_list:
        dtag_index = dtag_to_index[_dtag]
        dtag_distance_to_cluster[_dtag] = {}
        dtag_coord = reduced_array[dtag_index, :]
        for cluster, cluster_dtags in clusters_dict.items():
            cluster_indexes = np.array([dtag_to_index[_cluster_dtag] for _cluster_dtag in cluster_dtags])
            cluster_coords = reduced_array[cluster_indexes, :]

            cluster_squared_vectors = np.sqrt(np.sum(np.square(cluster_coords - dtag_coord), axis=1))

            median_squared_distance = np.median(cluster_squared_vectors)

            dtag_distance_to_cluster[_dtag][cluster] = median_squared_distance

    cluster_widths = {}
    for cluster, cluster_dtags in clusters_dict.items():
        cluster_indexes = np.array([dtag_to_index[cluster_dtag] for cluster_dtag in cluster_dtags])
        cluster_coords = reduced_array[cluster_indexes, :]
        cluster_median = np.median(cluster_coords, axis=0).reshape((1, cluster_coords.shape[1]))
        cluster_median_deviation = np.median(np.sqrt(np.sum(np.square(cluster_coords - cluster_median), axis=1)))
        cluster_widths[cluster] = cluster_median_deviation

    print(f"Cluster median absolute deviation is: {cluster_widths}")

    # Get the centermost cluster
    cluster_medians = {}
    for cluster, cluster_dtags in clusters_dict.items():
        cluster_indexes = np.array([dtag_to_index[cluster_dtag] for cluster_dtag in cluster_dtags])
        cluster_coords = reduced_array[cluster_indexes, :]
        cluster_median = np.median(cluster_coords, axis=0).reshape((1, cluster_coords.shape[1]))
        cluster_medians[cluster] = cluster_median
    print(f"Cluster medians are: {cluster_medians}")

    median_of_medians = np.median(np.vstack([x for x in cluster_medians.values()]), axis=0).reshape(1,
                                                                                                    cluster_coords.shape[
                                                                                                        1])
    print(f"Global median of clusters is: {median_of_medians}")

    centermost_cluster = min(
        cluster_medians,
        key=lambda _cluster_num: np.sqrt(np.sum(np.square((median_of_medians - cluster_medians[_cluster_num])))),
    )
    print(f"Centermost cluster is: {centermost_cluster}")

    return cophenetic_matrix, dtag_distance_to_cluster, centermost_cluster, clusters_dict

def get_clusters_nn(
        reduced_array,
        dtag_list,
        dtag_array,
        dtag_to_index,
        pandda_fs_model,
):


    # Get the n nearest neighbours for each point
    from sklearn.neighbors import NearestNeighbors

    nbrs = NearestNeighbors(n_neighbors=30).fit(reduced_array)
    distances, indices = nbrs.kneighbors(reduced_array)

    # Get neighbourhood radii
    radii = {}
    for j, row in enumerate(distances):
        mean_distance = np.mean(row)
        radii[j] = mean_distance

    # Sort datasets by radii
    radii_sorted = {index: radii[index] for index in sorted(radii, key=lambda _index: radii[_index])}

    # Loop over datasets from narrowest to broadest, checking whether any of their neighbours have been claimed
    # If so, skip to next
    for index in radii_sorted:
        ...

    # Save a bokeh plot
    labels = [dtag.dtag for dtag in dtag_list]
    # known_apos = [dtag.dtag for dtag, dataset in datasets.items() if any(dtag in x for x in cluster_cores.values())]
    known_apos = []
    for cluster_num, cluster_dtags in clusters_dict.items():
        print(f"\tCluster {cluster_num} dtags are {cluster_dtags}")
        for cluster_core_dtag in cluster_dtags:
            known_apos.append(cluster_core_dtag.dtag)

    print(f"Labels are: {labels}")
    print(f"Known apos are: {known_apos}")

    save_plot_pca_umap_bokeh(
        reduced_array,
        labels,
        known_apos,
        pandda_fs_model.pandda_dir / f"pca_umap.html",
    )

    #

    return distance_matrix, dtag_distance_to_cluster, centermost_cluster, clusters_dict



def get_comparators_closest_cluster(
        datasets: Dict[Dtag, Dataset],
        alignments,
        grid,
        comparison_min_comparators,
        comparison_max_comparators,
        structure_factors,
        sample_rate,
        resolution_cutoff,
        pandda_fs_model: PanDDAFSModel,
        process_local,
        get_clusters: Callable,
        batch=False,
        cluster_selection="close"
):
    dtag_list = [dtag for dtag in datasets]
    dtag_array = np.array(dtag_list)
    dtag_to_index = {dtag: j for j, dtag in enumerate(dtag_list)}

    dtags_by_res = list(
        sorted(
            dtag_list,
            key=lambda dtag: datasets[dtag].reflections.resolution().resolution,
        )
    )

    highest_res_datasets = dtags_by_res[:comparison_min_comparators + 1]
    highest_res_datasets_max = max(
        [datasets[dtag].reflections.resolution().resolution for dtag in highest_res_datasets])

    # Load the xmaps
    print("Truncating datasets...")
    shell_truncated_datasets: Datasets = truncate(
        datasets,
        resolution=Resolution(highest_res_datasets_max),
        structure_factors=structure_factors,
    )

    # Generate aligned xmaps
    print("Loading xmaps")

    load_xmap_paramaterised = partial(
        from_unaligned_dataset_c_flat,
        grid=grid,
        structure_factors=structure_factors,
        sample_rate=sample_rate,
    )

    # Get reduced array
    total_sample_size = len(shell_truncated_datasets)
    print(f"Total sample size = {total_sample_size}")
    batch_size = min(90, total_sample_size)
    print(f"Batch size is: {batch_size}")
    num_batches = (total_sample_size // batch_size) + 1
    print(f"Num batches is: {num_batches}")
    # batches = [
    #     np.arange(x*batch_size, min((x+1)*batch_size, total_sample_size))
    #     for x
    #     in range(0, num_batches)]
    tmp_batches = {}
    j = 1
    while True:
        print(f"\tJ is: {j}")
        new_batches = np.array_split(np.arange(total_sample_size), j)
        print(f"\t\tlen of new batches is {len(new_batches)}")
        tmp_batches[j] = new_batches
        j = j + 1

        if any(len(batch) < batch_size for batch in new_batches):
            batches = tmp_batches[j - 2]
            break
        else:
            print("\t\tAll batches larger than batch size, trying smaller split!")
            continue
    print(f"Batches are:")
    print(batches)

    from sklearn.decomposition import PCA, IncrementalPCA
    ipca = IncrementalPCA(n_components=min(200, batch_size))

    print("Fitting!")
    for batch in batches:
        print(f"\tLoading dtags: {dtag_array[batch]}")
        start = time.time()
        results = process_local(
            [
                partial(
                    load_xmap_paramaterised,
                    shell_truncated_datasets[key],
                    alignments[key],
                )
                for key
                in dtag_array[batch]
            ]
        )
        print("Got xmaps!")

        # Get the maps as arrays
        print("Getting xmaps as arrays")
        xmaps = {dtag: xmap
                 for dtag, xmap
                 in zip(dtag_list, results)
                 }

        finish = time.time()
        print(f"Mapped in {finish - start}")

        # Get pca
        xmap_array = np.vstack([xmap for xmap in xmaps.values()])
        ipca.partial_fit(xmap_array)

    # Transform
    print(f"Transforming!")
    transformed_arrays = []
    for batch in batches:
        print(f"\tTransforming dtags: {dtag_array[batch]}")
        start = time.time()
        results = process_local(
            [
                partial(
                    load_xmap_paramaterised,
                    shell_truncated_datasets[key],
                    alignments[key],
                )
                for key
                in dtag_array[batch]
            ]
        )
        print("Got xmaps!")

        # Get the maps as arrays
        print("Getting xmaps as arrays")
        xmaps = {dtag: xmap
                 for dtag, xmap
                 in zip(dtag_list, results)
                 }

        finish = time.time()
        print(f"Mapped in {finish - start}")

        # Get pca
        xmap_array = np.vstack([xmap for xmap in xmaps.values()])
        transformed_arrays.append(ipca.transform(xmap_array))

    reduced_array = np.vstack(transformed_arrays)

    print(f"Reduced array shape: {reduced_array.shape}")

    # Cluster
    distance_matrix, dtag_distance_to_cluster, centermost_cluster, clusters_dict = get_clusters(
        reduced_array,
        dtag_list,
        dtag_array,
        dtag_to_index,
        pandda_fs_model,
    )

    # Get the comparators: for each dataset, get cluster with closest median distance
    comparators = {}
    for j, dtag in enumerate(dtag_list):
        print(f"Finding closest for dtag: {dtag}")
        current_res = datasets[dtag].reflections.resolution().resolution

        # Get dtags ordered by distance
        row = distance_matrix[j, :].flatten()
        print(f"\tRow is: {row}")
        # closest_dtags_indexes = np.flip(np.argsort(row))
        # cluster_distances = {}
        # for cluster_num, cluster_dtags in clusters_dict.items():
        #     distances = row[np.array([dtag_to_index[_dtag] for _dtag in cluster_dtags])]
        #     median_distance = np.median(distances)
        #     print(f"\t\tMedian distance to cluster {cluster_num} is: {median_distance}")
        #     cluster_distances[cluster_num] = median_distance

        # print(f"\tClosest dtags are: {closest_dtags}")
        # print(f"\tdistances are: {np.take_along_axis(row, closest_dtags_indexes, axis=0)}")

        cluster_distances = dtag_distance_to_cluster[dtag]

        if cluster_selection == "close":

            print(cluster_distances)
            closest_cluster = min(cluster_distances, key=lambda x: cluster_distances[x])
            # print(f"\tClosest cluster is: {closest_cluster}")
            # closest_cluster_dtags = clusters_dict[closest_cluster]
            # print(f"\tClosest cluster dtags ate: {closest_cluster_dtags}")

        elif cluster_selection == "center":
            closest_cluster = centermost_cluster
            # closest_cluster_dtags = clusters_dict[closest_cluster]

        elif cluster_selection == "far":
            closest_cluster = max(cluster_distances, key=lambda x: cluster_distances[x])

        elif cluster_selection == "next":
            cluster_distances_sorted = list(sorted(cluster_distances, key=lambda x: cluster_distances[x]))
            if len(cluster_distances) < 2:
                closest_cluster = cluster_distances_sorted[0]
            else:
                closest_cluster = cluster_distances_sorted[1]

        print(f"\tClosest cluster is: {closest_cluster}")
        closest_cluster_dtags = clusters_dict[closest_cluster]
        print(f"\tClosest cluster dtags ate: {closest_cluster_dtags}")

        distances_to_cluster = {_dtag: dtag_distance_to_cluster[_dtag][closest_cluster]
                                for _dtag
                                in dtag_distance_to_cluster}
        dtags_by_distance_to_cluster = [x for x in sorted(distances_to_cluster, key=lambda y: distances_to_cluster[y])]
        print(f"Distances to cluster: {distances_to_cluster}")
        print(f"Dtags by distance to cluster: {dtags_by_distance_to_cluster}")

        # Decide the res upper bound
        truncation_res = max(current_res + resolution_cutoff, highest_res_datasets_max)
        print(f"\tTrucation res is: {truncation_res}")

        # Go down the list of closes datasets seeing if they fall within truncation res and adding them to comparators
        # if so
        potential_comparator_dtags = []
        for j, potential_comparator_dtag in enumerate(dtags_by_distance_to_cluster):

            if datasets[dtag].reflections.resolution().resolution < truncation_res:
                potential_comparator_dtags.append(potential_comparator_dtag)
            else:
                continue

            # of enough accuulated, continue
            if len(potential_comparator_dtags) > comparison_min_comparators:
                comparators[dtag] = potential_comparator_dtags
                break

        if len(potential_comparator_dtags) < comparison_min_comparators:
            raise Exception(
                (
                    f"Dtag {dtag} has too few comparators: "
                    f"only {len(potential_comparator_dtags)}:"
                    f" {potential_comparator_dtags}"
                )
            )

    return comparators


def get_comparators_closest_cluster_neighbours(
        datasets: Dict[Dtag, Dataset],
        alignments,
        grid,
        comparison_min_comparators,
        comparison_max_comparators,
        structure_factors,
        sample_rate,
        resolution_cutoff,
        pandda_fs_model: PanDDAFSModel,
        process_local,
        batch=False,
        cluster_selection="close"
):
    dtag_list = [dtag for dtag in datasets]
    dtag_array = np.array(dtag_list)
    dtag_to_index = {dtag: j for j, dtag in enumerate(dtag_list)}

    dtags_by_res = list(
        sorted(
            dtag_list,
            key=lambda dtag: datasets[dtag].reflections.resolution().resolution,
        )
    )

    highest_res_datasets = dtags_by_res[:comparison_min_comparators + 1]
    highest_res_datasets_max = max(
        [datasets[dtag].reflections.resolution().resolution for dtag in highest_res_datasets])

    # Load the xmaps
    print("Truncating datasets...")
    shell_truncated_datasets: Datasets = truncate(
        datasets,
        resolution=Resolution(highest_res_datasets_max),
        structure_factors=structure_factors,
    )

    # Generate aligned xmaps
    print("Loading xmaps")

    load_xmap_paramaterised = partial(
        from_unaligned_dataset_c_flat,
        grid=grid,
        structure_factors=structure_factors,
        sample_rate=sample_rate,
    )

    # Get reduced array
    total_sample_size = len(shell_truncated_datasets)
    print(f"Total sample size = {total_sample_size}")
    batch_size = min(90, total_sample_size)
    print(f"Batch size is: {batch_size}")
    num_batches = (total_sample_size // batch_size) + 1
    print(f"Num batches is: {num_batches}")
    # batches = [
    #     np.arange(x*batch_size, min((x+1)*batch_size, total_sample_size))
    #     for x
    #     in range(0, num_batches)]
    tmp_batches = {}
    j = 1
    while True:
        print(f"\tJ is: {j}")
        new_batches = np.array_split(np.arange(total_sample_size), j)
        print(f"\t\tlen of new batches is {len(new_batches)}")
        tmp_batches[j] = new_batches
        j = j + 1

        if any(len(batch) < batch_size for batch in new_batches):
            batches = tmp_batches[j - 2]
            break
        else:
            print("\t\tAll batches larger than batch size, trying smaller split!")
            continue
    print(f"Batches are:")
    print(batches)

    from sklearn.decomposition import PCA, IncrementalPCA
    ipca = IncrementalPCA(n_components=min(200, batch_size))

    print("Fitting!")
    for batch in batches:
        print(f"\tLoading dtags: {dtag_array[batch]}")
        start = time.time()
        results = process_local(
            [
                partial(
                    load_xmap_paramaterised,
                    shell_truncated_datasets[key],
                    alignments[key],
                )
                for key
                in dtag_array[batch]
            ]
        )
        print("Got xmaps!")

        # Get the maps as arrays
        print("Getting xmaps as arrays")
        xmaps = {dtag: xmap
                 for dtag, xmap
                 in zip(dtag_list, results)
                 }

        finish = time.time()
        print(f"Mapped in {finish - start}")

        # Get pca
        xmap_array = np.vstack([xmap for xmap in xmaps.values()])
        ipca.partial_fit(xmap_array)

    # Transform
    print(f"Transforming!")
    transformed_arrays = []
    for batch in batches:
        print(f"\tTransforming dtags: {dtag_array[batch]}")
        start = time.time()
        results = process_local(
            [
                partial(
                    load_xmap_paramaterised,
                    shell_truncated_datasets[key],
                    alignments[key],
                )
                for key
                in dtag_array[batch]
            ]
        )
        print("Got xmaps!")

        # Get the maps as arrays
        print("Getting xmaps as arrays")
        xmaps = {dtag: xmap
                 for dtag, xmap
                 in zip(dtag_list, results)
                 }

        finish = time.time()
        print(f"Mapped in {finish - start}")

        # Get pca
        xmap_array = np.vstack([xmap for xmap in xmaps.values()])
        transformed_arrays.append(ipca.transform(xmap_array))

    reduced_array = np.vstack(transformed_arrays)

    print(f"Reduced array shape: {reduced_array.shape}")

    # clusterer = hdbscan.HDBSCAN(
    #     min_cluster_size=30,
    #     min_samples=1,
    #     cluster_selection_method="leaf",
    # )
    # clusterer.fit(reduced_array)
    # labels = clusterer.labels_
    # print(f"Labels are: {labels}")
    # probabilities = clusterer.probabilities_
    #
    # # Plot cluster results
    # fig, ax = plt.subplots()
    #
    # clusterer.condensed_tree_.plot(
    #     select_clusters=True,
    #     axis=ax,
    # )
    #
    # fig.savefig(str(pandda_fs_model.pandda_dir / f"hdbscan_condensed_tree.png"))
    # fig.clear()
    # plt.close(fig)
    #
    # # Plot cluster results
    # fig, ax = plt.subplots()
    #
    # clusterer.single_linkage_tree_.plot(
    #     axis=ax,
    # )
    #
    # fig.savefig(str(pandda_fs_model.pandda_dir / f"hdbscan_single_linkage_tree.png"))
    # fig.clear()
    # plt.close(fig)

    # # Plot cluster results
    # fig, ax = plt.subplots()
    #
    # clusterer.minimum_spanning_tree_.plot(
    #     axis=ax,
    # )
    #
    # fig.savefig(str(pandda_fs_model.pandda_dir / f"hdbscan_minimum_spanning_tree.png"))
    # fig.clear()
    # plt.close(fig)

    linkage = get_linkage_from_observations(reduced_array)

    save_dendrogram_plot(linkage,
                         [_dtag.dtag for _dtag in dtag_list],
                         str(pandda_fs_model.pandda_dir / f"dendrogram.png"),
                         # threshold=0.3,
                         )

    rootnode, nodelist = spc.hierarchy.to_tree(linkage, rd=True)

    def recurse_node(node, min_samples):

        _clusters = []
        # if node.count > min_samples:
        if node.left.count >= min_samples:
            left_clusters = recurse_node(node.left, min_samples)
            for left_cluster in left_clusters:
                _clusters.append(left_cluster)

        if node.right.count >= min_samples:
            right_clusters = recurse_node(node.right, min_samples)
            for right_cluster in right_clusters:
                _clusters.append(right_cluster)

        if node.count >= min_samples:
            if (node.right.count < min_samples) and (node.left.count < min_samples):
                _clusters.append(node.pre_order(lambda x: x.id))

        return _clusters

    clusters = recurse_node(rootnode, 30)
    print(clusters)

    clusters_dict = {}
    dtag_to_cluster = {}
    for j, cluster in enumerate(clusters):
        clusters_dict[j] = dtag_array[np.array(cluster)]
        for dtag in clusters_dict[j]:
            dtag_to_cluster[dtag] = j
    print(clusters_dict)

    save_dendrogram_plot(linkage,
                         [
                             f"{_dtag.dtag}_{dtag_to_cluster[_dtag]}" if _dtag in dtag_to_cluster else _dtag.dtag
                             for _dtag
                             in dtag_list
                         ],
                         str(pandda_fs_model.pandda_dir / f"dendrogram_with_clusters.png"),
                         # threshold=0.3,
                         )

    #
    #
    # # Get the cores of each cluster
    # cluster_cores = {}
    # for n in np.unique(labels):
    #     if n != -1:
    #         indexes = np.arange(len(labels))
    #         cluster_member_mask = labels == n
    #         cluster_member_indexes = np.nonzero(cluster_member_mask)
    #         cluster_member_values = probabilities[cluster_member_mask]
    #         cluster_members_sorted_indexes = np.argsort(cluster_member_values)
    #
    #         if np.sum(cluster_member_indexes) >= 30:
    #             cluster_cores[n] = cluster_member_indexes[cluster_member_mask][cluster_members_sorted_indexes][:30]
    #
    #         else:
    #             print(f"There were less than 30 members of the cluster!")
    #
    # print(f"Cluster cores are:")
    # print(cluster_cores)

    # Save a bokeh plot
    labels = [dtag.dtag for dtag in dtag_list]
    # known_apos = [dtag.dtag for dtag, dataset in datasets.items() if any(dtag in x for x in cluster_cores.values())]
    known_apos = []
    for cluster_num, cluster_dtags in clusters_dict.items():
        print(f"\tCluster {cluster_num} dtags are {cluster_dtags}")
        for cluster_core_dtag in cluster_dtags:
            known_apos.append(cluster_core_dtag.dtag)

    print(f"Labels are: {labels}")
    print(f"Known apos are: {known_apos}")

    save_plot_pca_umap_bokeh(
        reduced_array,
        labels,
        known_apos,
        pandda_fs_model.pandda_dir / f"pca_umap.html",
    )

    #
    cophenetic_matrix = spsp.distance.squareform(spc.hierarchy.cophenet(linkage))
    dtag_distance_to_cluster = {}
    for _dtag in dtag_list:
        dtag_index = dtag_to_index[_dtag]
        dtag_distance_to_cluster[_dtag] = {}
        dtag_coord = reduced_array[dtag_index, :]
        for cluster, cluster_dtags in clusters_dict.items():
            cluster_indexes = np.array([dtag_to_index[_cluster_dtag] for _cluster_dtag in cluster_dtags])
            cluster_coords = reduced_array[cluster_indexes, :]

            cluster_squared_vectors = np.sqrt(np.sum(np.square(cluster_coords - dtag_coord), axis=1))

            median_squared_distance = np.median(cluster_squared_vectors)

            dtag_distance_to_cluster[_dtag][cluster] = median_squared_distance

    cluster_widths = {}
    for cluster, cluster_dtags in clusters_dict.items():
        cluster_indexes = np.array([dtag_to_index[cluster_dtag] for cluster_dtag in cluster_dtags])
        cluster_coords = reduced_array[cluster_indexes, :]
        cluster_median = np.median(cluster_coords, axis=0).reshape((1, cluster_coords.shape[1]))
        cluster_median_deviation = np.median(np.sqrt(np.sum(np.square(cluster_coords - cluster_median), axis=1)))
        cluster_widths[cluster] = cluster_median_deviation

    print(f"Cluster median absolute deviation is: {cluster_widths}")

    # Get the centermost cluster
    cluster_medians = {}
    for cluster, cluster_dtags in clusters_dict.items():
        cluster_indexes = np.array([dtag_to_index[cluster_dtag] for cluster_dtag in cluster_dtags])
        cluster_coords = reduced_array[cluster_indexes, :]
        cluster_median = np.median(cluster_coords, axis=0).reshape((1, cluster_coords.shape[1]))
        cluster_medians[cluster] = cluster_median
    print(f"Cluster medians are: {cluster_medians}")

    median_of_medians = np.median(np.vstack([x for x in cluster_medians.values()]), axis=0).reshape(1,
                                                                                                    cluster_coords.shape[
                                                                                                        1])
    print(f"Global median of clusters is: {median_of_medians}")

    centermost_cluster = min(
        cluster_medians,
        key=lambda _cluster_num: np.sqrt(np.sum(np.square((median_of_medians - cluster_medians[_cluster_num])))),
    )
    print(f"Centermost cluster is: {centermost_cluster}")

    # Get the comparators: for each dataset, get cluster with closest median distance
    comparators = {}
    for j, dtag in enumerate(dtag_list):
        print(f"Finding closest for dtag: {dtag}")
        current_res = datasets[dtag].reflections.resolution().resolution

        # Get dtags ordered by distance
        row = cophenetic_matrix[j, :].flatten()
        print(f"\tRow is: {row}")
        # closest_dtags_indexes = np.flip(np.argsort(row))
        # cluster_distances = {}
        # for cluster_num, cluster_dtags in clusters_dict.items():
        #     distances = row[np.array([dtag_to_index[_dtag] for _dtag in cluster_dtags])]
        #     median_distance = np.median(distances)
        #     print(f"\t\tMedian distance to cluster {cluster_num} is: {median_distance}")
        #     cluster_distances[cluster_num] = median_distance

        # print(f"\tClosest dtags are: {closest_dtags}")
        # print(f"\tdistances are: {np.take_along_axis(row, closest_dtags_indexes, axis=0)}")

        cluster_distances = dtag_distance_to_cluster[dtag]

        if cluster_selection == "close":

            print(cluster_distances)
            closest_cluster = min(cluster_distances, key=lambda x: cluster_distances[x])
            # print(f"\tClosest cluster is: {closest_cluster}")
            # closest_cluster_dtags = clusters_dict[closest_cluster]
            # print(f"\tClosest cluster dtags ate: {closest_cluster_dtags}")

        elif cluster_selection == "center":
            closest_cluster = centermost_cluster
            # closest_cluster_dtags = clusters_dict[closest_cluster]

        elif cluster_selection == "far":
            closest_cluster = max(cluster_distances, key=lambda x: cluster_distances[x])

        elif cluster_selection == "next":
            cluster_distances_sorted = list(sorted(cluster_distances, key=lambda x: cluster_distances[x]))
            if len(cluster_distances) < 2:
                closest_cluster = cluster_distances_sorted[0]
            else:
                closest_cluster = cluster_distances_sorted[1]

        print(f"\tClosest cluster is: {closest_cluster}")
        closest_cluster_dtags = clusters_dict[closest_cluster]
        print(f"\tClosest cluster dtags ate: {closest_cluster_dtags}")

        distances_to_cluster = {_dtag: dtag_distance_to_cluster[_dtag][closest_cluster]
                                for _dtag
                                in dtag_distance_to_cluster}
        dtags_by_distance_to_cluster = [x for x in sorted(distances_to_cluster, key=lambda y: distances_to_cluster[y])]
        print(f"Distances to cluster: {distances_to_cluster}")
        print(f"Dtags by distance to cluster: {dtags_by_distance_to_cluster}")

        # Decide the res upper bound
        truncation_res = max(current_res + resolution_cutoff, highest_res_datasets_max)
        print(f"\tTrucation res is: {truncation_res}")

        # Go down the list of closes datasets seeing if they fall within truncation res and adding them to comparators
        # if so
        potential_comparator_dtags = []
        for j, potential_comparator_dtag in enumerate(dtags_by_distance_to_cluster):

            if datasets[dtag].reflections.resolution().resolution < truncation_res:
                potential_comparator_dtags.append(potential_comparator_dtag)
            else:
                continue

            # of enough accuulated, continue
            if len(potential_comparator_dtags) > comparison_min_comparators:
                comparators[dtag] = potential_comparator_dtags
                break

        if len(potential_comparator_dtags) < comparison_min_comparators:
            raise Exception(
                (
                    f"Dtag {dtag} has too few comparators: "
                    f"only {len(potential_comparator_dtags)}:"
                    f" {potential_comparator_dtags}"
                )
            )

    return comparators


def get_shells(
        datasets: Dict[Dtag, Dataset],
        comparators: Dict[Dtag, List[Dtag]],
        min_characterisation_datasets,
        max_shell_datasets,
        high_res_increment,
):
    # For each dataset + set of comparators, include all of these to be loaded in the set of the shell of their highest
    # Common reoslution

    # Get the dictionary of resolutions for convenience
    resolutions = {dtag: datasets[dtag].reflections.resolution().resolution for dtag in datasets}

    # Get the shells: start with the highest res dataset and count up in increments of high_res_increment to the
    # Lowest res dataset
    reses = np.arange(min(resolutions.values()), max(resolutions.values()), high_res_increment)
    shells_test = {res: set() for res in reses}
    shells_train = {res: {} for res in reses}

    # Iterate over comparators, getting the resolution range, the lowest res in it, and then including all
    # in the set of the first shell of sufficiently low res

    for dtag, comparison_dtags in comparators.items():
        low_res = max([resolutions[comparison_dtag] for comparison_dtag in comparison_dtags])

        # Find the first shell whose res is higher
        for res in reses:
            if res > low_res:
                shells_test[res] = shells_test[res].union({dtag, })
                shells_train[res][dtag] = set(comparison_dtags)

                # Make sure they only appear in one shell
                break

    # Create shells
    shells = {}
    for j, res in enumerate(reses):

        # Collect a set of all dtags
        all_dtags = set()

        # Add all the test dtags
        for dtag in shells_test[res]:
            all_dtags = all_dtags.union({dtag, })

        # Add all the train dtags
        for test_dtag, train_dtags in shells_train[res].items():
            all_dtags = all_dtags.union(train_dtags)

        # Create the shell
        shell = Shell(
            res,
            shells_test[res],
            shells_train[res],
            all_dtags,
        )
        shells[res] = shell

    # Delete any shells that are empty
    shells_to_delete = []
    for res in reses:
        if len(shells_test[res]) == 0 or len(shells_train[res]) == 0:
            shells_to_delete.append(res)

    for res in shells_to_delete:
        del shells[res]

    return shells


def truncate(datasets: Dict[Dtag, Dataset], resolution: Resolution, structure_factors: StructureFactors):
    new_datasets_resolution = {}

    # Truncate by common resolution
    for dtag in datasets:
        truncated_dataset = datasets[dtag].truncate_resolution(resolution, )

        new_datasets_resolution[dtag] = truncated_dataset

    dataset_resolution_truncated = Datasets(new_datasets_resolution)

    # Get common set of reflections
    common_reflections = dataset_resolution_truncated.common_reflections(structure_factors)

    # truncate on reflections
    new_datasets_reflections = {}
    for dtag in dataset_resolution_truncated:
        reflections = dataset_resolution_truncated[dtag].reflections.reflections
        reflections_array = np.array(reflections)
        print(f"{dtag}")
        print(f"{reflections_array.shape}")

        truncated_dataset = dataset_resolution_truncated[dtag].truncate_reflections(common_reflections,
                                                                                    )
        reflections = truncated_dataset.reflections.reflections
        reflections_array = np.array(reflections)
        print(f"{dtag}: {reflections_array.shape}")

        new_datasets_reflections[dtag] = truncated_dataset

    return new_datasets_reflections


def validate_strategy_num_datasets(datasets, min_characterisation_datasets=30):
    if len(datasets) < min_characterisation_datasets:
        return False
    else:
        return True


def validate(datasets: Dict[Dtag, Dataset], strategy=None, exception=None):
    if not strategy(datasets):
        print(datasets)
        raise exception


def get_common_structure_factors(datasets: Dict[Dtag, Dataset]):
    for dtag in datasets:
        dataset = datasets[dtag]
        reflections = dataset.reflections
        column_labels = reflections.columns()
        for common_f_phi_label_pair in constants.COMMON_F_PHI_LABEL_PAIRS:

            f_label = common_f_phi_label_pair[0]
            if f_label in column_labels:
                return StructureFactors(common_f_phi_label_pair[0], common_f_phi_label_pair[1])

    # If couldn't find common names in any dataset return None
    return None
