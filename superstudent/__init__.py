"""Super Student: mirror Canvas courses into a searchable study library for Claude and ChatGPT."""

__version__ = "1.7.0"

# Everything a long-running process (the app window, the AI connector, a sync) may need later. Loading it all up
# front means an update that replaces these files on disk can't leave a running process with half old, half
# new code.
MODULES = ("assistants", "canvas", "compact", "config", "content", "describe", "evidence", "exams", "extract", "guides", "index", "layout",
           "library", "media", "notes", "ocr", "omml", "outline", "overview", "packs", "render", "scheduler",
           "slide_render", "sync", "uninstall", "util")


def load_everything(extra: tuple = ()) -> None:
    import importlib

    for name in MODULES + tuple(extra):
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception:      # an optional part that can't load here is reported where it's used
            pass
