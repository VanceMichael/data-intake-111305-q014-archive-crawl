import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .catalog import Catalog, CatalogError, ContinuityError
from .client import Client, collect
from .decoding import records
from .origin import load_scenario, serve

EXPORTS = {
    "full": ("/exports/full.ldif", "snapshot"),
    "rename": ("/exports/rename.ldif", "changes"),
    "modify": ("/exports/modify.ldif", "changes"),
}


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def intake(base_url, archive_path, directory, feed_path="/feed",
           export_names=None, require_complete=True):
    """Fetch the feed, archive each declared LDIF export and interpret it.

    The feed is the supplier's delivery list.  Each record names the path to
    download; the raw bytes are archived before interpretation so a failed or
    held change can still be examined later.
    """

    archive = Archive(archive_path)
    catalog = Catalog(archive)
    try:
        capture_id, response = collect(Client(base_url), archive, "ldif-feed", feed_path)
        if response.status != 200:
            return {"capture_id": capture_id, "status": response.status,
                    "detail": "feed was not delivered"}
        delivered = records(response.body, response.headers.get("content-type", ""))
        results = []
        for item in delivered:
            if export_names and item.get("export") not in export_names:
                continue
            path = item.get("path")
            if not path:
                results.append({"export": item.get("export"), "status": "invalid",
                                "detail": "delivery record has no path"})
                continue
            file_capture, file_response = collect(
                Client(base_url), archive, "ldif-export", path
            )
            if file_response.status != 200:
                results.append({
                    "export": item.get("export"), "capture_id": file_capture,
                    "status": "not-delivered", "http_status": file_response.status,
                })
                continue
            try:
                outcome = catalog.ingest(
                    file_capture,
                    directory=item.get("directory") or directory,
                    export=item["export"],
                    sequence=item["sequence"],
                    kind=item.get("kind", "changes"),
                    last_change_sequence=item.get("last_change_sequence"),
                )
            except CatalogError as error:
                results.append({
                    "export": item.get("export"), "capture_id": file_capture,
                    "status": "invalid", "detail": str(error),
                })
                continue
            results.append({"capture_id": file_capture, **outcome.as_dict()})
        summary = {"capture_id": capture_id, "delivered": results}
        try:
            summary["catalog"] = catalog.current_catalog(
                directory, require_complete=require_complete
            )
        except ContinuityError as error:
            summary["catalog"] = {"directory": directory, "status": catalog.status(directory).as_dict(),
                                  "refused": str(error)}
        summary["pending"] = catalog.pending(directory)
        return summary
    finally:
        archive.close()


def _demo():
    with tempfile.TemporaryDirectory() as temporary:
        archive_path = Path(temporary) / "archive.sqlite3"
        with serve(load_scenario()) as origin:
            raw = capture(origin.base_url, archive_path, "local-source", "/feed")
            joined = intake(origin.base_url, archive_path, "union-a")
        print(json.dumps({"operation": "ldif_intake", "raw": raw,
                          "interpretation": joined}, ensure_ascii=False, indent=2))


def _serve(args):
    with serve(load_scenario(args.scenario)) as origin:
        print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
        try:
            import threading
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser(description="馆藏 LDIF 导出接收与目录演进")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    loader = subcommands.add_parser("intake")
    loader.add_argument("--base-url", required=True)
    loader.add_argument("--archive", type=Path, required=True)
    loader.add_argument("--directory", required=True)
    loader.add_argument("--feed", default="/feed")
    loader.add_argument("--export", action="append", dest="exports")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        _demo()
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "intake":
        print(json.dumps(
            intake(args.base_url, args.archive, args.directory, args.feed, args.exports),
            ensure_ascii=False, indent=2,
        ))
    else:
        _serve(args)


if __name__ == "__main__":
    main()
