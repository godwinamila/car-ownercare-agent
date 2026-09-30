"""Chat with the Elyra customer assistant in the terminal, without starting the HTTP service.

    python cli.py                        # no context
    python cli.py --country Netherlands  # tell the assistant which country the customer is in
"""

import argparse
import uuid

from agent import chat


def main() -> None:
    parser = argparse.ArgumentParser(description="Elyra customer assistant (terminal)")
    parser.add_argument("--country", help="Customer's country, e.g. Netherlands")
    args = parser.parse_args()

    session_id = str(uuid.uuid4())
    context = {"channel": "Website chat"}
    if args.country:
        context["country"] = args.country
    print("Elyra customer assistant - type 'exit' to quit.\n")
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
