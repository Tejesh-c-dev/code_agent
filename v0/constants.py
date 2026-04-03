# Quick note: one-line comment added as requested.
EXTENSION_TO_SKIP = [".png",".jpg",".jpeg",".gif",".bmp",".svg",".ico",".tif",".tiff"]
DEFAULT_DIR = "generated"

# Using free models only
DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"  # Free LLaMA 3.3 70B model
DEFAULT_MAX_TOKENS = 2000 # i wonder how to tweak this properly. we dont want it to be max length as it encourages verbosity of code. but too short and code also truncates suddenly.
