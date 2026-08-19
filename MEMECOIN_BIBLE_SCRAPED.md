# Memecoin Bible — Sniping Guide

*Guide méthodologique complet et opérationnel pour le sniping et copytrading de ruggers sur Pump.fun & Solana.*

---

## 1. Méthode 1 · Sniper via le Développeur (Méthode Reine)

**Principe :** Repérer un token qui vient de rug, remonter la source de financement du créateur sur Solscan, vérifier sa rentabilité statistique sur ses 10 derniers tokens, et sniper automatiquement ses prochaines créations dès le **bloc-0**.

```text
[ CEX / Master Wallet ] ──(Montant fixe ex: 2.5 SOL)──> [ Fresh Creator Wallet ] ──> [ Création Token Bloc-0 ] ──> [ Snipe B0 Immédiat ]
```

### Les 7 étapes de qualification d'un rugger

1. **Vérifier le potentiel de gain :** Il doit y avoir au moins **+100% d'amplitude** entre la fin de la 1ère bougie (post-bundle) et le point culminant (ATH).
2. **Contrôler la taille de la première bougie :** La première bougie ne doit pas dépasser **15 000 $ de Market Cap**. Au-delà, le point d'entrée est trop haut et le ratio Risk/Reward est dégradé par rapport au plancher de liquidité Pump.fun (~2 500 $).
3. **Tracer la source de funding (Solscan) :** Identifier quand, d'où et avec quel montant exact de SOL le wallet créateur a été approvisionné (ex: CEX Binance/Coinbase ou Master Wallet).
4. **Remonter l'historique des lancements :** Lister tous les tokens créés par la même source de funding sur les 7 à 10 derniers jours.
5. **Backtest sur 10 tokens minimum :** Vérifier sur les 10 derniers lancements combien ont atteint le palier **+100%**. Le **Winrate minimum doit être de 33%** (1 victoire à +100% couvre 2 pertes à -30%/-50%).
6. **Calcul de l'espérance mathématique :**
   $$\text{Gain Net} = (\text{Nb Wins} \times +100\%) - (\text{Nb Losses} \times 40\%)$$
   *Exemple :* 7 victoires (+700%) et 3 pertes (-120%) = **+580% net** $\rightarrow$ Opérateur hautement qualifié.
7. **Exécution automatisée :** Surveillance de la source de funding $\rightarrow$ détection immédiate de la création $\rightarrow$ achat bloc-0 $\rightarrow$ sortie automatique à +100% (TP) ou sur détection de dev-sell.

### Configuration des filtres de recherche (Axiom / Photon)

Pour repérer rapidement les ruggers qualifiés sans être pollué par le bruit :
- **Audit / Dev Creations :** `Max 5 à 10 creations` (élimine les spammeurs de masse sans système qui lancent 50 tokens sans pump).
- **Volume Minimum :** `≥ 20 000 $` (isole les tokens ayant eu une réelle activité de pump).
- **Colonnes essentielles à afficher :** Market Cap, Volume, Fees paid, Dev Creation count, Funding Time (masquer le reste : KOLs, Twitter, Insiders non fiables).
- **Unité d'analyse :** Toujours raisonner en **SOL** plutôt qu'en USD (plus précis pour évaluer la taille des bundles et liquidités).
- **Créneaux horaires clés :** Forte concentration de volume et d'opérateurs US/internationaux entre **00h00 et 06h00 UTC+1**.

---

## 2. Méthode 2 · Copytrader les Wallets de Pump / Clusters

Quand le développeur masque son adresse de création via des montants randomisés ou des mixeurs complexes, on traque directement les **wallets satellites qu'il utilise pour pump le token**.

### Les 4 techniques de détection des wallets de pump

1. **L'analyse des Bougies Rouges (Candle Inspection) :** Cliquer sur la bougie de dump massif pour lister les adresses exactes qui ont liquidé de gros montants au sommet.
2. **La Double Signature / Bundles :** Identifier les adresses qui achètent dans la même milliseconde / bloc-0 avec des montants similaires (ex: 4 wallets à 10 SOL chacun).
3. **Le Panier Croisé (Wallet de Référence) :** Vérifier sur GMGN/Axiom si le wallet suspect a acheté les mêmes tokens (`Token A`, `Token B`, `Token C`) que ceux attribués à l'opérateur.
4. **Le Dev Buy Pattern :** Le dev achète souvent une petite part symbolique sur son wallet public (ex: 0.1 SOL avec le badge créateur), pendant que ses wallets de bundle achètent 40% à 60% de la supply.

### Critères de qualification d'un wallet à copier
- **Point d'entrée bas :** Le wallet doit entrer dans le bloc-0 ou début bloc-1 (si son entrée est trop haute sur la courbe, le copytrade achètera au sommet de la bougie).
- **Winrate > 50%** sur ses 15 dernières opérations.
- **Profil de dump étagé :** 1 gros achat initial suivi de multiples ventes partielles (prise de profit méthodique).

---

## 3. Typologie des Schémas de Financement (Funding Patterns)

### 1. Le Schéma Direct / Naïf (Rare chez les pros)
`Wallet A` (Créateur 1) $\rightarrow$ `Wallet B` (Créateur 2) $\rightarrow$ `Wallet C` (Créateur 3).
- **Facilité de tracking :** Immédiate.
- **Inconvénient :** Souvent utilisé par des développeurs peu expérimentés avec peu de volume.

### 2. Le Schéma CEX / Échange Centralisé (Le plus fréquent et rentable)
L'opérateur utilise un compte Binance/Coinbase pour financer un fresh wallet par token :
`CEX Hot Wallet` ──(Montant récurrent, ex: 2.495 SOL)──> `Fresh Wallet` ──> `Création Token`.
- **Règle de tracking :** Créer des **intervalles de montants serrés** (ex: `2.40 - 2.60 SOL`) avec le filtre **`Fresh Wallet Only`** (rejette les wallets ayant un historique antérieur pour éviter les faux positifs du CEX).

### 3. Le Schéma Adresse Mère & Sous-Mères (Opérateurs Structurés)
`Master Wallet (> 200 SOL)` ──> `Sous-Mère (renouvelée toutes les 3-4h)` ──(9 SOL)──> `Wallets de Bundle (4 x 2.25 SOL)`.
- **Méthodologie :** Remonter les transactions de l'adresse intermédiaire pour capturer l'ensemble de la flotte avant le lancement.

---

## 4. Infrastructure & Avantage Bloc-0 (B0)

### L'impact économique de la latence

| Mode d'Exécution | Bloc d'Entrée | Prix d'Entrée Relatif | Marge de Gain sur TP 100% | Risque sur Plancher (2.5k MC) |
| :--- | :--- | :--- | :--- | :--- |
| **Bot Dédié (Jito / B0)** | **Bloc 0 (B0)** | Plancher (~3k$ MC) | **Plein potentiel (+150% à +300%)** | **Minimal (-10% à -20%)** |
| **Bot Public (Trojan/Bloom)** | **Bloc +1 à +2** | Milieu de pump (~8k-12k$ MC) | Faible (+20% à +40%) | Élevé (-60% à -80%) |
| **Trading Manuel** | **Bloc +4 à +5** | Sommet de bougie (~15k$+ MC) | Négatif (Achat du top) | **Perte totale (-80% à -90%)** |

> **Le manque à gagner de la latence :** Être 2e ou 3e après le bundle coûte en moyenne **~0.115 SOL par trade**. Sur 100 snipes (10 jours à 10 tokens/jour), cela représente **11.5 SOL (~2 000 $) de perte sèche** uniquement due aux frais et au glissement de prix.

### Décomposition et optimisation des 3 types de frais

1. **Base Network Fee :** Frais fixes Solana (~0.000005 SOL / 5 000 lamports).
2. **Priority Fee (Compute Unit Price) :** Frais pour inciter les validateurs standards à inclure la transaction.
3. **MEV / Jito Tip :** Pourboire direct aux leaders de bloc pour garantir l'inclusion dans le bundle bloc-0 sans subir de front-running.
   - *Règle :* Ne pas sur-payer aveuglément les priority fees sur les TX Senders ; équilibrer dynamiquement entre Jito Tip et Priority Fee selon la congestion du réseau.

---

## 5. Gestion du Risque & Discipline de Trading

> *"La technique s'apprend en quelques heures. La discipline détermine si vous serez encore là dans six mois."*

### Règles de capital strictes
- **Taille de position maximale :** **5% du capital total** par trade (ex: 50 € par snipe pour un capital de 1 000 € ; 100 € pour 2 000 €).
- **Nombre de targets actives :** Ne pas surveiller plus de **2 à 3 ruggers qualifiés simultanément** (chacun produisant 5 à 10 tokens par jour).
- **Durée de vie d'un rugger :** Un opérateur conserve généralement son schéma pendant **1 à 2 semaines** avant de modifier ses montants ou sa structure de funding. Auditer en continu.

### Circuit Breakers (Coupe-Circuits Émotionnels)
- **Circuit Breaker Journalier :** **5 pertes consécutives = arrêt immédiat** pour le reste de la journée. Aucune tentative de « se refaire » (Revenge Trading proscrit).
- **Circuit Breaker Hebdomadaire :** **-35% de drawdown sur la semaine = pause complète** et ré-audit complet des targets enregistrées en base de données.
- **Règle du TP Fixe :** Ne jamais transformer un trade mécanique à +100% en trade d'espoir de +500% par cupidité. La rentabilité provient de la régularité mathématique, pas des coups d'éclat isolés.

---

## 6. Synthèse des Métriques Clés

| Paramètre | Valeur Recommandée | Justification |
| :--- | :--- | :--- |
| **Winrate Minimum Target** | `≥ 33%` (1 win / 3 trades) | Assure une rentabilité nette positive avec TP +100% / SL -40% |
| **Market Cap Max Première Bougie** | `≤ 15 000 $` | Protège contre un effondrement vers le plancher de liquidité |
| **Taille de Sizing par Ordre** | `2% à 5% de la bankroll` | Encaisse les séries de 5 à 7 pertes sans entamer le capital |
| **Take Profit (TP)** | `+100%` | Point de sortie standard avant le dump du bundle opérateur |
| **Stop Loss / Dev Sell** | `Vente immédiate sur Dev-Sell` | Sortie dès que le créateur ou ses adresses de bundle initient la vente |
