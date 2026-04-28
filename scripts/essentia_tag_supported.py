#!/usr/bin/env python3
"""Run the upstream Essentia tagger with codecs this image can decode."""

import importlib
import sys


UPSTREAM_APP_DIR = "/app"
UNSUPPORTED_CODECS = {".opus"}


def main():
    if UPSTREAM_APP_DIR not in sys.path:
        sys.path.insert(0, UPSTREAM_APP_DIR)

    module = importlib.import_module("tag_music")

    module.AUDIO_EXTENSIONS.difference_update(UNSUPPORTED_CODECS)
    module.main()


if __name__ == "__main__":
    main()
