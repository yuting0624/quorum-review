"""Optional export formatters, loaded on demand.

Formatters are imported lazily so a deployment that never exports to a given
format does not pay for its dependencies at startup.
"""

import importlib

# The only modules this function can ever load. User input is a key into this
# dict, never a module path, so nothing outside this table is reachable.
FORMATTERS = {
    "markdown": "app.formatters.markdown",
    "html": "app.formatters.html",
    "pdf": "app.formatters.pdf",
}


def load_formatter(name: str):
    """Return the formatter module registered under ``name``."""
    module_path = FORMATTERS.get(name)
    if module_path is None:
        raise ValueError(f"unsupported format: {name!r}")
    return importlib.import_module(module_path)


def available_formats() -> list:
    return sorted(FORMATTERS)
