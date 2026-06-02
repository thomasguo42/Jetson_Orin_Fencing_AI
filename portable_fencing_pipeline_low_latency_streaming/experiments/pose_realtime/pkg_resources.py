from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path


class DistributionNotFound(Exception):
    pass


@dataclass
class Distribution:
    project_name: str
    version: str
    location: str

    def has_metadata(self, name: str) -> bool:
        return name == "top_level.txt"

    def get_metadata(self, name: str) -> str:
        if name != "top_level.txt":
            raise FileNotFoundError(name)
        name = self.project_name.replace("-lite", "").replace("-full", "")
        if name == "mmcv":
            return "mmcv\n"
        return f"{name.replace('-', '_')}\n"


def _location_for_package(package: str) -> str:
    spec = importlib.util.find_spec(package)
    if spec is None:
        raise DistributionNotFound(package)
    if spec.origin:
        return str(Path(spec.origin).resolve().parent)
    if spec.submodule_search_locations:
        return str(Path(next(iter(spec.submodule_search_locations))).resolve())
    raise DistributionNotFound(package)


def get_distribution(package: str) -> Distribution:
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        spec = importlib.util.find_spec(package)
        if spec is None:
            raise DistributionNotFound(package)
        version = "0"
    return Distribution(
        project_name=package,
        version=version,
        location=_location_for_package(package),
    )
