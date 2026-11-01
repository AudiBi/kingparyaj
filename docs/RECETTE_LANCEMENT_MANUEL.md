# Recette — lancement manuel des jeux (Keno, Lucky6, Horse Races)

À jouer sur un environnement de **recette** avec les vrais composants : PostgreSQL, Redis, Celery worker + beat
(ou `GAME_LOOP_ENABLED=true`), WebSocket via nginx. Les tests automatiques tournent sur SQLite : ce qui suit
couvre ce qu'ils ne peuvent pas voir.

Matériel : 2 bureaux, 3 agents (A1 et A2 au bureau 1, B1 au bureau 2), 1 compte joueur avec solde, 1 TV « salle »
et 1 écran « joueur » par jeu, les panels admin.

Pour chaque jeu (Keno `/agent/keno`, Lucky6 `/agent/lucky6`, Horse Races `/agent/horse-races`) :

## 0. Pré-requis
- [ ] Le cycle tourne (logs : `keno_tick`, `lucky6_tick`, `horse_race_tick`, ou « Cycle automatique des jeux démarré »).
- [ ] Admin → réglage « Lancement manuel » coché (valeur par défaut) pour le jeu.
- [ ] Une manche/un tirage est ouvert sans compte à rebours sur le panel agent et sur la TV (« en attente du lancement »).

## 1. Vente illimitée
- [ ] A1 vend 10 fiches (espèces, ticket, compte). Aucune fermeture automatique, même après 10 minutes.
- [ ] Le compteur « fiches/tickets vendus » du panel A1 monte à chaque vente ; il monte aussi sur A2 (≤ 10 s ou via WebSocket).
- [ ] Les écrans publics (TV salle, API publique) **n'affichent aucun total** de fiches ni de mises.
- [ ] Laisser la manche ouverte au-delà de l'heure de fermeture configurée : elle reste ouverte (comportement voulu).

## 2. Qui peut lancer
- [ ] B1 (n'a rien vendu sur cette manche) : bouton désactivé avec explication ; un POST direct est refusé (400, « a vendu »).
- [ ] A2 vend 1 fiche → son bouton s'active ; A2 peut lancer. A1 aussi (il a vendu).
- [ ] Un joueur (rôle non agent) ne peut pas appeler la route de lancement.
- [ ] Sans jeton CSRF, la route de lancement redirige/refuse.

## 3. Lancement
- [ ] A1 clique « Lancer » → confirmation → tirage/course démarre sur **toutes** les TV et écrans joueur en < 2 s, au même point.
- [ ] Les paris sont fermés immédiatement : une vente tentée depuis A2 juste après est refusée (« fermés »).
- [ ] **Double clic simultané** (A1 et A2 en même temps) : un seul lancement ; l'autre reçoit « déjà lancé » ; aucun gain en double.
- [ ] La manche suivante s'ouvre aussitôt (numéro n+1), prête à vendre pendant l'animation de la précédente.
- [ ] Pendant l'animation, le bouton de la manche n+1 est bloqué ; un POST direct est refusé (« encore en cours »).
- [ ] Une fois l'animation terminée, le bouton se réactive pour les vendeurs de n+1.

## 4. Règlement et argent
- [ ] Après l'animation : fiches gagnantes/perdantes réglées, gains crédités **une seule fois** (compte : transaction `WIN-…` unique ; ticket : solde).
- [ ] « Payer les gains » (page `/agent/gains`) fonctionne sur une fiche gagnante du lancement.
- [ ] Reçus : avant le lancement, « Tirage lancé par un agent » ; réimpression après : heure réelle du tirage.
- [ ] Rapports / dashboard : le tirage apparaît à l'heure réelle du lancement ; commission figée à la vente inchangée.
- [ ] Journal d'audit : une entrée « lancement » au nom de l'agent (`keno_draw_launched` / `race_started`).

## 5. Reprise et pannes
- [ ] Recharger la TV et le panel pendant l'animation : reprise au bon point.
- [ ] Couper puis rétablir le WebSocket : reconnexion, état correct.
- [ ] Redémarrer l'appli/Celery manche ouverte avec fiches vendues : la manche reste ouverte, rien n'est tiré, aucune fiche perdue.
- [ ] Arrêter le cycle pendant une animation : la manche reste « en cours » (voulu) ; au redémarrage elle se termine et se règle.
- [ ] Annulation admin d'une manche ouverte avec fiches : remboursement unique (`REFUND-…`).

## 6. Keno uniquement
- [ ] Le nettoyage « tirages périmés » (toutes les 15 min) n'annule pas un tirage ouvert sans heure, même vide, vieux de plus d'1 h.
- [ ] Un seul tirage ouvert à la fois, même avec worker Celery **et** boucle intégrée actifs ensemble.
- [ ] Changer le réglage admin (manuel → à l'heure) avec un tirage ouvert : il reste manuel, ne part pas seul.
- [ ] Tirages à l'heure déjà créés avant le déploiement : ils partent à leur heure, puis le mode manuel prend le relais.
- [ ] Dashboard agent « Prochain tirage Keno » : « En attente du lancement » ; page admin Tirages : « Lancé par un agent ».

## 7. Retour en arrière
- [ ] Décocher « Lancement manuel » dans l'admin du jeu : retour aux manches à l'heure sans redéploiement.
- [ ] Aucune migration à annuler (vérifier : `alembic current` inchangé).

## Charge
- [ ] 100 ventes en 1 minute sur la même manche depuis 3 agents : aucune erreur, totaux exacts, lancement possible pendant la rafale (il attend les ventes en cours).

## Critères de passage
Tous les points cochés, aucun gain en double, aucun total public, aucune manche lancée sans agent vendeur.
