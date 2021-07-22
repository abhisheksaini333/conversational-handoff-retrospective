"""Local operator commands. No remote requests or background mutations."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from handoff.core import Coordinator


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    args = parser.parse_args(argv)
    try:
        if not Path(args.database).is_file():
            raise ValueError('database file does not exist')
        core = Coordinator(args.database)
        if args.command == 'status':
            result = core.snapshot_counts()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        print('Operation failed: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
