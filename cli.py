"""Chat with the Owner Care agent in the terminal, without starting the HTTP service.

    python cli.py                      # anonymous: the agent will ask who you are
    python cli.py --owner ZK-OWN-1004  # simulate an owner signed in to the ZEEKR app
"""

import argparse
import uuid

from agent import chat


def main() -> None:
    parser = argparse.ArgumentParser(description="ZEEKR Owner Care agent (terminal)")
    parser.add_argument("--owner", help="Verified owner ID to pass as session context, e.g. ZK-OWN-1001")
    args = parser.parse_args()

    session_id = str(uuid.uuid4())
    context = {"owner_id": args.owner, "channel": "ZEEKR app"} if args.owner else {"channel": "Website chat"}
    print("ZEEKR Owner Care - type 'exit' to quit.\n")
    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if message.lower() in ("exit", "quit"):
            break
        if message:
            print(f"\nAria: {chat(session_id, message, context)}\n")


if __name__ == "__main__":
    main()
