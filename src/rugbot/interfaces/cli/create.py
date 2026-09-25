"""CLI for Pump.fun token creation and automated launch bundle assembly."""

# ruff: noqa: C901, PLR0911, PLR0912, PLR0915, BLE001, TRY003, TRY300, S110, PLC0415, PLR2004, TC002, ARG001

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import base58
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from rugbot.execution.create_builder import build_create_v2_instruction
from rugbot.execution.launch.bundle_assembler import (
    LAMPORTS_PER_SOL,
    SOLANA_TX_MTU_BYTES,
    assemble_launch_bundle,
)
from rugbot.execution.launch.exit_controller import (
    DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS,
    DEFAULT_TP_LEVELS,
    DEFAULT_TRAILING_STOP_PCT,
)
from rugbot.execution.launch.metadata_generator import (
    generate_template_metadata,
)
from rugbot.execution.sender.jito import JitoSender
from rugbot.ingest.pump.create_decoder import CREATE_V2_ACCOUNT_NAMES
from rugbot.runtime.config import ExecutionMode, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and launch a Pump.fun token (dry-run by default)"
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Optional topic/narrative to auto-generate token name, symbol, and metadata",
    )
    parser.add_argument("--name", type=str, default=None, help="Token name")
    parser.add_argument("--symbol", type=str, default=None, help="Token ticker symbol")
    parser.add_argument("--uri", type=str, default=None, help="Metadata URI")
    parser.add_argument(
        "--buy-sol", type=float, default=None, help="Optional atomic first buy in SOL"
    )
    parser.add_argument(
        "--tip-sol",
        type=float,
        default=0.003,
        help="Optional Jito tip in SOL (default: 0.003 SOL)",
    )
    parser.add_argument(
        "--creator", type=str, default=None, help="Creator pubkey (default: payer)"
    )
    parser.add_argument("--mayhem", action="store_true", help="Enable Pump Mayhem mode")
    parser.add_argument("--cashback", action="store_true", help="Enable Cashback mode")
    parser.add_argument(
        "--auto-exit",
        action="store_true",
        help="Attach automated exit ladder (TP ladder + trailing stop + dead launch refund)",
    )
    parser.add_argument(
        "--mint-keypair",
        type=Path,
        default=None,
        help="Optional path to existing mint keypair file",
    )
    parser.add_argument(
        "--rpc", type=str, default=None, help="Solana RPC HTTP endpoint"
    )
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


def _resolve_payer_keypair(payer_arg: str | None) -> tuple[Keypair, bool]:
    """Resolve payer keypair and boolean indicating if real private key is available."""
    pk = os.environ.get("SOLANA_PRIVATE_KEY")
    if pk:
        try:
            if pk.startswith("base64:"):
                decoded = base64.b64decode(pk.removeprefix("base64:"), validate=True)
            else:
                decoded = base58.b58decode(pk.strip())
            kp = Keypair.from_bytes(decoded)
            return kp, True
        except Exception:
            pass

    # In dry-run or when only pubkey string is given, generate ephemeral keypair
    ephemeral = Keypair()
    return ephemeral, False


async def _get_recent_blockhash(rpc: str | None) -> Hash:
    if not rpc:
        return Hash.default()
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
        if isinstance(resp, dict):
            val = resp.get("result", {}).get("value", {})
            bh_str = val.get("blockhash")
            if bh_str:
                return Hash.from_string(bh_str)
    except Exception:
        pass
    finally:
        await client.close()
    return Hash.default()


async def _simulate_transaction(
    rpc: str | None,
    tx: Transaction,
) -> dict[str, object] | None:
    if rpc is None:
        return None
    from rugbot.integrations.solana_rpc import SolanaClient

    client = SolanaClient(rpc)
    try:
        b64 = base64.b64encode(bytes(tx)).decode("ascii")
        sim_resp = await client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [b64, {"commitment": "finalized", "encoding": "base64"}],
            }
        )
        return {"simulated": True, "response": sim_resp}
    except Exception as exc:
        return {"simulated": False, "error": str(exc)}
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    resolve_dotenv(include_signing=True)

    # 1. Resolve Mint
    mint_kp = _load_or_generate_mint(args.mint_keypair)
    mint_pubkey = mint_kp.pubkey()

    # 2. Resolve Payer / Signer
    payer_kp, has_real_signer = _resolve_payer_keypair(args.payer)
    payer_pubkey = Pubkey.from_string(args.payer) if args.payer else payer_kp.pubkey()

    creator_str = args.creator or str(payer_pubkey)
    try:
        creator_pubkey = Pubkey.from_string(creator_str)
    except Exception as exc:
        print(
            json.dumps({"status": "abstain", "message": f"invalid creator: {exc}"}),
            file=sys.stderr,
        )
        return 1

    # 3. Resolve Metadata (Topic Auto-Gen or Explicit Arguments)
    name = args.name
    symbol = args.symbol
    uri = args.uri

    if args.topic:
        try:
            # Generate deterministic template or LLM metadata
            meta = generate_template_metadata(args.topic)
            name = name or meta.name
            symbol = symbol or meta.symbol
            uri = uri or meta.metadata_uri
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "abstain",
                        "message": f"metadata generation failed: {exc}",
                    }
                ),
                file=sys.stderr,
            )
            return 1

    if not name or not symbol or not uri:
        print(
            json.dumps(
                {
                    "status": "abstain",
                    "message": "either --topic or all of (--name, --symbol, --uri) must be provided",
                }
            ),
            file=sys.stderr,
        )
        return 1

    # 4. Amounts & Calculations
    buy_lamports = (
        int(args.buy_sol * LAMPORTS_PER_SOL)
        if args.buy_sol is not None and args.buy_sol > 0
        else 0
    )
    tip_lamports = (
        int(args.tip_sol * LAMPORTS_PER_SOL)
        if args.tip_sol is not None and args.tip_sol > 0
        else 0
    )

    # 5. Fetch Blockhash for Simulation / Assembly
    recent_blockhash = asyncio.run(_get_recent_blockhash(args.rpc))

    # 6. Assemble Atomic Launch Bundle
    try:
        bundle = assemble_launch_bundle(
            payer=payer_kp,
            mint=mint_kp,
            name=name,
            symbol=symbol,
            uri=uri,
            recent_blockhash=recent_blockhash,
            creator=creator_pubkey,
            buy_sol_lamports=buy_lamports,
            jito_tip_lamports=tip_lamports,
            mayhem_mode=bool(args.mayhem),
            cashback=bool(args.cashback),
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "abstain", "message": f"bundle assembly failed: {exc}"}
            ),
            file=sys.stderr,
        )
        return 1

    # 7. Detailed Account Breakdown for First Instruction
    create_ix = build_create_v2_instruction(
        payer=payer_pubkey,
        creator=creator_pubkey,
        mint=mint_pubkey,
        name=name,
        symbol=symbol,
        uri=uri,
        mayhem_mode=bool(args.mayhem),
        cashback=bool(args.cashback),
    )
    accounts_detailed = [
        {
            "name": acc_name,
            "pubkey": str(meta.pubkey),
            "is_signer": meta.is_signer,
            "is_writable": meta.is_writable,
        }
        for acc_name, meta in zip(
            CREATE_V2_ACCOUNT_NAMES, create_ix.accounts, strict=True
        )
    ]

    # 8. Execution Mode & Submission Gating
    will_submit = bool(args.yes)
    mode = ExecutionMode(args.mode)
    if will_submit and mode is not ExecutionMode.LIVE:
        msg = "refusing to submit: --yes requires --mode live (fail-closed)"
        if args.json_output:
            print(json.dumps({"status": "abstain", "message": msg, "mode": mode.value}))
        else:
            print(msg, file=sys.stderr)
        return 1

    # 9. Simulation (Simulate first transaction or create_ix)
    sim_result: dict[str, object] | None = None
    if args.rpc and bundle.transactions:
        sim_result = asyncio.run(
            _simulate_transaction(args.rpc, bundle.transactions[0])
        )

    # 10. Exit Ladder Config (if auto-exit requested)
    exit_config = None
    if args.auto_exit:
        exit_config = {
            "take_profit_ladder": DEFAULT_TP_LEVELS,
            "trailing_stop_pct": DEFAULT_TRAILING_STOP_PCT,
            "dead_launch_timeout_seconds": DEFAULT_DEAD_LAUNCH_TIMEOUT_SECONDS,
            "capital_recovery_active": True,
        }

    payload = {
        "status": "dry_run" if not will_submit else "submitted",
        "would_submit": will_submit,
        "mode": mode.value,
        "mint": str(mint_pubkey),
        "payer": str(payer_pubkey),
        "creator": str(creator_pubkey),
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "mayhem_mode": bool(args.mayhem),
        "cashback": bool(args.cashback),
        "buy_sol": args.buy_sol,
        "expected_tokens_out": bundle.expected_tokens,
        "tip_sol": args.tip_sol,
        "total_bundle_transactions": len(bundle.transactions),
        "total_instructions": bundle.instructions_count,
        "transaction_sizes_bytes": list(bundle.wire_sizes),
        "mtu_limit_bytes": SOLANA_TX_MTU_BYTES,
        "fits_mtu": bundle.fits_mtu,
        "accounts": accounts_detailed,
        "simulation": sim_result,
        "auto_exit_plan": exit_config,
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
                "\n[DRY-RUN] Atomic launch bundle assembled successfully. "
                "Use --yes --mode live to submit to Jito Block Engine.",
                file=sys.stderr,
            )

    # 11. Live Submission Path
    if will_submit:
        if not has_real_signer:
            print(
                json.dumps(
                    {
                        "status": "abstain",
                        "message": "live submission requires valid SOLANA_PRIVATE_KEY in environment",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        async def _submit() -> int:
            sender = JitoSender()
            try:
                res = await sender.send_bundle(list(bundle.wire_bytes_list))
                if res.acknowledged:
                    logger.info(f"Launch bundle accepted by Jito: {res.signature}")
                    return 0
                logger.error(f"Jito rejected bundle: {res.error_message}")
                return 1
            finally:
                await sender.close()

        return asyncio.run(_submit())

    return 0
