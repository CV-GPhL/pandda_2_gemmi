from __future__ import annotations

import typing
import dataclasses

import os
import shutil
from pathlib import Path

from joblib.externals.loky import set_loky_pickler

set_loky_pickler('pickle')

from typing import *
from functools import partial

from pandda_gemmi.constants import *
from pandda_gemmi.python_types import *
from pandda_gemmi.common import Dtag, EventIDX
# from pandda_gemmi.dataset import (StructureFactors, Dataset, )
from pandda_gemmi.shells import Shell
from pandda_gemmi.edalignment import Alignment, Grid, Partitioning, Xmap
from pandda_gemmi.model import Zmap, Model
from pandda_gemmi.event import Event


@dataclasses.dataclass()
class SiteTableFile:
    ...
    # @staticmethod
    # def from_events(events: Events):
    #     pass
    #


@dataclasses.dataclass()
class EventTableFile:
    ...
    # @staticmethod
    # def from_events(events: Events):
    #     pass


@dataclasses.dataclass()
class Analyses:
    analyses_dir: Path
    pandda_analyse_events_file: Path
    pandda_analyse_sites_file: Path
    pandda_html_summaries_dir: Path

    @staticmethod
    def from_pandda_dir(pandda_dir: Path):
        analyses_dir = pandda_dir / PANDDA_ANALYSES_DIR
        pandda_analyse_events_file = analyses_dir / PANDDA_ANALYSE_EVENTS_FILE
        pandda_analyse_sites_file = analyses_dir / PANDDA_ANALYSE_SITES_FILE
        pandda_html_summaries_dir = analyses_dir / PANDDA_HTML_SUMMARIES_DIR

        return Analyses(analyses_dir=analyses_dir,
                        pandda_analyse_events_file=pandda_analyse_events_file,
                        pandda_analyse_sites_file=pandda_analyse_sites_file,
                        pandda_html_summaries_dir=pandda_html_summaries_dir,
                        )

    def build(self):
        if not self.analyses_dir.exists():
            os.mkdir(str(self.analyses_dir))
        if not self.pandda_html_summaries_dir.exists():
            os.mkdir(str(self.pandda_html_summaries_dir))


@dataclasses.dataclass()
class DatasetModels:
    path: Path

    @staticmethod
    def from_dir(path: Path):
        return DatasetModels(path=path)


@dataclasses.dataclass()
class LigandDir:
    path: Path
    pdbs: typing.List[Path]
    cifs: typing.List[Path]
    smiles: typing.List[Path]

    @staticmethod
    def from_path(path: Path):
        pdbs = list(path.glob("*.pdb"))
        cifs = list(path.glob("*.cifs"))
        smiles = list(path.glob("*.smiles"))

        return LigandDir(path,
                         pdbs,
                         cifs,
                         smiles,
                         )


@dataclasses.dataclass()
class DatasetDir:
    path: Path
    input_pdb_file: Path
    input_mtz_file: Path
    ligand_dir: Union[LigandDir, None]
    source_ligand_cif: Union[Path, None]
    source_ligand_pdb: Union[Path, None]
    source_ligand_smiles: Optional[Path]

    @staticmethod
    def from_path(path: Path, pdb_regex: str, mtz_regex: str,
                  ligand_dir_name: str,
                  ligand_cif_regex: str, ligand_pdb_regex: str,
                  ligand_smiles_regex: str):

        try:
            input_pdb_file: Path = next(path.glob(pdb_regex))
            input_mtz_file: Path = next(path.glob(mtz_regex))

            source_ligand_dir = path / ligand_dir_name

            if source_ligand_dir.exists():
                ligand_dir = LigandDir.from_path(source_ligand_dir)
                ligand_search_path = source_ligand_dir
            else:
                ligand_dir = None
                ligand_search_path = path

            try:
                ligands = ligand_search_path.rglob(ligand_cif_regex)
                source_ligand_cif = next(ligands)
            except:
                source_ligand_cif = None

            try:
                source_ligand_smiles = next(ligand_search_path.rglob(ligand_smiles_regex))
            except:
                source_ligand_smiles = None

            try:
                ligands = ligand_search_path.rglob(ligand_pdb_regex)

                if source_ligand_cif:
                    stem = source_ligand_cif.stem

                elif source_ligand_smiles:
                    stem = source_ligand_smiles.stem

                else:
                    stem = None

                source_ligand_pdb = None
                if stem:
                    for ligand_path in ligands:
                        if ligand_path.stem == stem:
                            source_ligand_pdb = ligand_path

            except:
                source_ligand_pdb = None

            return DatasetDir(
                path=path,
                input_pdb_file=input_pdb_file,
                input_mtz_file=input_mtz_file,
                ligand_dir=ligand_dir,
                source_ligand_cif=source_ligand_cif,
                source_ligand_pdb=source_ligand_pdb,
                source_ligand_smiles=source_ligand_smiles
            )
        except:
            return None


@dataclasses.dataclass()
class DataDirs:
    dataset_dirs: typing.Dict[Dtag, DatasetDir]

    @staticmethod
    def from_dir(directory: Path, pdb_regex: str, mtz_regex: str, ligand_dir_name, ligand_cif_regex: str,
                 ligand_pdb_regex: str,
                 ligand_smiles_regex: str, process_local=None):

        # ensure the directory/path given here ends in "/":
        dataset_dir_paths = list(directory.joinpath('').glob("*"))

        dataset_dirs = {}

        if process_local:
            dtags = []
            for dataset_dir_path in dataset_dir_paths:
                dtag = Dtag(dataset_dir_path.name)
                dtags.append(dtag)

            results = process_local(
                [
                    partial(DatasetDir.from_path,
                            dataset_dir_path, pdb_regex, mtz_regex, ligand_dir_name,
                            ligand_cif_regex,
                            ligand_pdb_regex, ligand_smiles_regex)
                    for dataset_dir_path
                    in dataset_dir_paths
                ]
            )

            for dtag, result in zip(dtags, results):
                if result:
                    dataset_dirs[dtag] = result

        else:

            for dataset_dir_path in dataset_dir_paths:
                dtag = Dtag(dataset_dir_path.name)

                dataset_dir = DatasetDir.from_path(dataset_dir_path, pdb_regex, mtz_regex, ligand_dir_name,
                                                   ligand_cif_regex,
                                                   ligand_pdb_regex, ligand_smiles_regex)
                if dataset_dir:
                    dataset_dirs[dtag] = dataset_dir

        return DataDirs(dataset_dirs)

    def to_dict(self):
        return self.dataset_dirs


@dataclasses.dataclass()
class ZMapFile:
    path: Path

    @staticmethod
    def from_zmap(zmap: Zmap):
        pass

    @staticmethod
    def from_dir(path: Path, dtag: str):
        return ZMapFile(path / PANDDA_Z_MAP_FILE.format(dtag=dtag))

    def save_reference_frame_zmap(self, zmap: Zmap):
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = zmap.zmap
        ccp4.update_ccp4_header(2, True)
        ccp4.grid.symmetrize_max()
        ccp4.write_ccp4_map(str(self.path))


@dataclasses.dataclass()
class MeanMapFile:
    path: Path

    @staticmethod
    def from_zmap_file(zmap: ZMapFile):
        return MeanMapFile(zmap.path.parent / "mean.ccp4")

    @staticmethod
    def from_dir(path: Path, dtag: str):
        return ZMapFile(path / PANDDA_Z_MAP_FILE.format(dtag=dtag))

    def save_reference_frame_zmap(self, zmap: Zmap):
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = zmap.zmap
        ccp4.update_ccp4_header(2, True)
        ccp4.grid.symmetrize_max()
        ccp4.write_ccp4_map(str(self.path))


@dataclasses.dataclass()
class StdMapFile:
    path: Path

    @staticmethod
    def from_zmap_file(zmap: ZMapFile):
        return StdMapFile(zmap.path.parent / "std.ccp4")

    @staticmethod
    def from_dir(path: Path, dtag: str):
        return ZMapFile(path / PANDDA_Z_MAP_FILE.format(dtag=dtag))

    def save_reference_frame_zmap(self, zmap: Zmap):
        ccp4 = gemmi.Ccp4Map()
        ccp4.grid = zmap.zmap
        ccp4.update_ccp4_header(2, True)
        ccp4.grid.symmetrize_max()
        ccp4.write_ccp4_map(str(self.path))


@dataclasses.dataclass()
class EventMapFile:
    path: Path

    @staticmethod
    def from_event(event: Event, path: Path):
        rounded_bdc = round(1 - event.bdc.bdc, 2)
        event_map_path = path / PANDDA_EVENT_MAP_FILE.format(dtag=event.event_id.dtag.dtag,
                                                             event_idx=event.event_id.event_idx.event_idx,
                                                             bdc=rounded_bdc,
                                                             )
        return EventMapFile(event_map_path)


@dataclasses.dataclass()
class EventMapFiles:
    path: Path
    event_map_files: typing.Dict[EventIDX, EventMapFile]

    # @staticmethod
    # def from_events(events: Events, xmaps: Xmaps):
    #     pass

    @staticmethod
    def from_dir(dir: Path):
        return EventMapFiles(dir, {})

    def get_events(self, events: typing.Dict[EventIDX, Event]):
        event_map_files = {}
        for event_idx in events:
            event_map_files[event_idx] = EventMapFile.from_event(events[event_idx], self.path)

        self.event_map_files = event_map_files

    def add_event(self, event: Event):
        self.event_map_files[event.event_id.event_idx] = EventMapFile.from_event(event, self.path)

    def __iter__(self):
        for event_idx in self.event_map_files:
            yield event_idx

    def __getitem__(self, item):
        return self.event_map_files[item]


@dataclasses.dataclass()
class ProcessedDataset:
    path: Path
    dataset_models: DatasetModels
    input_mtz: Path
    input_pdb: Path
    source_mtz: Path
    source_pdb: Path
    z_map_file: ZMapFile
    event_map_files: EventMapFiles
    source_ligand_cif: Union[Path, None]
    source_ligand_pdb: Union[Path, None]
    source_ligand_smiles: Optional[Path]
    input_ligand_cif: Path
    input_ligand_pdb: Path
    input_ligand_smiles: Path
    source_ligand_dir: Union[LigandDir, None]
    input_ligand_dir: Path
    log_path: Path

    @staticmethod
    def from_dataset_dir(dataset_dir: DatasetDir, processed_dataset_dir: Path) -> ProcessedDataset:
        dataset_models_dir = processed_dataset_dir / PANDDA_MODELLED_STRUCTURES_DIR

        # Copy the input pdb and mtz
        dtag = processed_dataset_dir.name
        source_mtz = dataset_dir.input_mtz_file
        source_pdb = dataset_dir.input_pdb_file
        source_ligand_cif = dataset_dir.source_ligand_cif
        source_ligand_pdb = dataset_dir.source_ligand_pdb
        source_ligand_smiles = dataset_dir.source_ligand_smiles

        input_mtz = processed_dataset_dir / PANDDA_MTZ_FILE.format(dtag)
        input_pdb = processed_dataset_dir / PANDDA_PDB_FILE.format(dtag)
        input_ligand_cif = processed_dataset_dir / PANDDA_LIGAND_CIF_FILE
        input_ligand_pdb = processed_dataset_dir / PANDDA_LIGAND_PDB_FILE
        input_ligand_smiles = processed_dataset_dir / PANDDA_LIGAND_SMILES_FILE

        z_map_file = ZMapFile.from_dir(processed_dataset_dir, processed_dataset_dir.name)
        event_map_files = EventMapFiles.from_dir(processed_dataset_dir)

        source_ligand_dir = dataset_dir.ligand_dir
        input_ligand_dir = processed_dataset_dir / PANDDA_LIGAND_FILES_DIR

        log_path = processed_dataset_dir / "log.json"

        return ProcessedDataset(
            path=processed_dataset_dir,
            dataset_models=DatasetModels.from_dir(dataset_models_dir),
            input_mtz=input_mtz,
            input_pdb=input_pdb,
            source_mtz=source_mtz,
            source_pdb=source_pdb,
            z_map_file=z_map_file,
            event_map_files=event_map_files,
            source_ligand_cif=source_ligand_cif,
            source_ligand_pdb=source_ligand_pdb,
            source_ligand_smiles=source_ligand_smiles,
            input_ligand_cif=input_ligand_cif,
            input_ligand_pdb=input_ligand_pdb,
            input_ligand_smiles=input_ligand_smiles,
            source_ligand_dir=source_ligand_dir,
            input_ligand_dir=input_ligand_dir,
            log_path=log_path,
        )

    def build(self):
        if not self.path.exists():
            os.mkdir(str(self.path))

        shutil.copyfile(self.source_mtz, self.input_mtz)
        shutil.copyfile(self.source_pdb, self.input_pdb)

        if self.source_ligand_cif: shutil.copyfile(self.source_ligand_cif, self.input_ligand_cif)
        if self.source_ligand_pdb: shutil.copyfile(self.source_ligand_pdb, self.input_ligand_pdb)
        if self.source_ligand_smiles: shutil.copyfile(self.source_ligand_smiles, self.input_ligand_smiles)

        input_ligand_dir_path = self.input_ligand_dir
        if not input_ligand_dir_path.exists():
            if self.source_ligand_dir:
                shutil.copytree(str(self.source_ligand_dir.path),
                                str(self.input_ligand_dir),
                                )

        dataset_models_path = self.dataset_models.path
        if not dataset_models_path.exists():
            os.mkdir(str(self.dataset_models.path))


@dataclasses.dataclass()
class ProcessedDatasets:
    path: Path
    processed_datasets: typing.Dict[Dtag, ProcessedDataset]

    @staticmethod
    def from_data_dirs(data_dirs: DataDirs, processed_datasets_dir: Path, process_local=None):
        processed_datasets = {}

        if process_local:
            results = process_local(
                [
                    partial(
                        ProcessedDataset.from_dataset_dir,
                        dataset_dir,
                        processed_datasets_dir / dtag.dtag,
                    )
                    for dtag, dataset_dir
                    in data_dirs.dataset_dirs.items()
                ]
            )

            processed_datasets = {dtag: result for dtag, result in zip(data_dirs.dataset_dirs, results, )}

        else:
            for dtag, dataset_dir in data_dirs.dataset_dirs.items():
                processed_datasets[dtag] = ProcessedDataset.from_dataset_dir(dataset_dir,
                                                                             processed_datasets_dir / dtag.dtag,
                                                                             )

        return ProcessedDatasets(processed_datasets_dir,
                                 processed_datasets)

    def __getitem__(self, item):
        return self.processed_datasets[item]

    def __iter__(self):
        for dtag in self.processed_datasets:
            yield dtag

    def build(self, process_local=None):
        if not self.path.exists():
            os.mkdir(str(self.path))

        if process_local:
            process_local(
                [
                    self.processed_datasets[dtag].build
                    for dtag
                    in self.processed_datasets
                ]
            )

        else:

            for dtag in self.processed_datasets:
                self.processed_datasets[dtag].build()


@dataclasses.dataclass()
class ShellDir:
    path: Path
    log_path: Path

    @staticmethod
    def from_shell(shells_dir, shell_res):
        shell_dir = shells_dir / str(shell_res)
        log_path = shell_dir / "log.json"
        return ShellDir(shell_dir, log_path)

    def build(self):
        if not self.path.exists():
            os.mkdir(self.path)


@dataclasses.dataclass()
class ShellDirs:
    path: Path
    shell_dirs: Dict[float, ShellDir]

    @staticmethod
    def from_pandda_dir(pandda_dir: Path, shells: Dict[float, Shell]):

        shells_dir = pandda_dir / PANDDA_SHELL_DIR

        shell_dirs = {}
        for shell_res, shell in shells.items():
            shell_dirs[shell_res] = ShellDir.from_shell(shells_dir, shell_res)

        return ShellDirs(shells_dir, shell_dirs)

    def build(self):
        if not self.path.exists():
            os.mkdir(self.path)

        for shell_res, shell_dir in self.shell_dirs.items():
            shell_dir.build()


@dataclasses.dataclass()
class PanDDAFSModel:
    pandda_dir: Path
    data_dirs: DataDirs
    analyses: Analyses
    processed_datasets: ProcessedDatasets
    log_file: Path
    shell_dirs: Optional[ShellDirs]

    @staticmethod
    def from_dir(input_data_dirs: Path,
                 output_out_dir: Path,
                 pdb_regex: str, mtz_regex: str,
                 ligand_dir_name, ligand_cif_regex: str, ligand_pdb_regex: str, ligand_smiles_regex: str,
                 process_local=None,
                 ):
        analyses = Analyses.from_pandda_dir(output_out_dir)
        data_dirs = DataDirs.from_dir(input_data_dirs, pdb_regex, mtz_regex, ligand_dir_name, ligand_cif_regex,
                                      ligand_pdb_regex, ligand_smiles_regex, process_local=process_local)
        processed_datasets = ProcessedDatasets.from_data_dirs(data_dirs,
                                                              output_out_dir / PANDDA_PROCESSED_DATASETS_DIR,
                                                              process_local=process_local,
                                                              )
        log_path = output_out_dir / PANDDA_LOG_FILE

        return PanDDAFSModel(pandda_dir=output_out_dir,
                             data_dirs=data_dirs,
                             analyses=analyses,
                             processed_datasets=processed_datasets,
                             log_file=log_path,
                             shell_dirs=None,
                             )

    def build(self, overwrite=False, process_local=None):
        if not self.pandda_dir.exists():
            os.mkdir(str(self.pandda_dir))

        self.processed_datasets.build(process_local=process_local)
        self.analyses.build()
