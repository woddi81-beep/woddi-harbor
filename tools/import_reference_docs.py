from __future__ import annotations

import argparse
import json

from app.sources import configure_document_sources, sync_source


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure and import production Markdown documentation.")
    parser.add_argument(
        "--operations-path",
        default="/opt/woddi-ai/doku/documentation-operation-main",
    )
    parser.add_argument(
        "--customer-path",
        default="/opt/woddi-ai/doku/documentation-customer-main",
    )
    parser.add_argument("--no-reindex", action="store_true")
    args = parser.parse_args()

    result = {
        "configuration": configure_document_sources(args.operations_path, args.customer_path),
        "operations": sync_source("operation-docs", reindex=not args.no_reindex),
        "customer": sync_source("customer-docs", reindex=not args.no_reindex),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
