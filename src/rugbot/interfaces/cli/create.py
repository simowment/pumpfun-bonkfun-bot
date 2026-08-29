"""CLI for Pump.fun token creation (build+simulate only by default)."""

# ruff: noqa: C901, PLR0911, PLR0912, PLR0915, BLE001, TRY003, S110, ANN001, PLC0415, F841, PLR2004

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from rugbot.execution.create_builder import build_create_v2_instruction
from rugbot.runtime.config import ExecutionMode, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a Pump.fun token (dry-run by default)"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--uri", required=True)
    parser.add_argument(
        "--buy-sol", type=float, default=None, help="Optional first buy in SOL"
    )
    parser.add_argument(
        "--creator", type=str, default=None, help="Creator pubkey (default: payer)"
    )
    parser.add_argument("--mayhem", action="store_true")
    parser.add_argument("--cashback", action="store_true")
    parser.add_argument("--mint-keypair", type=Path, default=None)
    parser.add_argument("--rpc", type=str, default=None)
    parser.add_argument(
        "--payer", type=str, default=None, help="Payer pubkey (default: signer)"
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--yes", action="store_true", help="Actually submit (requires LIVE mode)"
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.OBSERVE.value,
    )
    return parser


def _load_or_generate_mint(path: Path | None) -> Keypair:
    if path is not None and path.exists():
        raw = path.read_bytes().strip()
        # try json array
        try:
            arr = json.loads(raw.decode())
            if isinstance(arr, list) and len(arr) == 64:
                return Keypair.from_bytes(bytes(arr))
        except Exception:
            pass
        try:
            decoded = base58.b58decode(raw.decode().strip())
            if len(decoded) == 64:
                return Keypair.from_bytes(decoded)
        except Exception:
            pass
        raise SystemExit(f"cannot parse mint keypair at {path}")
    return Keypair()


async def _maybe_simulate(
    rpc: str | None, payer: Pubkey, ix
) -> dict[str, object] | None:
    if rpc is None:
        return None
    from solders.message import Message
    from solders.transaction import Transaction

    from rugbot.integrations.solana_rpc import SolanaClient

    client = SolanaClient(rpc)
    try:
        resp = await client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}],
            }
        )
        blockhash_str = None
        if isinstance(resp, dict):
            result = resp.get("result", {})
            if isinstance(result, dict):
                v = result.get("value", {})
                if isinstance(v, dict):
                    blockhash_str = v.get("blockhash")
        if not blockhash_str:
            return {"simulated": False, "error": "no blockhash"}
        from solders.hash import Hash

        bh = Hash.from_string(blockhash_str)
        msg = Message([ix], payer)
        tx = Transaction([], msg, bh)
        # simulate
        b64 = base64.b64encode(bytes(tx)).decode()
        sim_resp = await client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [b64, {"commitment": "finalized", "encoding": "base64"}],
            }
        )
        return {"simulated": True, "response": sim_resp}
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    resolve_dotenv(include_signing=True)

    # Resolve mint
    mint_kp = _load_or_generate_mint(args.mint_keypair)
    mint_pubkey = mint_kp.pubkey()

    # Resolve payer/creator
    import os

    payer_str = args.payer
    if payer_str is None:
        # try signer from env
        pk = os.environ.get("SOLANA_PRIVATE_KEY")
        if pk:
            try:
                if pk.startswith("base64:"):
                    decoded = base64.b64decode(
                        pk.removeprefix("base64:"), validate=True
                    )
                else:
                    decoded = base58.b58decode(pk)
                kp = Keypair.from_bytes(decoded)
                payer_str = str(kp.pubkey())
            except Exception:
                payer_str = str(mint_pubkey)
        else:
            payer_str = str(mint_pubkey)

    try:
        payer_pubkey = Pubkey.from_string(payer_str)
    except Exception as exc:
        print(
            json.dumps({"status": "abstain", "message": f"invalid payer: {exc}"}),
            file=sys.stderr,
        )
        return 1

    creator_str = args.creator or payer_str
    try:
        creator_pubkey = Pubkey.from_string(creator_str)
    except Exception as exc:
        print(
            json.dumps({"status": "abstain", "message": f"invalid creator: {exc}"}),
            file=sys.stderr,
        )
        return 1

    # Validate name/symbol/uri strictly (no guessing)
    if not args.name or not args.symbol or not args.uri:
        print(
            json.dumps({"status": "abstain", "message": "name/symbol/uri required"}),
            file=sys.stderr,
        )
        return 1

    try:
        ix = build_create_v2_instruction(
            payer=payer_pubkey,
            creator=creator_pubkey,
            mint=mint_pubkey,
            name=args.name,
            symbol=args.symbol,
            uri=args.uri,
            mayhem_mode=bool(args.mayhem),
            cashback=bool(args.cashback),
        )
    except Exception as exc:
        print(json.dumps({"status": "abstain", "message": str(exc)}), file=sys.stderr)
        return 1

    accounts_payload = [
        {
            "name": meta.pubkey,
            "pubkey": str(meta.pubkey),
            "signer": meta.is_signer,
            "writable": meta.is_writable,
        }
        for meta in ix.accounts
    ]
    # Better: zip with names
    from rugbot.ingest.pump.create_decoder import CREATE_V2_ACCOUNT_NAMES

    accounts_detailed = [
        {
            "name": name,
            "pubkey": str(meta.pubkey),
            "is_signer": meta.is_signer,
            "is_writable": meta.is_writable,
        }
        for name, meta in zip(CREATE_V2_ACCOUNT_NAMES, ix.accounts, strict=True)
    ]

    data_b58 = base58.b58encode(bytes(ix.data)).decode()
    data_b64 = base64.b64encode(bytes(ix.data)).decode()

    sol_spent = args.buy_sol

    # fail-closed gate
    will_submit = bool(args.yes)
    mode = ExecutionMode(args.mode)
    if will_submit and mode is not ExecutionMode.LIVE:
        msg = "refusing to submit: --yes requires --mode live (fail-closed)"
        if args.json_output:
            print(json.dumps({"status": "abstain", "message": msg, "mode": mode.value}))
        else:
            print(msg, file=sys.stderr)
        return 1
    if will_submit and not args.yes:
        # unreachable
        pass

    # Simulate if rpc provided
    sim_result: dict[str, object] | None = None
    if args.rpc:
        sim_result = asyncio.run(_maybe_simulate(args.rpc, payer_pubkey, ix))

    payload = {
        "status": "dry_run" if not will_submit else "submitted",
        "would_submit": will_submit,
        "mode": mode.value,
        "mint": str(mint_pubkey),
        "payer": str(payer_pubkey),
        "creator": str(creator_pubkey),
        "name": args.name,
        "symbol": args.symbol,
        "uri": args.uri,
        "mayhem_mode": bool(args.mayhem),
        "cashback": bool(args.cashback),
        "buy_sol": sol_spent,
        "accounts": accounts_detailed,
        "data_base58": data_b58,
        "data_base64": data_b64,
        "simulation": sim_result,
    }

    if args.json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            json.dumps(payload, indent=2, sort_keys=True),
            file=sys.stderr if will_submit else sys.stdout,
        )
        if not will_submit:
            print(
                "DRY-RUN: transaction built and not submitted. Use --yes --mode live to submit.",
                file=sys.stderr,
            )

    if will_submit:
        # Live submission not fully implemented; requires signing and routing.
        # Fail-closed: we do not silently succeed.
        print(
            json.dumps(
                {
                    "status": "abstain",
                    "message": "live submission path not yet wired; dry-run only",
                }
            ),
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
