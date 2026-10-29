"""Command line: python -m pipeline <spot|imbalance|regulation|fx> [--data-dir DIR] [--zones FR,NL]"""
import argparse
import os
import sys

from .fx import run_fx
from .imbalance import Imbalance
from .regulation import Regulation
from .spot import SpotPrices

DATASETS = {cls.name: cls for cls in (SpotPrices, Imbalance, Regulation)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__)
    parser.add_argument("dataset", choices=[*DATASETS, "fx"])
    parser.add_argument("--data-dir", help="where the CSV files live (default: data/)")
    parser.add_argument("--zones", help="comma separated zone codes (default: all)")
    args = parser.parse_args(argv)

    if args.dataset == "fx":
        return run_fx(args.data_dir)
    api_key = os.environ.get("ENTSOE_API_KEY")
    if not api_key:
        sys.exit("Set the ENTSOE_API_KEY environment variable.")
    zones = args.zones.split(",") if args.zones else None
    return DATASETS[args.dataset](api_key, data_dir=args.data_dir, zones=zones).run()


if __name__ == "__main__":
    sys.exit(main())
