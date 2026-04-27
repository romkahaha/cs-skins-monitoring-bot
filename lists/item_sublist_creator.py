import importlib.util
import json
from pathlib import Path

# === что искать ===
INCLUDE_STRINGS = [
    "Battle-Scarred",
    "Factory New",
    "Field-Tested",
    "Minimal Wear",
    "Well-Worn",
]

# === что исключать ===
EXCLUDE_STRINGS = [
    "StatTrak™",
    "Souvenir",
    "Knife",
    "Gloves",
    "knife",
    "gloves",
]

# === файлы ===
INPUT_FILE = "lists/screening_super_full.py"
OUTPUT_FILE = "lists/skins_normal.py"


def normalize_string_list(strings):
    cleaned = []
    for s in strings:
        if s is None:
            continue
        s = str(s).strip()
        if s:
            cleaned.append(s)
    return cleaned


def load_items_from_py(py_file):
    py_path = Path(py_file).resolve()

    spec = importlib.util.spec_from_file_location("items_module", py_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "ITEMS"):
        raise ValueError(f"Файл {py_file} не содержит переменную ITEMS")

    items = module.ITEMS
    if not isinstance(items, list):
        raise ValueError("ITEMS должен быть list")

    return items


def item_matches(item, include_strings, exclude_strings):
    if include_strings:
        include_ok = any(s in item for s in include_strings)
    else:
        include_ok = True

    if not include_ok:
        return False

    if exclude_strings and any(s in item for s in exclude_strings):
        return False

    return True


def filter_items(items, include_strings, exclude_strings):
    return [
        item for item in items
        if isinstance(item, str) and item_matches(item, include_strings, exclude_strings)
    ]


def format_list_for_comment(strings):
    if not strings:
        return "[]"
    return "[" + ", ".join(repr(s) for s in strings) + "]"


def write_python_list(items, outfile, input_file, include_strings, exclude_strings):
    path = Path(outfile)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Auto-generated filtered item list\n")
        f.write(f"# INPUT_FILE = {input_file!r}\n")
        f.write(f"# INCLUDE_STRINGS = {format_list_for_comment(include_strings)}\n")
        f.write(f"# EXCLUDE_STRINGS = {format_list_for_comment(exclude_strings)}\n\n")

        f.write("ITEMS = [\n")
        for item in items:
            f.write(f"    {json.dumps(item, ensure_ascii=False)},\n")
        f.write("]\n")


if __name__ == "__main__":
    include_strings = normalize_string_list(INCLUDE_STRINGS)
    exclude_strings = normalize_string_list(EXCLUDE_STRINGS)

    items = load_items_from_py(INPUT_FILE)
    filtered_items = filter_items(items, include_strings, exclude_strings)

    write_python_list(
        filtered_items,
        OUTPUT_FILE,
        INPUT_FILE,
        include_strings,
        exclude_strings,
    )

    print(f"Source items:   {len(items)}")
    print(f"Filtered items: {len(filtered_items)}")
    print(f"Saved to:       {Path(OUTPUT_FILE).resolve()}")