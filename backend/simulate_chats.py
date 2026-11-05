import os
import sys

# Delegate directly to root simulate_chats.py
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import simulate_chats

if __name__ == "__main__":
    simulate_chats.main_suite()

