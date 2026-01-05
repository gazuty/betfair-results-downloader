import json
from pathlib import Path

NB_PATH = Path("notebooks/List closed orders with RaceDbExample.ipynb")
PATCH_CODE_PATH = Path("tmp_cell_patch.py")

if not NB_PATH.exists():
    raise FileNotFoundError(f"Notebook not found: {NB_PATH.resolve()}")
if not PATCH_CODE_PATH.exists():
    raise FileNotFoundError(f"Patch code not found: {PATCH_CODE_PATH.resolve()}")

nb = json.loads(NB_PATH.read_text(encoding="utf-8"))
cells = nb.get("cells", [])

patch_code = PATCH_CODE_PATH.read_text(encoding="utf-8").rstrip() + "\n"

# If patch already applied, exit cleanly
for c in cells:
    if c.get("cell_type") == "code":
        src = "".join(c.get("source", []))
        if "PATCH: make customerOrderRef / customerStrategyRef optional" in src:
            print("Patch already present. No changes made.")
            raise SystemExit(0)

# Heuristic: insert AFTER the first cell that references 'df_co ='
insert_idx = None
for i, c in enumerate(cells):
    if c.get("cell_type") == "code":
        src = "".join(c.get("source", []))
        if "df_co" in src and "=" in src and "json_normalize" in src:
            insert_idx = i + 1
            break

if insert_idx is None:
    # fallback: insert near top (after secrets cell if present)
    insert_idx = 1 if len(cells) > 0 else 0

new_cell = {
    "cell_type": "code",
    "metadata": {},
    "outputs": [],
    "execution_count": None,
    "source": [line + "\n" for line in patch_code.splitlines()],
}

cells.insert(insert_idx, new_cell)
nb["cells"] = cells

NB_PATH.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"Inserted patch cell at index {insert_idx} in {NB_PATH}")
