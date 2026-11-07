# Lot 2 : persistance et reprise

## Architecture et compatibilité

La branche dérive du Lot 1 non fusionné (`f5f1e10`) et de main actualisée
(`cea210d`). Aucun historique ni fichier utilisateur n'a été supprimé.
SQLAlchemy 2.x reste l'ORM ; Alembic versionne désormais le schéma.
PostgreSQL 16 est la référence durable ; SQLite conserve le mode local.

Correspondances de tables : `runs` = workflow_runs et décisions (`selection_info`,
`result_json`) ; `messages` = api_usage et réponses ; `ledger` = budget_ledger.
Les tickets métier `tasks` sont conservés. `work_items` porte exclusivement
l'exécution de workflows (pas une deuxième copie des tickets de prototypes).
`task_attempts`, `approvals`, `agents`, `audit_events` complètent le schéma.
Le catalogue `agents` est disponible, mais son administration n'est pas automatisée.

Responsabilités : la file attribue les leases et planifie les retries ; LangGraph
conserve la progression ; le registre monétaire conserve les coûts possibles ;
les événements d'audit enregistrent les transitions. Le `thread_id` du cycle est
le run ID ; tâche exécutable et tentative ont leurs propres identifiants.
Les approbations humaines utilisent un thread `approval:<task_id>` et une écriture
SQL idempotente. L'appel explicite `company approve` reprend l'interruption.

## Installation et démarrage

```bash
python -m pip install -r requirements.txt
# DATABASE_URL configurée dans .env, sans l'afficher dans les logs.
alembic upgrade head
MOCK_MODE=true python -m company init
MOCK_MODE=true python -m company.worker enqueue reunion-exemple-001
MOCK_MODE=true python -m company.worker run --once
```

Le worker durable nécessite PostgreSQL. `company once` garde son interface locale ;
utiliser la file et le worker pour bénéficier des checkpoints et de la reprise.
Le worker n'accepte que MOCK_MODE=true dans ce lot. Les appels réels restent
accessibles uniquement par la CLI existante, avec configuration et autorisation.
La file ne génère ni exécute automatiquement un prototype : l'approbation du
 ticket métier reste nécessaire.

Docker : définir POSTGRES_PASSWORD dans le fichier .env ignoré par Git, puis :

```bash
docker compose up --build -d
docker compose exec worker python -m company.container_entrypoint python -m company.worker enqueue reunion-001
docker compose exec worker python -m company.container_entrypoint python -m company status
```

Le service `migrate` attend PostgreSQL sain, applique Alembic et doit réussir avant
le démarrage du worker. La base et les rapports disposent de volumes persistants.
Les identifiants sont transmis par variables ; l'URL est encodée avec SQLAlchemy.
Dans un environnement à proxy TLS, BUILD_CA_BUNDLE désigne un bundle de confiance
monté uniquement pendant le build. Aucun secret ni .env n'entre dans l'image.

## Migration et restauration

1. Arrêter les workers et toute écriture avant migration d'une base existante.
2. Sauvegarder PostgreSQL et les rapports ; conserver la version du code associée.
3. Exécuter `alembic upgrade head` ; vérifier les totaux, comptes et rapports.
4. Redémarrer les workers après vérification.

La baseline adopte le schéma historique après contrôle des colonnes et types.
Un schéma différent échoue et nécessite une migration explicite ; ne pas forcer
`alembic stamp head`. Les montants historiques sont arrondis à neuf décimales.
Les données historiques ne permettent pas de reconstruire une facture opérateur.
La CLI initialise les bases locales via Alembic ; en déploiement, le service
migrate doit terminer avant les workers. Ne pas lancer des migrations de versions
différentes simultanément. Neuf décimales ne restaurent pas la précision perdue
par les anciens Float.

Exemple de sauvegarde/restauration avec Compose (sans afficher de mot de passe) :

```bash
docker compose exec -T postgres pg_dump -U company -d company -Fc > company-backup.dump
# Restaurer dans une NOUVELLE base, jamais écraser la base active.
docker compose exec postgres createdb -U company company_restored
docker compose exec -T postgres pg_restore -U company -d company_restored --exit-on-error < company-backup.dump
```

Valider la base restaurée et pointer une instance arrêtée vers elle avant reprise.
Copier aussi le volume des rapports. La restauration doit être répétée régulièrement.
Aucun downgrade destructif n'est requis pour restaurer un snapshot historique.

## Comptabilité

NUMERIC(18,9) sur PostgreSQL, entiers de nano-EUR sur SQLite, Decimal côté Python.
Arrondi HALF_UP centralisé. EUR uniquement : une valeur USD/GBP est refusée,
jamais additionnée implicitement. La conversion FX doit être explicitement réalisée
en amont avec une preuve indépendante ; aucun taux n'est inventé.

`estimated_eur` conserve la réservation initiale, `amount_eur` le coût retenu pour
le plafond, `actual_eur` le calcul d'après les tokens reçus. Ce dernier n'est PAS
une facture opérateur. La paire de tarifs appliquée est enregistrée dans
`tariff_version` ; les anciens coûts portent `legacy`.
Une clé d'opération unique empêche une deuxième réservation du même appel.
Une réponse réussie déjà enregistrée est réutilisée. Une opération réservée sans
réponse persistée bloque une nouvelle tentative : rapprochement humain requis.
`Budget.reconcile_abandoned(before)` marque les réservations anciennes inconnues
sans libérer leur montant. Ne l'utiliser qu'après avoir identifié les exécutions
interrompues ; il ne vérifie pas l'état chez un fournisseur.

Les réservations PostgreSQL utilisent un verrou transactionnel ; régularisations
et réservations partagent ce verrou. SQLite utilise BEGIN IMMEDIATE.
Les coûts réels peuvent dépasser une prévision : aucun plafond bancaire absolu
n'est garanti. Plafonds et alertes opérateurs restent indispensables en mode réel.

## File et reprise

États : PENDING/RETRY_SCHEDULED → RUNNING → SUCCEEDED, FAILED ou
WAITING_APPROVAL. Les tâches en attente peuvent être CANCELLED. FAILED constitue
la dead-letter queue logique. Les tentatives restent consultables.

Acquisition PostgreSQL par FOR UPDATE SKIP LOCKED. Le lease contient un token neuf
par tentative ; heartbeat toutes les 40 s pour un lease de 120 s. Toute validation
vérifie le token et l'expiration, y compris l'enregistrement final du workflow.
Les expirations sont récupérées avant acquisition. Les retries d'échec utilisent
un backoff exponentiel plafonné à 300 s avec jitter ; nombre de tentatives borné.
Une panne SQL déclenche au maximum cinq essais dans la boucle worker ; Compose
peut redémarrer le processus. Livraison at-least-once, jamais garantie physique
exactement une fois.

Les travaux marqués `external` sont placés WAITING_APPROVAL après expiration ou
échec ambigu. Ils ne sont pas rejoués automatiquement. Aucun connecteur d'email,
paiement ou GitHub, ni outbox d'envoi n'est ajouté : une future intégration devra
fournir une clé opérateur ou une procédure de rapprochement vérifiée.

## Validation et limites

Les tests utilisent de vrais schémas PostgreSQL isolés. Les processus enfants
s'arrêtent par os._exit après claim, checkpoint ou réservation ; les nouveaux
processus reprennent depuis la base. Un fichier représente l'effet externe
réussi avant crash ; sa tâche reste en attente sans rejouer cet effet.
Le test de panne stoppe puis redémarre le conteneur PostgreSQL de test.

```bash
MOCK_MODE=true python -m pytest -q
# Pour tout valider : définir TEST_DATABASE_URL vers une base PostgreSQL jetable.
# TEST_PG_CONTAINER = conteneur jetable à stopper/redémarrer, jamais production.
MOCK_MODE=true python -m pytest -q
```

Sans TEST_DATABASE_URL, les intégrations PostgreSQL sont ignorées explicitement.
Les variantes SQLite des tests de checkpoint PostgreSQL sont également ignorées.
La CI provisionne PostgreSQL 16 pour Python 3.11 et 3.12 ; son résultat distant
reste à observer après création de PR.

Limites : les fichiers de rapports ne participent pas à la transaction SQL ; un
rapport orphelin reste possible. Les prototypes ne disposent pas encore d'un claim
durable et doivent être traités avec une seule instance. Les workers réels,
rapprochements opérateurs et FX sont hors activation de ce lot. Le schéma des
checkpoints est administré par le checkpointer officiel, séparément d'Alembic.
Les horloges des workers doivent être synchronisées pour les expirations.
Les dépendances restent des plages compatibles, sans verrou complet.

## Résultats observés dans l'environnement cloud

Python 3.12, PostgreSQL 16 : **47 tests réussis, 9 ignorés**.
Les 9 ignorés sont exclusivement les variantes SQLite des intégrations de crash
et checkpoints PostgreSQL ; les variantes PostgreSQL ont toutes été exécutées.
`pip check`, compilation Python et `git diff --check` passent.
Image Docker construite avec vérification TLS ; Compose a appliqué les migrations
sur une base neuve, puis le worker a terminé un cycle MOCK.
`pg_dump -Fc` puis `pg_restore --exit-on-error` dans une nouvelle base ont conservé
le travail terminé et la révision Alembic.

Le worker et la CLI d'approbation prennent un verrou consultatif de session par
thread avant d'utiliser le checkpointer. Un ancien processus encore vivant ne
peut donc pas écrire les mêmes checkpoints en parallèle d'un remplaçant. Le token
et l'expiration restent vérifiés lors de la validation SQL du résultat.

| Fonctionnalité | État | Fichiers | Vérification | Limites |
|---|---|---|---|---|
| Migrations et PostgreSQL | Terminé | migrations/, database.py, Dockerfile, docker-compose.yml | vierge, historique, réexécution, Compose, restauration | maintenance sans écritures pendant migration |
| Montants et réservations | Terminé pour EUR | money.py, budget.py, 002_money.py | Decimal, arrondis, concurrence SQLite/PG, crash, refus USD | pas de facture opérateur ni conversion FX implicite |
| File et workers | Terminé pour MOCK | queue.py, worker.py, database.py | processus concurrents, leases, fencing, retries, dead-letter | worker réel désactivé ; prototypes hors file |
| Checkpoints et approbations | Terminé pour cycle MOCK | workflow.py, approvals.py, worker.py | arrêt après proposition, reprise sans doublon, approbation idempotente | checkpoint et effet externe ne sont pas une transaction unique |
| Crash et panne PostgreSQL | Terminé | tests/test_crash.py | 7 scénarios, arrêt de processus et du serveur | aucun service payant appelé |
| CI et PR | Partiel | .github/workflows/tests.yml | mêmes commandes exécutées localement | résultat GitHub distant et création de PR à vérifier |
