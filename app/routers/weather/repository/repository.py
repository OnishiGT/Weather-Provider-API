#!/usr/bin/env python
# -*- coding: utf-8 -*-

# SPDX-FileCopyrightText: 2019-2021 Alliander N.V.
#
# SPDX-License-Identifier: MPL-2.0

import glob
import shutil
from abc import ABCMeta, abstractmethod
from datetime import datetime
from enum import Enum
from os.path import basename, splitext
from pathlib import Path
from typing import List

import structlog
import xarray as xr

from app.routers.weather.utils.geo_position import GeoPosition
from app_config import get_setting


class RepositoryUpdateResult(Enum):
    failure = 0
    completed = 1
    timed_out = 2


class WeatherRepositoryBase(metaclass=ABCMeta):
    """
    This is the base class for all weather data storage repositories. Any new repositories should implement this
    as their base class.

    All valid stored repository files are named as follows:
    {file_prefix}_{file_identifier}.nc
    or
    {file_prefix}_{file_identifier}_{permanent_suffix}.nc
    Any files found not matching this pattern shall be deleted as being temporary in nature.
    """

    def __init__(self):
        """
        Specification of required fields for a weather data storage repository:
            - repository_folder:    Contains the folder where the repository will be saved. Is set inside a main
                                    repository folder, and based on a sub-folder passed by the repository itself
                                    through the _get_repo_subfolder() function.
            - file_prefix:          Contains a string with the file_prefix to use for all files within the
                                    repository. Is set from the repository itself.
            - runtime_limit:        Contains the maximum time the update function of the repository is allowed to be
                                    running, in seconds.
            - first_day_of_repo:    Contains a datetime indicating the oldest moment allowed to be stored in the
                                    repository.
            - last_day_of_repo:     Contains a datetime indicating the newest moment allowed to be stored in the
                                    repository.
            - permanent_suffixes:   Contains a list of suffixes that can be added to the repository files to
                                    indicate that the file should not be deleted. Any file not matching the prefix
                                    or having a suffix not matching this list will be deleted upon cleanup.
            - file_identifier_length:   This is the length in characters that the unique identifier part of the
                                        filename takes up. Usually this is based on a datetime.

        """
        self.repository_folder = Path(get_setting("REPO_FOLDER")).joinpath(
            self._get_repo_sub_folder()
        )
        self.logger = structlog.get_logger(__name__)
        self.repository_name = None
        self.file_prefix = None
        self.runtime_limit = 60 * 60 * 2  # seconds * minutes * hours (2 hours default)
        self.first_day_of_repo = None
        self.last_day_of_repo = None
        self.permanent_suffixes = None
        self.file_identifier_length = None

    @abstractmethod
    def _get_repo_sub_folder(self):
        print("This method is abstract and should be overridden.")

    def _validate_repo_folder(self):
        """
        This function checks whether or not the repository folder already exists (starting from its parent folder)
        and creates it, if it (or its parent folder) don't exist yet.
        """
        if not Path(
                self.repository_folder
        ).exists():  # If the folder doesn't exist yet, create it
            self.logger.debug(
                f"Attempting to create folder[{self.repository_folder}]",
                datetime=datetime.utcnow(),
            )
            try:
                # If the main folder for all repositories doesn't exist yet, create it..
                if not Path(self.repository_folder).parent.exists():
                    Path(self.repository_folder).parent.mkdir()
                Path(self.repository_folder).mkdir()
            except OSError as e:
                self.logger.error(f"An error occurred creating the directory: {e}")
                raise e

    def cleanup(self):
        """
        This is the cleanup function for any Weather Repository.

        Any files not matching the pattern required for the Repository shall be deleted.
        """
        self._validate_repo_folder()
        self.logger.debug(
            f"Verifying existing files for {self.repository_name} in [{self.repository_folder}]",
            datetime=datetime.utcnow(),
        )

        # Delete any files that aren't of a permanent type
        self._delete_non_permanent_files()

        # First we delete any files that are too old or new to be valid
        self._delete_files_outside_of_scope()

        # Only one file may exist per month. Select the proper file to remain and remove any others
        self._delete_excess_files()

    @abstractmethod
    def update(self):
        print("This method is abstract and should be overridden.")

    def gather_period(
            self, begin: datetime, end: datetime, coordinates: List[GeoPosition]
    ) -> xr.Dataset:
        """
            A function that gathers the repository files associated with a requested period, and then returns the full
            weather data that matches both that period as the requested locations from those files, as an Xarray Dataset
        Args:
            begin:          A datetime holding the starting moment for the requested period to gather data for
            end:            A datetime holding the ending moment for the requested period to gather data for
            coordinates:    A list of GeoPositions holding the coordinates that the data request is for
        Returns:
            A Xarray Dataset containing all of the repository data that matches both the requested period, as well as
            the requested coordinates.

        """
        self.cleanup()
        self.logger.debug(
            f"Gathering repository data for the period of {begin} to {end}",
            datetime=datetime.utcnow(),
        )

        # Get a list of files matching the requested period
        file_list = self._get_file_list_for_period(begin, end)

        if len(file_list) == 0:
            oldest_file = self.get_oldest_repository_file()
            self.logger.error(
                f"No files were found for the period of {begin} to {end}",
                datetime=datetime.utcnow(),
            )
            raise FileNotFoundError(
                f"The [{self.repository_name}] repository does not contain data for the period of [{begin.date()}] to "
                f"[{end.date()}]. To preserve storage this repository only stores up to the file holding the date of "
                f"[{self.first_day_of_repo.date()}]"
            )

        # Load files into datasets, select the requested data and aggregate that into a single dataset
        ds = xr.Dataset()
        for file in file_list:
            ds_temp = xr.open_dataset(file).load()

            ds_temp = self._filter_dataset_by_coordinates(coordinates, ds_temp)
            if file == file_list[0]:
                ds = ds_temp
            else:
                ds = ds.combine_first(ds_temp)
        return ds

    def load_file(self, file: Path) -> xr.Dataset:
        """
            A function that loads and returns the full data for a specific repository file as an Xarray Dataset
        Args:
            file:   The filename (in the Path format by PathLib) specifying the file to load
        Returns:
            An Xarray Dataset containing all of the weather data held within the specified file.

        """
        if file.exists():
            with xr.open_dataset(file) as ds:
                ds.load()
            return ds

        # Raise a FileNotFoundError if the file doesn't exist
        self.logger.error(
            f"File [{str(file)} does not exist]", datetime=datetime.utcnow()
        )
        raise FileNotFoundError

    def purge_repository(self):
        """
        Function to fully delete the repository's folder and create a new clean one. Use with care!
        """
        self.logger.warning(
            f"Purging the entire repository folder for {self.repository_name}!",
            datetime=datetime.utcnow(),
        )
        shutil.rmtree(self.repository_folder, ignore_errors=True)
        self._validate_repo_folder()  # Rebuild the folder after deletion

    def _delete_non_permanent_files(self):
        """
        A function that deletes any and all files in the repository's folder that are not considered permanent in
        nature. Only the files matching either repository files without a suffix or those with suffix listed in the
        permanent_suffixes field are allowed.
        Every other file should be deleted from the repository immediately.
        """
        # TODO: Enhance the detection of files that do not belong in the folder.
        #       A repository folder should only have repository files..
        len_filename_until_after_date = (
                len(str(self.repository_folder.joinpath(self.file_prefix)))
                + self.file_identifier_length
                + 2
        )
        for file_name in glob.glob(
                str(self.repository_folder.joinpath(self.file_prefix)) + "*.nc"
        ):
            file_suffix = file_name[len_filename_until_after_date:-3]
            if len(file_suffix) != 0 and file_suffix not in self.permanent_suffixes:
                self.logger.debug(
                    f"File [{file_name}] is not a permanent file for {self.repository_name} "
                    f"and needs to be deleted"
                )
                self._safely_delete_file(file_name)

    @abstractmethod
    def _delete_files_outside_of_scope(self):
        pass

    def _delete_excess_files(self):
        """
        A function that selects the proper file to keep when more than one permanent file exists for a given
        identifier. The other files are deleted.
        """
        len_filename_until_date = (
                len(str(self.repository_folder.joinpath(self.file_prefix))) + 1
        )
        file_list = glob.glob(
            str(self.repository_folder.joinpath(self.file_prefix)) + "*.*"
        )
        identifier_list = list(
            set(
                [
                    file[
                    len_filename_until_date: len_filename_until_date
                                             + self.file_identifier_length
                    ]
                    for file in file_list
                ]
            )
        )

        for identifier in identifier_list:
            files_with_specific_identifier = glob.glob(
                str(self.repository_folder.joinpath(self.file_prefix))
                + "_"
                + identifier
                + "*.nc"
            )

            if len(files_with_specific_identifier) > 1:
                self.logger.debug(
                    f"More than one file was found for identifier [{identifier}]"
                )
                file_to_retain = None
                highest_ranking_suffix = None

                for file in files_with_specific_identifier:
                    suffix = file[
                             len_filename_until_date + self.file_identifier_length + 1: -3
                             ]

                    if highest_ranking_suffix is None or suffix == "":
                        highest_ranking_suffix = suffix
                        file_to_retain = file

                    # TEMP trumps INCOMPLETE because in the normal process incomplete files will always be replaced with
                    # temporary files
                    if highest_ranking_suffix == "INCOMPLETE" and suffix == "TEMP":
                        highest_ranking_suffix = suffix
                        file_to_retain = file

                for file in files_with_specific_identifier:
                    if file != file_to_retain:
                        self._safely_delete_file(str(Path(file)))

    def _safely_delete_file(self, file: str):
        """ Basic function to safely remove files from the repository if possible, and supply errors if not """
        try:
            self.logger.debug(
                f"Safely deleting file [{file}]", datetime=datetime.utcnow()
            )
            Path(file).unlink()
        except OSError as e:
            self.logger.error(f"Could not safely delete file: {e}")
            raise OSError(f"Could not safely delete file: {file}")
        return True

    @abstractmethod
    def _get_file_list_for_period(self, start: datetime, end: datetime):
        print("This method is abstract and should be overridden.")

    def _filter_dataset_by_coordinates(
            self, coordinates: List[GeoPosition], ds: xr.Dataset
    ) -> xr.Dataset:
        """
            A function that filters a given Xarray Dataset down to the values matching a given list of locations.
        Args:
            coordinates:    A list of GeoPositions that the data is requested for.
            ds:             An Xarray Dataset containing the data to be filtered.
        Returns:
            An Xarray Dataset containing only the weather data that matched the given list of coordinates.
        """
        ds_selected = xr.Dataset()
        coordinate_list = self.get_grid_coordinates(coordinates)

        for coordinate in coordinate_list:
            # First filter a single coordinate in the list
            ds_single_coord = ds.stack(dimensions={"coord": ["lat", "lon"]})
            ds_single_coord = ds_single_coord.where(
                ds_single_coord.lat == coordinate.get_WGS84()[0], drop=True
            )
            ds_single_coord = ds_single_coord.where(
                ds_single_coord.lon == coordinate.get_WGS84()[1], drop=True
            )
            ds_single_coord = ds_single_coord.unstack("coord")

            # Then append this to a clean list
            if coordinate == coordinate_list[0]:
                ds_selected = ds_single_coord
            else:
                ds_selected = ds_selected.combine_first(ds_single_coord)

        return ds_selected

    @abstractmethod
    def get_grid_coordinates(self, coordinates: List[GeoPosition]) -> List[GeoPosition]:
        print("This method is abstract and should be overridden.")
        return [GeoPosition(0, 0)]

    def get_oldest_repository_file(self):
        oldest_file = Path(
            self._get_file_list_for_period(
                self.first_day_of_repo, self.last_day_of_repo
            )[0]
        )
        oldest_file = splitext(basename(oldest_file))[0]
        return oldest_file[len(self.file_prefix) + 1:]
