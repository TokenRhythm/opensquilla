"""Return uppercase UTF-8 text using the standard library."""

import sys

if __name__ == "__main__":
    print(sys.stdin.read().upper())
