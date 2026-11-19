"""A small installed command whose output depends on the shipped source."""

import argparse
from pathlib import Path
import runpy


def main():
    parser = argparse.ArgumentParser()
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument('--version', action='store_true')
    command.add_argument('--status', action='store_true')
    args = parser.parse_args()
    location = Path(__file__).resolve().parent
    if args.version:
        print((location / 'VERSION').read_text().strip())
    elif (location / 'lookup.py').is_file():
        lookup = runpy.run_path(str(location / 'lookup.py'))['lookup']
        result = lookup({}, 'missing')
        print('No entry found' if result is None else result)
    elif (location / 'feature.txt').is_file():
        print('Feature enabled' if (location / 'feature.txt').read_text() == 'on\n' else 'Feature disabled')
    elif (location / 'auth.py').is_file():
        enabled = runpy.run_path(str(location / 'auth.py'))['ALLOW_ANONYMOUS']
        print('Anonymous access enabled' if enabled else 'Anonymous access disabled')
    else:
        print('Delivery fixture ready')


if __name__ == '__main__':
    main()
