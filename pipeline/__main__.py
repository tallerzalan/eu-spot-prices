"""Command line: python -m pipeline <spot|imbalance|regulation|ida|fx> [--data-dir DIR] [--zones FR,NL]"""
import argparse
import os
import sys

from .fx import run_fx
from .ida import IntradayAuctions
from .imbalance import Imbalance
from .regulation import Regulation
from .spot import SpotPrices

DATASETS = {cls.name: cls for cls in (SpotPrices, Imbalance, Regulation, IntradayAuctions)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description=__doc__)
    parser.add_argument("dataset", choices=[*DATASETS, "fx"])
    parser.add_argument("--data-dir", help="where the CSV files live (default: data/)")
    parser.add_argument("--zones", help="comma separated zone codes (default: all)")
    parser.add_argument("--skip-if-up-to-date", action="store_true",
                        help="exit without fetching when there is nothing new to expect (spot: tomorrow is complete)")
    args = parser.parse_args(argv)

    if args.dataset == "fx":
        return run_fx(args.data_dir)
    api_key = os.environ.get("ENTSOE_API_KEY")
    if not api_key:
        sys.exit("Set the ENTSOE_API_KEY environment variable.")
    zones = args.zones.split(",") if args.zones else None
    dataset = DATASETS[args.dataset](api_key, data_dir=args.data_dir, zones=zones)
    if args.skip_if_up_to_date and dataset.is_up_to_date():
        print(f"[{dataset.name}] already up to date, nothing to fetch")
        return 0
    return dataset.run()


if __name__ == "__main__":
    sys.exit(main())
