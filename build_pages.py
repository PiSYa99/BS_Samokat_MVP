"""Update the browser's Python bundle after modifying the model or shop logic."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
with ZipFile(ROOT / "docs/shop-engine.zip", "w", ZIP_DEFLATED) as archive:
    files = [ROOT / name for name in ("store.py", "shop_service.py", "pages_bridge.py",
             "data/assortment.json", "data/profiles.json", "data/catalog-photos.json")]
    for source in sorted((ROOT / "ml_v4").iterdir()):
        if source.suffix in {".py", ".json", ".zip"} and not source.name.startswith("test_"):
            files.append(source)
    for source in files:
        archive.write(source, source.relative_to(ROOT))
