"""Benchmark development, test, and direct-play commands."""
import argparse
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("develop", "test", "direct"))
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        return None
    command = parser.parse_args(argv[:1]).command
    if command == "direct":
        from coding4game_gui.benchmark.direct.run_direct_baseline import main as run
        return run(argv[1:])
    from coding4game_gui.benchmark.code.run_codex import main as run
    return run(argv)


if __name__ == "__main__":
    main()
