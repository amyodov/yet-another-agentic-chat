"""Console entry point for `yaac-chat`.

Deliberately imports nothing from textual. The window itself lives in `chat_app`, which is imported only once
this has checked the optional dependency is installed -- so a missing extra is one sentence rather than a
traceback, and importing any part of the package stays harmless.

That last part is not politeness. An earlier version raised `SystemExit` while importing the app module, which
took the whole test session down during collection instead of skipping one file: a module that exits on import
is a trap for everything that merely looks at it.
"""

import argparse

from .backend import add_rendezvous_flags, check_zmq_capabilities, chosen_rendezvous, configure_logging

NEEDS_TEXTUAL = (
    "yaac-chat needs textual, which ships as an optional extra so that an MCP-only install stays small.\n"
    '  uvx --from "yet-another-agentic-chat[chat]" yaac-chat\n'
    "or, in an existing environment:\n"
    "  pip install 'yet-another-agentic-chat[chat]'"
)


def main() -> None:
    """Parse arguments, check pyzmq, then hand over to the window. Unlike the MCP server, this owns the terminal."""
    parser = argparse.ArgumentParser(
        prog="yaac-chat",
        description="Join a YAAC channel as a person, in a terminal window.",
    )
    add_rendezvous_flags(parser)
    parser.add_argument("--channel", help="Channel to join at startup. Without it, the channel list opens first.")
    parser.add_argument("--name", help="Your name on that channel. Required with --channel.")
    args = parser.parse_args()
    configure_logging()  # before chosen_rendezvous, which may have a warning to give
    # A window left open is the steadiest hat a net can have, since it outlives the agent sessions around it --
    # so --bind is worth more here than anywhere, and --connect is for a window that only wants to listen.
    endpoint, role = chosen_rendezvous(args)
    if bool(args.channel) != bool(args.name):
        parser.error("--channel and --name go together")

    try:
        check_zmq_capabilities()
    except RuntimeError as exc:
        raise SystemExit(f"[yaac] {exc}") from exc

    try:
        from .chat_app import run
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        raise SystemExit(NEEDS_TEXTUAL) from None

    run(endpoint, args.channel, args.name, role=role)


if __name__ == "__main__":
    main()
