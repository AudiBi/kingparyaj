# Lucky6 — King Paryaj

Jeu de tirage à **manches partagées** (une manche toutes les N minutes, la même pour tous les bureaux),
joué **uniquement au bureau, par un agent**. Ne pas confondre avec « Lucky Wheel » (ancien jeu « Lucky »).

## Règles

- 48 boules numérotées de 1 à 48, 8 couleurs de 6 boules.
- **Pari 6 numéros (`SIX`)** : le joueur choisit 6 numéros différents (à la main ou « Aléatoire », tiré par le serveur).
- Le serveur tire **35 boules**, dans un ordre précis.
- Si les 6 numéros du joueur sortent, le gain = mise × **multiplicateur de la position où sort le 6e numéro trouvé**
  (position 6 à 35). Plus il sort tôt, plus il paie. Arrondi au centime inférieur, plafonné au gain maximum par pari.
- **Pari 1re boule (`FIRST_ODD` / `FIRST_EVEN`)** : impaire ou paire, cote fixe (1,90 par défaut).

Probabilité exacte que le 6e numéro sorte en position k : C(k−1, 5) / C(48, 6).
Probabilité qu'un pari 6 numéros gagne (6e numéro dans les 35) : 13,23 %.

Table par défaut (taux de redistribution exact **85,06 %**) :

| Position | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| × | 10000 | 7500 | 5000 | 2500 | 1000 | 500 | 300 | 200 | 150 | 100 | 80 | 70 | 60 | 50 | 40 |

| Position | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 31 | 32 | 33 | 34 | 35 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| × | 30 | 25 | 20 | 15 | 12 | 9 | 8 | 7 | 6 | 5 | 4 | 3 | 2 | 1,5 | 1 |

## Architecture (aucune table créée)

| Élément | Où |
|---|---|
| Manche | `game_rounds` (`game_type = 'lucky6'`) : réglages **figés** dans `config`, 35 boules dans `result` |
| Pari | `game_bets` : `bet_type` SIX / FIRST_ODD / FIRST_EVEN, `selection` (6 numéros triés ou `[]`), `odds` figée |
| Argent | `WalletService` (compte) ou solde du ticket bureau ; références `BET-L6-`, `WIN-L6-`, `REFUND-L6-<pari>` |
| Cycle commun | `app/services/round_game_service.py` (`RoundGameService`, extrait de Horse Races, partagé avec lui) |
| Règles Lucky6 | `app/services/lucky6_service.py` (`Lucky6Service`) |
| Calculs purs | `app/services/lucky6_engine.py` (tirage, position du 6e, taux exact, ajustement de table, validation) |
| Équité | `app/services/fairness.py` (commun Keno / Horse Races / Lucky6) |
| Configuration | Redis `settings:lucky6`, validée par `lucky6_engine.validate_config` |
| Cycle automatique | tâche Celery `lucky6_tick` toutes les 3 s (beat `lucky6-tick`) |
| Écran guichet | `app/services/counter_screen.py` (jeu `l6`) |

### Refactorisation Horse Races

Le cycle de vie des courses a été déplacé, sans changement de comportement, dans `RoundGameService` :
création, ouverture / fermeture des paris, départ, arrivée, règlement idempotent, annulation avec remboursement,
tick automatique, validation + diffusion. `HorseRaceService` n'en garde que les règles propres aux courses
(concurrents, cotes, classement). Noms publics et imports existants conservés ; les 350 tests existants passent.

## Cycle d'une manche

`BETTING_OPEN` → (N s avant l'heure) `BETTING_CLOSED` → `RUNNING` (35 boules calculées depuis le seed) →
`FINISHED` (fin de l'animation) → `SETTLED` (paris réglés). `CANCELLED` possible avant la fin : mises remboursées.

- Les manches sont calées sur l'horloge (toutes les 5 min : 10:00, 10:05…), avec au moins 1 min de paris.
- La manche suivante est créée dès la fermeture des paris de la précédente : on parie sur la suivante pendant le tirage.
- Durée du tirage = présentation + 35 × intervalle entre boules + affichage du résultat (80 s par défaut).
- Prise de pari sous verrou **partagé** (paris simultanés illimités) ; transitions sous verrou exclusif.
- Règlement idempotent : `UPDATE … WHERE status = 'PENDING'` + référence unique de gain.

## Écrans

- **Panel agent** `/agent/lucky6` : grille de 48 boules, Aléatoire (serveur), Effacer, mises proposées,
  pari 1re boule, résumé (meilleur multiplicateur, gain maximum), BET, reçu imprimable (empreinte du tirage,
  lien de vérification), tirage en direct, derniers paris (numéros trouvés, position du 6e), derniers tirages.
- **Écran de salle** `/lucky6/ecran` : TV, thème cosmique, sphère, grande boule, 35 cases avec le multiplicateur
  de chaque position, grille 1-48, 1re boule, table de paiement, derniers tirages.
- **Écran du guichet** `/lucky6/ecran?c=<code>` : en plus, le ticket en saisie et les tickets du joueur,
  numéros trouvés « x / 6 » en direct, case de la table atteinte, « GAGNÉ ! » + montant. Aucune donnée d'identité.
- Les boules viennent du serveur ; l'animation les dévoile au rythme de l'**horloge du serveur** :
  un écran rechargé ou reconnecté reprend au même point. Temps réel par WebSocket, reprise automatique.
- **Vérification** `/lucky6/verifier?manche=<id>` : empreinte et 35 boules (ordre compris) recalculées
  par le serveur et dans le navigateur.

Méthode : empreinte = SHA-256(seed). Pour i = 47 à 13 : u = 7 premiers octets de
HMAC-SHA256(seed, `lucky6:<n° de manche>:<i>`) / 2^56, j = ⌊u × (i+1)⌋, échange des cases i et j
d'une liste 1…48 ; les cases 47 à 13 donnent les 35 boules dans l'ordre de sortie.

## Configuration (admin → Jeux → Lucky6)

Modifiable à tout moment ; appliquée aux **prochaines manches** (une manche créée garde ses réglages).

- Lucky6 ouvert / fermé ; manches automatiques ; heures d'ouverture (Haïti) ;
- intervalle entre manches, fermeture des paris, rythme du tirage (refusé s'il dépasse l'intervalle) ;
- mises min / max (max ≤ 100 000 HTG), mises proposées, gain maximum par pari ;
- table de paiement (positions 6 à 35, 0 = non payée, jamais croissante) avec probabilité et part du taux
  de chaque ligne, **taux exact** recalculé par le serveur ; « Ajuster au taux cible » (proportions gardées,
  arrondi vers le bas, jamais au-dessus de la cible) ; « Table par défaut » ;
- taux de redistribution maximum : une table ou une cote pair/impair au-dessus est refusée ;
- pari pair / impair activé ou non, et sa cote.

Liste des manches avec actions (ouvrir, fermer, tirer, régler, annuler + remboursement) et détail des paris.
Chaque modification est journalisée (`lucky6_config`, ancienne et nouvelle valeur).

## API

| Route | Qui | Rôle |
|---|---|---|
| `GET /agent/lucky6` | agent | page de jeu |
| `GET /agent/api/lucky6/state` | agent | manche affichée, manche ouverte, règles |
| `GET /agent/api/lucky6/quick-pick` | agent | 6 numéros aléatoires (serveur) |
| `POST /agent/api/lucky6/bet` | agent + CSRF | pari (ticket ou compte) |
| `POST /agent/api/lucky6/screen` | agent + CSRF | aperçu sur l'écran du guichet (affichage seulement) |
| `GET /agent/api/lucky6/my-bets`, `/bets/{id}`, `/history` | agent | ses paris, réimpression, tirages |
| `GET/PUT /admin/api/lucky6/config`, `POST /admin/api/lucky6/paytable/preview` | admin | configuration |
| `GET/POST /admin/api/lucky6/rounds`, `GET …/{id}`, `POST …/{id}/open\|close\|start\|settle\|cancel` | admin | manches |
| `GET /lucky6/ecran`, `/lucky6/api/live`, `/lucky6/api/guichet/{code}`, `/lucky6/verifier` | public | écrans |
| `GET /api/v1/lucky6/config`, `/rounds/current`, `/rounds/history`, `/rounds/{id}`, `/rounds/{id}/verify` | public | lecture seule (aucun pari en ligne) |

## Migration `c4d8e1a6f2b9` (à lancer par vous)

Remplace la contrainte `ck_game_bets_bet_type` pour accepter `SIX`, `FIRST_ODD`, `FIRST_EVEN`.
Aucune donnée modifiée. Retour arrière refusé tant qu'un pari Lucky6 existe.

```
alembic upgrade head
```

Puis redémarrer l'application, le worker Celery et Celery beat (nouvelle tâche `lucky6-tick`).

## Tests

`tests/test_services/test_lucky6_engine.py`, `test_lucky6_service.py`, `tests/test_api/test_lucky6_api.py` :
tirage (35 boules uniques, déterministe, vérifiable, ordre), probabilités exactes et simulation, table et taux,
ajustement, validation, paris (sélection, mises, agent seulement, jeu fermé, pari pair/impair), règlement par
position avec la table figée, plafond, idempotence, ticket, annulation et remboursement, cycle automatique
calé sur l'horloge, isolation Horse Races / Lucky6, droits, CSRF, refus des valeurs envoyées par le navigateur.
