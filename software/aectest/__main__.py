"""`python -m aectest`."""
import sys

from .cli import NACK_MSG, Nack, main

try:
    sys.exit(main())
except Nack as e:
    sys.exit(NACK_MSG.format(e))
