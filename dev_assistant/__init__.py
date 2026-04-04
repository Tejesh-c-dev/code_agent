# Quick note: one-line comment added as requested.
import sys

if hasattr(sys.stdout, "reconfigure"):
	sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
	sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dev_assistant.prompts import *

__author__ = "morph"
