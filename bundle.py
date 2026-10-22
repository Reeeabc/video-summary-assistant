"""Export an allowlisted source ZIP, excluding credentials, tasks, and parent repo."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = [".gitignore", ".env.example", "AGENTS.md", "README.md", "DEPLOY.md", "VALIDATION.md",
         "UPSTREAM.json", "requirements.txt", "setup.sh", "video_assistant.py", "bundle.py",
         "bilibili_extractor.py", "tests/test_assistant.py", "tests/test_bilibili_extractor.py",
         "PROBE_DIAGNOSTICS.md", "temporary_audio.py", "tests/test_temporary_audio.py",
         "runtime_config.py", "report_delivery.py", "tests/test_workflow.py"]


def bundle(destination):
    sources = [ROOT / name for name in FILES]
    sources += sorted((ROOT / "vendor" / "video-report").rglob("*.md"))
    sources += sorted((ROOT / "vendor" / "video-report").rglob("*.html"))
    if not all(p.is_file() and not p.is_symlink() for p in sources):
        raise ValueError("A required source is absent or a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sources:
            archive.write(path, path.relative_to(ROOT))
    print(f"Exported {len(sources)} source files: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    bundle(parser.parse_args().destination)
