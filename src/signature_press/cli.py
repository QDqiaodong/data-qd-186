import argparse
import json
import sys
from .audit import process_audit
from .batch import process_batch
from .io import load, publish
from .service import process


def main(argv=None):
    parser = argparse.ArgumentParser(prog='signature-press')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--proof-batch', action='store_true',
                       help='plan a schema-version-2 proof batch with retained legacy signatures')
    modes.add_argument('--binding-audit', action='store_true',
                       help='audit an executed v2 work order against post-binding observations')
    args = parser.parse_args(argv)
    try:
        data = load(args.source)
        if args.binding_audit:
            result = process_audit(data)
        elif args.proof_batch:
            result = process_batch(data)
        else:
            result = process(data)
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result['summary'], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
