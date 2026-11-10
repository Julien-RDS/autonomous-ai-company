# Supervision locale en lecture seule

Branche `feat/supervision-dashboard`, depuis `main` b42b0b2 (CI run 22 verte).

## Périmètre retenu

Interface de **consultation** servie par le dashboard FastAPI existant, sans framework
frontend ni JavaScript : rendu côté serveur, une feuille de style locale, thème clair
et sombre (`prefers-color-scheme`), mise en page fluide ordinateur / mobile.

- Aucune route ne modifie la base : uniquement des `GET`, lectures sans commit ;
  `POST/PUT/PATCH/DELETE` renvoient 405 ou 404 (test d'empreinte de toutes les tables).
- Aucun bouton d'approbation, de relance, d'exécution ou de rapprochement. Quand une
  intervention est nécessaire, la page affiche **la commande CLI** et son effet ;
  les autorisations restent séparées (génération ≠ exécution, à usage unique).
- Les actions web demanderont un chantier distinct : authentification, CSRF,
  permissions explicites.

## Pages

| Route | Contenu |
|---|---|
| `/` | Mode MOCK/réel, travaux par état, réunions, tâches à valider, **À votre attention** (commande + effet), budget, dernières exécutions sandbox |
| `/runs`, `/runs/{id}` | Mission, décision et justification, propositions, critiques, votes, sources **non vérifiées** (URL en texte, non cliquables), erreurs, réponses par étape, tâches créées, coûts |
| `/runs/{id}/report` | Rapport en texte échappé, ou `?download=1` en `text/plain` |
| `/tasks`, `/tasks/{id}` | Ticket métier vs travail exécutable, approbation de génération, autorisations d'exécution (image, profil, digest, consommée/disponible), fichiers générés, exécutions |
| `/tasks/{id}/files/{n}` | Fichier n du manifeste en texte échappé, ou téléchargement `text/plain` |
| `/work`, `/work/{id}` | État, priorité, planification, tentatives, worker et lease, revues humaines, journal d'audit |
| `/sandbox`, `/sandbox/{id}` | Verdict, résumé déclaré (non fiable), image exacte, version du validateur, digest, nettoyages audités, preuve conservée, sorties bornées |
| `/budget` | Réservations initiales, coûts calculés depuis les tokens, coûts inconnus, montants rapprochés — distingués et filtrables |

Routes conservées : `/api/status`, `/api/sandbox-runs`.

## Sécurité

- CSP `default-src 'none'; style-src 'self'` : **aucun script** ne peut s'exécuter,
  même si un contenu échappait au filtrage ; `nosniff`, `frame-ancestors 'none'`,
  `Referrer-Policy: same-origin`, `Cache-Control: no-store`. La politique de référent
  conserve l'origine des formulaires locaux (connexion comprise), sans transmettre
  de référent aux autres origines.
- Toute donnée de modèle, de recherche ou de conteneur est échappée ; les caractères de
  contrôle et bidi des sorties sont affichés sous forme `\x1b` / `\u202e`.
- **Aucun chemin n'est accepté du navigateur.** Le rapport est recalculé à partir de
  l'identifiant validé (`reports/<id>.md`) : le chemin stocké en base est ignoré, les
  liens symboliques et sorties de répertoire sont refusés, la lecture est bornée à 512 Kio.
  Les fichiers de prototype viennent du manifeste en base, par numéro (50 maximum).
- Le HTML/JS généré n'est jamais rendu : texte échappé, ou téléchargement `text/plain`
  avec `Content-Security-Policy: sandbox`.
- Erreur de base : page neutre 503, sans trace, SQL ni DSN.
- Écoute inchangée sur `127.0.0.1:8000`.

## Performances

Pagination à 25 éléments (pages 1 à 10 000, sinon 422). Une requête par liste et des
agrégats `GROUP BY` / `IN` : un test vérifie que le nombre de requêtes par page ne
croît pas avec le volume de données.

## Lancement

```bash
python -m company dashboard          # http://127.0.0.1:8000
```

## Vérification effectuée

- Tests : `tests/test_supervision.py` (28 cas, SQLite et PostgreSQL), dont contre-épreuves
  par mutation (lecture du chemin stocké, échappement désactivé : détectées).
- Suite complète, Python 3.12.3 et 3.11.18, PostgreSQL 16 réel, Docker rootful cgroup v1,
  en root et en utilisateur non root : **201 réussis, 12 ignorés, 0 échec**.
  Ignorés : 11 variantes SQLite de tests PostgreSQL, 1 arrêt PostgreSQL (`TEST_PG_CONTAINER`).
- Serveur réellement démarré par `python -m company dashboard` ; parcours HTTP de toutes les
  pages, en-têtes, 404, 405 et 422 vérifiés.
- **Aucun navigateur réel n'était disponible** (téléchargement de Chromium bloqué dans cet
  environnement). Les captures ont été produites avec WeasyPrint, un moteur de rendu
  HTML/CSS, à partir du serveur en marche : elles montrent la mise en page et le thème clair,
  pas le thème sombre ni l'interaction clavier dans un navigateur.

## Limites

- Lecture seule : aucune action depuis le web.
- Pas d'actualisation automatique (aucun script) : lien « Actualiser » et horodatage.
- Libellés d'état techniques affichés tels qu'en base (`pending_review`, `FAILED`…).
- Les réponses des agents n'ont pas d'horodatage individuel : affichage par étape du cycle.
