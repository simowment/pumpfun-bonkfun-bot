const AXIOM_SOLANA_QUERY =
  'chain=sol&pulseChains=sol&trackerChains=sol,robinhood,bnb,eth';

export function tokenLinks(mint, bondingCurve) {
  const mintAddress = encodeURIComponent(mint);
  return {
    axiom: bondingCurve
      ? `https://axiom.trade/meme/${encodeURIComponent(bondingCurve)}?${AXIOM_SOLANA_QUERY}`
      : null,
    solscan: `https://solscan.io/token/${mintAddress}`,
  };
}

export function accountLinks(wallet) {
  const address = encodeURIComponent(wallet);
  return {
    gmgn: `https://gmgn.ai/sol/address/${address}`,
    solscan: `https://solscan.io/account/${address}`,
  };
}
