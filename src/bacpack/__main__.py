import sys

from .process_title import WEB_SHORT_NAME, WEB_TITLE, set_process_title
from .serve import main

if __name__ == "__main__":
    set_process_title(WEB_TITLE, WEB_SHORT_NAME)
    sys.exit(main())
