import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ATLAS_HEADLESS", "1")
