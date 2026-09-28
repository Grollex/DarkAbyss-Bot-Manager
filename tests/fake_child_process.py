import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--mode", choices=("sleep", "exit"), default="sleep")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stdout", default="")
    parser.add_argument("--stderr", default="")
    args = parser.parse_args()

    if args.stdout:
        print(args.stdout, flush=True)
    if args.stderr:
        print(args.stderr, file=sys.stderr, flush=True)

    if args.mode == "exit":
        return args.exit_code

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
