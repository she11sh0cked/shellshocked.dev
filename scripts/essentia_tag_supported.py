#!/usr/bin/env python3
"""Run the upstream Essentia tagger with codecs this image can decode."""

import importlib.util
import sys


UPSTREAM_TAGGER = "/app/tag_music.py"
UNSUPPORTED_CODECS = {".opus"}


def main():
    spec = importlib.util.spec_from_file_location("upstream_tag_music", UPSTREAM_TAGGER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    module.AUDIO_EXTENSIONS.difference_update(UNSUPPORTED_CODECS)
    module.main()


if __name__ == "__main__":
    main()
