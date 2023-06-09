# -*- coding: utf-8 -*-
"""Functionality for building list of calculation parameters."""

##### IMPORTS #####
import dataclasses
import datetime
import itertools
import logging
import pathlib
from typing import Iterator, NamedTuple, Optional, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
import pydantic
import tqdm

from otp4gb import centroids, routing, util


##### CONSTANTS #####
LOG = logging.getLogger(__name__)
CROWFLY_DISTANCE_CRS = "EPSG:27700"
RUC_WEIGHTS = {
    "A1": 1,
    "B1": 1,
    "C1": 1,
    "C2": 1,
    "D1": 1.25,
    "D2": 1.25,
    "E1": 1.5,
    "E2": 1.5,
}


##### CLASSES #####
class CostSettings(NamedTuple):
    """Settings for the OTP routing."""

    server_url: str
    modes: list[routing.Mode]
    datetime: datetime.datetime
    arrive_by: bool = False
    search_window_seconds: Optional[int] = None
    wheelchair: bool = False
    max_walk_distance: float = 1000
    crowfly_max_distance: Optional[float] = None


# Pylint incorrectly flags no-member for pydantic.BaseModel
class CalculationParameters(pydantic.BaseModel):  # pylint: disable=no-member
    """Parameters for `calculate_costs` function."""

    server_url: str
    modes: list[str]
    datetime: datetime.datetime
    origin: routing.Place
    destination: routing.Place
    arrive_by: bool = False
    searchWindow: Optional[int] = None
    wheelchair: bool = False
    max_walk_distance: float = 1000
    crowfly_max_distance: Optional[float] = None


@dataclasses.dataclass
class RUCLookup:
    """Path to RUC lookup file and names of columns."""

    path: pathlib.Path
    id_column: str = "zone_id"
    ruc_column: str = "ruc"


@dataclasses.dataclass
class IrrelevantDestinations:
    """Path to file containing irrelevant destinations, and column name."""

    path: pathlib.Path
    zone_column: str = "zone_id"


##### FUNCTIONS #####
def _to_crs(data: gpd.GeoDataFrame, crs: str, name: str) -> gpd.GeoDataFrame:
    """Convert `data` to `crs` and raise error for invalid geometries."""
    original_crs = data.crs
    invalid_before: pd.Series = ~data.is_valid

    if invalid_before.any():
        LOG.warning(
            "%s (%s%%) invalid features in %s data before CRS conversion",
            invalid_before.sum(),
            invalid_before.sum() / len(data) * 100,
            name,
        )

    data = data.to_crs(crs)

    invalid_after: pd.Series = ~data.is_valid
    if not invalid_before.equals(invalid_after):
        raise ValueError(
            f"{invalid_after.sum()} ({invalid_after.sum()/len(data):.0%}) "
            f"invalid features after converting {name} from {original_crs} to {crs} "
        )

    return data


def _calculate_distance_matrix(
    origins: gpd.GeoDataFrame, destinations: gpd.GeoDataFrame, crs: str
) -> pd.DataFrame:
    """Calculate distances between all `origins` and `destinations`.

    Geometries are converted to `crs` before calculating distance.

    Parameters
    ----------
    origins, destinations : gpd.GeoDataFrame
        Points for calculating distances between, should
        have the same index of zone IDs.
    crs: str
        Name of CRS to convert to before calculating
        distances e.g. 'EPSG:27700'.

    Raises
    ------
    ValueError
        If the CRS conversion causes invalid features.
    """
    distances = pd.DataFrame(
        {
            "origin": np.repeat(origins.index, len(destinations)),
            "destination": np.tile(destinations.index, len(origins)),
        }
    )

    for name, data in (("origin", origins), ("destination", destinations)):
        data = _to_crs(data, crs, f"{name} centroids")

        distances = distances.merge(
            data["geometry"],
            left_on=name,
            how="left",
            validate="m:1",
            right_index=True,
        )
        distances.rename(columns={"geometry": f"{name}_centroid"}, inplace=True)

    distances.loc[:, "distance"] = gpd.GeoSeries(distances["origin_centroid"]).distance(
        gpd.GeoSeries(distances["destination_centroid"])
    )

    return distances.set_index(["origin", "destination"])["distance"]


def _summarise_list(values: Sequence | np.ndarray, max_values: int = 10) -> str:
    """Create string of first few values."""
    message = ", ".join(str(i) for i in values[:max_values])

    if len(values) > max_values:
        message += "..."

    return message


def _load_ruc_lookup(data: RUCLookup, zones: np.ndarray) -> pd.Series:
    """Load RUC lookup data into Series of weights to apply."""
    LOG.info("Loading RUC lookup from %s", data.path.name)
    lookup_data = pd.read_csv(data.path, usecols=[data.id_column, data.ruc_column])
    lookup: pd.Series = lookup_data.set_index(data.id_column, verify_integrity=True)[
        data.ruc_column
    ]

    unknown_zones = lookup.index[~lookup.index.isin(zones)]
    if len(unknown_zones) > 0:
        raise ValueError(
            f"{len(unknown_zones)} values in RUC lookup zones not "
            f"found in centroids data: {_summarise_list(unknown_zones)}"
        )

    lookup = lookup.astype(str).str.upper()
    invalid_ruc = lookup[~lookup.isin(RUC_WEIGHTS)].unique().tolist()
    if len(invalid_ruc) > 0:
        raise ValueError(
            f"{len(invalid_ruc)} values in RUC classifications not "
            f"found in centroids data: {_summarise_list(invalid_ruc)}"
        )

    missing_zones = zones[~np.isin(zones, lookup.index)]
    if len(missing_zones) > 0:
        LOG.warning(
            "RUC distance factor defaulting to 1 for %s zones: %s",
            len(missing_zones),
            _summarise_list(missing_zones),
        )
        lookup = pd.concat([lookup, pd.Series(data=1, index=missing_zones)])

    lookup = lookup.replace(RUC_WEIGHTS).astype(float)

    return lookup


def _load_irrelevant_destinations(
    data: IrrelevantDestinations, zones: np.ndarray
) -> np.ndarray | None:
    """Load array of destinations to exclude, return None if file is empty."""
    LOG.info("Loading irrelevant destinations from %s", data.path.name)
    irrelevant_data = pd.read_csv(data.path, usecols=[data.zone_column])
    irrelevant = irrelevant_data[data.zone_column].unique()

    unknown_zones = irrelevant[~np.isin(irrelevant, zones)]
    if len(unknown_zones) > 0:
        raise ValueError(
            f"{len(unknown_zones)} zones in irrelevant destinations not "
            f"found in centroids data: {_summarise_list(unknown_zones)}"
        )

    LOG.info("Given %s unique destinations to exclude", len(irrelevant))

    if len(irrelevant) == 0:
        return None

    return irrelevant


def _build_calculation_parameters_iter(
    settings: CostSettings,
    columns: centroids.ZoneCentroidColumns,
    origins: gpd.GeoDataFrame,
    destinations: gpd.GeoDataFrame | None = None,
    irrelevant_destinations: np.ndarray | None = None,
    distances: pd.DataFrame | None = None,
    distance_factors: pd.Series | None = None,
    progress_bar: bool = True,
) -> Iterator[CalculationParameters]:
    """Generate calculation parameters, internal function for `build_calculation_parameters`."""

    def row_to_place(row: pd.Series) -> routing.Place:
        point = row.at["geometry"]

        return routing.Place(
            name=str(row.at[columns.name]),
            id=str(row.name),
            zone_system=str(row.at[columns.system]),
            lon=point.x,
            lat=point.y,
        )

    od_pairs = None

    # Filter OD pairs based on distance before looping
    if settings.crowfly_max_distance is not None:
        if distance_factors is not None and distances is not None:
            # Factor distances down to account for RUC factor
            # increasing the max distance filter, factor applied
            # to use origin RUC
            distance_factors.index.name = "origin"
            distances = distances.divide(distance_factors, axis=0)
            LOG.info(
                "Applied RUC distance factors to %s OD pairs",
                (distance_factors != 1).sum(),
            )
        else:
            LOG.info("No RUC distance factors applied")

        if distances is not None:
            od_pairs = distances.loc[
                distances <= settings.crowfly_max_distance
            ].index.to_frame(index=False)
            LOG.info(
                "Dropped %s requests with crow-fly distance > %s (%s remaining)",
                f"{len(distances) - len(od_pairs):,}",
                f"{settings.crowfly_max_distance:,}",
                f"{len(od_pairs):,}",
            )
    else:
        LOG.info("No crow-fly distance filtering applied")

    if od_pairs is None:
        od_pairs = pd.DataFrame(
            itertools.product(origins.index, origins.index),
            columns=["origin", "destination"],
        )

    if irrelevant_destinations is not None:
        exclude = od_pairs["destination"].isin(irrelevant_destinations)
        od_pairs = od_pairs.loc[~exclude]
        LOG.info(
            "Dropped %s OD pairs with irrelevant destinations, %s remaining",
            f"{exclude.sum():,}",
            f"{len(od_pairs):,}",
        )
    else:
        LOG.info("No irrelevant destinations excluded")

    if progress_bar:
        iterator = tqdm.tqdm(
            od_pairs.itertuples(index=False, name=None),
            desc="Building parameters",
            dynamic_ncols=True,
            total=len(od_pairs),
        )
    else:
        iterator = od_pairs.itertuples(index=False, name=None)

    for origin, destination in iterator:
        yield CalculationParameters(
            server_url=settings.server_url,
            modes=[str(m) for m in settings.modes],
            datetime=settings.datetime,
            origin=row_to_place(origins.loc[origin]),
            destination=row_to_place(
                origins.loc[destination]
                if destinations is None
                else destinations.loc[destination]
            ),
            arrive_by=settings.arrive_by,
            searchWindow=settings.search_window_seconds,
            wheelchair=settings.wheelchair,
            max_walk_distance=settings.max_walk_distance,
            crowfly_max_distance=settings.crowfly_max_distance,
        )


def build_calculation_parameters(
    zones: centroids.ZoneCentroids,
    settings: CostSettings,
    ruc_lookup: RUCLookup | None = None,
    irrelevant_destinations: IrrelevantDestinations | None = None,
    crowfly_distance_crs: str = CROWFLY_DISTANCE_CRS,
) -> list[CalculationParameters]:
    """Build a list of parameters for running `calculate_costs`.

    Parameters
    ----------
    zone_centroids :ZoneCentroids
        Positions of zones to calculate costs between.
    settings : CostSettings
        Additional settings for calculating the costs.
    ruc_lookup : RUCLookup, optional
        File containing zone lookup for rural urban classification,
        used for factoring max crow-fly distance filter.
    irrelevant_destinations: IrrelevantDestinations, optional
        File containing list of destinations to exclude from
        calculation parameters.
    crowfly_distance_crs : str, default `CROWFLY_DISTANCE_CRS`
        Coordinate reference system to convert geometries to
        when calculating crow-fly distances.

    Returns
    -------
    list[CalculationParameters]
        Parameters to run `calculate_costs`.
    """
    LOG.info("Building cost calculation parameters with settings\n%s", settings)

    zone_columns = [zones.columns.name, zones.columns.system, "geometry"]
    origins = zones.origins.set_index(zones.columns.id)[zone_columns]

    if zones.destinations is None:
        destinations = None
    else:
        destinations = zones.destinations.set_index(zones.columns.id)[zone_columns]

    if settings.crowfly_max_distance is not None and settings.crowfly_max_distance > 0:
        distances = _calculate_distance_matrix(
            origins,
            origins if destinations is None else destinations,
            crowfly_distance_crs,
        )
    else:
        distances = None

    if ruc_lookup is not None and distances is not None:
        distance_factors = _load_ruc_lookup(ruc_lookup, origins.index.values)
    else:
        distance_factors = None

    if irrelevant_destinations is not None:
        irrelevant = _load_irrelevant_destinations(
            irrelevant_destinations, origins.index.values
        )
    else:
        irrelevant = None

    parameters: list[CalculationParameters] = []
    iterator = _build_calculation_parameters_iter(
        settings,
        zones.columns,
        origins=origins,
        destinations=destinations,
        irrelevant_destinations=irrelevant,
        distances=distances,
        distance_factors=distance_factors,
    )

    for params in iterator:
        if isinstance(params, CalculationParameters):
            parameters.append(params)

        else:
            raise TypeError(
                f"unexpected type ({type(params)}) returned "
                "by `_build_calculation_parameters_iter`"
            )

    LOG.info("Built parameters for %s requests", f"{len(parameters):,}")
    return parameters


def save_calculation_parameters(
    zones: centroids.ZoneCentroids,
    settings: CostSettings,
    output_file: pathlib.Path,
    **kwargs,
) -> pathlib.Path:
    """Build calibration parameters and save to JSON lines file.

    Parameters
    ----------
    zone_centroids :ZoneCentroids
        Positions of zones to calculate costs between.
    settings : CostSettings
        Additional settings for calculating the costs.
    output_file : pathlib.Path
        File to save, suffix will be changed to '.jsonl'
        if it isn't already.

    Returns
    -------
    pathlib.Path
        Path to JSON lines file created.
    """
    parameters = build_calculation_parameters(zones, settings, **kwargs)
    pbar = tqdm.tqdm(parameters, desc="Saving parameters", dynamic_ncols=True)

    output_file = output_file.with_suffix(".jsonl")
    with open(output_file, "wt", encoding=util.TEXT_ENCODING) as file:
        for params in pbar:
            file.write(params.json() + "\n")

    LOG.info(
        "Written %s calculation parameters to JSON lines file: %s",
        len(parameters),
        output_file,
    )
    return output_file
