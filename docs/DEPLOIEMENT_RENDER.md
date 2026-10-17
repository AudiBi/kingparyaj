# Déploiement sur Render

Fichiers : `render.yaml` (Blueprint), `Docker/Dockerfile`, `.dockerignore`.

## 1. Mise en place
1. Pousser la branche sur GitHub (`AudiBi/kingparyaj`).
2. Render → **New → Blueprint** → choisir le dépôt et la branche. Render crée : PostgreSQL (`kingparyaj-db`),
   Redis (`kingparyaj-redis`) et l'application web (`kingparyaj`). Le worker et le beat Celery sont **en commentaire**
   dans `render.yaml` (optionnels, voir §2).
3. Renseigner les variables demandées (marquées `sync: false`) :
   - `BASE_URL`, `FRONTEND_URL`, `BACKEND_CORS_ORIGINS` sont déjà réglés sur `https://kingparyaj.com` dans `render.yaml` ;
4. Premier déploiement : bouton **Deploy** du service web. `alembic upgrade head` s'exécute avant la mise en ligne
   (Pre-Deploy Command). Vérifier `https://<service>.onrender.com/health/live`.
5. Créer le premier administrateur (onglet **Shell** du service web). Relire `scripts/seed_database.py`
   avant de l'utiliser : il peut créer des données de test.
6. Domaine `kingparyaj.com` (DNS chez Hostinger) :
   1. Render → service web → *Settings → Custom Domains → Add* : ajouter `kingparyaj.com` puis `www.kingparyaj.com`.
      Render affiche les valeurs DNS exactes à utiliser : **ce sont elles qui font foi**.
   2. Hostinger → *Domaines → kingparyaj.com → DNS / Serveurs de noms → Enregistrements DNS*.
      Supprimer les enregistrements qui entrent en conflit (A ou AAAA existants sur `@`, CNAME existant sur `www`,
      redirection ou « parking » Hostinger), puis ajouter :
      - `A` · nom `@` · valeur = l'adresse IP donnée par Render pour le domaine racine ;
      - `CNAME` · nom `www` · valeur = `kingparyaj.onrender.com` (adresse donnée par Render pour le service).
   3. Les serveurs de noms doivent rester ceux de Hostinger (`ns1/ns2.dns-parking.com`) pour que ces enregistrements comptent.
   4. Dans Render, cliquer *Verify* ; la propagation dure de quelques minutes à quelques heures. Le certificat HTTPS est
      automatique, et Render redirige `www` vers le domaine racine (ou l'inverse).
   5. Vérifier : `https://kingparyaj.com/health/live`, puis la connexion agent (les cookies sont sécurisés et exigent https).
   Si Hostinger héberge déjà un site sur ce domaine, ne pas le toucher avant d'avoir sauvegardé son contenu.
7. Jouer `docs/RECETTE_LANCEMENT_MANUEL.md` avant d'ouvrir aux agents.

## 2. Réglages à connaître
- **Coût indicatif** (à vérifier sur render.com/pricing) : web Starter ≈ 7 $ + PostgreSQL Basic 256 Mo ≈ 6 $ +
  Redis Starter ≈ 10 $ = **≈ 23 $/mois**, hors frais de l'espace de travail. Worker + beat Celery : ≈ 14 $ de plus.
  Si la mémoire de l'application (512 Mo) est saturée : passer le web en Standard (≈ 25 $).
- **Celery (optionnel)** : retirer le « # » du bloc commenté en bas de `render.yaml` pour ajouter le worker et le beat
  (notifications, nettoyages, résumés, rapports). Sur ces deux services, `SECRET_KEY` et `CSRF_SECRET_KEY` doivent être
  identiques à ceux du service web. Un seul beat.
- `GAME_LOOP_ENABLED=true` : le cycle des jeux tourne dans l'application (le worker Celery est facultatif).
- `WEB_CONCURRENCY` : nombre de processus uvicorn (2 par défaut dans `render.yaml`, 4 dans le Dockerfile).
- `PORT` est fourni par Render ; ne pas le définir.
- Plans : web et base payants. Un service web gratuit s'endort et arrêterait les jeux ; la base gratuite expire.
- Activer les sauvegardes de PostgreSQL (argent).
- Fichiers téléversés (`uploads/`) : le disque est effacé à chaque déploiement ; ajouter un **Disk** monté sur
  `/app/uploads` si l'application en enregistre.
- `autoDeploy: false` : aucun déploiement automatique ; passer à `true` une fois la recette terminée.

## 3. Corrections faites pour Render
- `postgres://…` fourni par Render converti en `postgresql+asyncpg://…` (`app/config.py`).
- `alembic` utilise `DATABASE_URL` (et non l'adresse locale de `alembic.ini`).
- L'ancien scheduler de tirages, qui gelait l'application en production, n'est plus démarré.
- Le Dockerfile installe `requirements.txt` (le bloc Poetry ne listait aucune dépendance) et suit `$PORT`.
