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
