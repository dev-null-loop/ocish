import sys

from . import main

raise SystemExit(main(["ocish", *sys.argv[1:]]))
