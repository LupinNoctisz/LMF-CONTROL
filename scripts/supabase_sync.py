"""Manual commands for the optional Supabase JSON snapshot bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services import backup_service, supabase_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincronização manual e opcional do banco JSON do LMF Control.")
    parser.add_argument("command", choices=("status", "push", "pull"))
    parser.add_argument("--confirm-replace", action="store_true", help="Confirma substituir o banco JSON local pelo snapshot remoto (pull).")
    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(supabase_sync.configuration_status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "pull" and not args.confirm_replace:
        parser.error("pull substitui o banco local; repita com --confirm-replace após parar o servidor LMF Control.")
    try:
        if args.command == "push":
            result = supabase_sync.push_local_json()
        else:
            result = supabase_sync.pull_to_local_json(lambda kind: backup_service.create_backup(kind))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "pull":
            print("Reinicie o LMF Control para carregar o JSON recebido na memória.")
        return 0
    except (supabase_sync.SupabaseSyncError, OSError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
