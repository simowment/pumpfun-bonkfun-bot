from solders.keypair import Keypair

from rugbot.domain.account_roles import AccountRoleProof
from rugbot.execution.create_builder import (
    build_create_v2_instruction,
)
from rugbot.ingest.pump.create_decoder import (
    CREATE_V2_DISCRIMINATOR,
    PINNED_PUMP_IDL_SHA256,
    PUMP_CREATE_V2_DECODER_VERSION,
    CompiledPumpCreateV2Instruction,
    decode_pump_create_v2_instruction,
)


def test_round_trip_encode_decode():
    payer = Keypair().pubkey()
    creator = Keypair().pubkey()
    mint = Keypair().pubkey()
    name = "TestCoin"
    symbol = "TST"
    uri = "https://example.com/meta.json"
    for mayhem, cashback in [(False, False), (True, True), (False, True)]:
        ix = build_create_v2_instruction(
            payer=payer,
            creator=creator,
            mint=mint,
            name=name,
            symbol=symbol,
            uri=uri,
            mayhem_mode=mayhem,
            cashback=cashback,
        )
        # verify discriminator
        assert bytes(ix.data[:8]) == CREATE_V2_DISCRIMINATOR
        # Build compiled instruction for decoder
        account_pubkeys = tuple(str(m.pubkey) for m in ix.accounts)
        # decoder expects account_pubkeys length covering program_id_index too, but we provide minimal
        # ensure program_id_index points to pump program within account_pubkeys: append pump program if needed
        # Our ix accounts are 16, but decoder expects 16 indices referencing full tx account_keys.
        # Simulate indices 0..15
        instruction = CompiledPumpCreateV2Instruction(
            as_of_slot=1,
            program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            program_id_index=15,
            account_indices=tuple(range(16)),
            account_pubkeys=tuple(str(m.pubkey) for m in ix.accounts)
            + ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",),
            account_role_proofs=tuple(
                AccountRoleProof(name=n, pubkey=str(m.pubkey))
                for n, m in zip(
                    [
                        "mint",
                        "mint_authority",
                        "bonding_curve",
                        "associated_bonding_curve",
                        "global",
                        "user",
                        "system_program",
                        "token_program",
                        "associated_token_program",
                        "mayhem_program_id",
                        "global_params",
                        "sol_vault",
                        "mayhem_state",
                        "mayhem_token_vault",
                        "event_authority",
                        "program",
                    ],
                    ix.accounts,
                    strict=True,
                )
            ),
            data=bytes(ix.data),
            transaction_index=0,
            outer_instruction_index=0,
            signature=None,
        )
        # Need to fix account_pubkeys to include program correctly: last index is program already
        # decoder validates fixed keys, which we already provide.
        decoded = decode_pump_create_v2_instruction(
            instruction,
            idl_hash=PINNED_PUMP_IDL_SHA256,
            decoder_version=PUMP_CREATE_V2_DECODER_VERSION,
        )
        assert not hasattr(decoded, "reason"), f"decode abstained: {decoded}"
        assert decoded.name == name
        assert decoded.symbol == symbol
        assert decoded.uri == uri
        assert decoded.is_mayhem_mode is mayhem
        assert decoded.is_cashback_enabled is cashback
