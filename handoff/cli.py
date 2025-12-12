"""Local operator commands. No remote requests or background mutations."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from handoff.core import Coordinator, verify_export_bundle
from handoff.http_contracts import strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('verify-export').add_argument('source')
    redact = commands.add_parser('redact')
    redact.add_argument('ticket')
    redact.add_argument('--confirm-ticket', required=True)
    commands.add_parser('retention').add_argument('--before', required=True, type=float)
    export = commands.add_parser('export')
    export.add_argument('conversation')
    export.add_argument('destination')
    commands.add_parser('backup').add_argument('destination')
    commands.add_parser('integrity')
    tickets = commands.add_parser('tickets')
    tickets.add_argument('--state', choices=['pending','human','completed','cancelled'])
    tickets.add_argument('--conversation')
    tickets.add_argument('--reason', choices=['explicit','low_confidence'])
    tickets.add_argument('--limit', type=int, default=50)
    tickets.add_argument('--after', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify-export':
            result = verify_export_bundle(strict_json(Path(args.source).read_text()))
            print(json.dumps(result, sort_keys=True))
            return 0
        if not args.database or not Path(args.database).is_file():
            raise ValueError('database file does not exist')
        core = Coordinator(args.database)
        if args.command == 'status':
            result = core.snapshot_counts()
        elif args.command == 'tickets':
            result = core.ticket_page(limit=args.limit, after=args.after, state=args.state, conversation=args.conversation, reason=args.reason)
        elif args.command == 'integrity':
            result = core.integrity()
        elif args.command == 'backup':
            result = core.backup(args.destination)
        elif args.command == 'export':
            bundle = core.export_bundle(args.conversation)
            descriptor = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(bundle, stream, sort_keys=True, allow_nan=False)
                    stream.write('\n')
                    stream.flush(); os.fsync(stream.fileno())
            except BaseException:
                Path(args.destination).unlink(missing_ok=True)
                raise
            result = {'path':args.destination, 'sha256':bundle['sha256']}
        elif args.command == 'retention':
            result = core.retention_preview(args.before)
        elif args.command == 'redact':
            if args.confirm_ticket != args.ticket:
                raise ValueError('confirmation does not match ticket')
            result = core.redact_ticket(args.ticket)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 1 if args.command == "integrity" and not result["ok"] else 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        print('Operation failed: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
