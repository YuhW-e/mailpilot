"""Apply the checked-in Firestore index and field configuration."""

import argparse
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("firestore.indexes.json").read_text())
    base = ["gcloud", "--quiet", f"--project={args.project}", "firestore"]

    def apply(*options: str) -> None:
        cmd = [*base, *options, "--database=(default)"]
        if args.dry_run:
            print(f"  {shlex.join(cmd)}")
        else:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

    existing = (
        []
        if args.dry_run
        else json.loads(
            subprocess.check_output(
                [*base, "indexes", "composite", "list", "--database=(default)", "--format=json"]
            )
        )
    )

    def signature(index: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
        return (
            index["queryScope"],
            [field for field in index["fields"] if field["fieldPath"] != "__name__"],
        )

    for index in config["indexes"]:
        group = index["collectionGroup"]
        if any(
            f"/collectionGroups/{group}/indexes/" in item["name"]
            and signature(item) == signature(index)
            and item.get("state") != "NEEDS_REPAIR"
            for item in existing
        ):
            continue
        fields = [
            f"--field-config=field-path={field['fieldPath']},order={field['order'].lower()}"
            for field in index["fields"]
        ]
        apply("indexes", "composite", "create", f"--collection-group={group}", *fields)
    for field in config["fieldOverrides"]:
        args_for_field = (field["fieldPath"], f"--collection-group={field['collectionGroup']}")
        if field.get("indexes") == []:
            apply("indexes", "fields", "update", *args_for_field, "--disable-indexes")
        if field.get("ttl"):
            apply("fields", "ttls", "update", *args_for_field, "--enable-ttl")


if __name__ == "__main__":
    main()
