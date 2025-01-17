import numpy as np
from sklearn import decomposition, metrics
import umap
from bokeh.plotting import ColumnDataSource, figure, output_file, show, save
from bokeh.palettes import magma, viridis, Category20, Category10, Set1

def embed_umap(reduced_array):
    distance_matrix = metrics.pairwise_distances(reduced_array)
    pca = decomposition.PCA(n_components=min(distance_matrix.shape[0], 50))
    reducer = umap.UMAP()
    transform = pca.fit_transform(distance_matrix)
    transform = reducer.fit_transform(transform)
    return transform


def bokeh_scatter_plot(embedding, labels, known_apos, plot_file):
    output_file(str(plot_file))

    annotations = []
    colours = []
    unique_annotations = np.unique(known_apos)
    colour_map = {annotation: j for j, annotation in enumerate(unique_annotations)}
    num_unique_annotations = len(unique_annotations)

    # palette = magma(num_unique_annotations)
    if num_unique_annotations <= 2:
        palette = Set1[num_unique_annotations]
    elif num_unique_annotations <= 10:
        palette = Category10[num_unique_annotations]
    elif num_unique_annotations <= 20:
        palette = Category20[num_unique_annotations]
    else:
        palette = viridis(num_unique_annotations)

    if known_apos[0] == -1:
        palette.insert(0,'#e6e6e6')

    for annotation in known_apos:
            annotations.append(annotation)
            colours.append(palette[colour_map[annotation]])

    # for label in labels:

        # if label in known_apos:
        #     apos.append("green")
        # else:
        #     apos.append("pink")


    source = ColumnDataSource(
        data=dict(
            x=embedding[:, 0].tolist(),
            y=embedding[:, 1].tolist(),
            dtag=labels,
            annotation=annotations,
            colour=colours,
        ))

    TOOLTIPS = [
        ("index", "$index"),
        ("(x,y)", "($x, $y)"),
        ("dtag", "@dtag"),
        ("annotation", "@annotation"),
        ("colour", "@colour")
    ]

    p = figure(plot_width=1200, plot_height=1200, tooltips=TOOLTIPS,
               title="Mouse over the dots",
               )

    p.circle('x', 'y', size=15, source=source, color="colour")

    save(p)


def save_plot_pca_umap_bokeh(dataset_connectivity_matrix, labels, known_apos, plot_file):
    embedding = embed_umap(dataset_connectivity_matrix)
    bokeh_scatter_plot(embedding, labels, known_apos, plot_file)
