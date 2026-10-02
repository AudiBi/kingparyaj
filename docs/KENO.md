# Keno — King Paryaj

Keno joué **uniquement au bureau, par un agent**. Deux modes (admin → Jeux → Keno) :

- **Partagé (par défaut)** : un tirage commun à tous les bureaux toutes les 5 minutes (réglable), calé sur l'horloge ;
- **Instantané (option)** : un ticket = un tirage.

## Keno partagé

- Les tirages sont créés à l'avance (les 2 prochains), avec leur empreinte publiée et leurs **réglages figés**
  (`keno_draws.config` : table de paiement, limites de mise et de numéros, gain maximum, rythme des écrans).
  Une modification de l'admin s'applique tout de suite aux tirages qui n'ont encore aucun ticket ;
  un tirage qui a des tickets garde les réglages avec lesquels ils ont été acceptés.
- Les tickets se prennent sur le tirage ouvert jusqu'à 10 s (réglable) avant l'heure ; sous verrou partagé
  (tickets simultanés illimités), débit `BET-KENO-<pari>` ou solde du ticket.
- Le worker (`keno_tick`, toutes les 3 s) tire à l'heure : 20 numéros calculés depuis le seed, règlement
  (`WIN-KENO-<pari>`, jamais deux fois), puis crée le tirage suivant. Un tirage échu est toujours tiré.
- Écrans : `/keno/salle` (TV de la salle) et `/keno/salle?c=<code>` (écran du joueur au guichet : ticket en
  saisie, ses tickets, « trouvés x / n », « GAGNÉ ! »). Les 20 boules sont dévoilées une par une au rythme de
  l'**horloge du serveur** (présentation 3 s, une boule toutes les 1,5 s, résultat 8 s) : tous les écrans sont
  synchronisés et reprennent au même point après une coupure.
- Panel agent `/agent/keno` : tirage en direct, grille 1-80, Aléatoire (serveur), Effacer, mises proposées,
  gains possibles, reçu (empreinte + lien de vérification), derniers tickets et derniers tirages.

| Route | Rôle |
|---|---|
| `GET /agent/api/keno/live` | tirage affiché, tirage ouvert, configuration |
| `POST /agent/api/keno/shared/bet` (+ CSRF) | ticket sur le tirage ouvert (champs autorisés : tirage, numéros, mise, joueur) |
| `POST /agent/api/keno/shared/screen` (+ CSRF) | aperçu sur l'écran du guichet |
| `GET /agent/api/keno/shared/my-bets`, `/shared/bets/{id}`, `/shared/history` | tickets de l'agent, réimpression, tirages |
| `GET /keno/salle`, `/keno/api/live`, `/keno/api/salle/{code}` | écrans publics |
| `POST /api/v1/keno/ticket-bets` | agent (API) : tirage partagé ou instantané selon le tirage |

Migration `d5e9f3b7a2c1` : ajoute `keno_draws.config` (JSON, vide pour les anciens tirages). Aucune donnée modifiée.

Celery beat : `keno-tick` (3 s) remplace `process-keno-draws` (5 min) et la planification quotidienne de 24 h.

## Règles

- Grille de 1 à 80. Le joueur choisit de 1 à 10 numéros (limites réglables par l'admin, 10 au maximum).
- 20 numéros gagnants uniques sont tirés parmi 80.
- Gain = mise × multiplicateur de la table de paiement (selon le nombre de numéros joués et trouvés),
  arrondi au centime inférieur et plafonné au « gain maximum par ticket ».

## Architecture (aucune table créée)

| Élément | Où |
|---|---|
| Tirage (round) | `keno_draws` (`mode` = `instant` ou `scheduled`, `server_seed`, `server_seed_hash`) |
| Ticket / pari | `keno_bets` (numéros, mise, trouvés, multiplicateur, gain, statut) |
| Argent | `WalletService` (compte joueur) ou solde du `tickets` bureau ; `transactions` |
| Calculs purs | `app/services/keno_engine.py` |
| Règles métier | `app/services/keno_service.py` (point d'entrée unique) |
| Équité vérifiable | `app/services/fairness.py` (commun avec Horse Races) |
| Configuration | Redis `settings:keno` (sans expiration), validée par `keno_engine.validate_config` |

## Keno instantané (option)

### Déroulement d'un ticket

1. La page agent prépare un tirage : seed secret de 256 bits, **empreinte sha256 affichée avant le pari**.
2. L'agent envoie : joueur (ticket ou téléphone), numéros, mise. Rien d'autre n'est accepté du navigateur.
3. Dans **une seule transaction** (`KenoService.play_instant`) :
   tirage verrouillé → contrôles (Keno ouvert, numéros 1-80 sans doublon, nombre de numéros, mise, solde)
   → débit (`BET-KENO-<pari>`) → 20 numéros calculés à partir du seed → règlement → crédit éventuel (`WIN-KENO-<pari>`) → commit.
   En cas d'erreur, rien n'est débité et le tirage reste disponible.
4. L'écran anime les 20 numéros reçus du serveur (l'animation ne choisit rien) ; le reçu imprimé
   indique les numéros, le gain, l'empreinte, le seed et le lien de vérification.
5. Un nouveau tirage est préparé pour le ticket suivant.

Un tirage instantané ne peut être joué qu'une fois (verrou + contrôle). Les tirages préparés jamais joués
sont annulés après 1 h par le nettoyage (sans conséquence : ils n'ont aucun pari).

### Écran du joueur (tireuse)

Page `/keno/ecran?c=<code>` (bouton « Écran joueur » de la page agent, ou adresse affichée sous la grille,
à ouvrir sur un 2e moniteur ou une TV). Le code est propre à chaque guichet (HMAC du secret de l'application).

- pendant la saisie : numéros demandés, mise, gain maximum et gains possibles (`POST /agent/api/keno/screen`, affichage seulement) ;
- au jeu : compte à rebours, brassage des 80 boules dans la sphère, sortie de chaque numéro par le tube,
  présentation en grand, tableau 1-80 et numéros sortis mis à jour, « TROUVÉ ! » sur les numéros du joueur,
  résultat et gain, seed révélé ;
- les numéros et leur ordre viennent du serveur (ticket déjà réglé) : l'animation ne choisit rien ;
- temps réel par WebSocket (`/ws/draws/keno-screen-<code>`), reprise après coupure ou rechargement
  (`GET /keno/api/ecran/<code>`) ; aucune donnée d'identité (ni ticket ni téléphone) n'est diffusée ;
- son optionnel (bouton en bas à droite), animations réduites si le système le demande.

## Vérification (« provably fair »)

Page publique `/keno/verifier?tirage=<id>` et API `GET /api/v1/keno/draws/{id}/verify`.

- empreinte = SHA-256(seed) ;
- pour i = 79 à 60 : u = 7 premiers octets de HMAC-SHA256(seed, `keno:<n° de tirage>:<i>`) / 2^56,
  j = ⌊u × (i+1)⌋, échange des cases i et j d'une liste 1…80 ;
- les cases 79 à 60 donnent les 20 numéros, dans l'ordre d'apparition.

La page refait le calcul dans le navigateur (WebCrypto) en plus du serveur.

## Configuration (admin → Jeux → Keno)

Modifiable à tout moment ; jamais appliquée à un ticket déjà accepté.

- Keno ouvert / fermé ; mode partagé ou instantané ;
- partagé : intervalle entre tirages, fermeture des paris, heures d'ouverture, rythme des écrans ;
- numéros minimum / maximum ; mises minimum / maximum ; mises proposées au guichet ;
- gain maximum par ticket ;
- table de paiement, avec le **taux de redistribution exact** de chaque ligne (loi hypergéométrique) ;
- « Ajuster la table à un taux cible » : garde les proportions, arrondit au dixième inférieur (jamais au-dessus de la cible) ;
- taux de redistribution maximum : une table au-dessus est refusée.

Chaque modification est enregistrée dans le journal d'audit (`keno_config`, ancienne et nouvelle valeur).

## API

| Route | Qui | Rôle |
|---|---|---|
| `GET /agent/keno` | agent | page de jeu |
| `GET /agent/api/keno/state` | agent | configuration + tirage préparé (empreinte) |
| `GET /agent/api/keno/quick-pick?count=N` | agent | sélection aléatoire du serveur (sans effet sur le tirage) |
| `POST /agent/api/keno/bet` | agent (+ CSRF) | joue un ticket instantané |
| `GET /agent/api/keno/bets/{id}` | agent | réimpression (ses propres tickets seulement) |
| `GET /agent/api/keno/history` | agent | derniers tickets |
| `GET /api/v1/keno/config` | public | règles, table, taux |
| `GET /api/v1/keno/draws/{id}/verify` | public | vérification |
| `GET /api/v1/keno/instant/draw`, `POST /api/v1/keno/ticket-bets` | agent (API) | même jeu instantané |
| `POST /api/v1/keno/bets`, `/quick-pick` | joueur | **refusé (403)** : paris uniquement chez un agent |
| `GET/PUT /admin/api/keno/config`, `POST /admin/api/keno/paytable/preview` | admin | configuration |
| `POST /admin/api/keno/draws/{id}/cancel` | admin | annule un tirage en attente et **rembourse** ses paris (`REFUND-KENO-<pari>`) |

## Migration `b7c2e4f9a1d3`

Colonnes `keno_draws.mode / server_seed / server_seed_hash`, statut `REFUNDED`,
`keno_bets.multiplier` en Numeric(10,2) (x1200 et x5000 dépassaient l'ancienne limite de 999,99),
contrainte « 1 à 10 numéros » corrigée (une liste vide passait). Retour arrière prévu (voir le fichier).

## Tests

`tests/test_services/test_keno_service.py`, `test_keno_instant.py`, `test_keno_worker.py`,
`tests/test_api/test_keno_api.py`, `test_keno_shared.py`, `tests/test_api/test_keno_shared_api.py` :
tirages partagés (créneaux, réglages figés, fermeture, tick, état en direct, écrans),  tirage (20 numéros uniques 1-80, déterministe, vérifiable),
validation, table de paiement, plafond, règlement, double paiement, remboursements, retour arrière,
configuration, droits (agent seulement, CSRF, reçus d'un autre agent), parcours complet.
