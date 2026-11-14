#!/usr/bin/env python3
"""启动本地入仓台：python3 run.py [--port 8080]"""

import sys

from server.app import main

if __name__ == "__main__":
    sys.exit(main())
