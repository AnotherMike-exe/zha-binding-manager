"""Entry point for `python -m zha_binding_manager`."""

import sys

from .manager import main

if __name__ == "__main__":
    sys.exit(main())
