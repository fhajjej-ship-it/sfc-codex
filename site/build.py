"""Render the public site using a published release tag."""
import argparse
from pathlib import Path
import re
from shutil import copyfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', args.tag):
        parser.error('Expected a stable release tag such as v0.1.39')

    source = Path(__file__).resolve().parent
    args.output.mkdir(parents=True, exist_ok=False)
    template = (source / 'index.html').read_text(encoding='utf-8')
    html = template.replace('@TAG@', args.tag).replace('@VERSION@', args.tag.removeprefix('v'))
    (args.output / 'index.html').write_text(html, encoding='utf-8')
    for name in ('styles.css', 'site.js'):
        copyfile(source / name, args.output / name)
    copyfile(source.parent / 'assets/sfc-app-icon.png', args.output / 'sfc-app-icon.png')


if __name__ == '__main__':
    main()
