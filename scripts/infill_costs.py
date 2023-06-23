# -*- coding: utf-8 -*-
"""Script to infill cost matrices produced by OTP4GB-py."""

##### IMPORTS #####
from __future__ import annotations

import argparse
import datetime
import enum
import functools
import pathlib
import re
import sys
import textwrap
from typing import Any, Callable, NamedTuple, Mapping

import caf.toolkit
import numpy as np
import pandas as pd
import pydantic
import tqdm
from matplotlib import figure
from matplotlib import pyplot as plt
from matplotlib.backends import backend_pdf
from numpy import polynomial
from pydantic import dataclasses, types
from scipy import stats, optimize

sys.path.extend((".", ".."))
import otp4gb
from otp4gb import centroids, config, cost, logging, parameters

##### CONSTANTS #####
LOG = logging.get_logger(otp4gb.__package__ + ".infill_costs")


##### CLASSES #####
class InfillParameters(caf.toolkit.BaseConfig):
    """Config for `infill_costs` module."""

    folders: list[types.DirectoryPath]
    infill_columns: dict[str, float]


@dataclasses.dataclass
class InfillArgs:
    """Arguments for `infill_costs` module."""

    config: types.FilePath

    @classmethod
    def parse(cls) -> InfillArgs:
        """Parse command line arguments."""
        parser = argparse.ArgumentParser(
            description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
        )
        parser.add_argument(
            "config", type=pathlib.Path, help="path to infill config file"
        )

        args = parser.parse_args()
        return InfillArgs(config=args.config)


class PlotType(enum.Enum):
    """Type of plot to create."""

    HEXBIN = enum.auto()
    SCATTER = enum.auto()


class _Config:
    arbitrary_types_allowed = True


@dataclasses.dataclass(config=_Config)
class PlotData:
    """Data for plotting to a graph."""

    x: pd.Series
    y: pd.Series
    title: str | None = None

    @pydantic.validator("y")
    def _check_index(cls, value: pd.Series, values) -> pd.Series:
        if not value.index.equals(values["x"].index):
            raise ValueError("x and y indices are different")

        return value

    def filter(
        self, min_x: float, max_x: float, min_y: float, max_y: float
    ) -> PlotData:
        """Filter data to within bounds given."""
        x_filter = (self.x > min_x) & (self.x < max_x)
        y_filter = (self.y > min_y) & (self.y < max_y)

        index = self.x.index[x_filter & y_filter]

        return PlotData(x=self.x.loc[index], y=self.y.loc[index], title=self.title)


class AxisLimit(NamedTuple):
    """Limits for plot axis."""

    min_x: int | float | None = None
    max_x: int | float | None = None
    min_y: int | float | None = None
    max_y: int | float | None = None

    def infill(self, data: PlotData) -> AxisLimit:
        """Update any missing values to min / max `data` values."""
        filtered = data.filter(
            min_x=-np.inf if self.min_x is None else self.min_x,
            max_x=np.inf if self.max_x is None else self.max_x,
            min_y=-np.inf if self.min_y is None else self.min_y,
            max_y=np.inf if self.max_y is None else self.max_y,
        )

        values = []

        for name, value in self._asdict().items():
            if value is not None:
                values.append(value)
                continue

            func = np.min if name.startswith("min") else np.max
            data = filtered.x if name.endswith("x") else filtered.y

            values.append(func(data))

        return AxisLimit(*values)


class InfillMethod(enum.Enum):
    """Cost infilling methods."""

    MEAN_RATIO = enum.auto()
    LINEAR = enum.auto()
    POLYNOMIAL_2 = enum.auto()
    POLYNOMIAL_3 = enum.auto()
    POLYNOMIAL_4 = enum.auto()
    EXPONENTIAL = enum.auto()
    LOGARITHMIC = enum.auto()


class InfillFunction:
    """Functions for infilling costs using crow-fly distances."""

    _MAX_LABEL_WIDTH = 20
    _FMT = ".2f"
    _function: Callable[[np.ndarray], np.ndarray]
    _equation: str
    _name: str

    def __init__(self, method: InfillMethod, x: pd.Series, y: pd.Series) -> None:
        """Fits infilling function to given data.

        Parameters
        ----------
        method : InfillMethod
            Method of infilling to use.
        x, y : pd.Series
            X and y values to fit infilling function to.
        """
        setup_function = self._get_function(method)

        try:
            setup_function(x, y)
        except RuntimeError as error:
            LOG.error("Infilling fit error: %s", error)

            self._equation = "y = NaN"
            self._function = lambda arr: arr * np.nan
            self._name = f"{method.name.replace('_', ' ').title()} Fit Error"

    def __call__(self, distances: np.ndarray) -> np.ndarray:
        """Calculate infill values based on `distances`."""
        return self._function(distances)

    @property
    def label(self) -> str:
        """Plot label for infill function with equation."""
        if len(self._name) > self._MAX_LABEL_WIDTH:
            name = textwrap.fill(self._name, self._MAX_LABEL_WIDTH)
        else:
            name = self._name

        equation = re.sub(r"y\s*=\s*", "", self._equation).strip()

        if len(equation) <= self._MAX_LABEL_WIDTH:
            return f"{name}\n$y={equation}$"

        equation = textwrap.wrap(equation, self._MAX_LABEL_WIDTH)
        equation = "".join(f"\n   ${i}$" for i in equation)

        return f"{name}\n$y=${equation}"

    def _get_function(
        self, method: InfillMethod
    ) -> Callable[[pd.Series, pd.Series], None]:
        lookup: dict[InfillMethod, Callable] = {
            InfillMethod.MEAN_RATIO: self._ratio,
            InfillMethod.LINEAR: self._linear,
            InfillMethod.POLYNOMIAL_2: functools.partial(self._polynomial, degree=2),
            InfillMethod.POLYNOMIAL_3: functools.partial(self._polynomial, degree=3),
            InfillMethod.POLYNOMIAL_4: functools.partial(self._polynomial, degree=4),
            InfillMethod.EXPONENTIAL: self._exponential,
            InfillMethod.LOGARITHMIC: self._logarithmic,
        }

        if method not in lookup:
            raise ValueError(f"no function defined for method: '{method}'")

        return lookup[method]

    def _ratio(self, x: pd.Series, y: pd.Series) -> None:
        ratio = np.mean(y / x)

        self._function = lambda arr: arr * ratio
        self._name = "Mean Ratio"
        self._equation = f"y = {ratio:.2f}x"

    def _linear(self, x: pd.Series, y: pd.Series) -> None:
        result = stats.linregress(x, y)

        self._function = lambda arr: (arr * result.slope) + result.intercept
        self._name = "Linear Regression"
        self._equation = f"y = {result.slope:.2f}x {result.intercept:+.2f}"

    def _polynomial(self, x: pd.Series, y: pd.Series, degree: int) -> None:
        poly = polynomial.Polynomial.fit(x, y, degree)
        self._name = f"Polynomial degree {degree}"

        self._equation = "y ="
        for i, value in enumerate(reversed(poly.coef)):
            power = degree - i

            if i == 0:
                self._equation += f"{value:.2f}"
            else:
                self._equation += f"{value:+.2f}"

            if power > 1:
                self._equation += f"x^{power}"
            elif power == 1:
                self._equation += "x"

        self._function = poly

    def _exponential(self, x: pd.Series, y: pd.Series) -> None:
        def exp(arr, a, b, c):
            return a * np.exp(b * arr) + c

        fit = optimize.curve_fit(exp, x, y, (0, 0.5, 1))
        a, b, c = fit[0]

        self._function = functools.partial(exp, a=a, b=b, c=c)
        self._name = "Exponential"
        self._equation = rf"y = {a:.2f} e^{{{b:.2f}x}} {c:+.2f}"

    def _logarithmic(self, x: pd.Series, y: pd.Series) -> None:
        def log(arr, a, b, c):
            return a * np.log(b * arr) + c

        fit = optimize.curve_fit(log, x, y, (0, 0.5, 1))
        a, b, c = fit[0]

        self._function = functools.partial(log, a=a, b=b, c=c)
        self._name = "Natural Log"
        self._equation = rf"y = {a:.2f} \ln ({b:.2f}x) {c:+.2f}"


##### FUNCTIONS #####
def calculate_crow_fly(
    origins_path: pathlib.Path,
    destinations_path: pathlib.Path | None,
    extents: centroids.Bounds | None = None,
) -> pd.Series:
    """Calculate crow-fly distances between origins and destination centroids.

    Parameters
    ----------
    origins_path : pathlib.Path
        CSV containing origin centroids with lat/lon coordinates.
    destinations_path : pathlib.Path, optional
        Optional destination centroids (origins used if not given).
    extents : centroids.Bounds, optional
        Extents to filter the centroids for.

    Returns
    -------
    pd.Series
        Crow-fly distances with origin and destination indices.
    """
    if destinations_path is None:
        LOG.info("Calculating crow-fly distances for centroids '%s'", origins_path.name)
    else:
        LOG.info(
            "Calculating crow-fly distance for origins '%s' and destinations '%s'",
            origins_path.name,
            destinations_path.name,
        )

    centroid_data = centroids.load_centroids(
        origins_path,
        destinations_path,
        zone_columns=centroids.ZoneCentroidColumns(),
        extents=extents,
    )
    if centroid_data.destinations is None:
        destinations = centroid_data.origins
    else:
        destinations = centroid_data.destinations

    return parameters.calculate_distance_matrix(
        centroid_data.origins.set_index(centroid_data.columns.id),
        destinations.set_index(centroid_data.columns.id),
        parameters.CROWFLY_DISTANCE_CRS,
    )


def filter_responses(responses_path: pathlib.Path, max_count: int = 10) -> pathlib.Path:
    """Filter responses with itineraries.

    Parameters
    ----------
    responses_path : pathlib.Path
        Path to JSON lines file containing OTP4GB responses.
    max_count : int, default 10
        Maximum number of responses to include in filter.

    Returns
    -------
    pathlib.Path
        Path to file containing filtered responses.
    """
    output_path = responses_path.with_name(responses_path.stem + "-filtered.jsonl")
    count = 0

    with open(responses_path, "rt", encoding="utf-8") as file:
        with open(output_path, "wt", encoding="utf-8") as out_file:
            for result in tqdm.tqdm(
                cost.iterate_responses(file), desc="Iterating responses"
            ):
                if result.plan is None or len(result.plan.itineraries) == 0:
                    continue

                out_file.write(result.json() + "\n")
                count += 1
                if count > max_count:
                    break

    LOG.info("Written filtered responses: %s", output_path.name)
    return output_path


def plot_axes(
    ax: plt.Axes,
    data: PlotData,
    plot_type: PlotType,
    axis_limit: AxisLimit,
) -> None:
    """Create plot on given axes."""
    if plot_type == PlotType.HEXBIN:
        hb = ax.hexbin(data.x, data.y, extent=axis_limit, mincnt=1)
        plt.colorbar(hb, ax=ax, label="Count", aspect=40)

    elif plot_type == PlotType.SCATTER:
        ax.scatter(data.x, data.y, rasterized=len(data.x) > 1000)
        ax.set_ylim(axis_limit.min_y, axis_limit.max_y)
        ax.set_xlim(axis_limit.min_x, axis_limit.max_x)

    ax.annotate(
        f"Total Count\n{len(data.x):,}",
        (0.8, 0.05),
        xycoords="axes fraction",
        bbox=dict(boxstyle="round", facecolor="white"),
    )

    ax.set_xlabel(data.x.name)
    ax.set_ylabel(data.y.name)

    if data.title is not None:
        ax.set_title(data.title)


def plot(
    *plot_data: PlotData,
    title: str,
    plot_type: PlotType,
    axis_limit: AxisLimit,
    fit_function: InfillFunction | None = None,
    subplots_kwargs: dict[str, Any] = dict(),
) -> figure.Figure:
    """Create infill hexbin or scatter plots."""
    default_figure_kwargs = dict(figsize=(15, 10), constrained_layout=True)
    default_figure_kwargs.update(subplots_kwargs)

    ncols = len(plot_data) if len(plot_data) > 0 else None
    fig, axes = plt.subplots(ncols=ncols, **default_figure_kwargs)
    fig.suptitle(title)

    for ax, data in zip(axes, plot_data):
        limit = axis_limit.infill(data)
        plot_axes(ax, data, plot_type, limit)

        if fit_function is not None:
            x = np.arange(limit.min_x, limit.max_x, 1)
            ax.plot(x, fit_function(x), "--", c="C1", label=fit_function.label)
            ax.legend(loc="upper right")

    return fig


def infill_metric(
    metric: pd.Series,
    distances: pd.Series,
    plot_file: pathlib.Path,
    method: InfillMethod,
) -> pd.Series:
    """Infill given `metric` using `method` given and plot graphs.

    Parameters
    ----------
    metric : pd.Series
        Series of values to infill, with indices
        origin and destination.
    distances : pd.Series
        Crow-fly distances with the same indices as
        `metric`.
    plot_file : pathlib.Path
        Path to save infill graphs to.
    method : InfillMethod
        Method of infilling to use.

    Returns
    -------
    pd.Series
        Infilled `metric`.
    """
    metric = metric.dropna()
    metric.name = re.sub(r"[\s_]+", " ", metric.name).title()

    # TODO Figure out issue with zones in metric which aren't in distances
    data = pd.concat([metric, distances], axis=1)

    for column, values in data.items():
        nan_values = values.isna().sum()
        if nan_values > 0:
            LOG.warning("%s Nan values in %s column", f"{nan_values:,}", column)

    before = len(data)
    data = data.dropna(how="any")
    after = len(data)
    message = (
        f"{after:,} ({after / before:.0%}) rows remaining "
        f"after dropping missing values, {before:,} before"
    )
    LOG.info(message)

    plot_data = [
        PlotData(
            x=data[distances.name],
            y=data[metric.name],
            title=f"Before Infilling\n{metric.name} vs {distances.name}",
        )
    ]

    missing = distances.index[~distances.index.isin(data.index)]
    LOG.info("Infilling %s values with %s method", f"{len(missing):,}", method)

    infill_function = InfillFunction(method, data[distances.name], data[metric.name])
    calculated = infill_function(distances.loc[missing])

    if calculated.index.isin(metric.index).sum() > 0:
        raise ValueError("Oops recalculated existing metrics")

    infilled = pd.concat([metric, calculated], axis=0)
    infilled.name = "Infilled " + metric.name

    infilled_data = pd.concat([distances, infilled], axis=1)

    plot_data.append(
        PlotData(
            x=infilled_data.loc[missing, distances.name],
            y=infilled_data.loc[missing, infilled.name],
            title=f"Only {infilled.name} vs {distances.name}",
        )
    )
    plot_data.append(
        PlotData(
            x=infilled_data[distances.name],
            y=infilled_data[infilled.name],
            title=f"After Infilling\n{infilled.name} vs {distances.name}",
        )
    )

    axis_limit = AxisLimit(min_x=0, max_x=200, min_y=0, max_y=None)
    infilled_limits = (axis_limit.infill(i) for i in plot_data)
    max_y = functools.reduce(max, (i.max_y for i in infilled_limits), 0)
    axis_limit = AxisLimit(axis_limit.min_x, axis_limit.max_x, axis_limit.min_y, max_y)

    with backend_pdf.PdfPages(plot_file) as pdf:
        for pt in PlotType:
            fig = plot(
                *plot_data,
                title=f"Infilling Comparison for {plot_file.stem} - {metric.name}",
                plot_type=pt,
                axis_limit=axis_limit,
                fit_function=infill_function,
                subplots_kwargs=dict(sharey=True, figsize=(20, 8)),
            )

            pdf.savefig(fig)
            plt.close(fig)

    LOG.info("Saved plots to %s", plot_file.name)

    LOG.info(
        "Infilled data contains %s valid values (of %s) for '%s'",
        (~infilled.isna()).sum(),
        len(infilled),
        metric.name,
    )

    return infilled


def infill_costs(
    metrics_path: pathlib.Path,
    columns: Mapping[str, float],
    distances: pd.Series,
    output_folder: pathlib.Path,
    methods: list[InfillMethod] | None = None,
) -> None:
    """Infill cost metrics using given `methods` and output graphs.

    Parameters
    ----------
    metrics_path : pathlib.Path
        Cost metrics file for infilling.
    columns : Mapping[str, float]
        Column names for infilling and factors to apply
        before infilling.
    distances : pd.Series
        Crow-fly distances for all OD pairs.
    output_folder : pathlib.Path
        Base folder to save outputs to, sub-folders are
        created for each infill method.
    methods : list[InfillMethod], optional
        Methods of infilling to use, if not given all
        infill methods will be used.

    Raises
    ------
    ValueError
        If distances has missing values.
    """
    LOG.info("Reading '%s'", metrics_path)
    metrics = pd.read_csv(metrics_path, index_col=["origin", "destination"])

    if distances.isna().sum() > 0:
        raise ValueError(f"distances has {distances.isna().sum()} Nan values")

    if methods is None:
        methods = list(InfillMethod)

    for method in methods:
        LOG.info(
            "Using infilling method '%s' on columns: %s",
            method.name,
            [i for i in columns if i in metrics.columns],
        )

        method_folder = output_folder / f"infill - {method.name.lower()}"
        method_folder.mkdir(exist_ok=True)
        infilled_metrics = []

        for column, factor in columns.items():
            if column not in metrics.columns:
                LOG.error(
                    "Metric column '%s' not found in '%s'", column, metrics_path.name
                )
                continue

            LOG.info("Multiplying '%s' column by %s before infilling", column, factor)

            infilled_metrics.append(
                infill_metric(
                    metrics[column] * factor,
                    distances,
                    method_folder / (metrics_path.stem + f"-{column}.pdf"),
                    method,
                )
            )

        infilled_df = pd.concat(infilled_metrics, axis=1)

        out_path = method_folder / (metrics_path.stem + "-infilled.csv")
        infilled_df.to_csv(out_path)
        LOG.info("Written: %s", out_path)

        produce_matrices(infilled_df, out_path)


def produce_matrices(infilled: pd.DataFrame, output_path: pathlib.Path) -> None:
    """Write columns in `infilled` to separate square CSVs.

    Parameters
    ----------
    infilled : pd.DataFrame
        Infilled cost data.
    output_path : pathlib.Path
        Base path to write outputs to (column name will be appended
        when writing the CSVs.)
    """
    for column in infilled:
        data = infilled[column].unstack()

        out_path = output_path.with_name(output_path.stem + f"-{column}.csv")
        data.to_csv(out_path)
        LOG.info("Written: %s", out_path)


def main(
    folder: pathlib.Path, params: config.ProcessConfig, infill_columns: dict[str, float]
) -> None:
    """Infill costs in given OTP `folder`.

    Parameters
    ----------
    folder : pathlib.Path
        Folder containing OTP4GB config and outputs.
    params : config.ProcessConfig
        OTP4GB config from `folder`.
    infill_columns : dict[str, float]
        Names of columns to infill and factors to apply to values
        before infilling.

    Raises
    ------
    FileNotFoundError
        If the cost metrics file can't be found.
    """
    logging.initialise_logger(otp4gb.__package__, folder / "logs/infill_costs.log")
    LOG.info("Infilling %s", folder)

    origin_path = config.ASSET_DIR / params.centroids
    destination_path = None
    if params.destination_centroids is not None:
        destination_path = config.ASSET_DIR / params.destination_centroids

    distances = calculate_crow_fly(origin_path, destination_path, None)
    distances = distances / 1000
    distances.name = "Crow-Fly Distance (km)"

    for time_period in params.time_periods:
        travel_datetime = datetime.datetime.combine(
            params.date, time_period.travel_time
        )
        # Assume time is in local timezone
        travel_datetime = travel_datetime.astimezone()
        LOG.info(
            "Given date / time is assumed to be in local timezone: %s",
            travel_datetime.tzinfo,
        )

        for modes in params.modes:
            matrix_path = folder / (
                f"costs/{time_period.name}/"
                f"{'_'.join(modes)}_costs_{travel_datetime:%Y%m%dT%H%M}.csv"
            )
            metrics_path = matrix_path.with_name(matrix_path.stem + "-metrics.csv")

            if not metrics_path.is_file():
                raise FileNotFoundError(metrics_path)

            recalculated_path = matrix_path.with_name(
                matrix_path.stem + "-recalculated.csv"
            )
            recalculated_metrics_path = recalculated_path.with_name(
                recalculated_path.stem + "-metrics.csv"
            )
            if not recalculated_metrics_path.is_file():
                LOG.info("Recalculating costs: '%s'", metrics_path.name)
                cost.cost_matrix_from_responses(
                    metrics_path.with_name(matrix_path.name + "-response_data.jsonl"),
                    recalculated_path,
                    params.iterinary_aggregation_method,
                )

            infill_costs(
                recalculated_metrics_path,
                infill_columns,
                distances,
                metrics_path.parent,
            )


def _run() -> None:
    args = InfillArgs.parse()

    params = InfillParameters.load_yaml(args.config)

    for folder in params.folders:
        main(folder, config.load_config(folder), params.infill_columns)


if __name__ == "__main__":
    _run()
