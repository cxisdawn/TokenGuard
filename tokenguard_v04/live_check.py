"""Fixed stdout fixture for an explicitly user-started live Claude hook check.

This script itself is offline, read-only and does not access model credentials.
Run it through Claude Bash without piping, redirecting, or shortening its output.
"""
import sys

LINE = 'TokenGuard heartbeat ok\n'
REPETITIONS = 400
MARKER = 'TG_LIVE_CHECK_DONE lines=400\n'


def main() -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.stdout.write(LINE * REPETITIONS + MARKER)


if __name__ == '__main__':
    main()
