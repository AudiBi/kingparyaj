# Impression directe des tickets (ESC/POS, Xprinter XP-80C)

Les tickets peuvent être envoyés **directement** à l'imprimante thermique, sans la fenêtre d'impression du
navigateur : longueur exacte du ticket, aucune marge blanche, coupe automatique, logo et code-barres.
Cela évite le format de page du pilote Windows (ex. « 72.1 x 210 mm »).

## Installation (une fois par poste)
1. Installer **QZ Tray** (gratuit) : https://qz.io/download/ — le laisser tourner (icône près de l'horloge).
2. Garder le pilote Windows de l'imprimante installé (QZ Tray s'en sert pour trouver son nom).
3. Dans l'application, ouvrir un reçu (Keno, Lucky6, Horse Races…) puis **Imprimante…** :
   - choisir **Impression directe ESC/POS** ;
   - **Détecter les imprimantes** et choisir la XP-80C ;
   - **Ticket test**, puis **Enregistrer**.
4. Au premier ticket, QZ Tray demande d'autoriser le site : cocher **Remember this decision** puis **Allow**.

Le réglage est mémorisé **dans le navigateur de chaque poste**. Le choix « Papier » (80 / 72 / 58 mm) sert aussi :
58 mm = 32 colonnes, 80 et 72 mm = 48 colonnes.

## Si l'impression directe échoue
QZ Tray absent ou arrêté, imprimante introuvable, délai dépassé (6 s pour la connexion) : un message s'affiche et le
ticket est imprimé par la fenêtre du navigateur. Aucun ticket n'est perdu.

## Détails techniques
- `app/static/js/escpos_print.js` : construction des commandes (jeu de caractères CP858, code-barres Code 128,
  logo en image monochrome, coupe partielle), envoi via `app/static/js/vendor/qz-tray.js` (QZ Tray 2.2.4, LGPL).
- Même contenu que le reçu du navigateur (`thermal_receipt.js`) : un seul `spec` pour les deux modes.
- Si les accents sortent mal, l'imprimante n'utilise pas CP858 : le dire, le jeu de caractères se règle dans `toBytes`.
- Tests : `tests/test_services/test_escpos_print.py` (Node requis).
