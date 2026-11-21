import argparse
import json
import sys
from .batch import process_batch
from .io import load, publish
from .service import process


def main(argv=None):
    parser = argparse.ArgumentParser(prog='signature-press')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--proof-batch', action='store_true',
                        help='plan a schema-version-2 proof batch with retained legacy signatures')
    args = parser.parse_args(argv)
    try:
        data = load(args.source)
        result = process_batch(data) if args.proof_batch else process(data)
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result['summary'], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
