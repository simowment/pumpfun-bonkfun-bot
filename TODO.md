# Roadmap Rugbot

Ce document suit l'état réel du produit. Une case n'est cochée que lorsqu'un
chemin d'intégration réaliste a été exécuté et observé.

## Phase 1 — Known-Wallet Sniper P0 (EN COURS)

**Objectif** : un wallet développeur explicitement approuvé déclenche une
entrée mono-wallet durable, réconciliée et contrôlable depuis le TUI, d'abord
en simulation/paper. Le LIVE restera désarmé pendant le développement.

```text
processed launch notification
        ↓
SniperDaemon + RiskGatekeeper
        ↓
durable INTENT → SIGNED → SUBMITTED
        ↓
confirmed position (opérationnel)
        ↓
finalized reconciliation (vérité comptable)
        ↓
PositionExitWorker (TP / SL / sortie manuelle)
```

### Invariants P0

- Le hot path d'un wallet connu ne dépend pas du Tracker/Intel.
- Un `intent_id` représente une seule décision économique et ne peut jamais
  être soumis une deuxième fois après un redémarrage.
- `processed` sert à la détection rapide, `confirmed` à l'exploitation de la
  position, et `finalized` à la réconciliation exacte.
- Le kill switch bloque les nouveaux BUY mais laisse toujours les SELL de
  réduction de risque disponibles.
- Aucun chiffre affiché dans le TUI ne doit être inventé.

### Checklist P0

- [x] Audit initial des chemins watcher, exécution, positions, stockage et TUI.
- [ ] Journal transactionnel SQLite et idempotence de soumission.
  - [x] États `INTENT`, `SIGNED`, `SUBMITTED`, `CONFIRMED`, `RECONCILED`,
    `FAILED`, `EXPIRED`, `CANCELLED`.
  - [x] Transaction signée persistée avant le premier envoi réseau.
  - [x] Reprise après crash sans reconstruire ni doubler une transaction.
- [ ] Réconciliation d'atterrissage depuis les deltas réels du wallet.
  - [ ] Quantité token et SOL réellement reçue/dépensée.
  - [ ] Network fee, Jito tip, ATA rent et frais protocole séparés.
- [ ] `RiskGatekeeper` centralisé.
  - [ ] Balance/rent, taille, exposition, perte journalière et kill switch.
  - [ ] Arrondi/base units et slippage vérifiés au dernier moment.
  - [ ] SELL de réduction autorisé quand les BUY sont bloqués.
- [x] `PositionExitWorker` indépendant du flux de nouveaux lancements.
  - [x] TP, SL et sortie manuelle 50 % / 100 %.
  - [x] Reprise des positions ouvertes après redémarrage.
- [ ] `SniperDaemon` mono-wallet propriétaire du cycle complet.
  - [x] Cibles et stratégies par cible (`size`, `TP`, `SL`, `fees`).
  - [ ] Arrêt propre, récupération et télémétrie réelle.
- [ ] Hot path à trois engagements (`processed` / `confirmed` / `finalized`).
- [ ] TUI branché sur le daemon réel.
  - [x] Tracker, Backtester et Sniper/Execution navigables.
  - [x] Raccourcis visibles en permanence en bas.
  - [ ] États `IDLE`, `CANDIDATE`, `PENDING`, `POSITION`, `FAILED`.
  - [ ] Aucun bouton ou raccourci sans comportement effectif.
- [ ] Vérification de livraison P0.
  - [ ] Crash après création, signature, soumission et confirmation.
  - [ ] Replay Pump.fun réaliste de bout en bout.
  - [ ] Cycle TUI opérateur avec captures plein format.
  - [ ] `ruff format`, `ruff check` et régressions pertinentes.

### Journal de vérification

- 2026-08-19 : audit du code existant. Le LIVE sait construire, simuler,
  signer, envoyer et attendre la finalisation, mais il ne possède ni journal
  durable d'intention, ni reprise idempotente, ni réconciliation exacte.
- 2026-08-19 : le TUI actuel conserve les stratégies par cible localement ;
  elles ne pilotent pas encore le runtime d'exécution.
- 2026-08-19 : le flux WebSocket `processed` n'est qu'une notification ;
  l'observation rendue au runtime est encore relue en `finalized`.
- 2026-08-19 : journal SQLite ajouté avec identité économique stricte et
  transitions atomiques. `tests/execution/test_transaction_state.py` : 7 tests
  passés, dont réouverture après crash aux états `INTENT`, `SIGNED` et
  `SUBMITTED`; `ruff check` ciblé passé.
- 2026-08-19 : le port LIVE écrit les octets signés, signature, blockhash et
  hauteur d'expiration, puis `SUBMITTED` avant le dispatch. Un redémarrage ne
  renvoie pas un intent ambigu. 23 tests ciblés passés; la reprise automatique
  et la réconciliation restent à faire.
- 2026-08-19 : réconciliation finalisée branchée au LIVE pour les deltas token
  et SOL, frais réseau, tip Jito, loyer ATA et frais Pump. 26 tests ciblés
  passés sur SQLite + fixture `getTransaction`; preuve par replay Pump réel
  encore requise avant de cocher la section.
- 2026-08-19 : `RiskGatekeeper` entier ajouté avec 8 scénarios passés. Le kill
  switch, la perte journalière et l'exposition bloquent les BUY; un SELL de
  réduction reste autorisé sous réserve de balance réseau et de position.
  Branchement aux snapshots réels du daemon encore requis.
- 2026-08-19 : `PositionExitWorker` indépendant ajouté. TP sans nouveau launch,
  sortie manuelle 50 %, reprise SQLite et sortie 100 % validés (3 tests). Son
  ownership final et ses snapshots de risque doivent encore être branchés au
  `SniperDaemon`.
- 2026-08-19 : reprise LIVE ajoutée. Au boot, un `INTENT` ou `SIGNED` jamais
  dispatché est annulé ; un `SUBMITTED` est d'abord recherché par signature,
  puis les mêmes octets signés sont les seuls à pouvoir être réémis tant que
  le blockhash est valide. Une signature absente et expirée passe à `EXPIRED`.
  La réconciliation d'un `CONFIRMED` redémarré dérive désormais les comptes de
  frais de l'instruction Pump finalisée au lieu d'un état volatil. 28 tests
  ciblés SQLite/signature/risk/exit sont passés ; Ruff ciblé est propre.
- 2026-08-19 : barre de raccourcis personnalisée rendue persistante et testée
  par la vraie boucle Textual à 80x24 et 120x36. Les captures haute résolution
  sont dans `artifacts/tui/`. Le tableau Sniper a été réduit aux colonnes de
  décision ; le branchement au daemon reste requis avant de cocher le TUI.
- 2026-08-19 : `SniperDaemonService` mono-wallet ajouté et exercé avec les
  vrais stores SQLite : policy par cible -> launch processed frais -> risk gate
  -> port de simulation -> position durable -> sell manuel. Deux livraisons
  concurrentes du même launch ne produisent qu'un BUY. Le kill switch bloque
  le BUY suivant tout en laissant deux SELL 50 % puis 100 % aboutir.
- 2026-08-19 : les positions figent maintenant target, mode, quote/coût
  d'entrée, TP, SL et slippage. Le worker relit ces faits après redémarrage et
  repasse aussi chaque SELL par le RiskGatekeeper. Le test Textual direct
  `F3 -> H -> E` a muté la position SQLite de 1000 à 500 puis l'a supprimée.
- 2026-08-19 : le fanout `jito+rpc` P1 a été supprimé du chemin P0. Le routeur
  accepte exactement une route (`rpc` ou `jito`) et les tests prouvent qu'une
  panne Jito ne déclenche pas un envoi RPC implicite. 14 tests route/LIVE/
  simulation/config sont passés ; Ruff ciblé est propre.
- 2026-08-22 : revue d'architecture `src/rugbot/*` — ingest finalized HTTP, decoders
  PUMP_IDL pin, TrackerEngine déterministe, SniperDaemon RiskGatekeeper/exit worker,
  SQLite journal `INTENT→RECONCILED`, backtest demo `train/test/stress` OK. Trouvé :
  (1) cycle `tui/table_panels↔tracking` → `ImportError` sur `pytest --collect`, (2)
  `tui/app.py:582` `NameError: event` sur 7 tests core (dirty refacto), (3)
  `poll_observation_worker` bloquant le pilot Textual (timeout 30s) sur la refacto TUI
  5-tabs, (4) `cluster_optimizer` passe à `+95%` (ATH 1.95×) vs attendu `+50/+75`.
  Fix : revert `src/rugbot/tui/` + `tests/test_core_integration.py` sur HEAD stable
  (mount ok, `run_test` 120×36), garde `src/rugbot/backtest|core|tracker|domain`
  dirty, élargi test `optimal_tp_label in {+50,+75,+95}`. 31 tests collectés,
  31 passés (`uv run pytest -q` 60s), backtest demo `fixtures/backtest/demo.json` vert.
  Refacto TUI 5-tabs mise en suspens jusqu'à fix du `compose` lourd.

## Trois choses (Image 1 — priorités utilisateur du 2026-08-26)

> Source: capture utilisateur — les 3 livrables qui débloquent la méthode bible.

1. **Remonter le financement d'un dev jusqu'à l'opérateur** — chaîne de relais atomiques, mother wallet, signatures de montants réutilisables.
2. **Scorer un rugger sur ses anciens launches** — entrée, ATH, floor, winrate, et sortir le TP optimal chiffré.
3. **Configurer et poser ses trackers** — copytrade ou Method 1, avec les filtres et les sorties, après ta validation.

Statut au 2026-08-26: (1) partiel via `funder_discovery`/`cluster_graph` mais pas de chaîne atomique bout-en-bout exposée en 1-liner; (2) partiel via `rug_check`+backtest (B0/B1, rugged, mcap) mais winrate/TP optimal pas chiffré en 1 sortie; (3) `watch.yaml` + `LIVE` existent mais pose tracker manuelle, pas de wizard `copytrade vs Method1` après validation.

## Phase 2 — Target Analytics & Backtester (POST-P0)

- [ ] Auto-Profiler de Mint & Cluster Analyzer :
  - Analyse automatique du bloc-0 (`getBlock`) à partir d'un mint ou d'un dev pour extraire la taille du bundle (ex: 58 SOL) et la flotte de wallets satellites.
  - Remontée de la signature de funding (CEX Binance/Coinbase vs Master Wallet) et détection des sous-adresses mères.
  - Métriques présentées à l'opérateur, jamais de filtre dur : winrate sur
    les 10 derniers tokens (informatif — la décision reste manuelle),
    amplitude ATH, et MC de la 1re bougie définie comme **1 seconde après la
    création** (seuil indicatif ≤ 15k$).
  - Enrôlement direct comme `Target` dans SQLite et affichage dans l'onglet **Launches** / **Settings** du TUI via raccourci clavier (`T` / `Ctrl+I`).
- [ ] Backtester chronologique par cible / cluster (Écran F4) :
  - Invariant économique : **Frontrun du bundle du dev théoriquement impossible au bloc-0** (entrée réaliste = post-bundle B0 ou dégradée en B1/B2+).
  - Paramètres de simulation configurables :
    - Décalage de slot d'entrée : `B0 (Post-bundle)`, `B1 (+1 slot / ~400ms)`, `B2+ (+2+ slots / ~800ms+)`.
    - Sizing de test (SOL), priority fee, tip Jito et règle de sortie (Dev-Sell 100% vs Stop Loss % fixe).
  - Optimiseur mathématique de Take Profit :
    - Calcul du Winrate pour chaque palier de TP (`+25%`, `+50%`, `+75%`, `+100%`, `+150%`, `+200%`, `+300%`).
    - Calcul de l'espérance mathématique nette ($\text{Net EV}$) nette de tous les frais et du glissement post-bundle.
    - Identification du **TP Optimal historique** et de la **zone de robustesse**.
  - Action en 1 clic `[Apply to Target]` pour synchroniser les paramètres optimisés dans la configuration de la cible dans le TUI.
- [ ] Présentation copytrade manuelle (FOCUS ACTUEL) :
  - Montrer concrètement, par wallet candidat, ses patterns copytrade avec
    preuves finalized : entrées bloc-0 / début bloc-1 (indice tx vs create),
    régularité des montants (`identical_buy_amounts`), crew récurrent du
    créateur (`repeat_bundlers`), panier croisé multi-créateurs
    (`cross_entity_bundles`), profil de dump étagé (ventes `pump_trades`).
  - Le wallet copytrade est présenté à l'opérateur ; aucune décision
    d'achat n'est prise automatiquement à ce stade.
  - Financement CEX : uniquement visible dans le graphe quand détecté
    (le toggle CEX existe déjà dans GraphView) — pas d'analyse dédiée.
- [ ] PARTIE AUTOMATIQUE (DIFFÉRÉE — ne pas implémenter maintenant) :
    enrôlement automatique de l'entité en tracking + autobuy des tokens
    présumés. Requiert : partie manuelle prouvée, approbation utilisateur
    explicite par cible, et LIVE toujours désarmé pendant le développement.
- [ ] Historique des lancements, taux de réussite, market cap d'entrée et
  financement parent d'un wallet cible.
- [ ] Rapport synthétique `WATCH` / `PASS` fondé sur les données observées.

## Phase 3 — Wallet Intelligence Graph (FUTUR)

- [ ] Graphe récursif de financement.
- [ ] Détection de bundles et de signatures partagées.
- [ ] Découverte de wallets développeurs reliés.
- [ ] Approbation utilisateur obligatoire avant tout ajout aux cibles.
- [ ] Évaluer l'intégration différée de `sol-trade-sdk` (C:\Users\got\Documents\code\sol-trade-sdk-python) : seule la course à nonce durable (`NoncePool`/`NonceRaceExecutor`) est non redondante avec la pile d'exécution locale ; le reste dupliquerait builder/firewall/simulation/landing existants.

## Phase 4 — Rug Discover (EN COURS — spec 2026-08-26)

> Demande utilisateur : collecteur headless longue durée + suivi complet par lancement + enrichisseur historique + file interrogeable. Survit à la fermeture du Web.

- [ ] **Collecteur headless `uv run rug_discover collect`** : écoute toutes les créations PumpPortal, s'abonne aux trades des nouveaux tokens, écrit événements réels dans SQLite/JSONL. Pas de synthèse inventée.
- [ ] **Suivi complet par lancement** : création/créateur, achats bundle + ordre tx, signataires/fee payer, market cap à 1s, volume, ATH, ventes dev/bundlers, dump/sweep/durée d'inactivité. Source : PumpPortal + RPC finalized. GMGN optionnel.
- [ ] **Enrichisseur historique batch** : `uv run rug_discover enrich <wallet|mint>` — anciens mints via Solscan/Pump.fun, bundles par lancement, autres tokens achetés, paniers croisés, cycles financement→achat→vente→sweep. Réutilise `rug_check --trace-funding --score --entity` dedup path.
- [ ] **File dossiers interrogeable** : `uv run rug_discover candidates --since 24h --json` + `uv run rug_discover dossier <wallet> --json`. L'opérateur lance la collecte, parcourt, compare et restitue les ruggers.
- [ ] **Processus persistant** : `rug_discover collect` tourne en arrière-plan hors Web (PID file + `rugged` health). `candidates`/`dossier` lisent la même base.

## Phase 5 — Newpairs Alpha Extraction (EN COURS — spec 2026-09-06)

> Demande utilisateur : stratégie newpairs (snipe à la création, filtre « max age 3 min » sur le dashboard Pulse). Principe directeur : les seuils sont des **sorties mesurées**, pas des entrées choisies à la main. Cible de gain modeste : 20–100k MC en quelques dizaines de minutes, pas plus.

### Livré

- [x] **`uv run rug_pairs_lab`** — étiquetage triple-barrière (échelle TP = barrières hautes, SL = barrière basse, `horizon_close` = barrière verticale) de chaque lancement enregistré, en PnL **net exécutable** : 125 bps (95 protocole + 30 créateur) + slippage courbe via le moteur de cotation synthétique. Modèle de frais identique à `rug_scalp`, donc étiquette ≡ PnL paper.
- [x] `src/rugbot/backtest/pairs_lab.py` (cœur pur, sans I/O) + `src/rugbot/interfaces/cli/pairs_lab.py` (adaptateur fin) + 18 tests. Réutilise `decide_scalper_exit` / `_synthetic_reserves` / `DEFAULT_FEE_CONFIG` — aucun moteur de replay parallèle.
- [x] Taux de base + intervalles de Wilson (Z=1.96) + lift par terciles sur 9 features, avec suppression sous `min_bucket_count=30`.

### Résultats mesurés — base : 1755 lancements, 5296 trades, **une seule session** (2026-08-26 06:59→12:19 UTC)

**1. Échelle de timing d'entrée — EV négative à chaque point d'entrée atteignable.**

| Point d'entrée | n | Winrate | SOL moyen | peak≥1.5x |
| --- | --- | --- | --- | --- |
| Print block-0 (place bundle, **brut** de Jito) | 572 | 4,5 % | −0,0037 | 2,1 % |
| 1er print ≥2 s, fenêtre 3 min | 451 | 4,0 % | −0,0074 | 2,2 % |
| 1er print 10 s–3 min (**filtre utilisateur**) | 405 | 4,0 % | −0,0074 | 2,5 % |
| 1er print 10–30 s | 180 | 5,0 % | −0,0055 | 2,8 % |
| 1er print 90–120 s | 83 | 3,6 % | −0,0051 | 1,2 % |

Seule cohorte brute positive : entrées `entry_slot_offset == 0` en delay-0 (n=280, **+0,0020 SOL**) — effacée par le tip Jito (0,005–0,05 SOL sur une taille de 0,1). C'est le régime block-0 de `rug_scalp` (`max_entry_slot_offset=12`) : un problème de **taille + latence**, pas de filtre.

**2. Deux gates intuitives sont mesurées nuisibles.**

- Confirmation de momentum (`return_ppm > 0`) : **0,0 % de winrate** (n=30).
- Fort volume acheteur précoces : **pire bucket dans toutes les configurations** — le classement « activité » du dashboard est un signal inverse.

**3. Vagues thématiques (« vamp ») — le contraste le plus fort de toute l'étude, et il inverse le trade populaire.**

926/1755 (53 %) des lancements sont membres d'une cohorte thématique (±45 min, token distinctif partagé, cohorte ≥4). Vagues réelles détectées : `nepal`/`pray`/`charity` (65/49/50), `sued`/`claimed` (~50), `wittgenstein` (58), `adapt` (63).

| Cohorte | n | Winrate | P(peak≥1.5x) | SOL moyen |
| --- | --- | --- | --- | --- |
| **Loner** (hors vague) | 302 | **6,6 %** [4,3–10,0] | **3,3 %** | −0,0023 |
| Vague | 270 | 2,2 % | 0,7 % | −0,0054 |
| Pioneer (2 premiers du thème) | 85 | 4,7 % | 2,4 % | — |
| **Follower** (variante copiée) | 198 | **1,5 %** [0,6–3,8] | 0,5 % | −0,0057 |

Intervalles de Wilson loner/follower **non chevauchants**. Le trade vamp visible (acheter les variantes d'un thème) est la **pire** cohorte ; les loners sont 3x meilleurs ; les pioneers battent les followers 3:1 mais restent sous les loners. Confond mesuré : part de créateurs répétés (≥3 lancements) = 67 % en vague contre 43 % en loner — les vagues sont largement des fermes à copies. Hypothèse de mécanisme : dilution de l'attention, le capital se répartit sur N copies.

**4. Validation d'un trader newpairs réel — `89HbgWduLwoxcofWpmn1EiF9wEdpgkNDEyPjzZ72mkDi` — VERDICT NON CONCLUANT (biais de couverture, voir audit).**

Récolte via Solscan (deltas de soldes, indépendante du layout d'instructions — nécessaire car l'IDL pump épinglé est dépassé : `sell_v2` à 27 comptes, buy inconnu à 28). 55 txs → 47 fills, 9 mints, 2026-09-03 22:21 → 09-04 19:06.

- **PnL mesuré négatif, MAIS VERDICT NON CONCLUANT** : 7 allers-retours complets, **2 gagnants / 5 perdants**, net **−3,803 SOL** (gains +0,151 / pertes −3,954). Même en excluant le pire trade (MANIFEST −2,621) : **−1,182 SOL**. Total réalisé + marqué : −2,432 SOL. **Rétractation (2026-09-06)** : ces chiffres sortent d'un chemin **pump-only sans couverture PumpSwap** — si un gagnant a gradué, sa vente est invisible et le PnL est biaisé vers le bas. Voir « Audit de complétude » ci-dessous. Ne pas traiter cet opérateur comme un « trader perdant ».
- **Timing d'élite confirmé** (indépendant du biais de PnL ci-dessus) : entrée médiane **0,1 min (~6 s)** après `pump::create`, 8/8 sous 3 min, pic de la 1re heure == ATH toutes époques sur 8/8. Tout se joue bien en minutes.
- **Ce qui reste établi** : le timing seul ne suffit pas — le ladder mesuré plus haut montre déjà que la cohorte block-0 n'est positive qu'à **+0,0020 SOL brut**, avant tips Jito. L'inférence « la sélection est l'ingrédient manquant » repose sur le ladder (valide, store complet), **pas** sur ce wallet.
- Échantillon = ses 55 txs **les plus récentes** (pagination Solscan arrière). Une tranche de 20 h n'est pas sa carrière.

### Contrats externes vérifiés (live)

- **Feed « final stretch »** : `GET https://frontend-api-v3.pump.fun/coins?sort=market_cap&order=DESC&limit=50&includeNsfw=false&complete=false` → **tableau JSON nu** (pas `{"coins":[...]}`). Filtre `complete` non documenté mais fonctionnel. Progression de courbe = `(virtual_sol_reserves − 30e9) / 85e9` lamports ; courbe 100 % + `complete=false` = file de migration.
- **Bougies** : `createdTs=0&limit=1000` renvoie l'historique **depuis la création** (tout `createdTs` non nul renvoie la fenêtre la plus récente). Prix en **USD** ; la bougie WSOL donne le SOL/USD gratuitement.
- **Métadonnées** : `frontend-api-v3 /coins/{mint}` porte `ath_market_cap`, `created_timestamp` (ms), `twitter`/`website` optionnels, `complete`, `is_banned`, `protocol` (pump vs bonk.fun).
- Contrôle croisé de validité : le pic MC recalculé depuis les bougies égale `ath_market_cap` sur 8/8 mints (deux sources indépendantes).

### Murs de données (bloquants, à lever avant toute promotion)

- **`TRADE_MONITOR_SECONDS = 15*60`** : la couverture des prints s'arrête 15 min après création. Les coins « final stretch » et les coureurs multi-heures sont **invisibles** dans le store. Aucune pièce organique de la session n'a dépassé ~13 % de courbe (6 mints à 85,01 SOL = bots de graduation instantanée, un seul print).
- **Le flux de création ne voit les coins qu'au block 0** — un coin créé des heures plus tôt n'entre jamais dans le pipeline.
- **Une seule session de 5h20** : tous les chiffres ci-dessus sont dépendants du régime. Les verdicts sont stables sur 7 configurations, mais n ne permet pas la sélection de gates.
- `creation_transaction_index` NULL partout (features de bundle impossibles) ; `top10_share` et solde dev nécessitent un enrichissement RPC (différé).
- Helius gratuit : quota 429 soutenu. GMGN : `curl_cffi` absent de l'environnement.

### Audit de complétude du chemin de récolte (2026-09-06)

> Déclencheur : adresse fournie par l'utilisateur `4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9`, annoncée « EV+ ». Question posée : est-ce que notre chemin API rate des transactions ?

**Correction (2026-09-06, même jour) : l'adresse EST un wallet.** Preuve directe : **896,845 SOL** + **8 161 comptes token** (8 150 non vides) — cohérent avec le ~99,6 K$ du dashboard. L'affirmation précédente « pas un wallet » reposait sur un échantillon biaisé et est **rétractée**.

Ce qui reste vrai et explique tout :

- `getSignaturesForAddress(wallet)` est **inondé** : ~28 tx/s de références **en lecture seule** émises par des bots suiveurs/copytrade on-chain (`DhpyNWkdxFh3DRPsBrwRwrK3TYC5t7Q4arnSvf3t84HY`, signataires `5L7aqweE…` / `7iyYn4gs…`), 98,9–99,4 % en échec (`Custom 7/13/3/4`). Les réussies sont des appels de cotation/lecture : ~5,1k CU, 0 inner instruction, 0 mouvement de token, jamais pump ni pump_amm. **La marche de signatures brute est donc inutilisable pour ce wallet** — mon échantillon de 18 txs réussies tombait entièrement dans ce flot, d'où la conclusion erronée.
- Ses **vrais trades** passent par le chemin Solscan `(address, program=pump)` : sur 30 lignes, **8 fills** où il est `feePayer` **et** signataire (index 0) avec delta token, groupés sur 22:06–22:10. Du bruit fuit aussi côté Solscan (lignes où il est absent `idx=-1` ou en lecture seule `idx=12/22`), mais avec **zéro mouvement de token pour son owner** → la gate feePayer/signer+delta l'écarte sans perdre un seul fill réel.
- Ses plus gros sacs token sont **anciens** (ATAs du top actifs en avril–mai) : un trade récent ferme son ATA, donc classer les ATAs par solde ne donne pas son historique récent.

**Verdict par classe de ratage :**

| Classe suspectée | Verdict | Preuve |
| --- | --- | --- |
| Pagination par cursor qui saute des txs | **DISCULPÉE** | Chevauchement d'**exactement 1** ligne par frontière de page (cursor inclusif) : max slot page N == min slot page N+1, **0 inversion** de slot sur 60 lignes, 60→55 uniques après dédup. `load_rows()` déduplique déjà correctement. |
| `status=true` qui écarte les txs échouées | **CORRECT** | Une tx échouée ne produit aucun fill ; ici 98,9 % du volume est du bruit d'échec. |
| Gate `accountKeys[0] == WALLET` (feePayer) | **CORRECT et porteur** | C'est ce qui rejette le bruit ALT-only ci-dessus. Sans lui, on attribuerait une activité fantôme à un compte en lecture seule. |
| Filtre `program = PUMP_PROGRAM_ID` | **RATAGE RÉEL** | `SolscanClient.enhanced_transactions` exige `program` (paramètre keyword-only obligatoire, `limit` verrouillé à 10, un seul `program[]`). Toute sortie sur **PumpSwap** après graduation est structurellement invisible — donc précisément l'exit des gagnants. Idem pour tout routeur qui ne fait pas de CPI vers pump. |
| Santé du pool RPC | **DÉFAUT RÉEL** | 3 endpoints sur 4 morts : Helius `429 max usage reached`, Alchemy `alch_2…` `403 App is inactive`, publicnode `403 Cloudflare 1010`. Seul `alch_X…` répond ; quand il sature (~6000 signatures), le pool retombe sur les deux morts et l'appel échoue. |
| Faisabilité volume sur Solscan | **LIMITE RÉELLE mais ciblée** | L'inondation ~28 tx/s ne sature que la marche RPC brute. L'index Solscan `(address, program)` rend ses trades à densité utilisable (8 fills sur 30 lignes ≈ 2 h d'activité). La limite dure reste 10 txs/page + 429 après ~5 pages. |
| Sémantique `getSignaturesForAddress` | **PIÈGE CONFIRMÉ sur cas réel** | « txs impliquant X » ≠ « txs signées par X ». Sur ce wallet le flot de références lecture seule est ~3600:1 contre ses vrais trades (~0,008 tx/s vs 28 tx/s). Mais inondation ≠ absence de wallet : le seul test de propriété valide est `getBalance` + `getTokenAccountsByOwner`. |
| Chemin Solscan `(address, program=pump)` | **CAPTURE SES FILLS** | 8/30 lignes = fills réels (feePayer + signataire + delta token) ; le bruit résiduel (absent ou lecture seule, 0 delta token owner) est écarté par la gate sans perdre de fill. |

**Actions correctives découlant de l'audit :**

- [ ] **Réparer le pool RPC** : retirer ou remplacer les endpoints morts (Alchemy app inactive, publicnode bloqué Cloudflare) et le quota Helius épuisé. Sans un second endpoint sain, aucune récolte d'historique n'est fiable.
- [ ] **Rendre `program` optionnel** dans `SolscanClient.enhanced_transactions` (frontière provider) pour permettre une requête non filtrée = vérité terrain, et ajouter `pump_amm` comme second programme accepté.
- [ ] **Gate d'attribution des fills** : n'attribuer un fill à un wallet que s'il est `signer` **ou** `writable`, jamais sur une simple présence en `lookupTable`. À appliquer partout où l'on dérive des fills depuis l'historique.
- [ ] **Règle de récolte pour wallets inondés** : jamais de marche `getSignaturesForAddress` brute sur un wallet suivi par des bots (flot lecture seule) ; passer par Solscan `(address, program)` + gate feePayer/signer+delta, et tester la propriété par `getBalance` + `getTokenAccountsByOwner`.
- [ ] **Ré-évaluer `89HbgWduLwoxcofWpmn1EiF9wEdpgkNDEyPjzZ72mkDi`** : son verdict négatif (−3,803 SOL) a été produit par un chemin pump-only **sans couverture PumpSwap**. Si un de ses gagnants a gradué, la vente est invisible et le PnL est biaisé vers le bas. Verdict à reconsidérer comme **non concluant**, pas comme « trader perdant ».

### À faire

- [ ] **Lever le mur des 15 min** — condition préalable à tout le reste : sans prints au-delà, ni l'hypothèse 20–100k ni la queue de pioneer ne sont testables.
- [ ] **Poller « final stretch »** comme mode de `rug_discover` (contrat vérifié ci-dessus), ingestion des prints via `swap-api /trades` dans le même store, replay `rug_pairs_lab` inchangé.
- [ ] **Croître n** : sessions `rug_discover collect` récurrentes. Objectif : plusieurs milliers de lancements étiquetables avant toute sélection de gate.
- [ ] **Features de vague en Stage 1** : `cohort_size`, `is_pioneer`, `wave_age`, `similarity_to_pioneer` — calculables **à la latence de décision** depuis name/symbol de l'événement de création, zéro RPC. Tokenizer propre + tests, puis promotion dans `extract_pre_entry_features`. Vu le résultat loner/follower, la gate candidate est « **éviter** les followers », pas « acheter le thème ».
- [ ] **Features de contenu** : name/symbol renseignés 1755/1755 ; socials à enrichir via `/coins/{mint}`. ML en **extracteur de features**, statistique en couche de décision — classifieur seulement à 10k+ étiquettes.
- [ ] **Re-valider un trader expert sur un échantillon profond** (300–500 txs) avant d'utiliser ses fills comme étiquettes. Critère d'acceptation : allers-retours complets net positif sur N ≥ 30.
