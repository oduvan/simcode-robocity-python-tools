import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")

# Make the in-repo packages importable when running from a source checkout.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def starter_path():
    return os.path.join(EXAMPLES, "starter_main.py")


def mine_path():
    return os.path.join(EXAMPLES, "mine_main.py")
