"""Connection settings and schema names, read once from the environment.

Everything that another module might otherwise hard-code lives here: where IRIS
is, and what the tables are called.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root is three levels up from this file: src/taxi/config.py -> test/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# override=False so a value already exported in the shell wins over the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class IrisConfig:
    host: str
    port: int
    namespace: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "IrisConfig":
        return cls(
            host=os.getenv("IRIS_HOST", "localhost"),
            # 1973 rather than 1972: compose remaps the superserver port because
            # another IRIS container on this machine may hold the default.
            port=int(os.getenv("IRIS_PORT", "1973")),
            namespace=os.getenv("IRIS_NAMESPACE", "USER"),
            user=os.getenv("IRIS_USER", "_SYSTEM"),
            password=os.getenv("IRIS_PASSWORD", "SYS"),
        )


# CSV locations *as IRIS sees them*. LOAD DATA runs inside the server process, so
# these must be container paths (the ./data bind mount), never host paths.
TRIPS_CSV = os.getenv("TRIPS_CSV_CONTAINER_PATH", "/data/2023_Green_Taxi_Trip_Data.csv")
ZONES_CSV = os.getenv("ZONES_CSV_CONTAINER_PATH", "/data/taxi_zone_lookup(in).csv")

SCHEMA = "Taxi"

# TripRaw is the all-VARCHAR landing table; Trip is the typed, enriched,
# quality-flagged table the application actually queries.
RAW_TRIPS = f"{SCHEMA}.TripRaw"
TRIPS = f"{SCHEMA}.Trip"
ZONES = f"{SCHEMA}.Zone"
